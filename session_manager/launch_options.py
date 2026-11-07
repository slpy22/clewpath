"""새 세션 생성 옵션 — 허용 목록·검사·claude 인자 조립, 생성 설정 저장과 다시 열 때 붙이기.

docs/designs/session-control.md 4-1·11 절(2026-11-07 설계 검토).

- **임의 인자를 받지 않는다.** 아래 허용 목록 밖의 키는 거절한다. `--settings`·`--mcp-config` 같은 옵션은 훅·명령을 실행시킬 수
  있어 통로를 하나라도 열면 승인 체계가 통째로 뚫린다.
- 위험도는 "새 세션의 권한을 넓히는가"로 나눈다(`widenings`). 부른 세션의 권한 모드는 Host 가 알 수 없으므로(레지스트리에 없음)
  넓히는 옵션은 모두 사람 승인 몫이다 — 이 모듈은 판정만 하고 승인은 부른 쪽(session_ops)이 한다.
- 생성 설정은 상태 DB `launch_settings` 에 Host 만 쓴다. 다시 열 때(`resume_flags`) 저장된 권한 모드가 화면·호출자의 `skip` 보다
  우선한다 — Claude 세션이 `skip:true` 로 띄워도 권한 확인으로 만든 세션은 자동 허용으로 뜨지 않는다(사장님 결정 D1 C).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass

from session_manager import store

SKIP_FLAG = "--dangerously-skip-permissions"
NARROW_MODES = ("manual", "plan", "dontAsk")
WIDE_MODES = ("acceptEdits", "auto", "bypassPermissions")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

PROMPT_MAX = 6000            # 첫 지시(에이전트 기획과 같은 상한)
APPEND_MAX = 4000            # 시스템 프롬프트 덧붙이기
TOTAL_MAX = 7500             # 인자 합계 — Windows 명령줄 8,191자(claude.cmd 경유) 아래로

# 받지 않는 옵션(이름으로 오면 이유를 밝혀 거절). 허용 목록 밖은 어차피 거절이지만, 흔히 시도할 것은 이유를 알려 준다.
REFUSED = {
    "settings": "훅·명령을 실행시킬 수 있음", "setting_sources": "설정 출처 바꾸기", "mcp_config": "MCP 서버(명령) 실행",
    "strict_mcp_config": "MCP 설정 바꾸기", "plugin_dir": "플러그인 코드 실행", "plugin_url": "플러그인 코드 실행",
    "agents": "도구·권한을 가진 에이전트 정의", "system_prompt": "기본 지시 교체(덧붙이기만 받음)",
    "dangerously_skip_permissions": "권한 모드는 permission_mode 로", "bare": "기본 동작 끄기",
    "debug_file": "임의 파일 쓰기", "file": "임의 파일 읽기", "remote_control": "원격 제어", "cloud": "클라우드 실행",
    "teleport": "원격 세션", "chrome": "브라우저 제어", "ide": "IDE 연결", "session_id": "세션 id 는 Host 가 정함",
    "max_budget_usd": "대화형 세션엔 비용 상한을 걸 수 없음(-p 전용)", "extra_args": "임의 인자",
}

_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}(\([^(),\n\r]{0,200}\))?$")
_MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_\-]{1,120}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,79}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_CMD_BAD = re.compile(r'[&|<>^%"]')           # claude.cmd(npm 설치본)를 거칠 때 cmd 가 해석하는 문자


class LaunchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class LaunchSpec:
    name: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    tools: list[str] | None = None              # None = 지정 안 함, [] = 도구 없음("")
    disallowed_tools: list[str] | None = None
    allowed_tools: list[str] | None = None
    add_dirs: list[str] | None = None
    append_system_prompt: str | None = None
    agent: str | None = None
    worktree: str | bool | None = None          # True = 이름 없이 -w

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


_KEYS = tuple(LaunchSpec.__dataclass_fields__)


def _text(key: str, v, limit: int) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise LaunchError("bad_option", f"{key} 는 글이어야 합니다")
    s = v.strip()
    if not s:
        return None
    if s.startswith("-"):
        raise LaunchError("bad_option", f"{key} 는 '-' 로 시작할 수 없습니다(옵션으로 읽힘)")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", s):
        raise LaunchError("bad_option", f"{key} 에 제어 문자가 있습니다")
    if len(s) > limit:
        raise LaunchError("too_long", f"{key} 가 너무 깁니다({len(s)}자 > {limit}자)")
    return s


def _tools(key: str, v) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\n]", v)]
    if not isinstance(v, (list, tuple)):
        raise LaunchError("bad_option", f"{key} 는 도구 이름 목록이어야 합니다")
    out = []
    for t in v:
        t = str(t).strip()
        if not t:
            continue
        if not (_TOOL.match(t) or _MCP_TOOL.match(t)):
            raise LaunchError("bad_option", f"{key} 의 도구 이름이 올바르지 않습니다: {t[:60]}")
        out.append(t)
    if len(out) > 64:
        raise LaunchError("too_long", f"{key} 가 너무 많습니다({len(out)}개)")
    return out


def _dirs(v) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    out = []
    for d in v:
        d = _text("add_dirs", d, 400)
        if not d:
            continue
        if not os.path.isabs(d):
            raise LaunchError("bad_option", f"add_dirs 는 전체 경로만 받습니다: {d}")
        d = os.path.normpath(d)
        if not os.path.isdir(d):
            raise LaunchError("no_dir", f"add_dirs 폴더가 없습니다: {d}")
        out.append(d)
    return out or None


def parse(options: dict | None, *, default_mode: str | None = "manual") -> LaunchSpec:
    """허용 목록 검사. default_mode: permission_mode 를 안 주면 쓸 값(프롬프트 생성 = 권한 확인)."""
    options = dict(options or {})
    for k in options:
        if k in REFUSED:
            raise LaunchError("refused_option", f"{k} 는 받지 않습니다 — {REFUSED[k]}")
        if k not in _KEYS:
            raise LaunchError("unknown_option", f"모르는 옵션입니다: {k}(허용: {', '.join(_KEYS)})")
    s = LaunchSpec()
    s.name = _text("name", options.get("name"), 80)
    m = _text("model", options.get("model"), 80)
    if m and not _MODEL.match(m):
        raise LaunchError("bad_option", f"model 이름이 올바르지 않습니다: {m}")
    s.model = m
    e = _text("effort", options.get("effort"), 10)
    if e and e not in EFFORTS:
        raise LaunchError("bad_option", f"effort 는 {', '.join(EFFORTS)} 중 하나입니다")
    s.effort = e
    pm = _text("permission_mode", options.get("permission_mode"), 20) or default_mode
    if pm and pm not in NARROW_MODES + WIDE_MODES:
        raise LaunchError("bad_option", f"permission_mode 는 {', '.join(NARROW_MODES + WIDE_MODES)} 중 하나입니다")
    s.permission_mode = pm
    s.tools = _tools("tools", options.get("tools"))
    s.disallowed_tools = _tools("disallowed_tools", options.get("disallowed_tools"))
    s.allowed_tools = _tools("allowed_tools", options.get("allowed_tools"))
    s.add_dirs = _dirs(options.get("add_dirs"))
    s.append_system_prompt = _text("append_system_prompt", options.get("append_system_prompt"), APPEND_MAX)
    a = _text("agent", options.get("agent"), 64)
    if a and not _SLUG.match(a):
        raise LaunchError("bad_option", f"agent 이름이 올바르지 않습니다: {a}")
    s.agent = a
    w = options.get("worktree")
    if w is True:
        s.worktree = True
    elif w not in (None, False, ""):
        w = _text("worktree", w, 64)
        if w and not _SLUG.match(w):
            raise LaunchError("bad_option", f"worktree 이름이 올바르지 않습니다: {w}")
        s.worktree = w
    return s


def widenings(spec: LaunchSpec) -> list[str]:
    """새 세션의 권한을 넓히는 항목 — 하나라도 있으면 사람 승인."""
    out = []
    if spec.permission_mode in WIDE_MODES:
        out.append(f"permission_mode={spec.permission_mode}")
    if spec.allowed_tools:
        out.append("allowed_tools")
    if spec.add_dirs:
        out.append("add_dirs")
    if spec.agent:
        out.append("agent")
    return out


def _perm_flags(mode: str | None) -> list[str]:
    if mode == "bypassPermissions":
        return [SKIP_FLAG]
    return ["--permission-mode", mode] if mode else []


def _list_flags(spec: LaunchSpec | dict) -> list[str]:
    """여러 값을 받는 옵션 — 쉼표로 묶어 한 인자로(뒤의 첫 지시를 도구 이름으로 먹지 않게, 그래도 맨 앞에 둔다)."""
    g = spec.get if isinstance(spec, dict) else (lambda k: getattr(spec, k))
    out: list[str] = []
    if g("tools") is not None:
        out += ["--tools", ",".join(g("tools"))]
    if g("disallowed_tools"):
        out += ["--disallowed-tools", ",".join(g("disallowed_tools"))]
    if g("allowed_tools"):
        out += ["--allowed-tools", ",".join(g("allowed_tools"))]
    for d in g("add_dirs") or []:
        out += ["--add-dir", d]
    return out


def new_argv(exe: str, session_id: str, spec: LaunchSpec, prompt: str = "") -> list[str]:
    """새 세션 argv. 여러 값 옵션 → 한 값 옵션 → --session-id → 첫 지시 순서(첫 지시가 옵션 값으로 먹히지 않게)."""
    if len(prompt) > PROMPT_MAX:
        raise LaunchError("too_long", f"첫 지시가 너무 깁니다({len(prompt)}자 > {PROMPT_MAX}자)")
    argv = [exe] + _list_flags(spec)
    if spec.name:
        argv += ["-n", spec.name]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    if spec.append_system_prompt:
        argv += ["--append-system-prompt", spec.append_system_prompt]
    if spec.agent:
        argv += ["--agent", spec.agent]
    if spec.worktree is True:
        argv += ["--worktree"]
    elif spec.worktree:
        argv += ["--worktree", spec.worktree]
    argv += _perm_flags(spec.permission_mode)
    argv += ["--session-id", session_id]
    if prompt:
        argv.append(prompt)
    check_total(exe, argv[1:])
    return argv


def check_total(exe: str, args: list[str]) -> None:
    total = sum(len(a) + 3 for a in args)
    if total > TOTAL_MAX:
        raise LaunchError("too_long", f"인자 합계가 너무 깁니다({total}자 > {TOTAL_MAX}자)")
    if exe.lower().endswith((".cmd", ".bat")):
        bad = next((a for a in args if _CMD_BAD.search(a)), None)
        if bad is not None:
            raise LaunchError("bad_char", "이 설치본(claude.cmd)은 & | < > ^ % \" 문자를 안전하게 넘기지 못합니다 — 빼고 다시 요청하세요")


# ---------------------------------------------------------------- 저장·다시 열기

_STORED = ("permission_mode", "effort", "model", "tools", "disallowed_tools", "allowed_tools", "add_dirs", "agent")


def save(session_id: str, spec: LaunchSpec, source: str) -> None:
    """생성 설정 기록(Host 만). 첫 지시·이름·덧붙인 지시는 다시 붙일 필요가 없어 저장하지 않는다
    (claude 가 첫 요청 때 시스템 프롬프트를 기록해 재개에 그대로 쓴다 — --system-prompt-snapshot)."""
    body = {k: getattr(spec, k) for k in _STORED if getattr(spec, k) is not None}
    with store.tx() as c:
        c.execute("INSERT INTO launch_settings(pc_id,session_id,permission_mode,effort,model,body_json,source,created)"
                  " VALUES('local',?,?,?,?,?,?,?) ON CONFLICT(pc_id,session_id) DO UPDATE SET"
                  " permission_mode=excluded.permission_mode, effort=excluded.effort, model=excluded.model,"
                  " body_json=excluded.body_json, source=excluded.source",
                  (session_id, spec.permission_mode, spec.effort, spec.model, json.dumps(body, ensure_ascii=False),
                   source, int(time.time())))


def load(session_id: str) -> dict | None:
    """저장된 생성 설정(없으면 None). 상태 DB 를 못 열면 StoreUnavailable — 부른 쪽은 넓히지 않는 쪽으로."""
    c = store.reader()
    try:
        r = c.execute("SELECT body_json, source, created FROM launch_settings WHERE pc_id='local' AND session_id=?",
                      (session_id,)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    try:
        body = json.loads(r["body_json"] or "{}")
    except ValueError:
        body = {}
    return {**body, "source": r["source"], "created": r["created"]}


def dump(session_id: str) -> dict | None:
    """휴지통 사본용 행(복구 때 되살린다 — 없으면 복구한 세션이 자동 허용으로 뜬다)."""
    c = store.reader()
    try:
        r = c.execute("SELECT permission_mode, effort, model, body_json, source, created FROM launch_settings"
                      " WHERE pc_id='local' AND session_id=?", (session_id,)).fetchone()
    finally:
        c.close()
    return dict(r) if r else None


def put(session_id: str, row: dict) -> None:
    """사본·원본 행을 그대로 넣는다. 그 사이 새로 생긴 값이 있으면 덮지 않는다."""
    with store.tx() as c:
        c.execute("INSERT OR IGNORE INTO launch_settings(pc_id,session_id,permission_mode,effort,model,body_json,source,created)"
                  " VALUES('local',?,?,?,?,?,?,?)",
                  (session_id, row.get("permission_mode"), row.get("effort"), row.get("model"),
                   row.get("body_json") or "{}", row.get("source"), row.get("created")))


def delete(session_id: str) -> None:
    with store.tx() as c:
        c.execute("DELETE FROM launch_settings WHERE pc_id='local' AND session_id=?", (session_id,))


def inherit(parent_id: str, child_id: str) -> None:
    """포크는 원본 설정을 이어받는다(포크로 권한을 넓히지 못하게). 원본에 설정이 없으면 아무것도 안 한다."""
    row = dump(parent_id)
    if row:
        put(child_id, {**row, "source": "fork"})


def stored_model(session_id: str) -> str | None:
    try:
        rec = load(session_id)
    except store.StoreUnavailable:
        return None
    return (rec or {}).get("model")


def resume_flags(session_id: str, skip_permissions: bool) -> list[str]:
    """다시 열 때 붙일 권한·도구 인자(모델 제외 — 부른 쪽이 라벨 모델과 함께 정한다).

    - 저장값 없음(옛 세션·바깥에서 만든 세션): 지금 동작 그대로(skip 이면 자동 허용).
    - 저장값 있음: 저장된 권한 모드가 이긴다. 화면이 '권한 확인'(skip=False)을 고르면 자동 허용·편집 허용 같은 넓은 모드도
      권한 확인으로 좁힌다. 넓히는 쪽(저장값보다 넓게)은 이 길로는 안 된다.
    - 상태 DB 를 못 열면 넓히지 않는 쪽(권한 확인)으로 띄운다.
    """
    try:
        rec = load(session_id)
    except store.StoreUnavailable:
        store.log(f"[launch] 생성 설정을 못 읽음 - 권한 확인으로 띄움: {session_id}")
        return []
    if rec is None:
        return [SKIP_FLAG] if skip_permissions else []
    mode = rec.get("permission_mode")
    if not skip_permissions and mode in WIDE_MODES:
        mode = None                          # 사람이 화면에서 좁힘 → 기본(권한 확인)
    out = _list_flags(rec)
    if rec.get("effort"):
        out += ["--effort", rec["effort"]]
    if rec.get("agent"):
        out += ["--agent", rec["agent"]]
    return out + _perm_flags(mode)
