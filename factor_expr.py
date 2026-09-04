# -*- coding: utf-8 -*-
r"""G25（第38轮）纯标准库表达式因子引擎 factor expression——"算子→表达式→因子"的唯一确定性载体。

动机（见《总体对标与统一改进总纲》G25）：此前每加一个因子都要写一段过程式代码、实时 analyzer 与离线
panel 各接一次，口径只靠"调用同一函数"口头保证。本模块用一个**白名单、无 eval/exec、无属性访问、无导入**
的小型表达式 DSL，把因子定义成**字符串 + 元数据**；同一棵语法树在两种上下文求值、逐值一致：
  · 时序上下文 eval_ts：输入 {字段: 等长序列}（离线=G21面板列 / 实时=bars 前缀经 compute_indicators），
    输出等长序列（暖机不足为 None），无未来函数——每个 t 只用 [t-n+1, t] 的尾窗。
  · 截面上下文 eval_cs：输入 {字段: {品种: 当日值}}，输出 {品种: 值}，cross_rank/scale/zscore 在同一时点跨品种。
因为求值是输入数组的**纯函数**，实时与离线只要喂相同数值就必然逐值相同（training-serving parity，测试钉死）。

安全边界（解析期强制、测试反向用例钉死）：只允许白名单算子；禁 '__'/'.'/';'/import/eval/exec/lambda；
变量名只是输入字段键、绝不作为代码；任何未知函数名直接 ExprError。**默认只承载新研究因子，旧技术/基本面
因子保持原过程式实现、综合分逐字节不变（G25 回退铁律），本模块不被 main 实时链路 import。**

算子白名单：
  时序（尾窗 n、无未来）：delay/delta/ts_sum/ts_mean/ts_std/ts_min/ts_max/ts_rank/ts_minmax/decay_linear/corr；
  状态递推（全序列因果、SMA播种）：ts_ema（标准EMA alpha=2/(n+1)）/ts_rma（Wilder RMA，RSI用）
  截面（同一时点跨品种）：cross_rank/scale/zscore
  逐元素/数学：abs/sign/log/tanh/max/min（二元）、四则与一元负号
因子治理（纯标准库，自含不依赖 tools）：pearson/spearman、高斯消元 _solve、orthogonalize 正交残差、
等权/IC 加权/ICIR 加权 combine。
"""
import math

# =========================== 安全解析器（递归下降，白名单） ===========================
_TS_OPS = {
    "delay": (2, "ts"), "delta": (2, "ts"), "ts_sum": (2, "ts"), "ts_mean": (2, "ts"),
    "ts_std": (2, "ts"), "ts_min": (2, "ts"), "ts_max": (2, "ts"), "ts_rank": (2, "ts"),
    "ts_minmax": (2, "ts"), "decay_linear": (2, "ts"), "corr": (3, "ts"),
    # 第61轮：状态型递推时序算子（全序列因果递推、无未来；跳过前导非有限值，前 n 个有限值SMA播种）
    "ts_ema": (2, "ts"),   # 标准EMA：alpha=2/(n+1)，逐字对齐 futures_data._ema_series
    "ts_rma": (2, "ts"),   # Wilder RMA：((n-1)*prev+x)/n，逐字对齐 RSI 的 avg_gain/avg_loss 平滑
}
_CS_OPS = {"cross_rank": (1, "cs"), "scale": (1, "cs"), "zscore": (1, "cs")}
_EL_OPS = {"abs": 1, "sign": 1, "log": 1, "tanh": 1, "max": 2, "min": 2}
WHITELIST = set(_TS_OPS) | set(_CS_OPS) | set(_EL_OPS)
# "__" 防 dunder、";" 防语句拼接；属性访问的 '.' 不是数字一部分时由分词器按非法字符拒绝；
# import/eval/exec/lambda 等名字即便出现也只是"输入字段名"、绝不执行，且作为函数调用过不了白名单。
_FORBID = ("__", ";")


class ExprError(ValueError):
    """表达式语法/安全/算子错误。"""


class _Tokenizer:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    def _skip(self):
        while self.i < self.n and self.s[self.i].isspace():
            self.i += 1

    def next_tok(self):
        self._skip()
        if self.i >= self.n:
            return ("end", None)
        ch = self.s[self.i]
        if ch in "+-*/(),":
            self.i += 1
            return ("op", ch)
        if ch.isdigit():
            j = self.i
            while j < self.n and (self.s[j].isdigit() or self.s[j] == "."):
                j += 1
            tok = self.s[self.i:j]
            self.i = j
            return ("num", tok)
        if ch.isalpha() or ch == "_":
            j = self.i
            while j < self.n and (self.s[j].isalnum() or self.s[j] == "_"):
                j += 1
            tok = self.s[self.i:j]
            self.i = j
            return ("name", tok)
        raise ExprError("非法字符 %r（位置%d）" % (ch, self.i))


class _Parser:
    """expr=term(('+'|'-')term)*；term=factor(('*'|'/')factor)*；factor=num/name/call/'('expr')'/-factor。"""

    def __init__(self, text):
        # 全局安全检查（属性访问/dunder/内建等一律不允许进入语法层）
        low = text.lower()
        for bad in _FORBID:
            if bad in low:
                raise ExprError("表达式含被禁止的片段 %r" % bad)
        self.tok = _Tokenizer(text)
        self.cur = self.tok.next_tok()

    def _advance(self):
        t = self.cur
        self.cur = self.tok.next_tok()
        return t

    def parse(self):
        node = self._expr()
        if self.cur[0] != "end":
            raise ExprError("表达式尾部有多余记号 %r" % (self.cur,))
        return node

    def _expr(self):
        node = self._term()
        while self.cur == ("op", "+") or self.cur == ("op", "-"):
            op = self._advance()[1]
            node = ("bin", op, node, self._term())
        return node

    def _term(self):
        node = self._factor()
        while self.cur == ("op", "*") or self.cur == ("op", "/"):
            op = self._advance()[1]
            node = ("bin", op, node, self._factor())
        return node

    def _factor(self):
        t = self.cur
        if t == ("op", "-"):
            self._advance()
            return ("neg", self._factor())
        if t == ("op", "+"):
            self._advance()
            return self._factor()
        if t[0] == "num":
            self._advance()
            try:
                v = float(t[1])
            except ValueError:
                raise ExprError("非法数字 %r" % t[1])
            if not math.isfinite(v):
                raise ExprError("数字必须有限")
            return ("num", v)
        if t[0] == "op" and t[1] == "(":
            self._advance()
            node = self._expr()
            if self.cur != ("op", ")"):
                raise ExprError("缺右括号")
            self._advance()
            return node
        if t[0] == "name":
            self._advance()
            if self.cur == ("op", "("):   # 函数调用：必须命中白名单
                fname = t[1]
                if fname not in WHITELIST:
                    raise ExprError("未知/未授权算子 %r（仅允许白名单算子）" % fname)
                self._advance()           # 吃掉 '('
                args = []
                if self.cur != ("op", ")"):
                    while True:
                        args.append(self._expr())
                        if self.cur == ("op", ","):
                            self._advance()
                            continue
                        break
                if self.cur != ("op", ")"):
                    raise ExprError("算子 %r 参数后缺右括号" % fname)
                self._advance()
                self._check_arity(fname, args)
                return ("call", fname, args)
            return ("var", t[1])          # 普通输入字段
        raise ExprError("意外记号 %r" % (t,))

    @staticmethod
    def _check_arity(fname, args):
        if fname in _TS_OPS:
            arity, _ = _TS_OPS[fname]
        elif fname in _CS_OPS:
            arity, _ = _CS_OPS[fname]
        else:
            arity = _EL_OPS[fname]
        if len(args) != arity:
            raise ExprError("算子 %r 需要 %d 个参数，实得 %d" % (fname, arity, len(args)))


def parse(text):
    """字符串 → AST（元组树）；非法/危险表达式抛 ExprError。"""
    if not isinstance(text, str) or not text.strip():
        raise ExprError("表达式必须是非空字符串")
    return _Parser(text).parse()


# =========================== 时序求值（纯函数、无未来） ===========================
def _isnum(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _series_len(data):
    for v in data.values():
        if isinstance(v, list):
            return len(v)
    raise ExprError("时序上下文至少需要一个序列字段")


def _window(vals, t, n):
    """取 [t-n+1, t] 尾窗内的有限值列表（无未来）。"""
    lo = max(0, t - n + 1)
    return [vals[k] for k in range(lo, t + 1) if _isnum(vals[k])]


def _safe_div(a, b):
    return a / b if _isnum(a) and _isnum(b) and abs(b) > 1e-15 else None


def _eval_ts(node, data):
    """唯一时序递归：AST + {字段: 等长list/标量} → 等长 list/标量；暖机/非法处为 None。

    时序算子的最后一个实参是"窗口 AST"，必须是正整数字面量（静态、防数据相关的未来函数），
    其余实参才作为序列递归求值；因此嵌套时序算子（delta(delta(...))）也能正确递归。
    """
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        if node[1] not in data:
            raise ExprError("字段 %r 不在输入数据中" % node[1])
        return data[node[1]]
    if kind == "neg":
        x = _eval_ts(node[1], data)
        return [(-v if _isnum(v) else None) for v in x] if isinstance(x, list) else (-x if _isnum(x) else None)
    if kind == "bin":
        return _bin_broadcast(node[1], _eval_ts(node[2], data), _eval_ts(node[3], data))
    if kind == "call":
        fn = node[1]
        if fn in _CS_OPS:
            raise ExprError("截面算子 %r 不能在时序上下文使用" % fn)
        if fn in _TS_OPS:
            children = node[2]
            series = [_eval_ts(c, data) for c in children[:-1]]
            n = _int_window(children[-1], data)
            return _ts_op(fn, series + [n])
        xs = [_eval_ts(a, data) for a in node[2]]   # 逐元素数学算子
        return _el_ts(fn, xs)
    raise ExprError("未知节点 %r" % (kind,))


def _bin_broadcast(op, a, b):
    """二元四则：标量/序列广播对齐，任一缺失为 None，除零安全。"""
    la = a if isinstance(a, list) else None
    lb = b if isinstance(b, list) else None
    if la is None and lb is None:
        return _bin_scalar(op, a, b)
    n = len(la) if la is not None else len(lb)
    out = [None] * n
    for t in range(n):
        out[t] = _bin_scalar(op, la[t] if la is not None else a, lb[t] if lb is not None else b)
    return out


def _bin_scalar(op, a, b):
    if not (_isnum(a) and _isnum(b)):
        return None
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return _safe_div(a, b)
    raise ExprError("未知运算符 %r" % op)


def _el_ts(fn, xs):
    """逐元素数学算子；max/min 二元，其余一元。标量或序列均可。"""
    def one(args):
        if fn == "abs":
            return abs(args[0]) if _isnum(args[0]) else None
        if fn == "sign":
            return (1 if args[0] > 0 else (-1 if args[0] < 0 else 0)) if _isnum(args[0]) else None
        if fn == "log":
            return math.log(args[0]) if _isnum(args[0]) and args[0] > 0 else None
        if fn == "tanh":
            return math.tanh(args[0]) if _isnum(args[0]) else None
        if fn == "max":
            return max(args) if all(_isnum(v) for v in args) else None
        if fn == "min":
            return min(args) if all(_isnum(v) for v in args) else None
        raise ExprError("未知逐元素算子 %r" % fn)
    if any(isinstance(x, list) for x in xs):
        n = len(next(x for x in xs if isinstance(x, list)))
        out = [None] * n
        for t in range(n):
            out[t] = one([x[t] if isinstance(x, list) else x for x in xs])
        return out
    return one(xs)


def _int_window(node, data):
    """算子的窗口参数必须是正整数字面量（静态、防数据相关的未来函数）。"""
    if node[0] != "num" or abs(node[1] - round(node[1])) > 1e-9 or node[1] < 1:
        raise ExprError("窗口长度必须是正整数字面量")
    return int(round(node[1]))


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


def _sample_std(vals):
    if len(vals) < 2:
        return None
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _seeded_recurrence(x, n, mode):
    """全序列因果递推（无未来）：跳过前导非有限值，前 n 个有限值用 SMA 播种，其后递推。

    mode="ema"：标准 EMA，alpha=2/(n+1)，递推 alpha*v+(1-alpha)*prev（逐字对齐 futures_data._ema_series）；
    mode="rma"：Wilder RMA，递推 ((n-1)*prev+v)/n（逐字对齐 _rsi_series 的 avg_gain/avg_loss 平滑）。
    输入中段的非有限值不会"重置"递推（只取有限子序列），这正好对齐 MACD 对 dif 非None连续子序列再 EMA 的口径。
    """
    out = [None] * len(x)
    finite = [(t, x[t]) for t in range(len(x)) if _isnum(x[t])]
    if len(finite) < n:
        return out
    prev = sum(v for _, v in finite[:n]) / n
    out[finite[n - 1][0]] = prev
    if mode == "ema":
        alpha = 2.0 / (n + 1.0)
        for k in range(n, len(finite)):
            t, v = finite[k]
            prev = alpha * v + (1.0 - alpha) * prev
            out[t] = prev
    elif mode == "rma":
        for k in range(n, len(finite)):
            t, v = finite[k]
            prev = ((n - 1) * prev + v) / n
            out[t] = prev
    else:
        raise ExprError("未知递推模式 %r" % mode)
    return out


def _ts_op(fn, args):
    x = args[0]
    n = args[-1]
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ExprError("窗口长度非法")
    out = [None] * len(x)
    if fn == "delay":
        for t in range(n, len(x)):
            out[t] = x[t - n] if _isnum(x[t - n]) else None
        return out
    if fn == "delta":
        d = _ts_op("delay", [x, n])
        for t in range(len(x)):
            out[t] = _bin_scalar("-", x[t], d[t])
        return out
    if fn == "ts_sum":
        for t in range(len(x)):
            w = _window(x, t, n)
            if len(w) >= n:
                out[t] = sum(w)
        return out
    if fn == "ts_mean":
        for t in range(len(x)):
            w = _window(x, t, n)
            out[t] = _mean(w) if len(w) >= n else None
        return out
    if fn == "ts_std":
        for t in range(len(x)):
            w = _window(x, t, n)
            out[t] = _sample_std(w) if len(w) >= n else None
        return out
    if fn in ("ts_ema", "ts_rma"):
        return _seeded_recurrence(x, n, "ema" if fn == "ts_ema" else "rma")
    if fn in ("ts_min", "ts_max"):
        for t in range(len(x)):
            w = _window(x, t, n)
            if len(w) >= n:
                out[t] = min(w) if fn == "ts_min" else max(w)
        return out
    if fn == "ts_rank":
        for t in range(len(x)):
            w = _window(x, t, n)
            if len(w) >= n and _isnum(x[t]):
                less = sum(1 for v in w if v < x[t])
                equal = sum(1 for v in w if v == x[t])
                out[t] = (less + 0.5 * (equal + 1)) / n
        return out
    if fn == "ts_minmax":
        for t in range(len(x)):
            w = _window(x, t, n)
            if len(w) >= n and _isnum(x[t]):
                lo, hi = min(w), max(w)
                out[t] = 0.5 if hi - lo <= 1e-15 else (x[t] - lo) / (hi - lo)
        return out
    if fn == "decay_linear":
        wts = list(range(1, n + 1))
        sw = sum(wts)
        for t in range(len(x)):
            seg = x[t - n + 1:t + 1]
            if len(seg) == n and all(_isnum(v) for v in seg):
                out[t] = sum(w * v for w, v in zip(wts, seg)) / sw
        return out
    if fn == "corr":
        y = args[1]
        for t in range(len(x)):
            xs, ys = [], []
            for k in range(max(0, t - n + 1), t + 1):
                if _isnum(x[k]) and _isnum(y[k]):
                    xs.append(x[k]); ys.append(y[k])
            out[t] = pearson(xs, ys) if len(xs) >= 3 else None
        return out
    raise ExprError("未实现的时序算子 %r" % fn)


def compute_ts(expr, data):
    """对外时序求值入口：表达式字符串或 AST + 字段序列 → 等长 list。"""
    ast = parse(expr) if isinstance(expr, str) else expr
    return _eval_ts(ast, data)


# =========================== 截面求值（同一时点跨品种） ===========================
def _average_ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    k = 0
    while k < len(order):
        j = k
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[k]]:
            j += 1
        avg = (k + j) / 2.0 + 1.0
        for t in range(k, j + 1):
            ranks[order[t]] = avg
        k = j + 1
    return ranks


def eval_cs(expr, cs_data, syms=None):
    """AST/字符串 + {字段:{品种:值}} → {品种:值}。截面算子在同一时点跨品种，时序算子禁用。"""
    ast = parse(expr) if isinstance(expr, str) else expr
    if syms is None:
        syms = sorted({s for v in cs_data.values() if isinstance(v, dict) for s in v})

    def go(node):
        k = node[0]
        if k == "num":
            return {s: node[1] for s in syms}
        if k == "var":
            if node[1] not in cs_data:
                raise ExprError("字段 %r 不在截面数据中" % node[1])
            return cs_data[node[1]]
        if k == "neg":
            x = go(node[1])
            return {s: (-v if _isnum(v) else None) for s, v in x.items()}
        if k == "bin":
            a, b = go(node[2]), go(node[3])
            return {s: _bin_scalar(node[1], a.get(s), b.get(s)) for s in syms}
        if k == "call":
            fn = node[1]
            if fn in _TS_OPS:
                raise ExprError("时序算子 %r 不能在截面上下文使用" % fn)
            if fn in _EL_OPS:
                xs = [go(a) for a in node[2]]
                out = {}
                for s in syms:
                    out[s] = _el_scalar(fn, [x.get(s) for x in xs])
                return out
            x = go(node[2][0])
            finite = [(s, v) for s, v in x.items() if _isnum(v)]
            out = {s: None for s in syms}
            if not finite:
                return out
            ss = [s for s, _ in finite]
            vals = [v for _, v in finite]
            if fn == "cross_rank":
                ranks = _average_ranks(vals)
                for s, r in zip(ss, ranks):
                    out[s] = (r - 1.0) / (len(vals) - 1) if len(vals) > 1 else 0.5
            elif fn == "scale":
                denom = sum(abs(v) for v in vals)
                for s, v in zip(ss, vals):
                    out[s] = v / denom if denom > 1e-15 else None
            elif fn == "zscore":
                m = _mean(vals)
                sd = _sample_std(vals)
                for s, v in zip(ss, vals):
                    out[s] = (v - m) / sd if sd and sd > 1e-15 else 0.0
            return out
        raise ExprError("未知节点 %r" % k)

    return go(ast)


def _el_scalar(fn, args):
    if fn == "abs":
        return abs(args[0]) if _isnum(args[0]) else None
    if fn == "sign":
        return (1 if args[0] > 0 else (-1 if args[0] < 0 else 0)) if _isnum(args[0]) else None
    if fn == "log":
        return math.log(args[0]) if _isnum(args[0]) and args[0] > 0 else None
    if fn == "tanh":
        return math.tanh(args[0]) if _isnum(args[0]) else None
    if fn in ("max", "min"):
        return (max if fn == "max" else min)(args) if all(_isnum(v) for v in args) else None
    raise ExprError("未知逐元素算子 %r" % fn)


# =========================== 因子治理：相关/正交/加权合成（纯标准库） ===========================
def pearson(xs, ys):
    """有限对齐样本 Pearson；样本<2 或零方差返回 None。"""
    pairs = [(x, y) for x, y in zip(xs, ys) if _isnum(x) and _isnum(y)]
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    if sxx <= 1e-15 or syy <= 1e-15:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(xs, ys):
    """Spearman=秩的 Pearson（并列平均秩）；非有限样本位置成对剔除。"""
    pairs = [(x, y) for x, y in zip(xs, ys) if _isnum(x) and _isnum(y)]
    if len(pairs) < 2:
        return None
    rx = _average_ranks([p[0] for p in pairs])
    ry = _average_ranks([p[1] for p in pairs])
    return pearson(rx, ry)


def solve(A, b):
    """高斯消元解线性方程组 Ax=b（方阵）；奇异返回 None。"""
    n = len(A)
    M = [list(map(float, A[i])) + [float(b[i])] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        M[col] = [v / d for v in M[col]]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col]
            if f:
                M[r] = [M[r][j] - f * M[col][j] for j in range(n + 1)]
    return [M[i][n] for i in range(n)]


def orthogonalize(target, bases):
    """target 对 bases（等长序列列表）做 OLS，返回 (残差序列, beta列表)。

    只用 target 与所有 base 同时有限的下标估 β；残差=target−Σβ·base，其余位置 None。
    共线/样本不足时 β 退化为空、残差为 target 副本（安全降级）。
    """
    n = len(target)
    idx = [t for t in range(n) if _isnum(target[t]) and all(_isnum(b[t]) for b in bases)]
    k = len(bases)
    beta = [0.0] * k
    if len(idx) > k + 2 and k >= 1:
        XtX = [[0.0] * k for _ in range(k)]
        Xty = [0.0] * k
        for t in idx:
            xv = [b[t] for b in bases]
            for i in range(k):
                Xty[i] += xv[i] * target[t]
                for j in range(k):
                    XtX[i][j] += xv[i] * xv[j]
        sol = solve(XtX, Xty)
        if sol is not None:
            beta = sol
    resid = [None] * n
    for t in idx:
        resid[t] = target[t] - sum(beta[i] * bases[i][t] for i in range(k))
    return resid, beta


def equal_weights(k):
    return [1.0 / k] * k if k else []


def ic_weights(ics):
    """按 |IC| 归一的有符号权重（w_i=IC_i/Σ|IC|，和=1、保留方向）；全零退化为等权。"""
    k = len(ics)
    denom = sum(abs(v) for v in ics if _isnum(v))
    if denom <= 1e-15:
        return equal_weights(k)
    return [((v if _isnum(v) else 0.0) / denom) for v in ics]


def icir_weights(ic_series_list):
    """每个因子给一条滚动 IC 序列，按 均值/样本std（信息比）归一为权重（和=1、保留方向）。"""
    irr = []
    for seq in ic_series_list:
        vals = [v for v in seq if _isnum(v)]
        if len(vals) >= 3:
            m = _mean(vals)
            sd = _sample_std(vals)
            irr.append(m / sd if sd and sd > 1e-15 else 0.0)
        else:
            irr.append(0.0)
    denom = sum(abs(v) for v in irr)
    k = len(ic_series_list)
    if denom <= 1e-15:
        return equal_weights(k)
    return [v / denom for v in irr]


def combine(matrix, weights):
    """matrix=[因子序列...]、weights 等长 → 逐点加权和（任一缺失按可得权重归一）。"""
    k = len(matrix)
    n = len(matrix[0])
    out = [None] * n
    for t in range(n):
        num = den = 0.0
        for i in range(k):
            v = matrix[i][t]
            if _isnum(v):
                num += weights[i] * v
                den += abs(weights[i])
        out[t] = num / den if den > 1e-15 else None
    return out


# =========================== 表达式因子库（研究侧、默认不进综合分） ===========================
# 每条：key/表达式/方向/中文名/说明；引擎只承载这些"新研究因子"，旧因子保持过程式原实现。
LIBRARY = (
    {"key": "expr_ma_bias5", "expr": "delta(close,5)/delay(close,5)", "direction": +1,
     "name": "5日价格动量(表达式版)", "note": "等价 ret5，用于实时/离线 parity 基准"},
    {"key": "expr_ma_ratio", "expr": "ts_mean(close,5)/ts_mean(close,20)-1", "direction": +1,
     "name": "短长均线比", "note": "5日均线上穿20日均线强度"},
    {"key": "expr_trend_per_vol", "expr": "(close/ts_mean(close,20)-1)/(ts_std(close,20)+0.000001)", "direction": +1,
     "name": "单位波动趋势", "note": "20日趋势除以其波动，风险调整动量"},
    {"key": "expr_price_accel", "expr": "delta(delta(close,5),5)/delay(close,10)", "direction": +1,
     "name": "价格二阶加速度", "note": "嵌套 delta 的动量变化率"},
    {"key": "expr_illiq", "expr": "abs(delta(close,1)/delay(close,1))/(volume+1)", "direction": -1,
     "name": "非流动性代理", "note": "Amihud |收益|/成交量 的无量纲代理（面板无成交额，用volume）"},
    # ===== G25续（第59轮）旧技术因子过程式→表达式：以下表达式刻意按 futures_data 过程式的**同一运算顺序**书写，
    # ret 用 close/delay-1（而非 delta/delay）以保证逐字节相等；SMA 用 ts_mean（与增量SMA仅末位舍入差异，见 factor_legacy_expr）。
    {"key": "expr_ret5_exact", "expr": "close/delay(close,5)-1", "direction": +1,
     "name": "5日收益(过程式逐字节镜像)", "note": "与 technical_profile.ret5 同运算序，float.hex 逐位相等；区别于 delta 写法的 expr_ma_bias5"},
    {"key": "expr_ret20_exact", "expr": "close/delay(close,20)-1", "direction": +1,
     "name": "20日收益(过程式逐字节镜像)", "note": "与 technical_profile.ret20 同运算序，float.hex 逐位相等"},
    {"key": "expr_ma10", "expr": "ts_mean(close,10)", "direction": 0,
     "name": "10日均线(表达式版)", "note": "对应 _sma_series(close,10)，窗内求和与增量累加仅末位浮点差异"},
    # ===== G25续（第60轮）更多过程式技术量按同运算序表达式化 =====
    {"key": "expr_ma5", "expr": "ts_mean(close,5)", "direction": 0,
     "name": "5日均线(表达式版)", "note": "对应 _sma_series(close,5)，与增量SMA仅末位舍入差异"},
    {"key": "expr_ma20", "expr": "ts_mean(close,20)", "direction": 0,
     "name": "20日均线(表达式版)", "note": "对应 _sma_series(close,20)，布林中轨同源"},
    {"key": "expr_ma60", "expr": "ts_mean(close,60)", "direction": 0,
     "name": "60日均线(表达式版)", "note": "对应 _sma_series(close,60)=TECH_LONG_MA"},
    {"key": "expr_boll_std20", "expr": "ts_std(close,20)", "direction": 0,
     "name": "20日样本标准差(表达式版)", "note": "与 _sample_std(close[-20:]) 同求和序，float.hex 逐位相等（布林带宽用）"},
    {"key": "expr_hv20", "expr": "ts_std(log(close/delay(close,1)),20)*15.874507866387544", "direction": 0,
     "name": "20日历史波动率年化(表达式版)", "note": "log收益样本std*sqrt252(=15.874507866387544)，与 _hv_at(.,20) 同运算序逐位相等"},
    # ===== G25续（第61轮）状态量表达式化：ts_ema/ts_rma 状态递推算子，MACD/RSI 与过程式逐位一致 =====
    {"key": "expr_macd_dif", "expr": "ts_ema(close,12)-ts_ema(close,26)", "direction": 0,
     "name": "MACD-DIF(表达式版)", "note": "12/26 EMA之差，SMA播种，与 technical_profile dif 逐位相等"},
    {"key": "expr_macd_dea", "expr": "ts_ema(ts_ema(close,12)-ts_ema(close,26),9)", "direction": 0,
     "name": "MACD-DEA(表达式版)", "note": "对DIF连续子序列再做9日EMA（嵌套ts_ema），与 dea 逐位相等"},
    {"key": "expr_macd_hist",
     "expr": "(ts_ema(close,12)-ts_ema(close,26)-ts_ema(ts_ema(close,12)-ts_ema(close,26),9))*2.0", "direction": 0,
     "name": "MACD柱(表达式版)", "note": "(DIF-DEA)*2，与 macd_hist 逐位相等"},
    {"key": "expr_rsi14",
     "expr": "100.0-100.0/(1.0+ts_rma(max(close-delay(close,1),0.0),14)/ts_rma(max(delay(close,1)-close,0.0),14))",
     "direction": 0, "name": "Wilder RSI14(表达式版)",
     "note": "ts_rma=Wilder平滑；非平盘分支与 _rsi_series 逐位相等，avg_loss≈0 的平盘强制100分支口径差异已在parity钉死"},
)


def library_dict():
    return {f["key"]: f for f in LIBRARY}


# =========================== 零网络/零DB 合成断言 ===========================
def _close_series():
    return [100.0, 101.0, 103.0, 102.0, 105.0, 108.0, 107.0, 110.0, 112.0, 115.0]


def selftest():
    # 1) 解析器：白名单放行、危险/未知算子拒绝（反向用例）
    assert parse("a+b*2")[0] == "bin"
    for bad in ["__import__('os')", "x.open", "eval(x)", "lambda:1", "foo(close,3)",
                "import os", "x;y", "globals()", "close..3", "ts_mean(close,-2)"]:
        try:
            compute_ts(bad, {"close": [1.0, 2.0], "x": [1.0, 2.0]})
            raise AssertionError("应拒绝: %s" % bad)
        except ExprError:
            pass
    # 窗口必须正整数
    try:
        compute_ts("ts_mean(close,close)", {"close": [1.0, 2.0]})
        raise AssertionError("窗口非常量应拒绝")
    except ExprError:
        pass

    # 2) delay/delta 手算
    c = _close_series()
    d = compute_ts("delay(close,2)", {"close": c})
    assert d[0] is None and d[1] is None and d[2] == c[0] and d[9] == c[7]
    dl = compute_ts("delta(close,1)", {"close": c})
    assert dl[1] == c[1] - c[0] and dl[0] is None

    # 3) ts_mean/ts_sum/ts_std/ts_min/max 手算
    tm = compute_ts("ts_mean(close,3)", {"close": c})
    assert tm[2] == (c[0] + c[1] + c[2]) / 3 and tm[0] is None and tm[1] is None
    tsum = compute_ts("ts_sum(close,3)", {"close": c})
    assert tsum[3] == c[1] + c[2] + c[3]
    tmin = compute_ts("ts_min(close,3)", {"close": c})
    tmax = compute_ts("ts_max(close,3)", {"close": c})
    assert tmin[3] == min(c[1:4]) and tmax[3] == max(c[1:4])
    sd = compute_ts("ts_std(close,3)", {"close": c})
    import statistics
    assert abs(sd[3] - statistics.stdev(c[1:4])) < 1e-12

    # 4) ts_rank/ts_minmax/decay_linear 手算
    tr = compute_ts("ts_rank(close,3)", {"close": [1.0, 2.0, 3.0]})
    # 末值3在窗[1,2,3]中最大：less=2,equal=1 →(2+0.5*2)/3=1.0
    assert abs(tr[2] - 1.0) < 1e-12
    mm = compute_ts("ts_minmax(close,3)", {"close": [2.0, 4.0, 6.0]})
    assert abs(mm[2] - 1.0) < 1e-12 and mm[0] is None
    mmflat = compute_ts("ts_minmax(close,3)", {"close": [5.0, 5.0, 5.0]})
    assert mmflat[2] == 0.5
    dlw = compute_ts("decay_linear(close,3)", {"close": [1.0, 2.0, 3.0]})
    assert abs(dlw[2] - (1 * 1 + 2 * 2 + 3 * 3) / 6) < 1e-12
    # 4b) ts_ema/ts_rma 状态递推手算：前 n 个有限值 SMA 播种、其后递推（跳过前导 None）
    es = compute_ts("ts_ema(close,3)", {"close": [1.0, 2.0, 3.0, 5.0]})
    seed = (1 + 2 + 3) / 3.0
    a = 2.0 / 4.0
    assert es[0] is None and es[1] is None and float.hex(es[2]) == float.hex(seed)
    assert float.hex(es[3]) == float.hex(a * 5.0 + (1 - a) * seed)
    rm = compute_ts("ts_rma(close,3)", {"close": [1.0, 2.0, 3.0, 6.0]})
    assert float.hex(rm[2]) == float.hex(seed)
    assert float.hex(rm[3]) == float.hex((2 * seed + 6.0) / 3.0)
    lead = compute_ts("ts_ema(close,2)", {"close": [None, None, 4.0, 6.0]})  # 前导 None 不影响播种
    assert lead[0] is None and lead[1] is None and lead[2] is None
    assert float.hex(lead[3]) == float.hex((4.0 + 6.0) / 2.0)   # 前2个有限值(4,6)SMA播种于idx3

    # 5) corr 与嵌套表达式、无未来（末根之外不依赖未来）
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [2.0, 4.0, 6.0, 8.0, 10.0]
    rr = compute_ts("corr(close,y,3)", {"close": x, "y": y})
    assert rr[4] == 1.0 and rr[0] is None and rr[1] is None
    nested = compute_ts("delta(delta(close,2),2)/delay(close,4)", {"close": c})
    assert len(nested) == len(c)
    # 扰动未来：改最后一根，前面所有输出必须不变（无未来函数）
    base = compute_ts("ts_mean(close,5)", {"close": c})
    pert = list(c); pert[-1] += 50.0
    after = compute_ts("ts_mean(close,5)", {"close": pert})
    assert all(base[t] == after[t] for t in range(len(c) - 1))

    # 6) 逐元素/四则/除零安全
    assert compute_ts("1/0", {"close": c}) is None or all(v is None for v in [compute_ts("close/0", {"close": c})[0]])
    sg = compute_ts("sign(close-104)", {"close": c})
    assert sg[0] == -1 and sg[4] == 1 and sg[3] == -1
    lg = compute_ts("log(close)", {"close": [1.0, math.e, 0.0, -1.0]})
    assert abs(lg[1] - 1.0) < 1e-12 and lg[2] is None and lg[3] is None
    # tanh 逐元素（G25续：声明式复刻综合分 tanh 压缩所需）
    th = compute_ts("tanh(close)", {"close": [0.0, 1.0, -1.0]})
    assert th[0] == 0.0 and abs(th[1] - math.tanh(1.0)) < 1e-15 and abs(th[2] - math.tanh(-1.0)) < 1e-15
    thc = eval_cs("tanh(m)", {"m": {"A": 0.5, "B": -0.5}})
    assert abs(thc["A"] - math.tanh(0.5)) < 1e-15 and abs(thc["B"] + math.tanh(0.5)) < 1e-15

    # 7) 截面 cross_rank/scale/zscore 手算
    cs = {"m": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}}
    cr = eval_cs("cross_rank(m)", cs)
    assert cr["A"] == 0.0 and abs(cr["D"] - 1.0) < 1e-12 and abs(cr["B"] - 1 / 3) < 1e-12
    sc = eval_cs("scale(m)", cs)
    assert abs(sum(abs(v) for v in sc.values()) - 1.0) < 1e-12
    zs = eval_cs("zscore(m)", cs)
    assert abs(sum(zs.values())) < 1e-12
    # 截面里用时序算子必须报错，反之亦然
    try:
        eval_cs("ts_mean(m,3)", cs); raise AssertionError
    except ExprError:
        pass
    try:
        compute_ts("cross_rank(close)", {"close": c}); raise AssertionError
    except ExprError:
        pass

    # 8) 治理：pearson/spearman/正交/加权
    assert pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert spearman([3, 1, 2], [6, 2, 4]) == 1.0
    assert pearson([1], [1]) is None
    # 正交：y=2*x1+3*x2，残差≈0、β恢复
    x1 = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    x2 = [2.0, 1.0, 3.0, 2.0, 4.0, 3.0]
    yv = [2 * a + 3 * b for a, b in zip(x1, x2)]
    resid, beta = orthogonalize(yv, [x1, x2])
    assert abs(beta[0] - 2.0) < 1e-9 and abs(beta[1] - 3.0) < 1e-9
    assert all(abs(r) < 1e-9 for r in resid)
    # 共线安全降级不崩
    r2, b2 = orthogonalize([1.0, 2.0, 3.0], [[1.0, 1.0, 1.0]])
    assert len(r2) == 3
    w = ic_weights([0.3, -0.1, 0.0])
    assert abs(sum(abs(v) for v in w) - 1.0) < 1e-12 and abs(w[0] - 0.75) < 1e-12
    assert equal_weights(4) == [0.25] * 4
    comb = combine([[1.0, 2.0], [3.0, 4.0]], [0.5, 0.5])
    assert comb[0] == 2.0 and comb[1] == 3.0
    iw = icir_weights([[1, 2, 1, 2], [-1, -2, -1, -2]])
    assert abs(iw[0] - 0.5) < 1e-12 and iw[1] < 0

    # 9) training-serving parity（结构性）：同一表达式喂两份数值相同、来源不同的序列必逐值相等
    offline = compute_ts("ts_mean(close,5)/ts_std(close,5)", {"close": c})
    realtime = compute_ts("ts_mean(close,5)/ts_std(close,5)", {"close": list(c)})
    assert offline == realtime
    # 因子库每条表达式都能编译且时序可算
    for f in LIBRARY:
        ast = parse(f["expr"])
        out = compute_ts(ast, {"close": c, "volume": [1000 + i for i in range(len(c))]})
        assert len(out) == len(c)
    print("factor_expr selftest ALL PASS（安全解析白名单/拒绝危险调用、delay-delta/窗口统计/"
          "ts_rank-minmax-decay/corr嵌套与无未来、截面cross_rank-scale-zscore、pearson-spearman/"
          "OLS正交恢复/IC·ICIR加权、实时离线结构性parity、因子库可编译 共9组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
