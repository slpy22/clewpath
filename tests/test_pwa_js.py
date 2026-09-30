"""PWA 인라인 스크립트 자동 테스트(node --test) 를 pytest 에 편입 — node 가 없으면 skip."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_pwa_node_tests_pass():
    # --test-reporter=tap 으로 고정: 기본 리포터는 TTY 여부에 따라 spec(ℹ fail 0)/tap(# fail 0) 이 갈린다
    files = sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "tests" / "pwa").glob("*.test.mjs"))
    r = subprocess.run([shutil.which("node"), "--test", "--test-reporter=tap", *files], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, "node --test 실패:\n" + r.stdout[-3000:] + "\n" + r.stderr[-2000:]
    assert "# fail 0" in r.stdout and "# pass 62" in r.stdout
