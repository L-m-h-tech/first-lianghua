# -*- coding: utf-8 -*-
r"""第94轮 B7（对标 scrapling 的 RAG/markdown 无 LLM 资产化）：页面 HTML 转存为规范化 markdown 资产。

对关键网页（新闻/期权/库存等）调 html_text.clean_text 转成干净 markdown 文本，落 cache/page_md/ 目录
并登记 manifest（source, url, timestamp, path, chars）供将来检索/复盘用。纯标准库、零网络、
只写 cache/（gitignored）；接线方式：各数据源解析函数成功后调 archive(source, url, html) 一行即可。

CLI：
  python tools/page_archive.py --source sina_news --url "http://..." --html-file page.html
  python tools/page_archive.py --selftest
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import html_text              # noqa: E402

ARCHIVE_DIR = os.path.join(_ROOT, "cache", "page_md")
MANIFEST = os.path.join(ARCHIVE_DIR, "manifest.json")


def archive(source, url, html, max_chars=None):
    """将 HTML 存为 markdown；返回归档路径或 None。失败静默不抛。"""
    if not html:
        return None
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        md = html_text.clean_text(html, max_len=max_chars)
        if not md.strip():
            return None
        day = time.strftime("%Y%m%d")
        safe_source = "".join(c if c.isalnum() or c in "-_" else "_" for c in source)[:40]
        ts = time.strftime("%H%M%S")
        fname = f"{day}_{safe_source}_{ts}.md"
        path = os.path.join(ARCHIVE_DIR, fname)
        Path(path).write_text(md, encoding="utf-8")
        _update_manifest({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
                           "url": url, "file": fname, "chars": len(md)})
        return path
    except Exception:
        return None


def _update_manifest(entry):
    try:
        m = []
        if os.path.exists(MANIFEST):
            with open(MANIFEST, encoding="utf-8") as f:
                m = json.load(f)
        m.append(entry)
        with open(MANIFEST, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def selftest():
    """零网络合成断言：纯文本/HTML/空输入、manifest 登记。"""
    checks = []
    import tempfile

    def ck(name, cond):
        checks.append((name, bool(cond)))
        if not cond:
            raise AssertionError("FAIL: " + name)

    with tempfile.TemporaryDirectory() as td:
        import page_archive as pa
        old_archive = pa.ARCHIVE_DIR
        pa.ARCHIVE_DIR = td
        pa.MANIFEST = os.path.join(td, "manifest.json")
        try:
            path = pa.archive("test_src", "https://x.test", "<p>Hello <script>x</script>World</p>")
            ck("归档文件存在", path and os.path.exists(path))
            ck("内容无script标签", "script" not in Path(path).read_text("utf-8").lower())
            ck("manifest已登记", os.path.exists(pa.MANIFEST) and "test_src" in Path(pa.MANIFEST).read_text("utf-8"))
            ck("空输入返回None", pa.archive("x", "", "") is None)
        finally:
            pa.ARCHIVE_DIR = old_archive
    return 0 if all(ok for _, ok in checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="B7 页面HTML→markdown归档（纯标准库，零网络）")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--url", default="")
    ap.add_argument("--html-file", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    html = Path(args.html_file).read_text(encoding="utf-8", errors="ignore") if args.html_file else ""
    path = archive(args.source, args.url, html)
    print("归档 → %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())