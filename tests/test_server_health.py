"""第156轮 A3：云服务器健康度页签回归——server_health 探测纯函数 + 落盘 + 看板页签渲染。

纪律：零网络（探测函数全部 monkeypatch/注入）、路径全部指向 tmp_path、不碰生产 reports/data/。
"""

import json

import report
import server_health


class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeState:
    stop = None  # health_loop 不在此测试


def test_probe_one_success(monkeypatch):
    def _get(url, timeout):
        assert url.endswith("/health")
        return _FakeResp(
            200,
            {"status": "ok", "uptime_s": 3661, "daily": 3, "minute": 5, "pops": 2, "fail": 0},
        )

    monkeypatch.setattr(server_health, "requests", type("R", (), {"get": staticmethod(_get)})())
    out = server_health._probe_one("http://1.2.3.4:9001", 1.0)
    assert out["online"] is True
    assert out["uptime_s"] == 3661
    assert out["daily"] == 3 and out["minute"] == 5 and out["pops"] == 2 and out["fail"] == 0
    assert out["error"] == ""


def test_probe_one_http_error_and_exception(monkeypatch):
    def _get500(url, timeout):
        return _FakeResp(500, None)

    monkeypatch.setattr(server_health, "requests", type("R", (), {"get": staticmethod(_get500)})())
    out = server_health._probe_one("http://1.2.3.4:9001", 1.0)
    assert out["online"] is False and out["error"].startswith("HTTP")

    def _boom(url, timeout):
        raise ConnectionError("refused")

    monkeypatch.setattr(server_health, "requests", type("R", (), {"get": staticmethod(_boom)})())
    out = server_health._probe_one("http://1.2.3.4:9001", 1.0)
    assert out["online"] is False and "refused" in out["error"]


def test_probe_servers_empty_and_aggregate(monkeypatch):
    base = server_health.probe_servers(urls=[], timeout=1.0)
    assert base["n_total"] == 0 and base["n_online"] == 0 and base["servers"] == []

    monkeypatch.setattr(
        server_health,
        "_probe_one",
        lambda u, t: {"url": u, "online": "a" in u, "error": ""}
        if "a" in u
        else {"url": u, "online": False, "error": "timeout"},
    )
    out = server_health.probe_servers(urls=["http://a:9001", "http://b:9001"], timeout=1.0)
    assert out["n_total"] == 2 and out["n_online"] == 1
    assert out["servers"][0]["url"] == "http://a:9001"
    json.dumps(out, ensure_ascii=False)  # JSON 安全


def test_write_server_health_ok_and_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(server_health.config, "SERVER_HEALTH_FILE", str(tmp_path / "sh.json"))
    monkeypatch.setattr(server_health, "probe_servers", lambda urls=None, timeout=None: {
        "generated_at": "2026-09-14 10:00:00", "n_total": 1, "n_online": 1,
        "servers": [{"url": "http://x:9001", "online": True, "error": "", "uptime_s": 60}],
    })
    assert server_health.write_server_health() is True
    d = json.loads((tmp_path / "sh.json").read_text(encoding="utf-8"))
    assert d["n_online"] == 1 and d["servers"][0]["uptime_s"] == 60

    monkeypatch.setattr(server_health, "probe_servers", lambda urls=None, timeout=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert server_health.write_server_health() is False


def test_servers_panel_html_missing_and_rendered(tmp_path, monkeypatch):
    # 文件缺失 -> 提示，不抛
    monkeypatch.setattr(report.config, "SERVER_HEALTH_FILE", str(tmp_path / "no.json"))
    h = report._servers_panel_html()
    assert "云服务器健康" in h and "健康页未生成" in h

    # 渲染：在线/离线两行 + 计数
    d = {
        "generated_at": "2026-09-14 10:00:00",
        "n_total": 2,
        "n_online": 1,
        "servers": [
            {"url": "http://39.98.1.1:9001", "online": True, "error": "", "uptime_s": 7300,
             "daily": 10, "minute": 20, "pops": 3, "fail": 0},
            {"url": "http://47.92.2.2:9001", "online": False, "error": "Forbidden"},
        ],
    }
    p = tmp_path / "sh.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(report.config, "SERVER_HEALTH_FILE", str(p))
    h = report._servers_panel_html()
    assert "39.98.1.1" in h and "47.92.2.2" in h
    assert "1/2" in h
    assert "在线" in h and "离线" in h
    assert "10" in h and "20" in h  # daily/minute 计数
    assert "Forbidden" in h
    assert "<script" not in h.lower().replace("<script>", "")  # 无注入（错误文本已转义）


def test_dashboard_has_servers_tab(monkeypatch):
    monkeypatch.setattr(report.config, "PAPER_ENABLED", True)
    h = report._dashboard_html()
    assert 'data-src="__servers__"' in h
    assert 'id="servers-panel"' in h
    assert "/*__SRV_DOM__*/" not in h  # 占位符已被替换
    assert "云服务器健康" in h
