"""운영 릴레이가 서빙하는 PWA 가 이 작업 트리의 pwa/ 와 같은지 비교한다(읽기만, 공개 주소).

2026-10-27 노트북 이전(P4-2) 뒤 릴레이는 006 작업 트리를 바로 서빙하지 않고 이미지 안의 pwa/ 를 서빙한다.
그래서 PWA 를 고쳐도 이미지를 다시 만들어 배포하기 전까지 폰에는 옛 화면이 보인다. release.ps1 이 출시 끝에
이 비교로 "이미지 재빌드가 필요한가"를 판단한다. 운영 이미지 태그를 묻지 않고(노트북 docker 는 서비스 모니터 몫)
공개 주소의 내용을 직접 비교한다.

주소 대응은 007 relay/relay.py 의 _process_request 와 같다:
  index.html → <base>/app ({{MODE}}→relay, {{BASE}}→"" 치환 뒤 비교)
  app.webmanifest · native-bridge.js · sw.js → <base>/<이름>
  vendor/<이름> → <base>/vendor/<이름>

  python ops/tools/pwa_drift.py [--base https://clewpath.pyongso.com/relay] [--pwa pwa]
  → JSON 한 줄 {"checked": N, "differ": ["sw.js", ...], "error": null}. 비교 실패 시 error 에 이유.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://clewpath.pyongso.com/relay"
TOP_FILES = ("app.webmanifest", "native-bridge.js", "sw.js")


def expected_files(pwa_dir: Path) -> dict[str, tuple[str, bytes]]:
    """{상대 경로: (공개 경로, 기대 바이트)} — 릴레이가 서빙하는 파일만."""
    out: dict[str, tuple[str, bytes]] = {}
    index = pwa_dir / "index.html"
    if index.is_file():
        t = index.read_text(encoding="utf-8").replace("{{MODE}}", "relay").replace("{{BASE}}", "")
        out["index.html"] = ("/app", t.encode("utf-8"))
    for name in TOP_FILES:
        f = pwa_dir / name
        if f.is_file():
            out[name] = ("/" + name, f.read_bytes())
    vendor = pwa_dir / "vendor"
    if vendor.is_dir():
        for f in sorted(vendor.iterdir()):
            if f.is_file():
                out["vendor/" + f.name] = ("/vendor/" + f.name, f.read_bytes())
    return out


def _fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — 고정 공개 주소
        return r.read()


def drift(base: str, pwa_dir: Path, timeout: float = 15.0) -> dict:
    files = expected_files(pwa_dir)
    if not files:
        return {"checked": 0, "differ": [], "error": f"pwa 파일이 없습니다: {pwa_dir}"}
    base = base.rstrip("/")
    differ: list[str] = []
    for rel, (path, want) in files.items():
        try:
            got = _fetch(base + path, timeout)
        except Exception as e:  # noqa: BLE001 — 하나라도 못 받으면 판단하지 않는다
            return {"checked": 0, "differ": [], "error": f"{base + path}: {e}"}
        if got != want:
            differ.append(rel)
    return {"checked": len(files), "differ": differ, "error": None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="운영 릴레이 PWA 와 작업 트리 pwa/ 비교")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--pwa", default=str(Path(__file__).resolve().parents[2] / "pwa"))
    ap.add_argument("--timeout", type=float, default=15.0)
    a = ap.parse_args(argv)
    res = drift(a.base, Path(a.pwa), a.timeout)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["error"] is None else 2


if __name__ == "__main__":
    sys.exit(main())
