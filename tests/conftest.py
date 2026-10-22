"""테스트 픽스처 — 실제 ~/.claude 대신 임시 디렉토리를 사용한다."""
import json
import os
from pathlib import Path

import tempfile

import pytest

# 테스트 프로세스 전체를 진짜 ~/.claude·터미널 관리 프로세스(브로커)와 격리한다. 앱 기동 스레드(_sweep_all)는
# 테스트가 끝난 뒤(monkeypatch 복원 후)에도 돌아, 진짜 브로커에 붙어 실행 중인 Host 의 터미널 스트림을
# 빼앗았다(2026-10-11: Host 가 터미널 11개를 놓침). 브로커 파이프 주소·키는 data_dir(= claude_home 아래)로 정해진다.
# 단, 테스트 안에서 `from tests.conftest import write_session` 을 하면 이 모듈이 'tests.conftest' 로 한 번 더
# 실행된다 — 그때 다시 덮어쓰면 fake_claude_home 이 넣은 값이 사라진다(단독 실행에서만 드러나던 실패). 한 번만.
if not os.environ.get("_SM_TEST_HOME_SET"):
    os.environ["SESSION_MANAGER_CLAUDE_HOME"] = tempfile.mkdtemp(prefix="sm-test-home-")
    os.environ["_SM_TEST_HOME_SET"] = "1"
os.environ["SM_PTY_BROKER"] = "off"
# 진짜 CP·릴레이와도 격리한다(2026-10-15 사고): ClewPath 가 띄운 세션은 Host 의 SM_CP_URL 을 물려받아, 테스트 중
# 뒤에서 도는 연결 스레드가 진짜 CP 에 익명 등록(/provision/anonymous)을 한 번씩 보내 「I9」 등록이 46개 쌓였다.
# CP 가 필요한 테스트는 monkeypatch 로 가짜 주소를 넣는다.
for _k in ("SM_CP_URL", "SM_RELAY_URL", "SM_RELAY_AGENT_TOKEN", "SM_RELAY_CLIENT_TOKEN", "SM_RELAY_ROOM"):
    os.environ.pop(_k, None)

# 진짜 claude 도 띄우지 않는다(2026-10-22 맥 실기·이 PC): 앱 기동이 남긴 정기 작업 줄이 +5분 뒤 시험 프로세스 안에서 '모델 목록
# 갱신'을 돌려, PATH 의 진짜 claude 가 진짜 ~/.claude 에 '/model' 세션을 남겼다(전체 시험이 5분을 넘긴 맥·Windows). 제품은
# 격리 모드면 건너뛰고(housekeeping._probe_blocked), 여기서는 spawn 주입이 없는 화면 읽기 자체를 막는다.
from session_manager import modelprobe as _modelprobe  # noqa: E402

if not getattr(_modelprobe.probe, "_test_guard", False):
    _real_probe = _modelprobe.probe

    def _probe_without_real_claude(exe, cwd, timeout=30.0, spawn=None):
        if spawn is None:
            raise RuntimeError("시험 중에는 진짜 claude 를 띄우지 않습니다 — modelprobe.probe 에 spawn 을 주입하세요")
        return _real_probe(exe, cwd, timeout=timeout, spawn=spawn)

    _probe_without_real_claude._test_guard = True
    _modelprobe.probe = _probe_without_real_claude

# 진짜 데이터 폴더의 핵심 파일을 시험이 지우지 않는지 시험마다 본다(2026-10-22 맥 실기 3b-2 이슈 B: 전체 시험 전후로 진짜
# ~/.claude/session_manager/clewpath.toml 이 사라져 있었다 — 원인 미확정). 있던 것이 사라지면 그 직후 시험을 실패시켜 범인을 가리킨다.
# 자리는 import 때(어느 시험도 HOME 을 바꾸기 전) 정한다. 내용은 보지 않는다 — Host 는 원자적으로 다시 써서 교체 중에도 파일이 있다.
_REAL_DATA = Path(os.path.expanduser("~")) / ".claude" / "session_manager"
_REAL_KEEP = [p for p in (_REAL_DATA / n for n in ("clewpath.toml", "cp_credentials.json", "devices.json")) if p.is_file()]


@pytest.fixture(autouse=True)
def _real_data_dir_untouched():
    yield
    gone = [p.name for p in _REAL_KEEP if not p.is_file()]
    if gone:
        _REAL_KEEP[:] = [p for p in _REAL_KEEP if p.is_file()]     # 한 번만 울린다 — 뒤 시험이 줄줄이 실패하지 않게
        pytest.fail(f"시험이 진짜 데이터 폴더의 파일을 지웠습니다: {', '.join(gone)} ({_REAL_DATA}) — 이 시험(또는 그 뒤에 남은 스레드)",
                    pytrace=False)


@pytest.fixture(autouse=True)
def _pty_broker_off(monkeypatch):
    """기존 테스트는 Host 내장 PTY 경로를 검증한다 — 진짜 브로커를 띄우지 않게 기본 off.
    브로커 테스트(test_ptyd.py)는 스스로 on 으로 바꾼다."""
    monkeypatch.setenv("SM_PTY_BROKER", "off")


@pytest.fixture(autouse=True)
def _fresh_peer_cache():
    """피어 레지스트리 스냅샷은 몇 초 캐시된다 — 앞 테스트의 (가짜 홈) 결과가 다음 테스트로 새지 않게
    (전체 실행에서만 가끔 실패하던 test_peer_map_carries_origin)."""
    from session_manager import peers
    peers._cache["at"] = 0
    yield
    peers._cache["at"] = 0


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """가짜 ~/.claude 구조를 만들고 환경변수로 주입한다."""
    home = tmp_path / ".claude"
    projects = home / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(home))
    return home


def write_session(home: Path, project_folder: str, session_id: str, lines: list[dict]):
    """가짜 세션 jsonl을 만든다."""
    pdir = home / "projects" / project_folder
    pdir.mkdir(parents=True, exist_ok=True)
    jsonl = pdir / f"{session_id}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return jsonl


@pytest.fixture
def sample_session(fake_claude_home):
    """대표 세션 1개를 만든다 — 첫 줄은 cwd 없는 사이드체인, 이후 본문에 cwd."""
    home = fake_claude_home
    write_session(home, "F--021-test", "11111111-2222-3333-4444-555555555555", [
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "side"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
        {"type": "user", "cwd": "F:\\021_test\\proj", "slug": "happy-test-bee",
         "gitBranch": "main", "version": "1.0.0",
         "message": {"role": "user", "content": "hi"}, "timestamp": "2026-06-01T00:00:01.000Z"},
        {"type": "assistant", "cwd": "F:\\021_test\\proj",
         "message": {"role": "assistant", "content": "hello", "usage": {"input_tokens": 10, "output_tokens": 5}},
         "timestamp": "2026-06-01T00:05:00.000Z"},
    ])
    return home
