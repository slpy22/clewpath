"""CHANGELOG.md 계약 — 현재 버전 항목 존재, 내림차순·중복 없음, release.ps1 게이트 연동."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEAD_RE = re.compile(r"^## (\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)$", re.M)


def _versions():
    return [(tuple(int(x) for x in m.group(1).split(".")), m.group(1)) for m in HEAD_RE.finditer((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))]


def test_current_version_has_entry_with_body():
    pyver = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(pyver)} \([^)]*\)\n(.+?)(?=\n## |\Z)", text, re.M | re.S)
    assert m and m.group(1).strip(), f"CHANGELOG.md 에 ## {pyver} 항목(본문 포함)이 없습니다"


def test_versions_descending_and_unique():
    vs = _versions()
    assert len(vs) >= 40
    keys = [k for k, _ in vs]
    assert keys == sorted(keys, reverse=True), "버전이 내림차순이 아닙니다"
    assert len(set(keys)) == len(keys), "중복 버전"
    assert vs[-1][1] == "0.3.0"


def test_release_script_gates_on_changelog():
    s = (ROOT / "ops" / "release.ps1").read_text(encoding="utf-8")
    assert "PrepareChangelog" in s and "CHANGELOG.md 에 ## $Version 항목이 없습니다" in s
    assert s.index("CHANGELOG.md 에 ## $Version 항목이 없습니다") < s.index("pyproject.toml 버전")   # 게이트가 bump 확인보다 앞
