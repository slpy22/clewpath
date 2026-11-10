"""세션 제어 MCP 서버 — ClewPath 플러그인이 띄운다(docs/designs/session-control.md 4 절).

업무 로직은 없다. 판단·승인은 전부 Host(session_ops)이고 여기는 Host 호출만 한다. 기존 mcp_server.py 에 넣지 않는 이유: 거기엔
resume_session 처럼 강한 도구가 있어 함께 노출되면 안 된다.

- 이름 변경은 `requiresUserInteraction` 표시로 자동 허용 세션에서도 Claude 확인창을 띄운다(X1 실측: 표시가 있으면 확인 전엔 안 불린다,
  `-p` 에서는 거절된다). 이동·삭제·권한을 넓히는 새 세션은 Host 승인(폰/2FA)이 사람 확인이다.
- 옵션 목록의 정본은 launch_options 다. 도구 설명을 거기서 만든다(손으로 맞추는 목록 금지). 검사는 Host 가 한다.
- 플러그인과 Host 의 판이 어긋나면(CLEWPATH_PLUGIN_API ≠ capabilities.api) 도구를 막고 설정에서 갱신하라고 알린다.

실행: python -m session_manager.session_control_mcp   (stdio)
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

from session_manager import config, launch_options

ASK_USER = {"anthropic/requiresUserInteraction": True}
SUMMARY = "com.clewpath/summary"      # 설치 화면에 보여 줄 한 줄(사람용). Claude 는 모르는 메타 키를 무시한다
GUARD = "com.clewpath/guard"          # 안전장치 한 줄(사람용)


def _ui(summary: str, guard: str, **extra) -> dict:
    return {SUMMARY: summary, GUARD: guard, **extra}
TIMEOUT_S = 60

mcp = FastMCP("sessions")
_checked = False


def _base() -> str:
    b = os.environ.get("SM_API_BASE", "").rstrip("/")
    if b:
        return b
    try:                                 # Host 가 실제로 연 포트(5100 이 막히면 다음 포트로 뜬다)
        d = json.loads((config.data_dir() / "runtime.json").read_text(encoding="utf-8"))
        return f"http://127.0.0.1:{int(d['port'])}"
    except Exception:  # noqa: BLE001
        return "http://127.0.0.1:5100"


def _http(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
    with httpx.Client(timeout=TIMEOUT_S, trust_env=False) as c:      # 루프백이다 — 프록시 환경변수를 타면 안 된다
        r = c.request(method, _base() + path, params=params, json=body)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": "bad_response", "status": r.status_code}


def _call(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    global _checked
    try:
        if not _checked:
            want = os.environ.get("CLEWPATH_PLUGIN_API", "")
            _, caps = _http("GET", "/api/v1/session-ops/capabilities")
            if want and str(caps.get("api")) != want:
                return {"error": "plugin_outdated", "message": "ClewPath 설정 화면에서 세션 제어 플러그인을 갱신하세요."}
            _checked = True
        status, d = _http(method, path, params=params, body=body)
    except httpx.HTTPError as e:
        return {"error": "host_unreachable", "message": f"ClewPath Host 에 닿지 못했습니다: {type(e).__name__}"}
    if status >= 400 and "error" not in d:
        return {"error": f"http_{status}", **d}
    return d


@mcp.tool(meta=_ui("이름·폴더로 세션 찾기", "읽기만"))
def sessions_find(query: str = "", cwd: str = "") -> dict:
    """이 PC 의 Claude 세션을 이름·폴더로 찾는다(읽기만). query = 이름 일부, cwd = 작업 폴더 전체 경로. 최대 50개, live 가 있으면 실행 중."""
    return _call("GET", "/api/v1/session-ops/find", params={"q": query, "cwd": cwd})


@mcp.tool(meta=_ui("이 에이전트의 지난 세션 대화 찾기", "읽기만 · 부른 세션이 속한 에이전트의 세션만(원본이 지워졌으면 보관본) · 비밀값 가림"))
def agent_history_search(query: str, session_id: str = "", limit: int = 20) -> dict:
    """이 세션이 속한 에이전트(ClewPath)의 지난 세션들과 지금 세션의 압축 전 대화에서 사장님 말·Claude 글 답을 찾는다(읽기만).
    query = 공백으로 나눈 낱말(모두 들어간 글만, 대소문자 무시). 최신 글부터, 결과마다 ref·at·앞뒤 150자.
    갈아탄 뒤 인수인계서에 없는 지난 결정·이유·지시를 확인할 때 쓴다. 0건이면 낱말을 줄여 다시 찾는다.
    session_id 를 주면 그 세션만(이 에이전트의 세션이어야 한다). 에이전트에 묶이지 않은 세션이면 not_an_agent."""
    return _call("POST", "/api/v1/agent-history/search", body={"q": query, "session_id": session_id, "limit": limit})


@mcp.tool(meta=_ui("찾은 대화의 앞뒤 읽기", "읽기만 · 같은 에이전트의 세션만 · 비밀값 가림"))
def agent_history_read(ref: str, before: int = 2, after: int = 4, at: int | None = None) -> dict:
    """agent_history_search 결과의 ref 앞뒤 글을 읽는다. at = 찾기 결과의 at(긴 글이면 그 위치를 가운데로 보여 준다).
    before·after = 앞뒤 글 수(최대 10). 잘렸으면 truncated, 이어 읽을 곳은 next·prev(ref). source=ledger 는 기록 파일이 지워져
    작업 장부에서 읽은 것(요청은 원문 일부, 답은 요약)이다."""
    body = {"ref": ref, "before": before, "after": after}
    if at is not None:
        body["at"] = at
    return _call("POST", "/api/v1/agent-history/read", body=body)


@mcp.tool(meta=_ui("세션 이름 바꾸기", "Claude 확인창을 거친다(자동 허용 세션도) · 실행 중 세션은 거절", **ASK_USER))
def session_rename(session_id: str, name: str) -> dict:
    """세션 이름을 바꾼다. 실행 중인 세션은 거절한다. 지금 이 세션의 이름은 /rename 으로 바꾼다. 사람 확인창이 뜬다."""
    return _call("POST", "/api/v1/session-ops/rename", body={"session_id": session_id, "name": name})


@mcp.tool(description=f"""새 대화형 세션을 띄운다. cwd = 있는 폴더의 전체 경로, name = 이름, prompt = 첫 지시({launch_options.PROMPT_MAX}자 이하).
기본은 권한 확인 모드(manual)다. options 로 받는 키: {', '.join(launch_options.LaunchSpec.__dataclass_fields__)}.
permission_mode: 바로 되는 것 {', '.join(launch_options.NARROW_MODES)} / 사람 승인이 필요한 것 {', '.join(launch_options.WIDE_MODES)}.
effort: {', '.join(launch_options.EFFORTS)}. allowed_tools·add_dirs·agent 도 권한을 넓히므로 사람 승인이다 — 그때는 세션이 바로 안 만들어지고
status=pending_approval 과 approval.id 가 온다(사장님 폰/2FA 승인 뒤 request_finish). 비용 상한은 대화형 세션에 걸 수 없다.
needs_trust=true 면 처음 쓰는 폴더라 사람이 터미널에서 신뢰를 골라야 한다.""",
          meta=_ui("새 세션 만들기(모델·effort·도구 제한 등 옵션)", "권한 확인 모드가 기본 · 권한을 넓히는 옵션은 폰/2차 인증 승인 · 시간당 20개"))
def session_create(cwd: str, name: str = "", prompt: str = "", options: dict | None = None) -> dict:
    return _call("POST", "/api/v1/session-ops/create", body={"cwd": cwd, "name": name or None, "prompt": prompt, "options": options})


def _request(what: str, preview_body: dict) -> dict:
    pv = _call("POST", f"/api/v1/session-ops/{what}/preview", body=preview_body)
    if pv.get("error") or not pv.get("ok"):
        return {"status": "nothing_to_do", "preview": pv} if not pv.get("error") else pv
    r = _call("POST", f"/api/v1/session-ops/{what}/request", body=preview_body)
    if r.get("error"):
        return {**r, "preview": pv}
    a = r.get("approval") or {}
    return {"status": "pending_approval", "approval_id": a.get("id"), "summary": a.get("summary"), "preview": pv,
            "next": "사장님이 폰 또는 PC 2차 인증으로 승인한 뒤 request_finish 를 부른다. 승인을 기다리며 반복해서 부르지 않는다."}


@mcp.tool(meta=_ui("세션 기록을 다른 폴더로 옮기기 요청(미리보기 포함)", "폰/2차 인증 승인 뒤에만 실행 · 작업 폴더 파일은 안 옮김 · 실행 중 세션은 빠짐"))
def sessions_move_request(ids: list[str], new_cwd: str) -> dict:
    """세션 기록을 다른 폴더로 옮기는 승인을 요청한다(미리보기 포함). new_cwd = 전체 경로만. 실행 중·자기 세션은 빠진다.
    작업 폴더 파일은 옮기지 않는다(그건 화면 전용). 이 도구만으로는 아무것도 옮겨지지 않는다 — 사람 승인 뒤 request_finish."""
    return _request("move", {"ids": ids, "new_cwd": new_cwd})


@mcp.tool(meta=_ui("세션을 휴지통으로 보내기 요청(미리보기 포함)", "폰/2차 인증 승인 뒤에만 실행 · 30일 안 복구 · 실행 중 세션은 빠짐"))
def sessions_delete_request(ids: list[str]) -> dict:
    """세션을 휴지통으로 옮기는 승인을 요청한다(미리보기 포함, 30일 복구). 실행 중·자기 세션은 빠진다.
    이 도구만으로는 아무것도 지워지지 않는다 — 사람 승인 뒤 request_finish."""
    return _request("delete", {"ids": ids})


@mcp.tool(meta=_ui("승인된 옮기기·지우기·새 세션 실행하고 결과 받기", "승인되지 않았으면 아무것도 하지 않음"))
def request_finish(approval_id: str) -> dict:
    """승인 상태를 확인하고, 승인됐으면 실행해 항목별 결과를 돌려준다. status: pending·rejected·expired·executed. 승인을 기다리며 반복해서 부르지 않는다."""
    return _call("POST", f"/api/v1/session-ops/finish/{approval_id}", body={})


@mcp.tool(meta=_ui("휴지통에서 세션 되살리기", "같은 경로에 파일이 있으면 덮어쓰지 않음"))
def session_restore(bucket: str) -> dict:
    """휴지통에서 세션을 되살린다. bucket = 삭제 결과의 항목별 bucket 값. 같은 경로에 파일이 있으면 덮어쓰지 않는다."""
    return _call("POST", "/api/v1/session-ops/restore", body={"bucket": bucket})


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
