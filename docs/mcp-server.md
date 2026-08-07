# ClewPath Host MCP 서버 — 다른 LLM 연동

다른 LLM(Claude Desktop, Cursor, Claude Code 등)이 **로컬 PC의 Claude Code 세션을
조회하고 재개해 대화**할 수 있게 해주는 MCP 서버.

내부적으로 ClewPath Host 외부 API(`http://127.0.0.1:5100/api/v1/*`)를
API 토큰으로 호출하는 **어댑터**다. 따라서 LLM 클라이언트가 있는 어느 PC에서 실행해도 된다
(인터넷으로 그 API 에 닿기만 하면 됨).

## 제공 도구(tools)

| 도구 | 설명 |
|---|---|
| `list_sessions(query="", label="", limit=30)` | 세션 목록 조회. `query`(부분일치)·`label`(라벨) 필터 |
| `resume_session(session_id, prompt, skip_permissions=True, timeout_sec=300)` | 세션 재개 + 프롬프트 1턴 → 어시스턴트 응답 반환 |
| `ask_session(session_id, prompt, ...)` | **원본을 fork 해서 1턴 질의 후 fork 자동삭제**(원본 불변, 일회성 질의) |
| `list_labels()` | 사용 중인 라벨과 횟수 조회 |
| `set_session_labels(session_id, labels=[...], name=None)` | 세션에 라벨/표시이름 설정(원본 불변) |
| `get_session_profile(session_id)` | 세션 가드레일 프로파일 + 실효 정책 조회 |
| `set_session_profile(session_id, permission_mode, allowed_tools, denied_tools, model, max_budget_usd, system_prompt, deny_patterns, ...)` | 세션에 추가 가드레일(조이기) 설정 |

`resume_session` 반환: `{session_id, answer, result, cost_usd, is_error, tool_uses}`

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SM_API_BASE` | `http://127.0.0.1:5100` | API 베이스 URL |
| `SM_API_TOKEN` | (없음) | **필수.** 외부 API 토큰 |

## 실행

```bash
pip install -e .              # 또는 pip install mcp httpx websockets
python -m session_manager.mcp_server
```
(stdio 트랜스포트로 동작 — MCP 클라이언트가 이 프로세스를 띄워 통신한다)

## 클라이언트 등록 예

### Claude Desktop / Claude Code (`claude_desktop_config.json` 또는 `.mcp.json`)

```json
{
  "mcpServers": {
    "clewpath": {
      "command": "C:\\Users\\slpy2\\.pyenv\\pyenv-win\\versions\\3.11.9\\python.exe",
      "args": ["-m", "session_manager.mcp_server"],
      "cwd": "F:\\021_3_AX 기술스택\\006_session_manager",
      "env": {
        "SM_API_BASE": "http://127.0.0.1:5100",
        "SM_API_TOKEN": "sk-sm-..."
      }
    }
  }
}
```

### Claude Code (CLI 한 줄 등록)

```bash
claude mcp add clewpath \
  -e SM_API_BASE=http://127.0.0.1:5100 \
  -e SM_API_TOKEN=sk-sm-... \
  -- python -m session_manager.mcp_server
```

## 사용 흐름 (LLM 입장)

1. `list_sessions(query="북한")` → 재개할 세션의 `session_id` 확인
2. `resume_session(session_id="4135c614-...", prompt="지난 작업 요약해줘")` → 응답 텍스트 수신

## 주의

- **인터랙티브 세션만 재개 가능** — `sdk-cli` 단발 세션은 claude 가 거부한다.
- `skip_permissions=True`(기본) 는 원격 PC에서 파일쓰기·명령실행을 확인 없이 수행한다.
  API 토큰이 유일한 방어선이므로 토큰을 안전하게 보관하라.
- 다른 PC에서 실행하려면 그 PC에도 `mcp`,`httpx`,`websockets` 와 이 패키지(또는
  `mcp_server.py` 단일 파일)가 있어야 한다. API 호출만 하므로 pywinpty/claude 는 불필요.
