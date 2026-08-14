"""Claude Code 훅 연동 - 세션의 '지금' 상태를 구조화 이벤트로 받는다.

왜 훅인가: 터미널 출력(ANSI) 파싱은 Claude 버전이 오를 때마다 깨진다.
Claude Code 는 ~/.claude/settings.json 의 hooks 로 세션 생명주기 이벤트
(질문 제출, 턴 종료, 권한 알림 등)를 외부 명령에 JSON(stdin)으로 넘겨준다.
훅은 전역 설정이라 래퍼 없이 - 사용자가 평소처럼 `claude` 로 시작한
세션에서도 - 이벤트가 온다. (경쟁 제품은 자기 래퍼로 시작한 세션만 안다)

구성:
  1) ensure_registered(port): settings.json 에 우리 훅을 병합 등록(멱등,
     기존 훅 보존). 명령은 **인라인 curl 한 줄** - Claude 는 Windows 에서도
     훅을 bash(Git Bash)로 실행하므로(실측: .cmd 파일 경로는 백슬래시가
     이스케이프로 먹혀 'command not found'), 파일 경유 없이 bash/cmd 양쪽에서
     똑같이 도는 명령이어야 한다. 포트가 바뀌면 등록을 갱신한다.
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

def hook_command(port: int) -> str:
    """stdin(JSON)을 로컬 Host 로 넘기는 셸 불문 한 줄.

    Claude 가 훅을 어느 셸로 실행하는지는 PC/버전마다 다르다(bash·cmd·
    PowerShell 5.1 실측). 세 셸 모두에서 유효한 형태의 조건:
    - `||` 금지: PS 5.1 파서 에러(타 PC 실사고 - 훅이 매번 ParserError 실패)
    - 맨몸 `@-` 금지: PS 가 스플래팅 토큰으로 파싱 - `"@-"` 로 인용(세 셸 모두
      curl 에 @- 가 전달됨)
    - 공백 있는 인용 헤더 금지(셸별 인용 규칙 지뢰) - `-H Content-Type:application/json`
      처럼 공백 없이(curl 유효 문법)
    - `cmd /c` 래핑 금지: bash(MSYS)가 `/c` 를 `C:\` 경로로 변환해 cmd 가
      대화형으로 떠버린다(실측)
    - `curl.exe` 명시: PS 의 curl 별칭(Invoke-WebRequest) 회피
    실패 무음화(구 `|| exit 0`)는 셸 공통 문법이 없어 포기 - Host 가 꺼져 있으면
    로그 창에 non-blocking 한 줄이 남지만 세션 진행엔 영향 없다.
    -m 3: Host 다운 시에도 Claude 를 3초 이상 붙잡지 않는다.
    """
    return ("curl.exe -s -m 3 --data-binary \"@-\" "
            "-H Content-Type:application/json "
            f"http://127.0.0.1:{port}/api/hooks/event")


def _is_ours(entry: dict) -> bool:
    cmd = str(entry.get("command", ""))
    # 신형(인라인 curl) + 구형(.cmd 파일, bash 에서 안 돌아 교체 대상) 둘 다 우리 것
    return "/api/hooks/event" in cmd or HOOK_CMD_NAME in cmd


def ensure_registered(port: int) -> bool:
    """settings.json 에 훅을 병합 등록한다. 반환: 파일을 고쳤는가.

    - 다른 훅/설정은 절대 건드리지 않는다(병합만).
    - 멱등: 이미 같은 명령(같은 포트)이면 no-op. 포트가 바뀌면 갱신.
    - 첫 수정 전에 settings.json.bak-clewpath 백업을 한 번 남긴다.
    """
    # 구형 .cmd 파일은 더 이상 안 쓴다(bash 실행 불가) - 남아 있으면 정리
    try:
        hook_cmd_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass

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

    cmd = hook_command(port)
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
            # 같은 명령(같은 포트)이면 그대로. 포트 변경/구형(.cmd)이면 교체.
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
