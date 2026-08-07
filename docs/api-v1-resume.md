# ClewPath Host 외부 API v1 — 세션 재개 (WebSocket, 구조화 JSON)

외부 프로그램이 로컬 PC의 Claude Code 세션을 **재개해 프롬프트를 주고받는** API.
사람용 웹 UI(`/session_mgr/`)와 별개이며, **API 토큰**으로 인증한다.

## 엔드포인트

```
ws://127.0.0.1:5100/api/v1/resume/{session_id}
```

| 파라미터 | 설명 |
|---|---|
| `session_id` (경로) | 재개할 세션 UUID. 인터랙티브(cli)로 만든 세션만 재개 가능 |
| `token` (쿼리) | API 토큰. `?token=<토큰>`. 또는 헤더 `Authorization: Bearer <토큰>` |
| `skip` (쿼리) | `--dangerously-skip-permissions`. 기본 `1`(ON). `0`이면 권한확인 유지 |

> 토큰이 없거나 틀리면 핸드셰이크에서 **403**으로 거부된다.
> 프로그램 API는 권한 프롬프트에 사람이 답할 수 없으므로 기본 `skip=1`이다.

## 인증

서버 실행 시 환경변수로 토큰을 등록한다(`start-public.ps1`에 설정됨):

```
SM_API_TOKEN  = "sk-sm-..."           # 단일 토큰
SM_API_TOKENS = "tokA,tokB,tokC"      # (선택) 쉼표로 여러 개
```

## 세션 목록 조회 (HTTP)

재개할 세션 UUID 를 찾기 위한 목록 API. 같은 API 토큰으로 인증한다.

```
GET http://127.0.0.1:5100/api/v1/sessions?q=<필터>&limit=<N>
Authorization: Bearer <토큰>          # 또는 ?token=<토큰>
```

| 파라미터 | 설명 |
|---|---|
| `q` | cwd/슬러그/프로젝트명 부분일치 필터(선택) |
| `limit` | 최대 개수(0=무제한) |

응답:
```json
{
  "total": 19,
  "sessions": [
    {"session_id":"4135c614-...","cwd":"E:\\004_북한법","project_folder":"E--004----",
     "slug":null,"git_branch":"HEAD","message_count":9,"size_bytes":12345,
     "started_at":"2026-...","ended_at":"2026-..."}
  ]
}
```
`ended_at` 최신순 정렬. 예: `curl -H "Authorization: Bearer $TOKEN" ".../api/v1/sessions?q=북한&limit=5"`

## 가드레일 (외부 API 강제)

외부 API로 세션을 쓰면 **서버가 정책을 강제**한다(호출자가 못 올림).

| 계층 | 내용 |
|---|---|
| **격리** | `ask`·`resume` 모두 **무조건 fork**(원본 불변) + 종료 시 fork 삭제 |
| **역량 제한** | `--disallowed-tools` 로 **Bash·Write·Edit·NotebookEdit·KillShell 하드 차단** (파일쓰기·명령실행 불가, 읽기·웹검색은 허용) |
| **비용 상한** | ask 1회당 `--max-budget-usd`(기본 $1) |
| **토큰별 일일 한도** | 하루 호출수(기본 200)·비용(기본 $5) 초과 시 **429** |
| **감사 로그** | 모든 호출을 `~/.claude/session_manager/api_audit.jsonl` 에 기록(토큰 해시·세션·fork·프롬프트·비용) |

환경변수로 조정: `SM_API_DENIED_TOOLS`, `SM_API_DAILY_USD`, `SM_API_DAILY_CALLS`, `SM_API_MAX_CALL_USD`.
로컬(127.0.0.1) 호출은 신뢰하여 한도 미적용. 한도 초과 응답: `HTTP 429 {"error": "..."}`.

## 세션별 가드레일 프로파일 (조이기 전용)

세션 소유자가 세션에 **추가 정책**을 걸어두면, fork 시 **전역 정책과 교집합(더 빡센 쪽)**으로 적용된다.

```
GET  /api/v1/sessions/{id}/profile    → {profile, effective}
POST /api/v1/sessions/{id}/profile    (또는 웹: /api/sessions/{id}/profile, 쿠키)
{
  "permission_mode": "plan",              // 읽기전용 등
  "allowed_tools": ["Read","Grep"],       // 화이트리스트(더 빡셈)
  "denied_tools": ["WebFetch"],           // 추가 차단(전역과 합집합)
  "model": "fable",                        // 모델 고정
  "max_budget_usd": 0.5,                   // 호출당 비용(전역과 min)
  "system_prompt": "북한법 질문에만 답하라.",  // 행동/주제 제약(append-system-prompt)
  "deny_patterns": ["(?i)password"],       // 프롬프트/응답 차단 정규식
  "max_prompt_chars": 4000,
  "max_output_chars": 8000
}
```
- **내용 필터**: 프롬프트가 `deny_patterns` 에 걸리면 `400`, 응답이 걸리면 `[차단]` 으로 대체(`filtered:true`).
  길이 초과 시 절단(`truncated:true`).
- 웹 UI: 세션 행 **⋯ → 🛡 API 정책** 에서 편집.
- 모든 값은 **조이기만** 가능 — 전역 정책보다 느슨해지지 않는다.

## 일회성 fork 질의 (HTTP) — 원본 불변

원본 세션을 fork(복제)해서 프롬프트 1턴을 처리하고 **fork 를 자동 삭제**한다.
원본은 절대 바뀌지 않는다. 지식이 쌓인 세션에 오염 없이 질문만 할 때 쓴다.

```
POST http://127.0.0.1:5100/api/v1/sessions/{session_id}/ask
Authorization: Bearer <토큰>
Content-Type: application/json

{"prompt": "요약해줘", "skip_permissions": true, "timeout": 300}
```
응답:
```json
{"session_id":"...","fork_id":"...","answer":"...","result":"...",
 "cost_usd":0.08,"is_error":false,"tool_uses":[],"fork_deleted":true}
```
내부적으로 `claude -p --resume <원본> --fork-session --session-id <새fork>` 로 실행 →
결과 수집 → fork 삭제. 상태를 남기지 않는 stateless 질의.

## 라벨 (HTTP)

세션에 태그/표시이름을 붙인다. 원본 jsonl 은 건드리지 않고 별도 JSON 사이드카에 저장된다.

```
POST http://127.0.0.1:5100/api/v1/sessions/{session_id}/labels
Authorization: Bearer <토큰>
Content-Type: application/json

{"labels": ["북한법", "중요"], "name": "북한법 사이트 SEO"}   # 제공한 필드만 갱신
```
- `labels` 를 `[]` 로 보내고 name 도 없으면 라벨 레코드가 삭제된다.
- 응답: `{"session_id": "...", "record": {"labels": [...], "name": "...", "updated_at": "..."}}`

```
GET  /api/v1/labels                         # 사용 중인 라벨과 횟수(많은 순)
GET  /api/v1/sessions?label=<라벨>           # 그 라벨이 붙은 세션만
```
세션 목록 응답의 각 항목에는 `labels`, `label_name` 이 함께 포함된다.

## 메시지 프로토콜 (재개 WebSocket)

연결 후 양방향으로 **JSON 텍스트 메시지**를 주고받는다(1 메시지 = 1 JSON).

### 클라이언트 → 서버

```json
{"type": "prompt", "text": "<사용자 프롬프트>"}
```
- 한 턴을 보낸다. 연결을 유지한 채 여러 번 보내면 **멀티턴**으로 대화가 이어진다.

(고급) 원시 stream-json user 메시지를 그대로 보내려면:
```json
{"type": "user", "message": {"role": "user", "content": [{"type":"text","text":"..."}]}}
```

### 서버 → 클라이언트

연결 직후 제어 이벤트:
```json
{"type": "ready", "session_id": "...", "cwd": "...", "skip_permissions": true}
```

이후 claude 의 `stream-json` 이벤트가 **그대로** 전달된다(주요 타입):

| type | 의미 |
|---|---|
| `system` (subtype `init`) | 세션 초기화(도구 목록, cwd, 모델 등) |
| `assistant` | 어시스턴트 메시지. `message.content[]` 에 `text` / `tool_use` 블록 |
| `user` | 툴 실행 결과(tool_result) 등 |
| `result` | 한 턴 종료. `result`(최종 텍스트), `total_cost_usd`, `num_turns`, `is_error` |
| `rate_limit_event` | 사용량/레이트리밋 정보 |

종료 시 제어 이벤트:
```json
{"type": "closed", "code": 0}
{"type": "error", "message": "..."}
```

## 흐름

```
연결 → {ready} 수신
  └ {prompt} 전송 ──► {system} {assistant} ... {result}  (1턴)
  └ {prompt} 전송 ──► ...                                 (다음 턴, 멀티턴)
연결 종료 → claude 프로세스 자동 종료
```

## 예제 (Python)

```python
import asyncio, json, websockets

SID   = "4135c614-...."     # 재개할 세션
TOKEN = "sk-sm-...."
URL   = f"ws://127.0.0.1:5100/api/v1/resume/{SID}?token={TOKEN}"

async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        print("ready:", json.loads(await ws.recv()))     # {"type":"ready",...}
        await ws.send(json.dumps({"type":"prompt","text":"한 단어로: 안녕"}))
        async for raw in ws:
            ev = json.loads(raw)
            if ev["type"] == "assistant":
                for b in ev["message"]["content"]:
                    if b.get("type") == "text":
                        print("answer:", b["text"])
            elif ev["type"] == "result":
                print("done. cost$:", ev.get("total_cost_usd"))
                break

asyncio.run(main())
```

## 예제 (JavaScript / 브라우저·Node)

```js
const SID="4135c614-...", TOKEN="sk-sm-...";
const ws = new WebSocket(`ws://127.0.0.1:5100/api/v1/resume/${SID}?token=${TOKEN}`);
ws.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  if (ev.type === "ready") ws.send(JSON.stringify({type:"prompt", text:"한 단어로: 안녕"}));
  if (ev.type === "assistant")
    for (const b of ev.message.content) if (b.type === "text") console.log("answer:", b.text);
  if (ev.type === "result") { console.log("done", ev.total_cost_usd); ws.close(); }
};
```

## 주의

- **재개 가능 세션 한정**: `sdk-cli` 로 만들어진 단발 작업 세션은 claude 가 `No conversation found`로 거부한다. 인터랙티브 세션을 사용하라.
- **권한 스킵**: 기본 `skip=1`은 claude 가 파일쓰기·명령실행을 확인 없이 수행한다. 토큰이 유일한 방어선이므로 토큰을 안전하게 관리하라.
- **세션 목록 조회**: 재개할 세션 UUID 는 웹 UI 또는 (추후 토큰 인증이 붙을) `GET /api/sessions` 로 확인한다. 현재 `/api/sessions` 는 웹 쿠키 인증이라 브라우저에서 확인하는 것을 권장.
- **동시성**: 한 연결 = claude 프로세스 1개. 연결을 닫으면 해당 프로세스가 종료된다.
```
