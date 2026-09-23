# 스파이크: 세션 간 일감 분배 — `ListAgents`+`SendMessage` 실측 (2026-09-23)

목적: 상위 세션이 하위(워커) 세션들에 오래 지속적으로 일감을 나눠 주는 운영을 ClewPath 동봉 스킬로
만들기 전에, claude 표준 세션 간 통신의 실제 동작을 확인한다. 실측 환경: Claude Code 2.1.278, Windows,
이 세션(관리) + 던질 워커 세션 1개(`claude -p` 로 생성 → ClewPath 웹터미널 WS 로 PTY 기동 → 실측 후 휴지통).

## 결과 요약

| 질문 | 답 |
|---|---|
| 워커가 "떠 있다"는 걸 어떻게 아나 | **`~/.claude/sessions/<pid>.json` 레지스트리**(읽기 전용으로 충분). `sessionId`, `name`(제목 또는 cwd 파생), `status`(idle/busy), `kind`, `messagingSocketPath`(명명 파이프). 프로세스가 끝나면 파일이 사라짐(수 초 내). `ListAgents` 도 이걸 보여 줌 |
| 이름 ↔ UUID 매핑 | 위 레지스트리로 확정. `ListAgents` 의 `[ref]` 는 UUID 접두어가 아님(별도 해시) — 매핑에 쓰지 말 것 |
| ClewPath PTY 로 띄운 세션도 피어인가 | **예**. `/ws/terminal/<sid>` 로 띄운 워커가 즉시 `ListAgents` 에 `interactive · idle` 로 나타남 → "화면 없는 워커 기동 API" 가 가능 |
| 바쁜 워커에 보낸 메시지 | **줄을 선다**(끊지 않음). 워커의 다음 도구 라운드에서 한꺼번에 소진 — 2건을 연달아 보내니 워커가 둘 다 읽고 순서대로 처리 |
| 워커 jsonl 에 남는 형태 | `type:user` 레코드, `message.content` 는 문자열 `"Another Claude session sent a message:\n<cross-session-message from=\"uds:\\\\.\\pipe\\LOCAL\\cc-msg-…\" from-name=\"<보낸 세션 이름>\" from-mode=\"bypass\">…</cross-session-message>"`. 레코드 키에 `promptSource`, `turnOrigin`, `permissionMode` 존재 |
| 보내는 쪽 jsonl | `tool_use name=SendMessage input={to, summary, message}`. `to` 는 이름 또는 `uds:\\.\pipe\...` 경로(답장 시). 파이프 경로 → 레지스트리 `messagingSocketPath` 로 세션 해석 가능 |
| 훅 | 워커에서 UserPromptSubmit(thinking)·Stop(ready) 모두 발화 → ClewPath 관제 알림(응답 완료) 그대로 동작 |
| 답장 | 워커가 `SendMessage(to=from 주소)` 로 보낸 것이 상위에 `<cross-session-message from-name="worker-f5">` 로 도착(상위가 도구 라운드 중이면 그때 소진) |
| 죽은 워커에 보내면 | `{"success":false,"message":"No agent named 'worker-f5' is reachable…"}` — tool_result 로 남으므로 monwatch 의 실패 감지에 연결 가능 |
| 권한 모드 | 둘 다 bypass 라 즉시 전달. **모드가 다르면 수신 세션의 사용자가 승인해야 전달**(문서·전송 결과 문구) → 스킬은 "워커와 관리의 권한 클래스 일치" 를 전제로 둔다 |
| `notify_when_idle` | 1회성 유휴 통지 구독 가능(폴링 금지) — 스킬의 "완료 대기" 수단 |

## 분배 규칙(확정)

```
워커가 레지스트리에 있는가(떠 있는가)?
  ├─ 예 → SendMessage (같은 세션에 -p --resume 절대 금지: 프로세스 2개 = 대화 분기)
  └─ 아니오 → 워커 기동(ClewPath PTY) 후 SendMessage, 또는 claude -p --resume(백그라운드·결과 파일)
```

동시성 안전은 이 한 줄이 전부다. 떠 있는 워커는 자기 턴 루프가 메시지를 순서대로 처리한다.

## ClewPath 에 필요한 확장(작음)

1. **라이브 판정 확장**: `~/.claude/sessions/*.json` 을 읽어 우리 PTY 가 아닌 대화형 세션도 `live`(이름·busy/idle 포함)로 표시 — 읽기 전용, 원칙 무관.
2. **관제 호출선**: `SendMessage` tool_use(`to` 이름/파이프 경로)와 수신 레코드(`from-name`)를 `→` 로 그림.
3. **실패 감지**: monwatch pending 매칭에 `SendMessage` 추가(`success:false` / is_error).
4. **(선택) 워커 기동 API**: 화면 없이 PTY 만 띄우는 `POST /api/sessions/{id}/terminal/start` — 상위가 `-p` 를 안 써도 되게. Host 업데이트 시 PTY 전부 종료되는 설계는 그대로(스킬에 재기동 규칙).
5. **스킬 동봉**: `~/.claude/skills/clewpath-workers/` 에 **사용자 명시 동작(설치 버튼+확인창)으로만** 새 파일 생성 — 불가침 원칙 예외 승인 필요.

## 정리

던질 워커 세션 4b8d747a 는 PTY 종료 → 휴지통. 사장님 세션(개발관리에이전트)은 건드리지 않음.

## 설계 확정 → v0.9.0 (2026-09-23, 사장님 결정)

**단일 전송 경로**: 상위 → 워커 통신은 `SendMessage` 하나. ClewPath 는 **"세션을 살려 둔다"** 하나만
맡는다(사장님 안: "살아 있으면 SendMessage, 아니면 살리고 나서 SendMessage — UI 는 있어도 없어도").
`-p --resume` 은 새 워커를 처음 만들 때 1회만(아직 살아 있지 않은 세션이라 이중 재개와 무관).

- **기존 `-p --resume` 경로 무변경(추가만)**: ClewPath 는 그 경로를 실행한 적이 없고(관리 세션의 Bash),
  관찰(호출선·실패 감지)만 한다 → 기존 서비스 회귀 0. 새 인식기는 병행 추가.
- **세션 하나는 한 방식만**: 워커로 살려 둔 세션에 `-p --resume` 을 하면 프로세스 2개 = 대화 분기.
  (지금도 "탭으로 연 세션에 -p" 하면 나는 사고 — 새 위험 아님.) 스킬·PWA 에 명시.

구현:
- `webterm._spawn`(스폰 본체 공유) + `start_terminal(sid, skip)` **멱등**(살아 있으면 already_live) →
  `POST /api/sessions/{id}/terminal/start`(로컬 전용, 409 cap/bg_hold, 404 no_cwd). 화면 없는 PTY 는
  여느 persist 터미널과 같은 객체 — 탭으로 열면 화면이 붙고, 닫아도 산다(= "UI 있어도 없어도").
- `peers.py`: `~/.claude/sessions/*.json` 읽기 전용 → `/api/v1/sessions` 에 `peer{name,status,pid}` 조인,
  PWA 목록 `🔗 이름 · 작업 중` 칩, `externallyActive` 1차 근거 추가. `resolve(to)` 가 이름/`이름 [ref]`/
  `uds:파이프` 를 UUID 로.
- 관제: `SendMessage` tool_use → `calls_out{via:"message"}`(✉ →), 수신 래퍼 → `from_peer`(📨) + 본문 정리.
- monwatch: pending 에 `SendMessage` 대상 추가, `"success":false` 도 실패로(죽은 워커 → 호출 실패 푸시).
- 스킬 `session_manager/skills/clewpath-workers/SKILL.md` 동봉 + `skillinstall.py` + 설정 화면
  "📦 워커 스킬 설치"(확인창, 덮어쓰기 재확인, 로컬 전용). CLAUDE.md 예외 등록. 기동·상태 조회는 파일을 만들지
  않는다(회귀 가드 `test_no_skill_install_on_startup`).
- 테스트 9건(`test_workers_dispatch.py`), 전체 294 passed.
