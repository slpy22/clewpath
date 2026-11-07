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


@mcp.tool()
def sessions_find(query: str = "", cwd: str = "") -> dict:
    """이 PC 의 Claude 세션을 이름·폴더로 찾는다(읽기만). query = 이름 일부, cwd = 작업 폴더 전체 경로. 최대 50개, live 가 있으면 실행 중."""
    return _call("GET", "/api/v1/session-ops/find", params={"q": query, "cwd": cwd})


@mcp.tool(meta=ASK_USER)
def session_rename(session_id: str, name: str) -> dict:
    """세션 이름을 바꾼다. 실행 중인 세션은 거절한다. 지금 이 세션의 이름은 /rename 으로 바꾼다. 사람 확인창이 뜬다."""
    return _call("POST", "/api/v1/session-ops/rename", body={"session_id": session_id, "name": name})


@mcp.tool(description=f"""새 대화형 세션을 띄운다. cwd = 있는 폴더의 전체 경로, name = 이름, prompt = 첫 지시({launch_options.PROMPT_MAX}자 이하).
기본은 권한 확인 모드(manual)다. options 로 받는 키: {', '.join(launch_options.LaunchSpec.__dataclass_fields__)}.
permission_mode: 바로 되는 것 {', '.join(launch_options.NARROW_MODES)} / 사람 승인이 필요한 것 {', '.join(launch_options.WIDE_MODES)}.
effort: {', '.join(launch_options.EFFORTS)}. allowed_tools·add_dirs·agent 도 권한을 넓히므로 사람 승인이다 — 그때는 세션이 바로 안 만들어지고
status=pending_approval 과 approval.id 가 온다(사장님 폰/2FA 승인 뒤 request_finish). 비용 상한은 대화형 세션에 걸 수 없다.
needs_trust=true 면 처음 쓰는 폴더라 사람이 터미널에서 신뢰를 골라야 한다.""")
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


@mcp.tool()
def sessions_move_request(ids: list[str], new_cwd: str) -> dict:
    """세션 기록을 다른 폴더로 옮기는 승인을 요청한다(미리보기 포함). new_cwd = 전체 경로만. 실행 중·자기 세션은 빠진다.
    작업 폴더 파일은 옮기지 않는다(그건 화면 전용). 이 도구만으로는 아무것도 옮겨지지 않는다 — 사람 승인 뒤 request_finish."""
    return _request("move", {"ids": ids, "new_cwd": new_cwd})


@mcp.tool()
def sessions_delete_request(ids: list[str]) -> dict:
    """세션을 휴지통으로 옮기는 승인을 요청한다(미리보기 포함, 30일 복구). 실행 중·자기 세션은 빠진다.
    이 도구만으로는 아무것도 지워지지 않는다 — 사람 승인 뒤 request_finish."""
    return _request("delete", {"ids": ids})


@mcp.tool()
def request_finish(approval_id: str) -> dict:
    """승인 상태를 확인하고, 승인됐으면 실행해 항목별 결과를 돌려준다. status: pending·rejected·expired·executed. 승인을 기다리며 반복해서 부르지 않는다."""
    return _call("POST", f"/api/v1/session-ops/finish/{approval_id}", body={})


@mcp.tool()
def session_restore(bucket: str) -> dict:
    """휴지통에서 세션을 되살린다. bucket = 삭제 결과의 항목별 bucket 값. 같은 경로에 파일이 있으면 덮어쓰지 않는다."""
    return _call("POST", "/api/v1/session-ops/restore", body={"bucket": bucket})


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
