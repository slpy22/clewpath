"""PowerShell 5.1 호환 회귀 — 사용자 PC 의 기본 셸에서 깨지지 않는지.

사용자 쪽 .ps1(install·update_runner·ensure_task·런처 템플릿)은 pwsh(7)가 없는 PC 에서
powershell.exe(5.1)로 돈다. 5.1 에서 깨지는 지점은 세 종류이고, 각각을 못박는다:

  1) PS7 전용 문법 (?.  ??  && ||) — 5.1 에선 파스 에러라 스크립트가 아예 안 뜬다.
  2) BOM 없는 UTF-8 — 5.1 은 ANSI 코드페이지로 읽는다. 한국어 Windows(cp949)는 한글이 깨지는 데서 끝나지만
     영어 Windows(cp1252)는 「—」(E2 80 94)의 0x94 가 ” 로 읽혀 문자열이 끊기고 **파스 에러**가 난다
     (2026-10-22 노트북: ensure_task.ps1 이 0.10.5~0.31.1 내내 한 줄도 안 돌았다). 이 PC 는 cp949 라 재현이 안 돼서
     파일을 'BOM 이면 UTF-8, 아니면 cp1252' 로 읽은 결과를 진짜 5.1 파서에 넣어 흉내 낸다(as_ps51_reads).
  3) 반대 방향: 5.1 의 Set-Content -Encoding UTF8 은 BOM 을 붙이는데, 파이썬이
     순수 utf-8 로 읽으면 tomllib/json 이 거부한다 → 설정·자격증명이 조용히 무시됨.
"""
from __future__ import annotations

import base64
import codecs
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
USER_SIDE_PS1 = [
    ROOT / "install.ps1",
    ROOT / "session_manager" / "update_runner.ps1",
    ROOT / "session_manager" / "ensure_task.ps1",
    ROOT / "session_manager" / "start-connector.template.ps1",
]
BOM = b"\xef\xbb\xbf"
needs_ps51 = pytest.mark.skipif(not shutil.which("powershell"), reason="powershell.exe 없음(맥·리눅스)")

# .NET 의 Windows-1252 는 정의 안 된 다섯 바이트(81 8D 8F 90 9D)를 같은 번호의 C1 제어문자로 읽는다(파이썬 cp1252 는 오류)
codecs.register_error("dotnet1252", lambda e: (e.object[e.start:e.end].decode("latin-1"), e.end))


def as_ps51_reads(raw: bytes) -> str:
    """영어 Windows(ANSI cp1252)의 powershell.exe 5.1 이 이 바이트를 스크립트로 읽은 글자 — BOM 이면 UTF-8, 아니면 cp1252."""
    if raw.startswith(BOM):
        return raw[len(BOM):].decode("utf-8")
    return raw.decode("cp1252", errors="dotnet1252")


def write_as_ps51_reads(src: bytes, dst: Path) -> Path:
    """as_ps51_reads 결과를 BOM 붙은 UTF-8 로 — 어느 로캘의 5.1 이든 그 글자 그대로 읽는다."""
    dst.write_bytes(BOM + as_ps51_reads(src).encode("utf-8"))
    return dst


def ps_encoded(cmd: str) -> str:
    """-EncodedCommand 값(UTF-16LE base64) — 한글·공백 경로가 든 명령도 따옴표·코드페이지 걱정 없이 넘긴다."""
    return base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")


def ps51_parse_errors(*paths: Path) -> list[list[str]]:
    """진짜 powershell.exe(5.1) 파서에게 묻는다 — 파일마다 ['L<줄>: <메시지>', ...]. 결과는 UTF-8 파일로 받는다(콘솔 코드페이지 무관)."""
    with tempfile.TemporaryDirectory(prefix="ps51-") as td:
        out = Path(td) / "errs.txt"
        lst = ",".join("'" + str(p).replace("'", "''") + "'" for p in paths)
        cmd = ("$r = New-Object System.Collections.Generic.List[string]; $i = 0; "
               f"foreach ($p in @({lst})) {{ $e = $null; "
               "[void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); "
               "foreach ($x in @($e)) { if ($x) { $r.Add(('{0}|L{1}: {2}' -f $i, $x.Extent.StartLineNumber, "
               "($x.Message -replace '\\s+', ' '))) } }; $i++ }; "
               f"[IO.File]::WriteAllLines('{str(out).replace(chr(39), chr(39) * 2)}', $r, (New-Object System.Text.UTF8Encoding($false)))")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", ps_encoded(cmd)],
                           capture_output=True, timeout=120)
        assert r.returncode == 0 and out.is_file(), r.stderr.decode("utf-8", "replace")
        res: list[list[str]] = [[] for _ in paths]
        for line in out.read_text(encoding="utf-8").splitlines():
            i, _, msg = line.partition("|")
            res[int(i)].append(msg)
        return res

# 5.1 이 파스 에러를 내는 PS7 전용 토큰. 주석은 제외하고 코드 라인만 본다.
_FORBIDDEN = [
    (re.compile(r"\)\s*\?\."), "?. (null-조건 연산자)"),
    (re.compile(r"\?\?"), "?? (null-병합 연산자)"),
    (re.compile(r"&&"), "&& (파이프라인 체이닝)"),
    (re.compile(r"(?<!\|)\|\|(?!\|)"), "|| (파이프라인 체이닝)"),
]


def _code_lines(path: Path):
    """주석을 벗겨낸 코드 부분만 돌려준다. '#' 이후는 버린다 —
    금지 토큰이 # 뒤에 오는 경우는 코드가 아니므로 오탐만 줄어든다."""
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        yield i, line.split("#", 1)[0]


@pytest.mark.parametrize("ps1", USER_SIDE_PS1, ids=lambda p: p.name)
def test_no_ps7_only_syntax(ps1):
    hits = []
    for lineno, code in _code_lines(ps1):
        for rx, label in _FORBIDDEN:
            if rx.search(code):
                hits.append(f"{ps1.name}:{lineno} {label}: {code.strip()[:60]}")
    assert not hits, "PS 5.1 파스 에러 유발:\n  " + "\n  ".join(hits)


@pytest.mark.parametrize("ps1", USER_SIDE_PS1, ids=lambda p: p.name)
def test_utf8_bom_present(ps1):
    """BOM 이 없으면 5.1 이 이 파일을 ANSI(cp949·cp1252)로 읽어 한글이 깨지거나(cp949) 파스 에러가 난다(cp1252)."""
    assert ps1.read_bytes().startswith(BOM), f"{ps1.name} 에 UTF-8 BOM 없음"


def _user_side_ps1_on_disk() -> list[Path]:
    """사용자 PC 에서 도는 .ps1 전부 — 설치 스크립트 + 업데이트로 배포되는 session_manager/ 아래."""
    return sorted({ROOT / "install.ps1", *(ROOT / "session_manager").rglob("*.ps1")})


def test_user_side_ps1_list_is_complete():
    """session_manager/ 에 새 .ps1 을 두면 USER_SIDE_PS1 에도 넣어야 위 검사들을 받는다(ensure_task·템플릿이 빠져 있다가 노트북에서 터졌다)."""
    assert sorted(USER_SIDE_PS1) == _user_side_ps1_on_disk()


@pytest.mark.parametrize("ps1", _user_side_ps1_on_disk(), ids=lambda p: p.name)
def test_nonascii_code_line_needs_bom(ps1):
    """코드 줄(주석 밖)에 비ASCII 가 있는 사용자 쪽 .ps1 은 BOM 필수 — 없으면 영어 Windows 5.1 에서 「—」 하나로 파스 에러."""
    lines = [n for n, code in _code_lines(ps1) if not code.isascii()]
    assert not lines or ps1.read_bytes().startswith(BOM), f"{ps1.name}: 비ASCII 코드 줄 {lines[:8]} 인데 BOM 없음"


def test_ensure_task_code_lines_are_ascii():
    """ensure_task.ps1 은 Host 가 무인으로 부르는 자기 회복 경로 — BOM 이 어떤 이유로 사라져도(복사·편집기) 영어 Windows 5.1 에서
    돌아야 한다. 그래서 코드 줄(문자열 포함)은 ASCII 만. 한글은 주석에만."""
    ens = ROOT / "session_manager" / "ensure_task.ps1"
    bad = [f"L{n}: {code.strip()[:70]}" for n, code in _code_lines(ens) if not code.isascii()]
    assert not bad, "비ASCII 코드 줄:\n  " + "\n  ".join(bad)


def test_template_is_read_as_utf8():
    """런처 템플릿을 읽는 곳은 -Encoding UTF8 — 빠지면 5.1 이 ANSI 로 읽어 만든 런처가 깨진다(노트북: 34·49·50행 구문 오류 6개).
    install.ps1(신규 설치)·ensure_task.ps1(업데이트 뒤 자기 회복) 두 곳."""
    for ps1 in (ROOT / "install.ps1", ROOT / "session_manager" / "ensure_task.ps1"):
        reads = [code for _, code in _code_lines(ps1) if re.search(r"Get-Content\b.*\$(tplPath|tpl|Template)\b", code, re.I)]
        assert reads, f"{ps1.name}: 템플릿 읽는 줄을 못 찾음"
        assert all(re.search(r"-Encoding\s+UTF8\b", c, re.I) for c in reads), f"{ps1.name}: {reads}"


@needs_ps51
def test_parses_under_real_ps51(tmp_path):
    """정적 스캔이 놓치는 문법(삼항 등)은 진짜 5.1 파서에게 물어본다 — 이 PC 로캘 그대로 + 영어 Windows(cp1252) 흉내 둘 다.
    흉내를 빼면 이 PC(cp949)에선 BOM 없는 ensure_task·템플릿도 통과해 노트북 사고를 못 잡는다."""
    emu = [write_as_ps51_reads(p.read_bytes(), tmp_path / f"cp1252-{p.name}") for p in USER_SIDE_PS1]
    errs = ps51_parse_errors(*USER_SIDE_PS1, *emu)
    names = [p.name for p in USER_SIDE_PS1] + [f"{p.name}(영어 Windows 5.1 흉내)" for p in USER_SIDE_PS1]
    bad = {n: e for n, e in zip(names, errs) if e}
    assert not bad, "PS 5.1 파스 실패:\n" + "\n".join(f"  {n}: {e}" for n, e in bad.items())


@needs_ps51
def test_ensure_task_parses_even_if_bom_is_lost(tmp_path):
    """BOM 이 벗겨져도(복사·편집기) 영어 Windows 5.1 에서 파스된다 — 한글은 주석에만 있으니 cp1252 로 읽혀도 주석 안에서 끝난다."""
    ens = ROOT / "session_manager" / "ensure_task.ps1"
    emu = write_as_ps51_reads(ens.read_bytes().removeprefix(BOM), tmp_path / "ensure_task-nobom-cp1252.ps1")
    assert ps51_parse_errors(emu) == [[]]


@needs_ps51
def test_cp1252_emulation_catches_misread_template(tmp_path):
    """흉내가 헛돌지 않는지 — 템플릿을 cp1252 로 잘못 읽어 만든 런처(고치기 전 install.ps1:504·ensure_task 25행이 영어 Windows
    5.1 에서 만든 것)는 구문 오류가 나야 한다. 이게 통과하면 위 흉내 검사도 믿을 수 없다."""
    tpl = ROOT / "session_manager" / "start-connector.template.ps1"
    text = tpl.read_bytes().removeprefix(BOM).decode("cp1252", errors="dotnet1252")
    text = text.replace("{{DATA_DIR}}", r"C:\Users\u\.claude\session_manager").replace(
        "{{CONF_FILE}}", r"C:\Users\u\.claude\session_manager\clewpath.toml").replace("{{PORT}}", "5100")
    launcher = tmp_path / "start-connector.ps1"
    launcher.write_bytes(BOM + text.encode("utf-8"))            # 5.1 의 Set-Content -Encoding UTF8 = BOM + 깨진 글자
    errs = ps51_parse_errors(launcher)[0]
    assert errs, "cp1252 로 잘못 읽은 템플릿이 구문 검사를 통과 — 흉내가 헛돈다"


# ---- 방향 반대: 5.1 이 쓴 파일(BOM 포함)을 파이썬이 읽는 쪽 ----

def test_appconfig_reads_bom_toml(tmp_path, monkeypatch):
    """5.1 의 Set-Content -Encoding UTF8 이 만든 설정 파일(BOM)이 무시되면 안 된다."""
    from session_manager import appconfig
    p = tmp_path / "clewpath.toml"
    p.write_bytes(b"\xef\xbb\xbf" + b'[server]\nport = 6200\n')
    monkeypatch.setenv("CLEWPATH_CONFIG", str(p))
    monkeypatch.delenv("SM_PORT", raising=False)
    appconfig.load(refresh=True)
    try:
        assert appconfig.get_int("server", "port", 5100) == 6200
    finally:
        appconfig.load(refresh=True)


def test_cp_client_reads_bom_credentials(tmp_path, monkeypatch):
    """install.ps1(5.1) 이 발급받아 저장한 자격증명(BOM)이 무시되면
    커넥터가 조용히 레거시 모드로 떨어진다."""
    from session_manager import cp_client
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    f = tmp_path / "session_manager" / "cp_credentials.json"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\xef\xbb\xbf" + b'{"connector_id": "c_1", "room_key": "rm_x"}')
    creds = cp_client.load_creds()
    assert creds and creds["connector_id"] == "c_1"
