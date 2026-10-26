"""get.sh 정본과 CP 사본이 같은지 — docs/designs/posix-install.md 7절, docs/deploy/get-sh-publish.md.

정본은 이 저장소의 `ops/get.sh` 다(함수·실행·e2e 시험이 모두 이 파일을 본다). 컨트롤플레인(007)은 그 바이트 그대로의 사본
`control_plane/get.sh` 를 `/get.sh` 로 서빙한다. 한쪽만 고치면 사용자가 받는 스크립트가 시험한 것과 달라지므로, 007 트리가
보이면 두 파일이 바이트까지 같아야 한다.

007 트리: `SM_E2E_007_DIR` → 006 옆의 `007_clewpath_server`(control_plane/api.py 가 있는 곳). 없으면 건너뛴다.
007 에 아직 get.sh 가 없으면(공개 전 main) 건너뛴다. 맞추는 법: `cp ops/get.sh <007>/control_plane/get.sh`(007 은 *.sh 를
.gitattributes 로 LF 고정) 뒤 007 의 tests/test_get_sh_route.py.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GET_SH = ROOT / "ops" / "get.sh"


def _find_007() -> Path | None:
    cands = []
    if os.environ.get("SM_E2E_007_DIR"):
        cands.append(Path(os.environ["SM_E2E_007_DIR"]))
    cands.append(ROOT.parent / "007_clewpath_server")
    for c in cands:
        if (c / "control_plane" / "api.py").is_file():
            return c
    return None


def test_cp_copy_matches_ops_get_sh():
    d = _find_007()
    if d is None:
        pytest.skip("007 트리를 찾지 못함 — SM_E2E_007_DIR 로 지정")
    copy = d / "control_plane" / "get.sh"
    if not copy.is_file():
        pytest.skip(f"{d.name} 에 control_plane/get.sh 가 아직 없음(/get.sh 공개 전)")
    a, b = GET_SH.read_bytes(), copy.read_bytes()
    assert a == b, (f"ops/get.sh({hashlib.sha256(a).hexdigest()[:12]}) 와 {copy}({hashlib.sha256(b).hexdigest()[:12]})"
                    " 가 다릅니다 — 정본은 ops/get.sh, 007 사본을 맞추세요"
                    + (" (사본에 CR 이 있습니다 — 007 .gitattributes 의 *.sh eol=lf 확인)" if b"\r" in b else ""))


def test_ops_get_sh_is_lf_utf8_without_bom():
    """사본을 바이트로 대조하므로 정본 자체가 서빙할 모양이어야 한다(CP 는 손대지 않고 그대로 준다)."""
    raw = GET_SH.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n") and b"\r" not in raw and raw.endswith(b'\nmain "$@"\n')
    raw.decode("utf-8")
