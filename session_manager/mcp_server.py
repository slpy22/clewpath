"""ClewPath Host MCP 서버 — 다른 LLM(Claude Desktop, Cursor 등)이 세션 관리 기능을 쓰도록 노출.

stdio 트랜스포트로 동작하는 MCP 서버. 내부적으로 ClewPath Host 외부 API
(기본 http://127.0.0.1:5100 의 /api/v1/*)를 호출한다. 로컬(루프백)은 무인증.
즉 이 MCP 서버는 "원격 API ↔ LLM" 어댑터다(LLM 이 있는 어느 PC에서든 실행 가능).

환경변수:
    SM_API_BASE   기본 http://127.0.0.1:5100   (API 베이스 URL. 원격이면 토큰 필요)
    SM_API_TOKEN  필수. 외부 API 토큰

제공 도구(tool):
    list_sessions(query="", limit=30)              세션 목록 조회
    resume_session(session_id, prompt, ...)        세션 재개 + 프롬프트 1턴 → 응답 반환

실행:  python -m session_manager.mcp_server
"""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlparse

import httpx
import websockets
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("SM_API_BASE", "http://127.0.0.1:5100").rstrip("/")
API_TOKEN = os.environ.get("SM_API_TOKEN", "")

mcp = FastMCP("clewpath")


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {API_TOKEN}"}


@mcp.tool()
def list_sessions(query: str = "", label: str = "", limit: int = 30) -> dict:
    """로컬 PC의 Claude Code 세션 목록을 조회한다.

    Args:
        query: cwd/슬러그/프로젝트명에 대한 부분일치 필터(비우면 전체).
        label: 이 라벨이 붙은 세션만(비우면 무시).
        limit: 최대 개수(0이면 제한 없음).

    Returns:
        {"total": N, "sessions": [{session_id, cwd, project_folder, slug,
         git_branch, message_count, size_bytes, started_at, ended_at,
         labels, label_name}, ...]}
        ended_at 최신순 정렬. 재개하려면 session_id 를 resume_session 에 넘긴다.
    """
    params: dict = {"limit": limit}
    if query:
        params["q"] = query
    if label:
        params["label"] = label
    r = httpx.get(f"{API_BASE}/api/v1/sessions", params=params,
                  headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def list_labels() -> dict:
    """현재 사용 중인 라벨과 각 사용 횟수를 조회한다.

    Returns: {"labels": [{"label": "북한법", "count": 3}, ...]} (많은 순)
    """
    r = httpx.get(f"{API_BASE}/api/v1/labels", headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def set_session_labels(session_id: str, labels: list[str] | None = None,
                       name: str | None = None) -> dict:
    """세션에 라벨(태그)과 표시 이름을 설정한다. 원본 세션은 건드리지 않는다.

    Args:
        session_id: 대상 세션 UUID.
        labels: 라벨 목록. 빈 리스트면 라벨 제거.
        name: 표시 이름(선택).

    Returns: {"session_id", "record": {labels, name, ...}}
    """
    body: dict = {}
    if labels is not None:
        body["labels"] = labels
    if name is not None:
        body["name"] = name
    r = httpx.post(f"{API_BASE}/api/v1/sessions/{session_id}/labels",
                   json=body, headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_session_profile(session_id: str) -> dict:
    """세션의 가드레일 프로파일과 실효(effective) 정책을 조회한다.

    Returns: {profile, effective} — effective 는 전역정책 ∩ 프로파일(실제 적용값).
    """
    r = httpx.get(f"{API_BASE}/api/v1/sessions/{session_id}/profile",
                  headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def set_session_profile(session_id: str,
                        permission_mode: str | None = None,
                        allowed_tools: list[str] | None = None,
                        denied_tools: list[str] | None = None,
                        model: str | None = None,
                        max_budget_usd: float | None = None,
                        system_prompt: str | None = None,
                        deny_patterns: list[str] | None = None,
                        max_prompt_chars: int | None = None,
                        max_output_chars: int | None = None) -> dict:
    """세션에 추가 가드레일(조이기 전용)을 건다. fork 시 전역정책과 교집합으로 적용된다.

    제공한 필드만 갱신. 원본 세션은 건드리지 않는다.

    Args:
        permission_mode: "plan"(읽기전용) 등.
        allowed_tools: 허용 도구 화이트리스트(예: ["Read","Grep"]).
        denied_tools: 추가 차단 도구(전역과 합집합).
        model: 모델 고정(예: "fable").
        max_budget_usd: 호출당 비용 상한(전역과 min).
        system_prompt: 행동/주제 제약(append-system-prompt).
        deny_patterns: 프롬프트/응답 차단 정규식 목록.
        max_prompt_chars / max_output_chars: 길이 제한.

    Returns: {session_id, profile}
    """
    body: dict = {}
    for k, v in (("permission_mode", permission_mode), ("allowed_tools", allowed_tools),
                 ("denied_tools", denied_tools), ("model", model),
                 ("max_budget_usd", max_budget_usd), ("system_prompt", system_prompt),
                 ("deny_patterns", deny_patterns), ("max_prompt_chars", max_prompt_chars),
                 ("max_output_chars", max_output_chars)):
        if v is not None:
            body[k] = v
    r = httpx.post(f"{API_BASE}/api/v1/sessions/{session_id}/profile",
                   json=body, headers=_auth_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def ask_session(session_id: str, prompt: str,
                skip_permissions: bool = True, timeout_sec: int = 300) -> dict:
    """세션을 fork(복제)해서 프롬프트 1턴을 묻고 답을 받은 뒤 fork 를 삭제한다.

    원본 세션은 절대 바뀌지 않는다(일회성 질의). 지식이 쌓인 세션에
    '오염 없이' 질문만 하고 싶을 때 쓴다. resume_session 과 달리 상태를 남기지 않는다.

    Args:
        session_id: 기준(원본) 세션 UUID.
        prompt: 물어볼 내용.
        skip_permissions: True 면 --dangerously-skip-permissions(기본).
        timeout_sec: 응답 대기 한도.

    Returns:
        {session_id, fork_id, answer, result, cost_usd, is_error, tool_uses, fork_deleted}
    """
    r = httpx.post(f"{API_BASE}/api/v1/sessions/{session_id}/ask",
                   json={"prompt": prompt, "skip_permissions": skip_permissions,
                         "timeout": timeout_sec},
                   headers=_auth_headers(), timeout=timeout_sec + 30)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def resume_session(session_id: str, prompt: str,
                         skip_permissions: bool = True,
                         timeout_sec: int = 300) -> dict:
    """지정한 세션을 재개해 프롬프트 한 턴을 보내고 어시스턴트 응답을 받는다.

    인터랙티브로 만들어진 세션만 재개 가능하다(sdk-cli 단발 세션은 불가).

    Args:
        session_id: 재개할 세션 UUID (list_sessions 로 확인).
        prompt: 보낼 사용자 프롬프트.
        skip_permissions: True 면 --dangerously-skip-permissions (기본). 자동화엔 권장.
        timeout_sec: 응답(result) 대기 한도 초.

    Returns:
        {"session_id", "answer"(어시스턴트 텍스트), "result"(최종 텍스트),
         "cost_usd", "is_error", "tool_uses"(사용된 툴 이름 목록)}
    """
    u = urlparse(API_BASE)
    ws_scheme = "wss" if u.scheme == "https" else "ws"
    skip = 1 if skip_permissions else 0
    url = (f"{ws_scheme}://{u.netloc}{u.path}/api/v1/resume/{session_id}"
           f"?token={API_TOKEN}&skip={skip}")

    answer: list[str] = []
    tool_uses: list[str] = []
    result_text = None
    cost = None
    is_error = False

    async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
        # 첫 메시지(ready 또는 error)
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if first.get("type") == "error":
            return {"session_id": session_id, "answer": None,
                    "result": first.get("message"), "is_error": True}

        await ws.send(json.dumps({"type": "prompt", "text": prompt}))

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout_sec)
            ev = json.loads(raw)
            t = ev.get("type")
            if t == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        answer.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        tool_uses.append(b.get("name", "?"))
            elif t == "result":
                result_text = ev.get("result")
                cost = ev.get("total_cost_usd")
                is_error = bool(ev.get("is_error"))
                break
            elif t in ("error", "closed"):
                result_text = ev.get("message") or "(connection closed)"
                is_error = True
                break

    return {
        "session_id": session_id,
        "answer": "".join(answer) or result_text,
        "result": result_text,
        "cost_usd": cost,
        "is_error": is_error,
        "tool_uses": tool_uses,
    }


def main():
    if not API_TOKEN:
        import sys
        print("[mcp] SM_API_TOKEN 미설정 - API 호출이 401 로 거부됩니다.", file=sys.stderr)
    mcp.run()  # 기본 stdio 트랜스포트


if __name__ == "__main__":
    main()
