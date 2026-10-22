"""시험 격리 회귀 — ClewPath 웹 터미널 안에서 시험을 돌려도 진짜 설정 파일을 건드리지 않는다.

맥 실기 3b-2 이슈 B(2026-10-22): 맥 측 Claude 가 ClewPath 웹 터미널 안에서 전체 시험을 돌린 뒤 진짜
~/.claude/session_manager/clewpath.toml 이 사라져 있었다. 런처가 Host 에 넣는 CLEWPATH_CONFIG(진짜 설정 파일 자리)가
브로커 → 웹 터미널 → claude → pytest 로 새고(ptyd.clean_env 는 CLAUDE* 만 뺀다), appconfig.config_path() 는 그 값을
conftest 의 가짜 홈(SESSION_MANAGER_CLAUDE_HOME)보다 먼저 본다. 그래서 설정 파일을 쓰고 지우는 시험
(test_service_s2::test_legacy_token_switch_off_in_config)이 진짜 파일을 덮어쓰고 지웠다. 터미널 앱에서 돌린 3회차에는
그 env 가 없어 재현되지 않았다.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_conftest_drops_inherited_config_path():
    """conftest 가 물려받은 CLEWPATH_CONFIG 를 걷어 낸다 — 아래 시험이 새 pytest 안에서 이것도 돌린다."""
    leaked = os.environ.get("CLEWPATH_CONFIG")      # 실패해도 env 전체(토큰 등)를 찍지 않게 이 값만 본다
    assert leaked is None, leaked


def test_leaked_clewpath_config_is_left_alone(tmp_path):
    real = tmp_path / "real-data" / "clewpath.toml"
    real.parent.mkdir()
    body = '[server]\nport = 5100\n# 진짜 설정 파일(시험이 건드리면 안 된다)\n'.encode("utf-8")
    real.write_bytes(body)
    env = dict(os.environ)
    for k in ("_SM_TEST_HOME_SET", "SESSION_MANAGER_CLAUDE_HOME"):   # 터미널에서 새로 띄운 pytest 처럼
        env.pop(k, None)
    env["CLEWPATH_CONFIG"] = str(real)                                # ClewPath 웹 터미널이 물려주는 그대로
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_service_s2.py::test_legacy_token_switch_off_in_config",
         "tests/test_test_isolation.py::test_conftest_drops_inherited_config_path"],
        cwd=ROOT, env=env, capture_output=True, timeout=300)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert real.is_file(), "시험이 물려받은 CLEWPATH_CONFIG 자리의 진짜 설정 파일을 지웠다\n" + out[-2000:]
    assert real.read_bytes() == body, "시험이 물려받은 CLEWPATH_CONFIG 자리의 진짜 설정 파일을 덮어썼다"
    assert r.returncode == 0, out[-3000:]
