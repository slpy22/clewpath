"""ops/tools/pwa_drift.py — 운영 릴레이 PWA 와 작업 트리 pwa/ 비교(release.ps1 5단계의 판단 근거).

릴레이 서빙 규칙(007 relay/relay.py _process_request)을 흉내 낸 로컬 서버로 시험한다.
"""
from __future__ import annotations

import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pwa_drift", ROOT / "ops" / "tools" / "pwa_drift.py")
pwa_drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pwa_drift)


def _make_pwa(d: Path) -> Path:
    p = d / "pwa"
    (p / "vendor").mkdir(parents=True)
    (p / "index.html").write_text("<html data-mode='{{MODE}}' base='{{BASE}}'>한글</html>", encoding="utf-8")
    (p / "sw.js").write_bytes(b"self.addEventListener('push',()=>{})")
    (p / "app.webmanifest").write_bytes(b"{}")
    (p / "native-bridge.js").write_bytes(b"// noop")
    (p / "vendor" / "xterm.min.js").write_bytes(b"xterm")
    return p


def _serve(pwa: Path):
    """relay.py 처럼 서빙: /app 은 치환한 index.html, 나머지는 파일 그대로."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/relay/app":
                body = (pwa / "index.html").read_text(encoding="utf-8").replace("{{MODE}}", "relay").replace("{{BASE}}", "").encode()
            else:
                f = pwa / self.path[len("/relay/"):]
                if not self.path.startswith("/relay/") or not f.is_file():
                    self.send_response(404); self.end_headers(); return
                body = f.read_bytes()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/relay"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch):
    """도구는 공개 주소용이라 사용자 프록시를 따른다(urllib 기본). 시험의 로컬 서버(127.0.0.1)는 프록시를 타지 않게 —
    전체 실행에서 앞 시험이 남긴 프록시 env 로 '연결 거부' 가 났다(단독 실행은 통과)."""
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def served(tmp_path):
    live = _make_pwa(tmp_path / "live")
    srv, base = _serve(live)
    yield live, base
    srv.shutdown()


def test_same_content_has_no_drift(served, tmp_path):
    live, base = served
    local = _make_pwa(tmp_path / "local")
    r = pwa_drift.drift(base, local, timeout=5)
    assert r == {"checked": 5, "differ": [], "error": None}


def test_changed_files_are_listed(served, tmp_path):
    live, base = served
    local = _make_pwa(tmp_path / "local")
    (local / "sw.js").write_bytes(b"self.addEventListener('push',()=>{ /* v2 */ })")
    (local / "index.html").write_text("<html data-mode='{{MODE}}'>새 화면</html>", encoding="utf-8")
    (local / "vendor" / "new-icon.png").write_bytes(b"png")      # 운영에 아직 없는 파일 → 404 = 비교 실패
    r = pwa_drift.drift(base, local, timeout=5)
    assert r["error"] and "new-icon.png" in r["error"]           # 못 받은 파일이 있으면 판단하지 않는다
    (local / "vendor" / "new-icon.png").unlink()
    r = pwa_drift.drift(base, local, timeout=5)
    assert r["error"] is None and sorted(r["differ"]) == ["index.html", "sw.js"]


def test_unreachable_relay_is_an_error_not_a_verdict(tmp_path):
    local = _make_pwa(tmp_path / "local")
    r = pwa_drift.drift("http://127.0.0.1:9/relay", local, timeout=2)
    assert r["checked"] == 0 and r["differ"] == [] and r["error"]


def test_maps_the_real_pwa_tree():
    files = pwa_drift.expected_files(ROOT / "pwa")
    assert files["index.html"][0] == "/app" and b"{{MODE}}" not in files["index.html"][1]
    assert files["sw.js"][0] == "/sw.js" and files["vendor/xterm.min.js"][0] == "/vendor/xterm.min.js"
    tracked = {p.relative_to(ROOT / "pwa").as_posix() for p in (ROOT / "pwa").rglob("*") if p.is_file()}
    assert set(files) == tracked, "릴레이가 서빙하지 않는 pwa 파일이 생기면 relay.py 와 이 대응을 함께 고친다"
