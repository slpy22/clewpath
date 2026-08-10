"""Claude Code 훅 연동 - 세션의 '지금' 상태를 구조화 이벤트로 받는다.

왜 훅인가: 터미널 출력(ANSI) 파싱은 Claude 버전이 오를 때마다 깨진다.
Claude Code 는 ~/.claude/settings.json 의 hooks 로 세션 생명주기 이벤트
(질문 제출, 턴 종료, 권한 알림 등)를 외부 명령에 JSON(stdin)으로 넘겨준다.
훅은 전역 설정이라 래퍼 없이 - 사용자가 평소처럼 `claude` 로 시작한
세션에서도 - 이벤트가 온다. (경쟁 제품은 자기 래퍼로 시작한 세션만 안다)

구성:
  1) ensure_registered(port): settings.json 에 우리 훅을 병합 등록(멱등,
     기존 훅 보존). settings 의 명령은 데이터 폴더의 clewpath-hook.cmd 하나.
     포트가 바뀌면 cmd 파일만 다시 쓰면 되고 settings.json 은 불변.
  2) update_from_event(payload): 이벤트를 세션별 메모리 상태로 접는다.
  3) status_map(): 세션 목록 API 가 세션별로 조인해 내려줄 스냅샷.
     신선도(stale) 판정은 소비자(PWA) 몫 - 여기선 관측 시각만 기록한다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# 구독하는 이벤트. 전부 저빈도(턴 단위)라 훅 실행 비용이 체감되지 않는다.
# PreToolUse/PostToolUse 는 도구 호출마다 프로세스를 띄우게 되어 뺐다.
EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd")

HOOK_CMD_NAME = "clewpath-hook.cmd"

# 세션별 상태(메모리 전용, 재기동하면 사라짐 - 훅이 다시 채운다).
_STATUS: dict[str, dict[str, Any]] = {}
_MAX_SESSIONS = 300


# ---------------------------------------------------------------- 경로 해석

def _claude_home() -> Path:
    home = (os.environ.get("SESSION_MANAGER_CLAUDE_HOME")
            or os.environ.get("CLAUDE_CONFIG_DIR"))
    return Path(home) if home else Path.home() / ".claude"


def settings_path() -> Path:
    return _claude_home() / "settings.json"


def hook_cmd_path() -> Path:
    return _claude_home() / "session_manager" / HOOK_CMD_NAME


# ---------------------------------------------------------------- 등록(멱등)

def _write_hook_cmd(port: int) -> None:
    """stdin(JSON)을 로컬 Host 로 그대로 넘기는 한 줄짜리 명령을 쓴다.

    curl.exe 는 Windows 10+ 기본 탑재. -m 3 으로 Host 다운 시에도 훅이
    Claude 를 붙잡지 않게 하고, exit /b 0 으로 항상 성공 처리한다
    (훅의 비정상 종료코드는 Claude 에 경고로 뜬다 - 우리 문제로 사용자
    세션을 시끄럽게 만들지 않는다).
    """
    p = hook_cmd_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "@curl.exe -s -m 3 -H \"Content-Type: application/json\" "
        f"--data-binary @- \"http://127.0.0.1:{port}/api/hooks/event\" >nul 2>&1\r\n"
        "@exit /b 0\r\n",
        encoding="ascii",
    )


def _is_ours(entry: dict) -> bool:
    return HOOK_CMD_NAME in str(entry.get("command", ""))


def ensure_registered(port: int) -> bool:
    """settings.json 에 훅을 병합 등록한다. 반환: 파일을 고쳤는가.

    - 다른 훅/설정은 절대 건드리지 않는다(병합만).
    - 멱등: 이미 등록돼 있으면 no-op.
    - 첫 수정 전에 settings.json.bak-clewpath 백업을 한 번 남긴다.
    """
    _write_hook_cmd(port)

    sp = settings_path()
    try:
        raw = sp.read_text(encoding="utf-8-sig") if sp.is_file() else "{}"
        settings = json.loads(raw or "{}")
        if not isinstance(settings, dict):
            raise ValueError("settings.json 최상위가 객체가 아님")
    except Exception as e:  # noqa: BLE001
        # 사용자의 설정 파일이 깨져 있으면 덮어쓰지 않는다 - 우리가 망가뜨린
        # 것처럼 보일뿐더러 실제 데이터 손실이 된다.
        print(f"[hooks] settings.json 읽기 실패, 등록 건너뜀: {e}", flush=True)
        return False

    cmd = str(hook_cmd_path())
    hooks_obj = settings.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        print("[hooks] settings.hooks 형식이 예상과 다름, 등록 건너뜀", flush=True)
        return False

    changed = False
    for ev in EVENTS:
        groups = hooks_obj.setdefault(ev, [])
        if not isinstance(groups, list):
            continue
        ours = [h for g in groups if isinstance(g, dict)
                for h in (g.get("hooks") or []) if _is_ours(h)]
        if ours:
            # 경로(포트 아님 - 포트는 cmd 파일 안)에 변화가 없으면 그대로 둔다
            if all(h.get("command") == cmd for h in ours):
                continue
            for h in ours:
                h["command"] = cmd
            changed = True
            continue
        groups.append({"hooks": [{"type": "command", "command": cmd}]})
        changed = True

    if not changed:
        return False

    bak = sp.with_name(sp.name + ".bak-clewpath")
    try:
        if sp.is_file() and not bak.exists():
            bak.write_bytes(sp.read_bytes())
    except Exception:  # noqa: BLE001
        pass
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8")
    print(f"[hooks] Claude 훅 등록: {', '.join(EVENTS)}", flush=True)
    return True


# ---------------------------------------------------------------- 상태 반영

def _classify_notification(message: str) -> str:
    """Notification 메시지를 권한요청/입력대기로 분류.

    공식 스키마에 종류 필드가 없어 문구로 나눈다. 문구가 바뀌어도
    '알 수 없는 알림'이지 크래시는 아니다(방어적 기본값 waiting).
    """
    m = (message or "").lower()
    if "permission" in m or "권한" in m:
        return "permission"
    return "waiting"


def update_from_event(payload: dict) -> None:
    sid = str(payload.get("session_id") or "")
    if not sid:
        return
    ev = str(payload.get("hook_event_name") or "")
    now = int(time.time())

    st = _STATUS.get(sid)
    if st is None:
        if len(_STATUS) >= _MAX_SESSIONS:
            oldest = min(_STATUS, key=lambda k: _STATUS[k].get("active_at", 0))
            _STATUS.pop(oldest, None)
        st = _STATUS[sid] = {}

    st["active_at"] = now
    if payload.get("cwd"):
        st["cwd"] = payload["cwd"]

    if ev == "SessionStart":
        st["phase"] = "idle"
    elif ev == "UserPromptSubmit":
        st["phase"] = "thinking"
        st["thinking_at"] = now
        st.pop("permission_at", None)
        st.pop("note", None)
    elif ev == "Notification":
        kind = _classify_notification(str(payload.get("message") or ""))
        st["phase"] = kind
        st[f"{kind}_at"] = now
        st["note"] = str(payload.get("message") or "")[:200]
    elif ev == "Stop":
        st["phase"] = "ready"
        st["ready_at"] = now
        st.pop("permission_at", None)
        st.pop("note", None)
    elif ev == "SessionEnd":
        st["phase"] = "ended"
        st["ended_at"] = now


def status_of(session_id: str) -> dict[str, Any] | None:
    return _STATUS.get(session_id)


def status_map() -> dict[str, dict[str, Any]]:
    return _STATUS
