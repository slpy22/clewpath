"""가짜 컨트롤플레인 — 맥·리눅스 설치 e2e(docs/designs/posix-install.md 6.3). 표준 라이브러리만, 127.0.0.1 에만 붙는다.

    python3 -I fake_cp.py --state DIR --fixtures DIR [--port 8099]

| 요청                               | 응답                                                                    |
|------------------------------------|-------------------------------------------------------------------------|
| GET  /cp/healthz                   | {"ok":true}                                                             |
| GET  /cp/updates/manifest          | state/current.jwt (application/jwt), 없으면 404 {"error":"no_release"}  |
| GET  /cp/updates/download/<ver>    | fixtures/zips/<ver>.zip, 없으면 404 {"error":"not_found"}               |
| GET  /cp/get.sh                    | fixtures/get.sh 원본 바이트 그대로                                       |
| POST /cp/provision/anonymous       | 가짜 자격증명(횟수 기록). state/provision_status(또는 env FAKE_CP_PROVISION_STATUS)에 숫자(예: 429)를 두면 그 코드 |
| POST /cp/connector/token           | 503 — Host 는 fail-open 으로 계속 돈다                                   |
| 그 밖                              | 404                                                                     |

최신 버전 전환은 드라이버가 state/current.jwt 를 바꿔치기해서 한다(요청마다 디스크에서 읽는다 — 진짜 CP 와 같다).
모든 요청은 state/requests.log 에 JSON 한 줄씩 남는다(드라이버가 '예상 밖 경로'를 센다).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_VER = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,63}$")      # 파일 이름이 된다 — 경로 글자는 받지 않는다
_LOCK = threading.Lock()


def serve(state: Path, fixtures: Path, port: int) -> ThreadingHTTPServer:
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / "requests.log"

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _log(self, status: int, n: int) -> None:
            rec = {"t": round(time.time(), 3), "m": self.command, "p": self.path, "s": status, "n": n,
                   "ua": self.headers.get("User-Agent", "")}
            with _LOCK, log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self._log(status, len(body))                # 먼저 남긴다 — 받는 쪽이 도중에 끊어도(curl --max-filesize) 요청은 셌다
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

        def _drain(self) -> None:                       # keep-alive 연결에서 다음 요청과 섞이지 않게 본문을 읽어 버린다
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(min(n, 1 << 20))

        def do_GET(self):  # noqa: N802
            p = self.path.split("?", 1)[0]
            if p == "/cp/healthz":
                return self._json(200, {"ok": True})
            if p == "/cp/updates/manifest":
                try:
                    tok = (state / "current.jwt").read_bytes().strip()
                except OSError:
                    return self._json(404, {"error": "no_release"})
                return self._send(200, tok, "application/jwt")
            if p.startswith("/cp/updates/download/"):
                ver = p[len("/cp/updates/download/"):]
                f = fixtures / "zips" / f"{ver}.zip"
                if not _VER.match(ver) or not f.is_file():
                    return self._json(404, {"error": "not_found"})
                return self._send(200, f.read_bytes(), "application/zip")
            if p == "/cp/get.sh":
                return self._send(200, (fixtures / "get.sh").read_bytes(), "text/plain; charset=utf-8")
            return self._json(404, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            self._drain()
            p = self.path.split("?", 1)[0]
            if p == "/cp/provision/anonymous":
                with _LOCK:
                    cnt = state / "provision_count"
                    n = int(cnt.read_text(encoding="ascii") or 0) if cnt.is_file() else 0
                    cnt.write_text(str(n + 1), encoding="ascii")
                forced = os.environ.get("FAKE_CP_PROVISION_STATUS", "")
                try:
                    forced = (state / "provision_status").read_text(encoding="ascii").strip() or forced
                except OSError:
                    pass
                if forced and forced != "200":
                    return self._json(int(forced), {"error": "injected"})
                return self._json(200, {"connector_id": f"c-e2e-{n + 1}", "room_key": f"room-e2e-{n + 1}",
                                        "credential_public_id": f"cpub-e2e-{n + 1}", "secret": "sec-e2e",
                                        "claim_secret": "claim-e2e"})
            if p == "/cp/connector/token":
                return self._json(503, {"error": "e2e_unavailable"})
            return self._json(404, {"error": "not_found"})

        def log_message(self, *a):                      # 표준오류를 더럽히지 않는다(기록은 requests.log)
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)   # 루프백만 — 컨테이너 밖으로는 원래 나갈 길도 없다
    srv.daemon_threads = True
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fake_cp")
    ap.add_argument("--state", required=True)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args(argv)
    srv = serve(Path(a.state), Path(a.fixtures), a.port)
    print(f"fake_cp: http://127.0.0.1:{srv.server_address[1]}/cp (state {a.state})", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
