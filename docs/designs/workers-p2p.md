# 워커 직접 통신(P2P) — clewpath-workers 스킬 v2 기획

- 작성: 2026-10-01 · 상태: **기획 — 방향 확정(Host 전역 명부부터, 2026-10-01 사장님), 다음 /plan-eng-review** · 크로스체크: Gemini(gemini-3.1-pro-preview)·Codex(gpt-6-astra)·Claude(opus-5.5)
- 대상: `session_manager/skills/clewpath-workers/SKILL.md`(동봉본 정본) → 설정의 📦 워커 스킬 설치(덮어쓰기 확인)로 반영

## 문제

지금은 중앙집중형이다. 워커 A 가 워커 B 에게 물어볼 것이 있어도 A → 관리 세션 → B → 관리 세션 → A 로 돈다.
관리 세션 컨텍스트에 남의 대화가 쌓이고(토큰 ×2 이상), 관리 세션이 바쁘면 워커끼리 기다린다.
`SendMessage` 의 답장은 **보낸 세션에게** 돌아오므로, 워커끼리 직접 주고받으면 그 내용은 관리 세션에 아예 들어가지 않는다 — 절감이 실제로 일어난다.

## 확인한 사실(코드)

- 관제는 **그룹 안 아무 멤버 → 아무 멤버** 의 `SendMessage` 를 호출선으로 그린다(`monitor.py:189-198`, 수신 래퍼도 `from_peer` 로 표시). → 워커끼리 직접 통신해도 사장님 화면에는 그대로 보인다. **Phase 1 은 Host 변경 없이 스킬만으로 가능.**
- 스킬 갱신 경로: 동봉본 수정 → Host 릴리스 → 설정 📦 설치가 digest 로 '다름' 감지 → 사장님이 덮어쓰기 확인(`skillinstall.install(overwrite=True)`). 불가침 원칙상 자동 설치 없음.
- 세션 이름(주소)은 재기동마다 바뀔 수 있다 — UUID 만 믿고 보낼 때마다 `/api/v1/sessions` 로 이름을 읽는다(v1 규칙 유지).

## 결정(크로스체크 3/3 합의)

1. **역할 분담**: 상위 = 배정·판단·최종 승인. 워커 = **배정 범위 안에서** 직접 협업. 동료의 업무·우선순위를 바꿀 권한은 없다.
2. **조직 구조 = 평면(테이블) 세션 + 업무 단위 책임자.** 세션을 트리(조직도)로 만들지 않는다.
   - 트리의 중간 관리 세션은 지금 없애려는 바로 그 비용(보고 중복·컨텍스트 누적·병목)을 다시 만든다. 워커 3~8개에선 득이 없다.
   - '팀장' 은 세션의 계급이 아니라 **업무(task)의 owner** 다. 같은 세션이 T1 에선 owner, T2 에선 협업자.
   - 위계가 필요한 건 세션이 아니라 **업무 분해** 쪽 → Phase 2 에서 `parent` 필드로(업무 트리 ≠ 세션 트리).
3. **담당 정보 = 기존 등록부 확장**(`.clewpath/workers.json`), **쓰기는 관리 세션만, 워커는 읽기만.**
4. **관리 세션은 4종만 받는다**: 업무 최종 결과(owner 가 1회) · 범위/우선순위 변경 요청 · 해결 못 한 차단 · 한도 도달.

## 목적 확장(2026-10-01 사장님): 일반 프로젝트 협업 + 개인 비서 인프라

이 스킬은 한 프로젝트의 협업 도구이면서, **세션·에이전트가 사장님의 작업 경력·이력으로 쌓이고**, 나중에
그 이력을 분석한 **개인 비서 에이전트가 사장님 대신 에이전트들을 관리**하는 기반이 된다. 그래서 설계 축이 바뀐다:

| 처음 기획 | 목적 확장 후 |
|---|---|
| 등록부 = 프로젝트 루트 파일 `.clewpath/workers.json` | **Host 전역 명부**(데이터 폴더, 프로젝트 횡단) — 비서가 한 곳에서 모든 팀·에이전트를 본다. 프로젝트 파일은 없어도 됨 |
| 워커 = 세션 UUID | **에이전트(영속 정체성) ≠ 세션**. 세션은 교체(대화 비대·continued-in)돼도 에이전트의 이력은 이어진다 |
| 진행 기록은 남기지 않음(관리 세션 부담) | **일감 장부(append-only)** — 누가 어떤 일감을 맡아 무엇을 냈는지. 관리 세션이 쓰는 건 배정·결과 2종뿐, 나머지는 Host 가 `[cw]` 헤더를 jsonl 에서 읽어 채운다(토큰 0) |
| 관리 세션 = 프로젝트의 최상위 | **관리자는 역할**이다. 지금은 프로젝트 관리 세션, 나중에 비서가 그 위에서 여러 팀을 관리 |

### 데이터 모델(3층)

- **에이전트** `ag_<id>`: 별칭·역할·태그(역량)·소속 팀·`sessions[]`(세션 UUID 이력: 시작/끝/교체 사유)·현재 세션·상태. → **경력이 쌓이는 단위.**
- **팀(프로젝트)** `tm_<id>`: 이름·루트 경로·관리자 에이전트·구성원·관제 그룹 id(자동 동기화). **팀 안은 평면.**
- **일감 장부** `<팀>-T17`: 이벤트 append(`created/assigned/ask/answer/review/result/blocked/done`) + 에이전트 id·산출물 경로·시각(Phase 2 에 비용). 팀 간 유일한 id.

### 조직 구조 재결론: 팀 안은 평면, 팀 위는 1단 트리

- 팀 안 세션 트리는 여전히 기각(중간 관리 비용).
- 비서가 생기면 **비서 → 팀(관리자) → 워커** 의 상위 1단이 생긴다. 이건 세션 계급이 아니라 '포트폴리오' 층 — 팀 레코드에 `parent`(비서 에이전트)만 두면 된다. 지금 스키마에 자리만 만든다.

### 쓰기·읽기 경로

- **유일한 작성자 = Host API**(직렬화·잠금, 어느 프로젝트·비서에서도 같은 길). 관리 세션은 curl 로 부른다(지금 start API 와 같은 방식).
- 워커는 보낼 때만 `GET /api/v1/team/<팀>` — 구성원 + **살아 있는 이름까지 한 번에**(2단 조회 제거).
- 비서(미래)는 기존 ClewPath **MCP 서버**에 도구를 더해 쓴다: 에이전트 목록·이력·팀 상태·일감 위임.
- 전부 로컬(Host 데이터 폴더). CP·릴레이로 보내지 않는다. claude 파일은 읽기만(불가침 원칙 그대로).

### 단계 재편

| 단계 | 내용 | 크기 |
|---|---|---|
| **1. 팀·에이전트 명부 + 스킬 v2** | Host `team` 모듈(명부 JSON + API: 팀·에이전트·세션 교체·일감 배정/결과) · 관제 그룹 자동 동기화 · 스킬 v2(아래 P2P 규칙 + API 사용 + 워커 모드) | M |
| 2. 장부 자동 채움·경력 화면 | `[cw]` 헤더 인덱서(jsonl 읽기 전용) · 에이전트 이력/일감 화면(PWA) · 위반 감지 알림 · 일감별 비용 | M |
| 3. 개인 비서 | MCP 도구(`list_agents`·`agent_history`·`team_status`·`delegate_task`) · 팀 위 포트폴리오 층 · 비서 에이전트 스킬 | L |

아래 1-1~1-3·1-5·1-6 의 P2P 규칙은 그대로 유효하다. **1-4(프로젝트 파일 등록부)는 위 Host 명부로 대체**한다.

## Phase 1 — 스킬 v2 (처음 기획: Host 변경 없음, S~M — 목적 확장으로 위 '단계 1' 로 대체)

### 1-1. 직접 통신 허용 기준

| 워커끼리 직접 | 반드시 관리 세션으로 |
|---|---|
| 같은 업무 안의 사실 확인·인터페이스 질의 | 목표·요구사항·우선순위 변경 |
| 산출물 전달(파일 경로)·리뷰 요청·테스트 결과 공유 | 업무 재배정·새 워커 생성·다른 업무에 영향 주는 인터페이스 변경 |
| 이미 합의된 인터페이스의 세부 조율 | `write_scope` 밖 파일 수정 필요·결론 불일치·풀리지 않는 차단 |

수신자는 자기 배정 범위를 벗어난 요청을 받으면 수행하지 않고 `BLOCKED` 로 관리 세션에 올린다.

### 1-2. 메시지 규약(첫 두 줄)

```
결론: 인증 응답 expires_at 단위가 초인지 밀리초인지 확인 부탁
[cw] task=T17 thread=T17-a type=ASK turn=1/4 reply=yes from=백엔드
- 배경: docs/auth-contract.md 에 단위 누락
- 필요한 답: 단위 + 근거 파일 경로
```

- `type`: `ASK / ANSWER / REVIEW / RESULT / BLOCKED` 5종만. 본문 5줄 이내, 긴 건 파일 경로.
- 답장은 같은 `task/thread`, `turn` +1. `reply=no` 에는 수신 확인을 보내지 않는다.
- `notify_when_idle` 은 답이 필요한 요청에만. **유휴 통지 ≠ 완료**.

### 1-3. 폭주·루프·교착 방지(프롬프트 규칙 — 최선 노력, 강제 아님)

- **1-hop**: 받은 요청을 제3자에게 넘기지 않는다(전달 사슬 금지). 필요하면 BLOCKED 로 올린다.
- **스레드당 최대 4회 전송**(질문→답→보충→최종). 넘으면 owner 가 관리 세션에 BLOCKED.
- 같은 상대에게 같은 질문 재전송 금지, 무응답 재전송 금지, 폴링 금지(v1 그대로).
- 답을 기다리는 동안 독립 작업을 계속. 할 일이 없어지거나 순환 의존이면 BLOCKED **1회**.
- 침묵 = 동의·완료가 아니다. 완료 책임은 owner 에게서 옮겨가지 않는다.

### 1-4. 등록부 v2(`.clewpath/workers.json`)

```json
{
  "version": 2, "revision": 12,
  "manager": {"session_id": "<uuid>", "alias": "관리"},
  "workers": {
    "백엔드": {"session_id": "<uuid>", "role": "API·DB", "tags": ["python","fastapi"], "cwd": "F:/proj/api",
              "write_scope": ["api/**", "migrations/**"], "state": "working", "last_dispatch": "…"},
    "프론트": {"session_id": "<uuid>", "role": "웹 UI", "tags": ["ts","react"], "cwd": "F:/proj/web",
              "write_scope": ["web/**"], "state": "idle"}
  },
  "tasks": {
    "T17": {"goal": "토큰 만료 처리", "owner": "백엔드", "collaborators": ["프론트"],
            "write_scope": {"백엔드": ["api/auth/**"], "프론트": ["web/src/auth/**"]},
            "done_when": "만료 401 → 자동 재발급, e2e 통과", "status": "open"}
  },
  "monitor_group_id": "<id>"
}
```

- 키(이름)는 **별칭**일 뿐 주소가 아니다. 보낼 때: 별칭 → `session_id` → `/api/v1/sessions` 의 `peer.name`.
- 워커의 cwd 가 프로젝트 루트와 다를 수 있으므로 **등록부의 절대 경로를 워커에게 알려 준다**(아래 1-5).
- 관리 세션은 배정·변경·완료 때만 갱신(`revision` +1). 진행 로그는 넣지 않는다(관리 세션 부담 재발 방지).
- **같은 파일의 편집자는 한 명**(`write_scope`). 남의 범위는 수정 제안(REVIEW)만.

### 1-5. 워커 쪽 규칙(지금 스킬에 없는 것)

v1 스킬은 관리 세션 관점만 있다. v2 는 같은 SKILL.md 에 **「워커 모드」 절**을 둔다.

- 새 워커 생성 프롬프트에 한 줄 추가: "너는 '<역할>' 워커다. 동료와의 통신은 clewpath-workers 스킬의 워커 규칙을 따른다. 등록부: `<절대 경로>`, 너의 별칭: `<별칭>`."
- 기존 워커에는 관리 세션이 온보딩 메시지 1회.
- 워커는 동료를 찾을 때 등록부를 **보낼 때만** 읽는다(주기 조회 금지).

### 1-6. 사전 점검(관리 세션이 P2P 를 켜기 전)

- 모든 워커·관리 세션의 **권한 모드가 같아야** 한다. 다르면 워커 간 메시지가 사용자 승인 대기에 걸린다(v1 규칙 3 을 워커끼리에도 적용).
- 워커 전원이 관제 그룹(`subs`)에 있어야 호출선이 그려진다.

### Phase 1 성공 기준

같은 종류의 일감을 v1(중앙 경유)과 v2(직접)로 한 번씩 돌려 **전체 세션 토큰 합·완료 시간·재작업(반려) 수**를 비교한다(관리 세션 수신 수만 보면 당연히 줄어 의미 없음 — Codex). 완료 책임 누락·편집 충돌은 늘지 않아야 한다.

## Phase 2 — 고도화(필요가 확인되면)

| 항목 | 내용 | 크기 |
|---|---|---|
| Host 팀 API | `GET /api/v1/team?registry=<경로>`: 등록부 + 살아 있는 이름을 한 번에(워커의 2단 조회 제거) | S |
| 관제 역할 표시 | 등록부 role 을 관제 그룹 labels 로 미러, 타임라인을 `task/thread` 로 묶기 | S |
| 위반 감지 알림 | monwatch 가 jsonl 에서 턴 초과·무응답 ASK·전달 사슬·순환을 감지해 웹푸시(강제는 못 하므로 '감지+알림') | M |
| 업무 트리 | `tasks[].parent` + 업무 보드 화면(대기/진행/차단/완료) | M |
| 편집 충돌 | 워커별 git worktree 또는 경로 예약 | M |
| 비용 집계 | 업무별 토큰 사용 집계·경고(세션 jsonl usage 합) | M |

## 기각·보류

- **세션 트리(다단계 위임)**: 3~8 워커 규모에서 중간 관리 비용만 늘어 기각. 업무 트리는 Phase 2.
- **워커가 등록부를 직접 수정**(Gemini 의 board.md 자율 분배 포함): 동시 쓰기 충돌·책임 불명. 관리 세션 단일 작성자 유지. 업무 보드는 Phase 2 에서 Host API(트랜잭션) 뒤로.
- **응답 타임아웃**: claude 세션은 시간을 잴 수 없다 — '턴 수' 와 'BLOCKED 1회' 로 대체(강제는 Phase 2 감지 알림).
- **`.clewpath/locks/` 빈 파일 잠금**(Gemini): 프로세스가 죽으면 잠금이 남는다. Phase 1 은 `write_scope` 배정으로 충분.

## 엔지니어링 리뷰 (단계 1, /plan-eng-review 2026-10-01)

### 결정

| # | 결정 | 이유 |
|---|---|---|
| E-1 | **저장 = SQLite 1개**(`<데이터 폴더>/team.db`, stdlib sqlite3, WAL) | 이력이 제품. `jsonl_log` 는 8MB 에서 한 세대만 남김(`jsonl_log.py:14`) → 경력 유실. SQL 로 비서가 바로 분석 |
| E-2 | **팀 API 는 읽기·쓰기 모두 로컬 전용**(`_LOCAL_ONLY_API` 에 `/api/v1/team` 전 동사) | 사장님: 폰이 API 를 직접 부를 일이 없다 — 필요하면 릴레이로 **세션에 요청**하고 세션이 PC 안에서 API 를 부른다. 원격 출발 작업도 세션 경유라 장부에는 항상 남는다 |
| E-3 | **관제 그룹: 구성원(manager·subs·역할 라벨)만 팀이 정본**, 이름·알림 설정은 사용자 값 유지. 워커 세션 교체는 에이전트의 현재 세션을 continued-in 사슬로 찾아 그룹도 갱신 | 지금 monwatch 는 관리 세션만 따라감(`monwatch.py:115`) — 워커가 교체되면 그룹에서 빠지는 구멍을 같이 막는다 |
| E-4 | **SKILL.md ↔ 라우트 대조 테스트**: 문서의 모든 `/api/…` 경로가 FastAPI 라우트에 존재 | 에이전트가 읽는 문서라 어긋나면 조용히 실패 |
| E-5 | **로컬 전용은 서버 라우트에서 검사**(`_is_local(request)` 아니면 403, 전 동사) + 커넥터 목록에도 추가(이중) | 커넥터 목록은 기기 API 외엔 POST 만 막음(`connector.py:107`) — Codex |
| E-6 | **전송 주소 = 파이프 주소 `uds:<messagingSocketPath>`**(팀 GET 이 이름과 함께 줌), 이름은 표시용 | 이름은 동명이면 모호(`peers.py:222`), 교체 중 경쟁 — Codex |
| E-7 | **관제 그룹 = 목표 상태 맞추기**(DB 가 원하는 구성원 → `mongroups` 에 적용, 실패면 `monitor_sync:'pending'` 응답 후 다음 변경·기동 때 재시도, 그룹 id 는 DB 에 1개만 → 중복 생성 없음) | DB 성공·그룹 실패의 부분 성공 — Codex |
| E-8 | **관제 화면 상한 8→12** + 열린 관제 화면에 구성원 변경 반영(그룹 변경 시 열린 webmonitor 에 add/remove) | `webmonitor.py:28` 상한 8, 열린 화면은 연결 시 목록 고정 — Codex |
| E-9 | **일감 상태기계**: `assigned → submitted → accepted / rejected → (reopened → assigned)`, 이벤트·상태는 **같은 트랜잭션**, 쓰기 요청에 `idem_key`(재시도 중복 방지), 배정마다 `assignment_ver` +1 → 옛 담당자의 제출은 409 | RESULT 와 승인 구분·재시도·재배정 경쟁 — Codex |
| E-10 | **에이전트를 팀에서 분리**: `agents`(전역 페르소나: 별칭·역할·태그) + `memberships(team, agent, role, write_scope, joined, left)` | 팀 이동·여러 팀 이력 보존 — 경력의 단위가 에이전트 — Codex |
| E-11 | **쓰기 권한 = 프롬프트 약속 + 기록**(강제 토큰 없음). `actor_session` 은 자기 신고로 표시 | 사장님 6B. **업그레이드 조건**: 비서가 사람 승인 없이 여러 팀을 관리하거나 다른 사용자 세션이 생기면 팀별 관리자 토큰(결정 로그 e2457b66) |
| E-12 | **일감 기한 감지**: 일감 `due`(선택) 를 Host 가 재고, 넘기면 관리자 세션에 웹푸시(monwatch 경로 재사용). ASK 단위 감지는 단계 2 | 무응답이면 '4턴' 이 작동 안 함 — Codex |
| E-13 | **산출물 증거**: 제출 시 Host 가 산출물 파일의 sha256·크기·mtime 과 그 폴더의 git HEAD(있으면)를 이벤트에 기록 | 경로만 남기면 덮어쓴 파일을 당시 산출물로 오인 — 경력 데이터 신뢰도 — Codex |

### 데이터 흐름

```
 관리 세션 ──curl(127.0.0.1)──▶ Host /api/v1/team/*  ──▶ team.db (teams·agents·agent_sessions·tasks·events)
   │  배정·결과 기록                    │                          ▲
   │                                   ├─ peers.snapshot(2s) ── 살아 있는 이름
   │                                   ├─ scanner continued_in ─ 현재 세션(사슬 끝) → agent_sessions 에 append
   ▼                                   └─ mongroups.save(구성원만) ── 관제 그룹(호출선·알림)
 워커 A ──GET team(보낼 때만)──▶ 이름 ──SendMessage──▶ 워커 B   (관리 세션 컨텍스트 무유입)
                                                    └─ 관제가 호출선으로 표시(monitor.py:189)
 폰 ──릴레이──▶ (팀 API 직접 불가, E-2) ──▶ 관리/비서 세션에 말로 요청
```

### 테이블(초안)

- `teams(id, name, root, manager_agent, parent, monitor_group_id, monitor_sync, created, archived)`
- `agents(id, alias, role, tags_json, status, created, retired)` — 전역 페르소나(E-10)
- `memberships(team_id, agent_id, role, write_scope_json, joined, left)` — 팀 안 별칭 유일
- `agent_sessions(agent_id, session_id, started, ended, reason)` — `session_id UNIQUE`(한 세션은 한 에이전트), reason=`created|continued|replaced|imported`
- `tasks(id, team_id, goal, owner_agent, collaborators_json, write_scope_json, done_when, due, status, assignment_ver, parent, created, closed)` — id 는 `<팀 약칭>-T<n>`, status 는 E-9 상태기계
- `events(id, ts, team_id, task_id, agent_id, kind, actor_session, idem_key UNIQUE, payload_json)` — append 전용(수정·삭제 API 없음), 제출 이벤트 payload 에 산출물 sha256·git HEAD(E-13)

### 테스트 범위

```
[+] session_manager/team.py
  ├── 팀/에이전트 생성·중복 별칭 거절·세션 바인딩 멱등(같은 session_id 두 번)   [GAP→단위]
  ├── 한 세션을 다른 에이전트에 붙이기 → 409                                    [GAP→단위]
  ├── current_session: continued-in 사슬 2단 → 끝 세션 + agent_sessions append   [GAP→단위]
  ├── 일감 배정/결과 → events append, 상태 전이(open→done/blocked)            [GAP→단위]
  ├── v1 workers.json 가져오기(정상·이미 존재·손상 파일)                        [GAP→단위]
  └── DB 손상/잠김 → 503 + 기동 유지(다른 기능 무영향)                           [GAP→단위]
[+] server.py /api/v1/team/*
  ├── GET 팀: 살아 있는 이름 포함(peers 스텁) · 없는 팀 404                      [GAP→API]
  ├── 릴레이 경유 모든 동사 차단(_is_local_only_api)                            [GAP→API, 보안]
  └── 구성원 변경 → mongroups 구성원 갱신, 이름·notify 는 보존                   [GAP→API]
[+] skills/clewpath-workers/SKILL.md
  └── 문서의 /api/… 경로 전부 라우트 존재(E-4)                                  [GAP→단위]
USER FLOW: 실사용 1회(워커 2 + 관리 1, ASK/REVIEW 왕복, 관제 호출선, 장부 배정·결과) [→E2E 수동]
```

### 테스트 범위 추가(E-5~E-13)

```
  ├── 서버 라우트 로컬 검사: 비루프백 클라이언트 GET/POST 모두 403 (E-5)             [GAP→API, 보안]
  ├── 팀 GET 의 address = uds:<pipe>, 살아 있지 않으면 address=null·live=false (E-6) [GAP→API]
  ├── 그룹 맞추기: mongroups.save 예외 → monitor_sync=pending, 재호출 시 같은 그룹 id (E-7) [GAP→단위]
  ├── webmonitor 상한 12 · 그룹 변경이 열린 관제에 add/remove (E-8)                 [GAP→단위]
  ├── 상태기계: 허용 전이만, 같은 idem_key 재요청 = 같은 결과, 옛 assignment_ver 제출 409 (E-9) [GAP→단위]
  ├── 에이전트 팀 이동: 옛 membership left 기록, 이력 유지 (E-10)                    [GAP→단위]
  ├── 기한 초과 일감 → 알림 1회(중복 없음), 제출되면 해제 (E-12)                     [GAP→단위]
  └── 제출 증거: 파일 sha256·크기, git HEAD(저장소 아니면 null), 없는 파일은 missing 표기 (E-13) [GAP→단위]
```

### NOT in scope (단계 1)

- 워커 간 ASK 단위 무응답·턴 초과 감지, `[cw]` 헤더 인덱서 — 단계 2(jsonl 읽기 인덱서와 함께).
- 폰/원격에서 팀 API — 사장님 결정 E-2(세션 경유로 충분).
- 쓰기 권한 강제(팀 토큰) — E-11, 업그레이드 조건부 TODO.
- 일감별 비용 집계·경력 화면·업무 보드 UI — 단계 2.
- MCP 비서 도구·포트폴리오 층 — 단계 3(스키마에 `teams.parent` 자리만).

### What already exists (재사용)

| 필요 | 기존 코드 | 처리 |
|---|---|---|
| 세션 교체 추적 | scanner `continued_in`(`scanner.py:110`) · monwatch 관리자 추적(`monwatch.py:115`) | 사슬 따라가기 재사용, 워커까지 확장 |
| 살아 있음·주소 | `peers.snapshot`(socket 포함) | 팀 GET 에서 조인 |
| 관제 그룹 | `mongroups.save/set_manager` | 목표 상태 맞추기로 호출 |
| 워커 기동 | `POST /api/sessions/<id>/terminal/start`(멱등) | 그대로 |
| 웹푸시 | monwatch `notify_error` 경로 | 기한 초과 알림에 재사용 |
| 스킬 배포 | `skillinstall`(digest 비교·덮어쓰기 확인) | 그대로 |

### Failure modes

| 경로 | 현실적 실패 | 테스트 | 처리 | 사용자에게 |
|---|---|---|---|---|
| team.db | 손상·잠김 | O | 503 + Host 기동 유지 | 스킬이 BLOCKED 보고 |
| 그룹 맞추기 | mongroups 쓰기 실패 | O | pending + 재시도 | 응답에 pending |
| 주소 조회 | 조회 직후 세션 교체 | O(교체 사슬) | SendMessage 실패 → 재조회 1회 → BLOCKED | 관제 호출 실패 알림(기존) |
| 제출 | 재시도 중복·옛 담당자 | O | idem_key·assignment_ver 409 | 409 사유 문구 |
| 증거 | 산출물 파일 없음 | O | missing 표기, 제출은 받음 | 이벤트에 missing |
| 기한 | Host 재기동 사이 초과 | O | 기동 시 1회 재평가 | 알림 1회 |

**critical gap 0**(모든 경로에 테스트 또는 처리 있음).

## Implementation Tasks

- [ ] **T1 (P1, CC ~40분)** — `session_manager/team.py`: SQLite 스키마(teams·agents·memberships·agent_sessions·tasks·events, WAL·user_version 마이그레이션), CRUD·상태기계(E-9)·idem_key·assignment_ver·팀 이동(E-10)
- [ ] **T2 (P1, CC ~20분)** — 현재 세션 해석(continued-in 사슬 → agent_sessions append) + 주소(`uds:`)·live 조인(E-6)
- [ ] **T3 (P1, CC ~25분)** — `server.py` `/api/v1/team/*` 라우트 + 서버측 로컬 검사(E-5) + 커넥터 목록 이중화 + v1 `workers.json` 가져오기
- [ ] **T4 (P1, CC ~20분)** — 관제 그룹 목표 상태 맞추기(E-7) + 기동 시 재동기화 + webmonitor 상한 12·열린 화면 반영(E-8)
- [ ] **T5 (P2, CC ~20분)** — 제출 증거(sha256·git HEAD, E-13) + 일감 기한 감지 웹푸시(E-12, monwatch 경로)
- [ ] **T6 (P1, CC ~30분)** — 스킬 v2(관리자/워커 모드·P2P 규칙·`[cw]`·팀 API 사용법·사전 점검·v1 이전) + 문서↔라우트 대조 테스트(E-4)
- [ ] **T7 (P1)** — 테스트 전부(위 두 다이어그램) · 실사용 1회 v1/v2 비교(토큰·시간·반려)

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 (plan) | issues_found → 결정에 반영 | 10건: 7건 일괄 반영(E-5~E-10), 2건 단계 1 로 앞당김(E-12·E-13), 1건 사장님 6B(E-11, 업그레이드 TODO) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (2026-10-01, FULL_REVIEW) | 13 decisions(E-1~E-13), 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | 단계 1 은 UI 없음(관제 상한·열린 화면 반영만) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** Codex 10건 중 코드로 확인된 3건(커넥터 POST 만 차단·루프백 인증 생략·관제 상한 8) 포함 전부 결정에 흡수. 쓰기 권한만 사장님이 약속+기록(6B) 선택.
- **VERDICT:** ENG CLEARED — ready to implement (단계 1 T1~T7).

NO UNRESOLVED DECISIONS
