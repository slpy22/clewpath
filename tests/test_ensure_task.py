"""Windows 자기 회복 경로(ensure_task.ps1 ← osdep._win.ensure_launcher ← liveness.ensure_launcher) — 2026-10-22 노트북 사고 회귀.

노트북(영어 Windows, ANSI cp1252, pwsh 없음 → powershell.exe 5.1)에서:
  - ensure_task.ps1 에 BOM 이 없어 5.1 이 cp1252 로 읽었고 「—」 때문에 파스 에러 → 한 줄도 안 돌았다.
  - Python 쪽은 stderr 를 버리고 빈 stdout 도 'launcher-ensured-<버전>' 표식에 써서, 0.10.5~0.31.1 16개 버전 내내 다시 안 했다.
  - 고쳐서 돌았더라도 BOM 없는 템플릿을 cp1252 로 읽어 만든 v2 런처가 구문 오류(34·49·50행)라 Host 가 안 떴을 것.
노트북은 정식 업데이트로만 고친다 — 업데이트 뒤 Host 기동 때의 이 경로만으로 스스로 v1 → v2 · 반복 트리거까지 가야 한다.

진짜 예약 작업·설치본·데이터 폴더는 건드리지 않는다: 가짜 설치 루트(공백·한글 경로)와 없는 작업 이름으로만 돌리고,
작업 동작·트리거 판단은 ensure_task.ps1 -LibOnly 로 함수만 불러 조회 결과를 흉내 낸 객체로 시험한다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_manager import config, liveness
from session_manager.osdep import _win
from tests.test_ps51_compat import BOM, as_ps51_reads, ps51_parse_errors, ps_encoded, write_as_ps51_reads

ROOT = Path(__file__).resolve().parent.parent
ENSURE = ROOT / "session_manager" / "ensure_task.ps1"
TEMPLATE = ROOT / "session_manager" / "start-connector.template.ps1"
on_windows_ps = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                                   reason="Windows powershell.exe 5.1 필요(맥·리눅스는 건너뜀)")
ENGINES = ["powershell"] + (["pwsh"] if shutil.which("pwsh") else [])
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 노트북의 옛 런처(v1, install.ps1 인라인판) — 첫 줄이 「# 자동 생성됨 (install.ps1)」
V1 = ("# 자동 생성됨 (install.ps1) — 재설치하면 덮어써집니다.\r\n"
      "$ErrorActionPreference = \"Stop\"\r\n"
      "Set-Location -Path $PSScriptRoot\r\n"
      "Start-Process -FilePath \"$PSScriptRoot\\.venv\\Scripts\\python.exe\" -ArgumentList \"-m\",\"session_manager.server\" -WindowStyle Hidden\r\n"
      ).encode("utf-8")


def _dec(b: bytes) -> str:
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        s = b.decode("oem" if sys.platform == "win32" else "latin-1", "replace")
    return s.replace("\r\n", "\n").strip()


def _q(p) -> str:
    return str(p).replace("'", "''")


def _fake_task() -> str:
    return f"ClewPathHost-m6-verify-{uuid.uuid4().hex[:10]}"      # 없는 작업 이름 — 진짜 ClewPathHost 를 조회·변경하지 않는다


class Box:
    """가짜 설치 루트(공백·한글) + 데이터 폴더 + 설정 파일 경로."""

    def __init__(self, tmp_path: Path, template: bytes | bool | None = None, launcher: bytes | None = V1):
        """template: None = 진짜 템플릿 복사, False = 템플릿 없음, bytes = 그 내용."""
        self.root = tmp_path / "앱 폴더" / "app"
        (self.root / "session_manager").mkdir(parents=True)
        self.data = tmp_path / "데이터 dir"
        self.data.mkdir()
        self.conf = self.data / "clewpath.toml"
        self.port = 5123
        if template is None:
            template = TEMPLATE.read_bytes()
        self.tpl = self.root / "session_manager" / "start-connector.template.ps1"
        if template is not False:
            self.tpl.write_bytes(template)
        self.launcher = self.root / "start-connector.ps1"
        if launcher is not None:
            self.launcher.write_bytes(launcher)

    def expected_launcher(self) -> bytes:
        text = TEMPLATE.read_bytes().decode("utf-8-sig").replace("{{DATA_DIR}}", str(self.data)).replace(
            "{{CONF_FILE}}", str(self.conf)).replace("{{PORT}}", str(self.port))
        return BOM + text.encode("utf-8")

    def run(self, engine: str = "powershell", script: Path = ENSURE, task: str | None = None):
        r = subprocess.run([engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                            "-Root", str(self.root), "-DataDir", str(self.data), "-ConfFile", str(self.conf),
                            "-Port", str(self.port), "-TaskName", task or _fake_task()],
                           capture_output=True, timeout=180, creationflags=_NO_WINDOW)
        r.out, r.err = _dec(r.stdout), _dec(r.stderr)
        return r


def _assert_good_launcher(path: Path):
    raw = path.read_bytes()
    assert raw.startswith(BOM), "런처가 BOM 으로 시작하지 않음"
    assert as_ps51_reads(raw).splitlines()[0].startswith("# clewpath-launcher v3")
    emu = write_as_ps51_reads(raw, path.with_name("cp1252-emu.ps1"))
    assert ps51_parse_errors(path, emu) == [[], []]                 # 이 PC 의 5.1 + 영어 Windows 5.1 흉내 모두 구문 0 오류


# ---------------------------------------------------------------- 끝까지 돌리기(가짜 폴더·없는 작업)

@on_windows_ps
@pytest.mark.parametrize("engine", ENGINES)
def test_v1_launcher_becomes_v3_with_bom(tmp_path, engine):
    """노트북 그대로: v1 런처 → 템플릿으로 v3 재생성(BOM·구문 0 오류·자리표시 채움·한글 그대로), 작업이 없으면 성공(exit 0)."""
    box = Box(tmp_path)
    r = box.run(engine)
    assert r.returncode == 0, (r.out, r.err)
    assert "launcher: regenerated (v3)" in r.out and "task: not registered - skip" in r.out, r.out
    assert box.launcher.read_bytes() == box.expected_launcher()
    _assert_good_launcher(box.launcher)


@on_windows_ps
def test_template_without_bom_is_still_read_as_utf8(tmp_path):
    """읽는 곳의 -Encoding UTF8 — BOM 없는 템플릿이라도 5.1 이 ANSI(이 PC cp949, 노트북 cp1252)로 읽지 않는다.
    (템플릿에 BOM 을 붙인 것과 별개의 두 번째 방어 — 이 PC 에서 고치기 전 코드면 한글이 cp949 로 깨진다.)"""
    box = Box(tmp_path, template=TEMPLATE.read_bytes().removeprefix(BOM))
    r = box.run("powershell")
    assert r.returncode == 0, (r.out, r.err)
    assert box.launcher.read_bytes() == box.expected_launcher()


@on_windows_ps
def test_template_missing_fails_without_touching_launcher(tmp_path):
    box = Box(tmp_path, template=False)
    r = box.run("powershell")
    assert r.returncode == 1, (r.out, r.err)
    assert "launcher: template missing" in r.out and "task: skip (launcher not idempotent)" in r.out
    assert box.launcher.read_bytes() == V1


@on_windows_ps
def test_broken_render_restores_original_launcher(tmp_path):
    """다시 쓴 런처가 구문 검사를 못 넘으면 원래 런처(바이트 그대로)로 되돌리고 트리거 단계로 가지 않는다(exit 1 → 표식 없음 → 다음 기동에 재시도)."""
    bad = BOM + "# clewpath-launcher v3 (깨진 템플릿)\r\n$x = \"unterminated\r\n".encode("utf-8")
    box = Box(tmp_path, template=bad)
    r = box.run("powershell")
    assert r.returncode == 1, (r.out, r.err)
    assert "launcher: regenerate failed" in r.out and "original restored" in r.out, r.out
    assert "task: skip (launcher not idempotent)" in r.out
    assert box.launcher.read_bytes() == V1


@on_windows_ps
def test_good_v3_launcher_is_left_alone(tmp_path):
    box = Box(tmp_path)
    box.launcher.write_bytes(box.expected_launcher())
    before = box.launcher.stat().st_mtime_ns
    r = box.run("powershell")
    assert r.returncode == 0, (r.out, r.err)
    assert "launcher: ok (v3)" in r.out
    assert box.launcher.stat().st_mtime_ns == before


@on_windows_ps
def test_good_v2_launcher_is_upgraded_to_v3(tmp_path):
    """0.32.2 까지의 멀쩡한 v2 런처(host.log 를 덮어써 사고 직전 로그가 사라짐)도 v3 으로 다시 만든다. 업데이트는 런처를 다시 쓰지
    않으므로(PAYLOAD 밖) 기존 설치본에 닿는 길은 Host 기동 때의 이 경로뿐 — 템플릿 첫 줄 표식을 올려야 'ok' 로 넘기지 않는다."""
    box = Box(tmp_path)
    box.launcher.write_bytes(box.expected_launcher().replace(b"clewpath-launcher v3", b"clewpath-launcher v2", 1))
    r = box.run("powershell")
    assert r.returncode == 0, (r.out, r.err)
    assert "launcher: regenerated (v3) - was: not v3" in r.out, r.out
    assert box.launcher.read_bytes() == box.expected_launcher()
    _assert_good_launcher(box.launcher)


# ---------------------------------------------------------------- 런처를 실제로 돌리기(host.log 한 세대 보존)
# 가짜 설치 루트에 venv(--without-pip)와 가짜 session_manager/server.py(첫 줄을 찍고 끝남)를 두고 런처를 그대로 실행한다.
# 포트는 비어 있는 것, runtime.json 은 없음 → 런처는 '죽어 있음' 으로 보고 띄운다. 진짜 Host·작업·데이터 폴더는 안 건드린다.

_FAKE_SERVER = 'import sys\nprint("new host first line", flush=True)\nsys.stderr.write("new host err\\n")\nsys.stderr.flush()\n'


@pytest.fixture(scope="module")
def venv_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("launcher-venv") / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(d)], check=True, timeout=180, creationflags=_NO_WINDOW)
    return d


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch_box(tmp_path: Path, venv: Path) -> Box:
    box = Box(tmp_path, launcher=None)
    box.port = _free_port()
    shutil.copytree(venv, box.root / ".venv")
    (box.root / "session_manager" / "__init__.py").write_text("", encoding="utf-8")
    (box.root / "session_manager" / "server.py").write_text(_FAKE_SERVER, encoding="utf-8")
    (box.data / "host.log").write_bytes("old 1\r\nold 2: Traceback 마지막\r\n".encode("utf-8"))
    (box.data / "host.err.log").write_bytes(b"old err\r\n")
    return box


def _run_launcher(box: Box, engine: str = "powershell") -> str:
    """런처를 작업 스케줄러처럼 -File 로 돌리고, 새 Host(가짜)의 첫 줄이 host.log 에 찍힐 때까지 기다려 그 내용을 돌려준다."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SM_", "PYTHON", "VIRTUAL_ENV"))}
    r = subprocess.run([engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(box.launcher)],
                       capture_output=True, timeout=120, creationflags=_NO_WINDOW, env=env)
    assert r.returncode == 0, (_dec(r.stdout), _dec(r.stderr))
    log = box.data / "host.log"
    for _ in range(300):
        if log.is_file() and b"new host first line" in log.read_bytes():
            return log.read_bytes().decode("utf-8")
        time.sleep(0.1)
    raise AssertionError(f"가짜 Host 가 안 떴다: {log.read_bytes() if log.is_file() else '(host.log 없음)'} / "
                         f"{(box.data / 'launcher.log').read_text(encoding='utf-8-sig') if (box.data / 'launcher.log').is_file() else ''}")


def _assert_pushed_aside(box: Box, monkeypatch, new_log: str):
    """옛 로그는 .1 로(host.log 는 끝에 런처 구분줄), 새 로그는 새 Host 것만, 새 Host 의 사고 꼬리는 죽은 Host 의 마지막 줄."""
    dot1 = (box.data / "host.log.1").read_bytes().decode("utf-8")
    assert re.fullmatch(r"old 1\r\nold 2: Traceback 마지막\r\n\r\n=== launcher start \d{4}-\d\d-\d\d \d\d:\d\d:\d\d ===\r\n", dot1), dot1
    assert (box.data / "host.err.log.1").read_bytes() == b"old err\r\n"
    assert "old" not in new_log and "launcher start" not in new_log, new_log
    monkeypatch.setattr(config, "data_dir", lambda: box.data)
    assert liveness.log_tail(10) == ["old 1", "old 2: Traceback 마지막"]


@on_windows_ps
@pytest.mark.parametrize("engine", ENGINES)
def test_launcher_keeps_the_previous_host_log(tmp_path, venv_dir, monkeypatch, engine):
    """③ Start-Process 의 리다이렉트는 파일을 새로 쓴다 — 고치기 전엔 죽은 Host 를 다시 띄우면 직전 로그가 지워져(5MB 미만이면
    회전도 안 했다) 자기 회복의 사고 기록이 새 Host 의 첫 줄이었다. 이제 띄우기 전에 한 세대 밀어 둔다."""
    box = _launch_box(tmp_path, venv_dir)
    box.launcher.write_bytes(box.expected_launcher())
    _assert_pushed_aside(box, monkeypatch, _run_launcher(box, engine))
    assert "start:" in (box.data / "launcher.log").read_text(encoding="utf-8-sig")


def test_install_ps1_treats_v2_and_later_as_idempotent():
    """install.ps1 은 멱등 런처(v2 이상)일 때만 5분 반복 트리거를 단다 — 템플릿 표식을 v3 으로 올려도 새 설치에서 반복 트리거가
    빠지지 않게(빠지면 Host 가 죽었을 때 다음 로그온까지 안 뜬다). 템플릿 없는 옛 zip 의 v1 인라인 런처는 멱등이 아니다."""
    src = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r'^\$launcherV2 = \(\$cfg -match "([^"]+)"\)', src, re.M)
    assert m, "install.ps1 의 멱등 판정 줄을 못 찾음"
    rx = re.compile(m.group(1), re.I)                                  # PowerShell -match 는 대소문자 무시
    assert rx.search(TEMPLATE.read_text(encoding="utf-8-sig").splitlines()[0])
    assert rx.search("# clewpath-launcher v2 — 자동 생성됨")
    assert not rx.search("# 자동 생성됨 (install.ps1) — 재설치하면 덮어써집니다.")


@on_windows_ps
def test_install_ps1_v1_fallback_launcher_keeps_the_previous_host_log(tmp_path, venv_dir, monkeypatch):
    """템플릿 없는 옛 zip 용 인라인 런처(install.ps1 의 v1 폴백)도 같은 원칙 — install.ps1 의 here-string 을 그대로 펼쳐 돌린다."""
    src = (ROOT / "install.ps1").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    i = src.index('if (-not $cfg) { $cfg = @"\n') + len('if (-not $cfg) { $cfg = @"\n')
    body = src[i:src.index('\n"@ }', i)]
    box = _launch_box(tmp_path, venv_dir)
    cmd = (f"$confFile = '{_q(box.conf)}'; $dataDir = '{_q(box.data)}'\n$cfg = @\"\n{body}\n\"@\n"
           f"[IO.File]::WriteAllText('{_q(box.launcher)}', $cfg, (New-Object System.Text.UTF8Encoding($true)))")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", ps_encoded(cmd)],
                       capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    assert r.returncode == 0 and box.launcher.is_file(), (_dec(r.stdout), _dec(r.stderr))
    assert box.launcher.read_text(encoding="utf-8-sig").startswith("# 자동 생성됨 (install.ps1)")
    assert ps51_parse_errors(box.launcher) == [[]]
    _assert_pushed_aside(box, monkeypatch, _run_launcher(box))


@on_windows_ps
def test_broken_v2_launcher_is_regenerated(tmp_path):
    """v2 표식은 있는데 구문이 깨진 런처(고치기 전 install.ps1 이 영어 Windows 5.1 에서 만든 것 — 템플릿을 cp1252 로 읽고 BOM 붙여 씀)도
    'ok' 로 넘기지 않고 다시 만든다. 표식만 보면 이런 PC 는 영영 안 고쳐진다."""
    box = Box(tmp_path)
    misread = TEMPLATE.read_bytes().removeprefix(BOM).decode("cp1252", errors="dotnet1252").replace(
        "{{DATA_DIR}}", str(box.data)).replace("{{CONF_FILE}}", str(box.conf)).replace("{{PORT}}", "5100")
    box.launcher.write_bytes(BOM + misread.encode("utf-8"))
    r = box.run("powershell")
    assert r.returncode == 0, (r.out, r.err)
    assert "launcher: regenerated (v3)" in r.out and "syntax" in r.out, r.out
    _assert_good_launcher(box.launcher)


# ---------------------------------------------------------------- Host 경로 전체(liveness → _win → ensure_task.ps1)

def _host_path(monkeypatch, box: Box, engine: str = "powershell"):
    """liveness.ensure_launcher 를 가짜 설치 루트로, 셸은 engine 으로 고정, 작업 이름은 없는 이름으로 바꿔 끼운다."""
    monkeypatch.setattr(liveness, "_install_root", lambda: box.root)
    monkeypatch.setattr(liveness, "_version", lambda: "9.9.9")
    fake = _fake_task()
    # _win 안의 이름만 바꾼다(전역 shutil·subprocess 는 그대로 — 같은 시험의 5.1 구문 검사가 써야 한다)
    monkeypatch.setattr(_win, "shutil", SimpleNamespace(which=lambda n: shutil.which(engine) if n == engine else None))
    monkeypatch.setattr(_win, "subprocess", SimpleNamespace(
        run=lambda args, **kw: subprocess.run(list(args) + ["-TaskName", fake], **kw)))
    config.data_dir().mkdir(parents=True, exist_ok=True)
    return config.data_dir() / "launcher-ensured-9.9.9"


@on_windows_ps
def test_laptop_cp1252_scenario_heals_through_host_path(fake_claude_home, tmp_path, monkeypatch):
    """노트북 재현: 설치본의 ensure_task.ps1 을 '영어 Windows 5.1 이 읽는 글자' 로 바꿔 놓고 Host 기동 경로를 그대로 탄다.
    고치기 전: 파스 에러 → 빈 출력이 표식에 써지고 런처는 v1 그대로. 고친 뒤: v3 재생성 + 표식에 결과."""
    box = Box(tmp_path)
    write_as_ps51_reads(ENSURE.read_bytes(), box.root / "session_manager" / "ensure_task.ps1")
    marker = _host_path(monkeypatch, box, "powershell")
    out = liveness.ensure_launcher(box.port)
    assert out and "launcher: regenerated (v3)" in out, out
    assert marker.read_text(encoding="utf-8") == out
    _assert_good_launcher(box.launcher)


@on_windows_ps
def test_script_failure_writes_no_marker_and_keeps_stderr(fake_claude_home, tmp_path, monkeypatch):
    """파스 에러(= exit 1·빈 stdout)면 표식을 쓰지 않는다 → 다음 기동에 다시. 이유(stderr)는 host.log(print)로."""
    box = Box(tmp_path)
    (box.root / "session_manager" / "ensure_task.ps1").write_bytes(BOM + b'"launcher: x\r\n$y = "unterminated\r\n')
    marker = _host_path(monkeypatch, box, "powershell")
    out = liveness.ensure_launcher(box.port)
    assert out.startswith("ensure failed") and "exit 1" in out and "ensure_task.ps1:2" in out, out   # stderr(At …ps1:2 char:…) 동봉
    assert not marker.exists()
    assert box.launcher.read_bytes() == V1


# ---------------------------------------------------------------- 작업 동작·트리거 판단(함수만, 조회 결과 흉내)

def _lib(engine: str, body: str, tmp_path: Path, dot: bool = True):
    """ensure_task.ps1 -LibOnly 로 함수만 불러(dot) body 를 돌리고 결과를 JSON 으로 받는다(실제 작업·파일은 안 건드림)."""
    out = tmp_path / f"lib-{uuid.uuid4().hex[:6]}.json"
    cmd = ((f". '{_q(ENSURE)}' -LibOnly\n" if dot else "") + f"$__r = & {{\n{body}\n}}\n"
           f"[IO.File]::WriteAllText('{_q(out)}', (ConvertTo-Json -InputObject @($__r) -Depth 6 -Compress), "
           "(New-Object System.Text.UTF8Encoding($false)))")
    r = subprocess.run([engine, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", ps_encoded(cmd)],
                       capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    assert r.returncode == 0 and out.is_file(), (_dec(r.stdout), _dec(r.stderr))
    return json.loads(out.read_text(encoding="utf-8-sig"))


_ACT = r"""
$sp = 'C:\Users\홍 길동\AppData\Local\ClewPath\app\start-connector.ps1'
$ch = Join-Path $env:SystemRoot 'System32\conhost.exe'
$ps5 = 'C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe'
$ps7 = 'C:\Program Files\PowerShell\7\pwsh.exe'
$std = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $sp + '"'
function Act($exe, $arg, $wd) { [pscustomobject]@{ Execute = $exe; Arguments = $arg; WorkingDirectory = $wd } }
function Hdl($acts, $build, $con) { Get-HeadlessAction -Actions $acts -StartPs $sp -Build $build -Conhost $con }
"""


@on_windows_ps
@pytest.mark.parametrize("engine", ENGINES)
def test_headless_action_only_for_our_default_shape(tmp_path, engine):
    res = _lib(engine, _ACT + r"""
Hdl @(Act $ps5 $std $null) 19045 $ch                                   # 0 기본(5.1)
Hdl @(Act $ps7 $std 'C:\wd') 26100 $ch                                 # 1 기본(7) + 작업 폴더 유지
Hdl @(Act ('"' + $ps7 + '"') $std $null) 17763 $ch                     # 2 따옴표 감싼 Execute, 경계 빌드
Hdl @(Act $ps5 $std.ToUpper() $null) 19045 $ch                         # 3 경로 대소문자만 다름
Hdl @(Act $ch ('--headless "' + $ps7 + '" ' + $std) $null) 26100 $ch   # 4 이미 conhost(사람이 바꾼 것 포함)
Hdl @(Act 'C:\WINDOWS\System32\cmd.exe' $std $null) 19045 $ch          # 5 다른 실행 파일
Hdl @(Act $ps5 '-NoProfile -File "D:\other\start-connector.ps1"' $null) 19045 $ch   # 6 다른 런처
Hdl @((Act $ps5 $std $null), (Act $ps5 $std $null)) 19045 $ch            # 7 동작 2개
Hdl @(Act $ps5 $std $null) 17134 $ch                                   # 8 1803 — --headless 없음
Hdl @(Act $ps5 $std $null) 19045 'C:\no\such\conhost.exe'              # 9 conhost 없음
Hdl @(Act $ps5 ($std + ' -X') $null) 19045 $ch                         # 10 -File 뒤에 뭔가 더 붙음(우리 모양 아님)
""", tmp_path)
    assert [x["Change"] for x in res] == [True, True, True, True] + [False] * 7, res
    ch = os.path.join(os.environ["SystemRoot"], "System32", "conhost.exe")
    std = r'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\홍 길동\AppData\Local\ClewPath\app\start-connector.ps1"'
    assert res[0]["Execute"].lower() == ch.lower()
    assert res[0]["Argument"] == r'--headless "C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" ' + std
    assert res[1]["Argument"] == r'--headless "C:\Program Files\PowerShell\7\pwsh.exe" ' + std and res[1]["WorkingDirectory"] == r"C:\wd"
    assert res[2]["Argument"] == r'--headless "C:\Program Files\PowerShell\7\pwsh.exe" ' + std
    for i, word in [(4, "conhost"), (5, "default"), (6, "default"), (7, "actions"), (8, "17763"), (9, "conhost"), (10, "default")]:
        assert word in res[i]["Reason"], (i, res[i])


@on_windows_ps
def test_task_plan_converts_default_action_with_or_without_repetition(tmp_path):
    """우리 기본 모양 동작은 반복 트리거가 있든 없든 conhost 로(2026-10-23 사장님 결정: 이미 5분 확인이 도는 PC 도 깜박임을 없앤다).
    사람이 바꾼 모양(이 PC 처럼 이미 conhost, 다른 실행 파일)은 반복 트리거가 있어도 그대로."""
    res = _lib("powershell", _ACT + r"""
function NewTrig($rep) { if ($rep) { [pscustomobject]@{ Repetition = [pscustomobject]@{ Interval = $rep } } } else { [pscustomobject]@{ Repetition = [pscustomobject]@{ Interval = $null } } } }
function NewTask($trs, $acts) { [pscustomobject]@{ Triggers = $trs; Actions = $acts } }
$both = @((NewTrig $null), (NewTrig 'PT5M'))
Get-TaskPlan -Task (NewTask @(NewTrig $null) @(Act $ps5 $std $null)) -StartPs $sp -Build 19045 -Conhost $ch                 # 0 노트북: 로그온 트리거뿐
Get-TaskPlan -Task (NewTask $both @(Act $ps5 $std $null)) -StartPs $sp -Build 19045 -Conhost $ch                            # 1 반복 있음 + 기본 모양
Get-TaskPlan -Task (NewTask @(NewTrig $null) @(Act $ps5 $std $null)) -StartPs $sp -Build 17134 -Conhost $ch                 # 2 옛 빌드
Get-TaskPlan -Task (NewTask @() @(Act $ps5 $std $null)) -StartPs $sp -Build 19045 -Conhost $ch                        # 3 트리거 없음
Get-TaskPlan -Task (NewTask $both @(Act $ch ('--headless "' + $ps7 + '" ' + $std) $null)) -StartPs $sp -Build 26100 -Conhost $ch   # 4 반복 있음 + 이미 conhost(이 PC)
Get-TaskPlan -Task (NewTask $both @(Act 'C:\WINDOWS\System32\cmd.exe' $std $null)) -StartPs $sp -Build 19045 -Conhost $ch          # 5 반복 있음 + 사람이 바꾼 모양
Get-TaskPlan -Task (NewTask $both @(Act $ps5 $std $null)) -StartPs $sp -Build 17134 -Conhost $ch                            # 6 반복 있음 + 옛 빌드
""", tmp_path)
    assert [(x["NeedRepetition"], x["Action"]["Change"]) for x in res] == [
        (True, True), (False, True), (True, False), (True, True), (False, False), (False, False), (False, False)], res


@on_windows_ps
def test_headless_probe_really_launches(tmp_path):
    """conhost --headless 로 그 셸을 실제로 한 번 띄워 본다 — 안 되면(옛 빌드·막힌 PC) 동작을 바꾸지 않는다."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
        if int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0]) < 17763:
            pytest.skip("Windows 10 1809 미만")
    res = _lib("powershell", r"""
$ch = Join-Path $env:SystemRoot 'System32\conhost.exe'
$ps5 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
Test-HeadlessWorks -Conhost $ch -Exe $ps5
Test-HeadlessWorks -Conhost 'C:\no\such\conhost.exe' -Exe $ps5
Test-HeadlessWorks -Conhost $ch -Exe 'C:\no\such\pwsh.exe'
""", tmp_path)
    assert res == [True, False, False]


@on_windows_ps
def test_install_ps1_headless_launch_uses_same_rules_without_clobbering(tmp_path):
    """install.ps1 의 신규 등록도 같은 판단(Get-HeadlessLaunch → ensure_task.ps1 -LibOnly 함수). 함수 안에서 점 소싱하므로
    ensure_task.ps1 의 param($Root·$Port …)이 install.ps1 의 변수를 덮지 않아야 한다 — 덮으면 그 뒤 설치가 엉뚱한 폴더로 간다."""
    src = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    i = src.index("function Get-HeadlessLaunch(")
    fn = src[i:src.index("\n}\n", i) + 3] if "\n}\n" in src[i:] else src[i:src.index("\r\n}\r\n", i) + 5]
    res = _lib("powershell", f"""
$ErrorActionPreference = 'Stop'                                   # install.ps1 과 같은 조건
$Root = '{_q(ROOT)}'; $Port = 7777; $DataDir = 'KEEP'
$startPs = 'C:\\Users\\홍 길동\\AppData\\Local\\ClewPath\\app\\start-connector.ps1'
$ps5 = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'
{fn}
$h = Get-HeadlessLaunch $ps5 ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $startPs + '"')
$o = Get-HeadlessLaunch $ps5 '-NoProfile -File "D:\\other.ps1"'
@{{ H = $h; Other = $o; Root = $Root; Port = $Port; DataDir = $DataDir; Leaked = [bool](Get-Command Get-HeadlessAction -ErrorAction SilentlyContinue) }}
""", tmp_path, dot=False)[0]
    assert (res["Root"], res["Port"], res["DataDir"], res["Leaked"]) == (str(ROOT), 7777, "KEEP", False), res
    assert res["Other"] is None
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
        new_enough = int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0]) >= 17763
    if new_enough:
        assert res["H"]["Execute"].lower().endswith(r"\system32\conhost.exe")
        assert res["H"]["Argument"].startswith('--headless "') and res["H"]["Argument"].endswith(r'start-connector.ps1"')
    else:
        assert res["H"] is None


# ---------------------------------------------------------------- Python 쪽 판정(가짜 subprocess — 모든 OS)

def _fake_run(monkeypatch, rc: int, out: bytes, err: bytes = b""):
    seen = {}

    def run(args, **kw):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=err)
    monkeypatch.setattr(_win, "shutil", SimpleNamespace(which=lambda n: r"C:\ps\powershell.exe" if n == "powershell" else None))
    monkeypatch.setattr(_win, "subprocess", SimpleNamespace(run=run))
    return seen


def test_win_ensure_success_returns_output_with_stderr(monkeypatch, tmp_path):
    seen = _fake_run(monkeypatch, 0, b"launcher: regenerated (v2) - was: not v2\r\ntask: not registered - skip\r\n", b"warn x\r\n")
    out = _win.ensure_launcher(tmp_path, tmp_path / "d", tmp_path / "c.toml", 5100)
    assert out == "launcher: regenerated (v2) - was: not v2\ntask: not registered - skip\nstderr: warn x"
    assert seen["args"][0] == r"C:\ps\powershell.exe" and "-File" in seen["args"] and "Bypass" in seen["args"]


@pytest.mark.parametrize("rc,out,err", [
    (1, b"", b"At ensure_task.ps1:30 char:47\r\nThe string is missing the terminator: \".\r\n"),   # 노트북: 파스 에러
    (0, b"", b""),                                                                              # 빈 출력도 실패
    (1, b"launcher: ok (v2)\r\ntask: update failed: Access is denied.\r\n", b""),
])
def test_win_ensure_failure_raises(monkeypatch, tmp_path, rc, out, err):
    _fake_run(monkeypatch, rc, out, err)
    with pytest.raises(RuntimeError) as ei:
        _win.ensure_launcher(tmp_path, tmp_path / "d", tmp_path / "c.toml", 5100)
    msg = str(ei.value)
    assert f"exit {rc}" in msg
    for part in (out, err):
        if part.strip():
            assert part.decode().strip().splitlines()[-1] in msg


def test_win_ensure_without_powershell_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(_win, "shutil", SimpleNamespace(which=lambda n: None))
    assert _win.ensure_launcher(tmp_path, tmp_path, tmp_path / "c.toml", 5100) is None


def test_ps_text_falls_back_to_oem_codepage():
    """우리 줄은 ASCII 지만 파스 에러·예외 문구는 OS 언어 + OEM 코드페이지(한국어 cp949, 영어 cp437)로 올 수 있다."""
    assert _win._ps_text("한글 오류\r\nb\r\n".encode("utf-8")) == "한글 오류\nb"
    if sys.platform == "win32":
        import ctypes
        oem = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        for s in ("파스 오류", "é"):                       # cp949 면 앞, cp437·cp850 이면 뒤 — 둘 다 UTF-8 로는 못 읽는 바이트
            try:
                raw = s.encode(oem)
            except UnicodeEncodeError:
                continue
            assert _win._ps_text(raw) == s
            break


# ---------------------------------------------------------------- 5분 반복 트리거(2026-10-23 노트북 0.32.0 회귀)
# 노트북(Windows 11 26300, powershell.exe 5.1)에서 옛 5.1 분기의 -RepetitionDuration ([TimeSpan]::MaxValue) 가 작업 XML 에
# P99999999DT23H59M59S 로 들어가 작업 스케줄러가 "incorrectly formatted or out of range" 로 거부했다. 트리거와 같은
# Set-ScheduledTask 에 묶인 conhost 동작 변경까지 실패. 실제 등록은 이 시험에서 하지 않는다(예약 작업 변경은 서비스 모니터 몫) —
# 트리거 객체가 '5분 간격·지속 시간 없음(무기한)' 으로 만들어지는지 두 엔진에서 본다.

def test_no_maxvalue_duration_or_version_branch_for_triggers():
    for f in (ENSURE, ROOT / "install.ps1"):
        text = "\n".join(ln for ln in f.read_text(encoding="utf-8-sig").splitlines() if not ln.lstrip().startswith("#"))
        assert "[TimeSpan]::MaxValue" not in text, f                  # 코드 줄만(주석의 사고 기록은 그대로)
        assert "P99999999" not in text, f
        # 반복 트리거를 만드는 줄 근처에 PowerShell 버전 분기가 다시 생기지 않게(작업 스케줄러 cmdlet 은 OS 모듈이라 버전과 무관)
        for i, line in enumerate(text.splitlines()):
            if "RepetitionInterval" in line:
                near = "\n".join(text.splitlines()[max(0, i - 3):i + 1])
                assert "PSVersion" not in near, (f, i, near)


@on_windows_ps
@pytest.mark.parametrize("engine", ENGINES)
def test_repeat_trigger_is_indefinite_5min_on_each_engine(tmp_path, engine):
    res = _lib(engine, r"""
$a = New-RepeatTrigger
$b = New-RepeatTrigger -Bounded
[pscustomobject]@{ I = "$($a.Repetition.Interval)"; D = "$($a.Repetition.Duration)"; BI = "$($b.Repetition.Interval)"; BD = "$($b.Repetition.Duration)" }
""", tmp_path)
    r = res[0] if isinstance(res, list) else res
    assert r["I"] == "PT5M" and r["D"] in ("", "None"), r          # 무기한 = 지속 시간 칸이 비어 있음
    assert r["BI"] == "PT5M" and r["BD"] == "P3650D", r


def test_action_and_trigger_are_updated_separately():
    """동작 변경과 트리거 변경은 따로(한쪽 거부가 다른 쪽을 막지 않게), 무기한이 거부되면 10년으로 한 번 더."""
    text = ENSURE.read_text(encoding="utf-8-sig")
    tail = text[text.index("$actionDone = $false"):]
    assert "Set-ScheduledTask -TaskName $TaskName -Action $newAction -ErrorAction Stop" in tail
    assert "-Trigger @($logon, (New-RepeatTrigger))" in tail and "-Trigger @($logon, (New-RepeatTrigger -Bounded))" in tail
    assert "-Action $newAction -Trigger" not in tail                # 한 번의 Set 에 묶지 않는다
    inst = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "-RepetitionDuration (New-TimeSpan -Days 3650)" in inst   # install.ps1 도 거부되면 10년으로 재시도
