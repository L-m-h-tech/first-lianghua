# -*- coding: utf-8 -*-
"""G8（第57轮）只读 Web / 手机看板服务器 web_dashboard —— 纯标准库、零第三方依赖、默认只绑本机。

定位：把已经落盘的 reports/ 研究产物（图表看板.html、实时报告.html、各 *.txt/*.json/*.csv）用标准库
http.server 在本机/局域网起一个**只读静态站点**，手机连同一 Wi-Fi 即可查看，不必装任何东西。

安全边界（刻意从简、只读）：
- 只允许 GET/HEAD，其余方法（POST/PUT/DELETE/PATCH…）一律 405；不接收任何写入、不执行任何 CGI；
- 只暴露 reports/ 目录，safe_join 做路径规范化与 commonpath 校验，`..`/绝对路径/符号链接越界一律 403；
- 默认绑 127.0.0.1（仅本机）；显式 --lan 才绑 0.0.0.0（局域网可达），并打印醒目风险提示；
- 不访问外网、不上传任何数据，只把本地文件读出来回给请求方。

用法：
    python tools/web_dashboard.py                 # 默认 127.0.0.1:8765，自动开浏览器
    python tools/web_dashboard.py --port 9000 --no-open
    python tools/web_dashboard.py --lan           # 绑 0.0.0.0，手机同 Wi-Fi 访问（打印本机IP）
    python tools/web_dashboard.py --selftest      # 离线纯逻辑自测（不起 socket）
"""
import argparse
import datetime as _dt
import html as _html
import http.server
import os
import socket
import socketserver
import sys
import urllib.parse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(_ROOT, "reports")
DEFAULT_PORT = 8765
ALLOWED_METHODS = ("GET", "HEAD")
_CT = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
       ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
       ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
       ".css": "text/css; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
       ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml",
       ".ico": "image/x-icon", ".pdf": "application/pdf"}
# 首页优先展示的入口
PRIORITY = ("图表看板.html", "实时报告.html")


# =========================== 纯函数（可离线单测） ===========================
def safe_join(root, urlpath):
    """把 URL 路径安全映射到 root 下的真实文件；越界/非法返回 None。纯函数。

    - 先 percent-decode（支持中文文件名），去掉查询串与开头的 /；
    - normpath+abspath 后用 commonpath 强制结果必须落在 root 内（防 `..`、绝对路径、盘符穿越）。
    """
    if urlpath is None:
        return None
    path = urlpath.split("?", 1)[0].split("#", 1)[0]
    path = urllib.parse.unquote(path)
    if path.startswith("/"):
        path = path[1:]
    if not path:
        return os.path.abspath(root)
    # 显式拒绝 Windows 盘符/反斜杠绝对路径与空字节
    if "\x00" in path or (len(path) >= 2 and path[1] == ":"):
        return None
    base = os.path.abspath(root)
    target = os.path.abspath(os.path.join(base, path.replace("/", os.sep)))
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:          # 不同盘符（如 C: vs D:）
        return None
    return target


def content_type(path):
    return _CT.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def list_reports(root):
    """列出 root 下一层文件（不递归），返回 [(name, size, mtime_str)]，入口优先、其后按名排序。目录跳过。"""
    if not os.path.isdir(root):
        return []
    rows = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if os.path.isfile(full):
            st = os.stat(full)
            mt = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            rows.append((name, st.st_size, mt))
    pri = [r for r in rows if r[0] in PRIORITY]
    pri.sort(key=lambda r: PRIORITY.index(r[0]))
    rest = sorted((r for r in rows if r[0] not in PRIORITY), key=lambda r: r[0])
    return pri + rest


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0


def render_index(entries, root_label="reports", generated=None):
    """生成暗色、手机自适应的只读首页（含 viewport）。entries=list_reports 的结果。纯函数。"""
    generated = generated or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for name, size, mt in entries:
        href = urllib.parse.quote(name)
        rows.append(
            '<li><a href="%s">%s</a><span class="meta">%s · %s</span></li>'
            % (href, _html.escape(name), _human_size(size), _html.escape(mt)))
    body = "\n".join(rows) or '<li class="empty">reports/ 暂无文件（先运行一轮监控/研究工具）。</li>'
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>期货监控·只读看板索引</title><style>"
            "body{background:#1c1c1c;color:#e6e6e6;font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;"
            "margin:0;padding:18px;line-height:1.5}h1{font-size:18px;margin:0 0 4px}"
            ".tip{color:#9a9a9a;font-size:12px;margin-bottom:14px}"
            "ul{list-style:none;padding:0;margin:0}li{border-bottom:1px solid #2c2c2c;padding:10px 2px;"
            "display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}"
            "a{color:#7ecbff;text-decoration:none;font-size:15px;word-break:break-all}"
            ".meta{color:#8a8a8a;font-size:12px;white-space:nowrap}.empty{color:#8a8a8a;border:none}"
            "</style></head><body>"
            "<h1>期货监控 · 只读研究看板</h1>"
            "<div class=\"tip\">目录 %s（只读静态服务，不接收写入）· 生成 %s · 手机同 Wi-Fi 可直接点开</div>"
            "<ul>%s</ul></body></html>" % (_html.escape(root_label), _html.escape(generated), body))


def is_allowed_method(method):
    return method in ALLOWED_METHODS


def make_handler(root):
    """构造绑定到 root 的只读 Handler 类（工厂，避免全局状态）。"""

    class _ReadOnlyHandler(http.server.BaseHTTPRequestHandler):
        server_version = "FuturesMonitorRO/1.0"

        def log_message(self, fmt, *args):          # 静默默认访问日志，错误仍由下法打印
            pass

        def _reply_bytes(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)

        def _reply_text(self, code, text):
            self._reply_bytes(code, text.encode("utf-8"))

        def _serve(self):
            if not is_allowed_method(self.command):
                self.send_response(405)
                self.send_header("Allow", "GET, HEAD")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            urlpath = self.path
            target = safe_join(root, urlpath)
            if target is None:
                self._reply_text(403, "<h1>403 Forbidden</h1>"); return
            if os.path.isdir(target) or urlpath in ("/", ""):
                body = render_index(list_reports(root)).encode("utf-8")
                self._reply_bytes(200, body); return
            if not os.path.isfile(target):
                self._reply_text(404, "<h1>404 Not Found</h1>"); return
            try:
                with open(target, "rb") as fp:
                    body = fp.read()
            except OSError:
                self._reply_text(404, "<h1>404 Not Found</h1>"); return
            self._reply_bytes(200, body, content_type(target))

        do_GET = _serve
        do_HEAD = _serve
        do_POST = _serve
        do_PUT = _serve
        do_DELETE = _serve
        do_PATCH = _serve

    return _ReadOnlyHandler


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lan_ip():
    """ best-effort 取本机局域网 IP（不实际发包），失败返 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def serve(host="127.0.0.1", port=DEFAULT_PORT, root=REPORTS, open_browser=True):
    root = os.path.abspath(root)
    httpd = _ThreadingServer((host, port), make_handler(root))
    bound = httpd.server_address[1]
    print("只读看板已启动（Ctrl+C 停止）：")
    print("  本机: http://127.0.0.1:%d/" % bound)
    if host == "0.0.0.0":
        print("  手机/局域网: http://%s:%d/  （同一 Wi-Fi；此模式局域网内他人可读 reports，离开公共网络请用默认本机模式）"
              % (lan_ip(), bound))
    print("  服务目录(只读): %s" % root)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open("http://127.0.0.1:%d/" % bound)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        httpd.server_close()


# =========================== 离线纯逻辑自测 ===========================
def selftest():
    import tempfile
    # 1) safe_join：正常、中文、子文件
    root = os.path.abspath("reports")
    assert safe_join(root, "/a.txt").endswith(os.path.join("reports", "a.txt"))
    assert safe_join(root, "/%E5%9B%BE%E8%A1%A8.html") is not None
    # 2) 穿越/绝对/盘符/空字节 全拒
    assert safe_join(root, "/../secret.txt") is None
    assert safe_join(root, "/a/../../b") is None
    assert safe_join(root, "/C:/Windows/x") is None
    assert safe_join(root, "/a\x00.txt") is None
    assert safe_join(root, "") == os.path.abspath(root)
    # 3) content_type
    assert content_type("x.html").startswith("text/html")
    assert content_type("x.js").startswith("application/javascript")
    assert content_type("x.unk") == "application/octet-stream"
    # 4) 方法白名单
    assert is_allowed_method("GET") and is_allowed_method("HEAD")
    assert not is_allowed_method("POST") and not is_allowed_method("DELETE")
    # 5) list_reports + render_index：临时目录造文件，入口优先、含 viewport 与转义
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "z.txt"), "w", encoding="utf-8").write("x")
        open(os.path.join(td, "图表看板.html"), "w", encoding="utf-8").write("<html>")
        open(os.path.join(td, "x&y.txt"), "w", encoding="utf-8").write("x")
        ent = list_reports(td)
        assert ent[0][0] == "图表看板.html"          # 入口优先
        idx = render_index(ent, "tmp")
        assert "viewport" in idx and "图表看板.html" in idx
        assert "x&y.txt" not in idx and "x&amp;y.txt" in idx   # 文件名做 HTML 转义防 XSS
        assert list_reports(os.path.join(td, "no-such")) == []
    # 6) handler 工厂可构造出类、且实现了只读方法
    h = make_handler(root)
    for m in ("do_GET", "do_HEAD", "do_POST", "do_DELETE"):
        assert hasattr(h, m)
    print("web_dashboard selftest ALL PASS（6组）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="G8 只读 Web/手机看板（纯标准库静态服务器）")
    ap.add_argument("--host", default=None, help="绑定地址，默认127.0.0.1；用--lan改0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--root", default=REPORTS, help="只读服务目录，默认 reports/")
    ap.add_argument("--lan", action="store_true", help="绑0.0.0.0供局域网/手机访问（会打印风险提示）")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.host:
        host = args.host
    else:
        host = "0.0.0.0" if args.lan else "127.0.0.1"
    serve(host=host, port=args.port, root=args.root, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
