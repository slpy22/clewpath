# 멀티 세션·멀티 에이전트 모니터링 설계

- 작성: 2026-09-03 (office-hours)
- 대상 버전: v0.6.0 (현재 v0.5.0)
- 상태: 기획 확정 → 다음 단계 plan-eng-review

## 1. 문제

관리 에이전트 세션 1개가 하위 세션(포털 개발, 논문 서비스 등) N개를
`claude -p --resume <UUID> "프롬프트"` 로 직접 호출하며 프롬프트를 주고받아
프로젝트를 오케스트레이션한다. 사장님은 이 **에이전트 간 소통을 실시간으로
관전**하고 싶다.

기존 ClewPath 는 **1:1 단일 세션 관리**(내가 한 세션에 지령 → 응답 확인)에
머물렀다. 이번 기능은 **1:N 오케스트레이션 관제**(관리 에이전트가 여러 세션을
부리는 것을 위에서 감독)로, 관찰 대상이 "나↔세션"이 아니라 "**에이전트↔에이전트**"로
바뀐다. 이 관점 전환이 UI/UX 를 가른다.

### 왜 기존 도구로 안 되나
- **터미널 재개 불가**: 하위 세션은 이미 외부 프로세스(관리 에이전트의 `claude -p`)가
  점유 중 → 재개하면 충돌.
- **뷰어는 스냅샷**: 현재 뷰어(`fetchRecentMessages`)는 정지 화면이라 실시간성 없음.

## 2. 기술 전제 (확정)

- 세션 jsonl 은 **append-only** → 실시간 tail = 충돌 없는 읽기전용 관전 가능.
- 관리 에이전트가 Bash 로 `claude -p --resume <UUID> "프롬프트"` 실행 시:
  - **관리 세션 jsonl**: assistant 메시지의 `tool_use`(name=Bash)에 그 명령(대상
    UUID + 프롬프트)이, 대응 `tool_result` 에 하위의 최종 응답 stdout 이 남는다.
  - **하위 세션 jsonl**: 그 프롬프트가 user 메시지로 append, 내부 진행(파일 수정,
    검색 진행 등)이 이어서 append 된다.
- 따라서 **관리 세션만 봐도 호출→응답 뼈대는 복원**되고, 하위 세션까지 병합하면
  **각 에이전트 내부 진행**까지 보인다.

## 3. 확정된 설계 결정 (office-hours 3문)

| 결정 | 선택 | 대안(기각) |
|---|---|---|
| **화면 근본 형태** | 오케스트레이션 타임라인 (시간축 단일, 호출선 `→` 1급 시민) | 관제실(병렬 컬럼) / 단일뷰어+탭 |
| **데이터 소스** | 관리+지정 하위 세션 N+1개 jsonl 실시간 tail 후 timestamp 병합 | 관리 세션 하나만 / 완전 자동발견 |
| **Phase 1 범위** | 호출선(`→`) 자동 연결 포함 완본, 파싱 실패 시 화살표 생략(우아한 저하) | 병합만(호출선 Phase2) / 개입까지 초판 |

### 화면 개념 (타임라인)
```
그룹: [관리 에이전트] [포털 개발] [논문 서비스]         ● live  ⟳자동스크롤
──────────────────────────────────────────────────────
● 09:01 [관리]→[포털]  "로그인 모듈 붙여줘"        (Bash 파싱)
    └ 09:02 [포털]  auth.py 생성                   (하위 tail)
    └ 09:05 [포털]  pytest 5 passed                (하위 tail)
    └ 09:05 [포털]→[관리]  "완료, 테스트 통과"      (tool_result)
● 09:06 [관리]→[논문]  "인용 5건 교차확인"
    └ 09:08 [논문]  검색 중 3/5
    └ 09:11 [논문]→[관리]  "2건 불일치 발견"
● 09:12 [관리]  종합: 포털 OK, 논문 수정 지시
```
- 세션마다 고정 색 + 이름. 호출선 `→` 은 source/target 세션을 잇는다.
- 시간순 단일 축. 하위 이벤트는 자신을 유발한 호출 아래로 들여쓰기(그룹핑).

## 4. 아키텍처 (초안 — plan-eng-review 에서 확정)

### 4.1 오케스트레이션 그룹
- 사장님이 세션 목록에서 "이 그룹"을 정의: 관리 1 + 하위 N.
- Phase 1 은 **세션 내 임시 그룹**(뷰 진입 시 선택). 저장·재사용은 Phase 2.

### 4.2 실시간 tail (Host)
- Host 가 그룹의 각 jsonl 을 watch: mtime 폴링(기존 스캐너와 동일 축) 또는 watchdog.
- 새로 append 된 줄만 증분 파싱 → 이벤트로 변환 → 뷰어로 push.
- **불가침 원칙 준수**: 읽기 전용. jsonl 절대 수정 안 함.

### 4.3 전송 (뷰어 ↔ Host/relay)
- 기존 뷰어 폴링 스냅샷을 실시간 스트림으로 승격. 후보: SSE 또는 WebSocket.
- 로컬(5100) 직결과 원격(relay 8787 경유) 모두 지원해야 함 — relay 는 blind 유지.
- **plan-eng-review 결정 사항**: SSE vs WS, relay 프레이밍, 재연결/백프레셔.

### 4.4 호출선 파싱 (Phase 1 핵심, 리스크)
- 관리 세션 jsonl 의 `tool_use`(Bash) command 에서 추출:
  - 패턴: `claude ... (-p|--print) ... (--resume|-r) <UUID> ... <프롬프트>`
  - `<UUID>` → 그룹 내 세션 매핑 → 호출선 source=관리, target=매핑세션.
  - 대응 `tool_result` → 응답 텍스트.
- **매핑 실패 시**: 그 줄은 화살표 없이 시간축에만 표시(저하). 절대 크래시 안 함.
- ⚠️ **실측 필요**: 실제 관리 세션 jsonl 에서 Bash 명령이 어떻게 기록되는지
  (한 줄 command? 프롬프트 이스케이프? heredoc?) 를 개발 착수 시 캡처 검증.

## 5. Phase 구분 (devflow 필수)

### Phase 1 (초판) — v0.6.0
- [ ] 오케스트레이션 그룹 임시 지정 UI (세션 목록에서 관리 1 + 하위 N 체크)
- [ ] Host: 그룹 jsonl 다중 실시간 tail (증분 파싱, 읽기전용)
- [ ] 전송: 뷰어 실시간 스트림 (SSE/WS — plan-eng-review 확정)
- [ ] 타임라인 뷰: timestamp 병합, 세션별 색·이름, 자동 스크롤, live 배지
- [ ] 호출선(`→`) 자동 연결 (관리 세션 Bash 파싱) + 매핑 실패 시 우아한 저하
- [ ] 하위 이벤트를 유발 호출 아래로 들여쓰기 그룹핑
- [ ] 회귀 가드: 읽기전용 보장 테스트(jsonl 무수정), 파싱 실패 폴백 테스트

### Phase 2 (고도화) — 대기
- [ ] **개입**: 관전 중 관리 에이전트에 사이드 프롬프트 주입 (⚠️ claude 동작에
      영향 → 불가침 원칙 사전 검토 필수)
- [ ] 오케스트레이션 그룹 **저장·재사용** (labels sidecar 확장)
- [ ] 새 이벤트/완료/에러 **알림**(FCM 연동, 기존 push 인프라 재사용)
- [ ] 타임라인 필터(에이전트별 접기, 호출선만 보기 등)

### Deferred (수요 확인 후)
- [ ] E2EE 룸 하의 원격 관제 정합성 (기기 페어링 보안과 연계)
- [ ] 완전 자동 발견 모드(`claude agents --json` 기반 그룹 자동 추정)
- [ ] 관계 그래프 뷰(타임라인 대신 노드-엣지 토폴로지)

## 6. 위험·미해결

1. **호출선 파싱 정확도** — 최대 리스크. 개발 착수 즉시 실측 캡처로 검증.
   실패해도 폴백(화살표 생략)이 있어 기능 자체는 성립.
2. **실시간 전송 방식** — SSE/WS 결정은 plan-eng-review 로.
3. **relay blind 유지** — 원격 관제 시에도 relay 는 내용 못 보게. 로컬 우선 구현
   후 원격 확장.
4. **개입 기능(Phase 2)** — claude 상태 변경 소지 → CLAUDE.md 불가침 원칙상
   구현 전 사장님 명시 승인 필요.

## 7. 다음 액션
- devflow STEP 4: 개발 착수. **첫 작업 = 호출선 파싱 실측 스파이크**(이슈 5 참조).
  스파이크 통과 후 monitor.py → server.py → connector.py → pwa 순.

---

## 8. plan-eng-review 확정 (2026-09-03)

### 8.1 잠근 결정
| # | 결정 | 확정 | 근거 |
|---|---|---|---|
| 1 | 실시간 전송 | **WebSocket 재사용** | webterm WS + relay `_pipe_terminal` 을 읽기전용 단방향으로 재사용. connector 에 monitor 메서드만 추가 |
| 2 | 병합 위치 | **Host 서버측** | 그룹 jsonl 이 전부 로컬 → tail·병합·호출선 파싱을 서버에서, relay 터널 1개로 |
| 3 | tail 메커니즘 | **오프셋 증분 폴링** | 파일별 바이트 오프셋 유지, ~300-500ms 주기 size 변화 시 증분 읽기. watchdog 의존성·Windows 불안정 회피 |
| 4 | 호출선 파싱 | **viewer 파서 추출 + tool_use_id 상관** | `read_conversation` 라인 파서를 공유 `parse_record` 로 추출(DRY), `_tool_results` 의 tool_use_id 짝맞춤 재사용 |
| 5 | 리스크 딜리스크 | **착수 즉시 실측 스파이크** | 관리 세션 jsonl 의 Bash `claude -p --resume <UUID>` 기록 형태를 먼저 캡처 검증 후 파서 스키마 확정 |
| 6 | 버퍼 상한 | **webterm 링버퍼+리플레이** | 최근 N 이벤트만 링버퍼(메모리 상한), 재연결 시 tail 리플레이. 전체 이력은 스냅샷 뷰어로 별도 조회 |

### 8.2 데이터 흐름
```
관리 세션 jsonl ─┐
포털  세션 jsonl ─┼─▶ monitor.py: N-way tail(오프셋) ─▶ merge(시간축)
논문  세션 jsonl ─┘                                      │
                                                         ▼
                              parse_callline (Bash regex + tool_use_id 상관)
                                                         │  실패→화살표 생략(저하)
                                                         ▼
        링버퍼(상한) ─▶ /ws/monitor/{group} ─▶ relay _pipe_monitor(단방향)
                                                         ▼
                                               PWA 타임라인(자동스크롤)
```

### 8.3 실측 실패 시나리오 (신규 코드패스별)
| 코드패스 | 실패 모드 | 테스트 | 에러처리 | 사용자 체감 |
|---|---|---|---|---|
| tail_offset | 파일 트렁케이트/회전 → 오프셋>size | 필수 | size 감소 감지 시 오프셋 리셋 | 이벤트 잠깐 재출력(무해) |
| parse_callline | Bash 기록 형태가 예상과 다름 | 스파이크+단위 | 매핑 실패→화살표 생략 | 호출선 없이 시간축만(저하) |
| merge_events | timestamp 역전/누락 | 필수 | 안정 정렬, 누락은 도착순 | 순서 미세 흔들림 |
| ws/monitor | 대상 세션 삭제/이동 중 | 필수 | 스트림 eof + 안내 | "세션 종료" 표시 |
| relay 터널 | 원격 연결 끊김 | 필수 | eof/error 전파 + 재연결 | live 배지 꺼짐→재연결 |

**크리티컬 갭 없음**: 모든 실패가 테스트+에러처리+명시적 사용자 표시를 가짐(무성 실패 없음).

### 8.4 NOT in scope (Phase 1 명시 제외)
- **개입**(사이드 프롬프트 주입) → Phase 2, claude 동작 영향이라 불가침 원칙 사전 승인 필요
- **그룹 저장·재사용** → Phase 2, 우선 세션 내 임시 그룹
- **알림(FCM)** → Phase 2
- **완전 자동 발견**(agents --json 그룹 추정) → Deferred, 오탐 위험
- **관계 그래프 뷰**(노드-엣지) → Deferred, 타임라인으로 충분

### 8.5 병렬화 전략
```
Lane A: monitor.py (tail+merge+parse) — 독립, 순수 로직, 단위테스트 가능
Lane B: pwa 타임라인 뷰 — 독립(목 이벤트 스트림으로 개발)
Lane C: server.py WS + connector.py relay — A 의 이벤트 스키마에 의존
실행: A·B 병렬 착수 → 스키마 확정 → C. 단 스파이크(파서 실측)는 A 최선두.
```

---

## 9. 파서 실측 스파이크 결과 (2026-09-03, claude 2.1.259) — 리스크 제거됨

재현: scratchpad 격리 dir 에서 `claude -p "..."` 로 하위 생성 → `claude -p --resume
<UUID> "..."` 로 관리 호출 → 양쪽 jsonl 실측 후 throwaway 정리.

### 9.1 관리측 (호출선 → 소스)
관리 세션 jsonl 의 assistant 레코드에 다음 구조로 기록됨(스키마 확정):
```
message.content[] = { "type":"tool_use", "name":"Bash",
                      "id":"toolu_...",                    ← 상관 키
                      "input":{"command":"...claude -p --resume <UUID> \"프롬프트\"..."} }
```
- `input.command` 은 단일 JSON-escaped 셸 문자열. `cd ... &&` prefix 가 붙어도 됨.
- 대응 tool_result: 이후 user 레코드의 `content[]` 에 `{"type":"tool_result",
  "tool_use_id":"toolu_...", "content":<하위 stdout>}`. **viewer.py `_tool_calls`/
  `_tool_results` 가 이미 이 짝맞춤을 함 → 재사용 확정.**

### 9.2 하위측 (내부 진행 + 응답 소스)
재개 프롬프트가 **정상 `user` 레코드로**, 응답이 **정상 `assistant` 레코드로** append:
```
… user 'Respond … READY'  → assistant text='READY'        (첫 -p 생성)
… user 'Reply … STEP2 DONE' → assistant text='STEP2 DONE'  (관리 재개 호출)
```
→ 하위 tail 은 추출한 공유 `parse_record`(user/assistant text+tool) 를 **그대로** 사용.

### 9.3 확정된 정규식 (3가지 변형 모두 통과)
```python
# 1차: -p 헤드리스 명시
r"claude\b.*?(?:-p|--print)\b.*?(?:--resume|-r)\s+([0-9a-fA-F-]{36})"
# 폴백: -p 없는 형태도 포착 (플래그 순서 무관)
r"claude\b.*?(?:--resume|-r)\s+([0-9a-fA-F-]{36})"
```
검증: `claude -p --resume <U> "x"` / `claude --resume <U> -p` / `cd foo && claude
-p --resume <U> "멀티\n프롬프트"` 전부 UUID 추출 성공.

### 9.4 설계 단순화 (스파이크가 밝힌 것)
- **호출선(→)+프롬프트 = 관리 tool_use command 에서** (UUID + 프롬프트).
- **하위 응답+내부 진행 = 하위 세션 자체 jsonl tail 에서** (깨끗한 assistant/tool).
- 관리 tool_result 내용은 응답 표시에 **불필요**(하위 jsonl 이 더 깨끗). 완료·타이밍
  상관용으로만 선택 사용. → 파서가 tool_result stdout 형식(json vs plain)에 안 얽매임.

### 9.5 스파이크가 드러낸 새 메타 레코드 (타임라인 파서가 무시)
`queue-operation`, `attachment`, `atis-latch`, `last-prompt`, `mode` — user/assistant
아닌 타입은 기존 뷰어처럼 skip. **별도 발견(모니터링 범위 밖, 2026-09-03 검증 완료)**:
`mode` 레코드는 `--resume` 호출이 남기는 것이고 **순수 `-p` 산물이 아님**. 실측:
순수 `-p`(재개 없음)=mode 0개→picker_hidden=True(정확). `-p --resume`(헤드리스
재개)=mode 1개→picker_hidden=False. **일반 케이스 회귀 없음, 스캐너 규칙 건재.**
헤드리스로 재개된 세션만 picker_hidden=False(피커 실측 미확인이나 저영향 — 재개
가능한 세션이라 오히려 타당할 수 있음). 오케스트레이션 대상 하위 세션은 반복 재개돼
picker_hidden=False 로 뜸(정상 세션처럼, 흐리지 않게) — 관제엔 무해.

**결론: 최대 리스크(호출선 파싱) 제거됨. 파서 스키마 확정, 구현 언블록.**

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 5 decisions locked, 0 critical gaps |

- **VERDICT:** ENG CLEARED — 아키텍처 5개 결정 확정, 크리티컬 갭 0. 구현 착수 가능(단 파서 실측 스파이크 최선두).

NO UNRESOLVED DECISIONS

## Phase 2-1. 관제 그룹 저장 + 관제 알림 (v0.8.1, 2026-09-12)

**왜 묶었나**: 관제 그룹은 WS 연결 단위(서버 메모리)라 앱을 닫으면 사라진다. 앱이 닫혀 있어도
"관리 턴 종료·하위 응답 완료" 푸시를 받으려면 Host 가 그룹을 알아야 한다 → 저장이 알림의 기반.

**알림 소스 = 훅(실증)**: `claude -p` 로 호출된 헤드리스 하위 세션도 UserPromptSubmit/Stop/SessionEnd
훅이 Host 에 도달한다(2026-09-12 hooktest 2건: thinking→ready→ended). 따라서 상시 tail 감시 없이
훅만으로 하위 "응답 완료"를 정확히 안다. 에러(호출 실패)는 훅으로 못 보므로 Phase 2-2(관리 tail 의
tool_result is_error)로 미룸.

**설계**
- `mongroups.py`: 데이터 폴더 `monitor_groups.json`(labels 사이드카와 같은 원자 저장·손상 내성).
  {id, name, manager, subs[], labels{}, notify{manager_stop, sub_stop, sub_start}}. 상한 50.
- `push._notify_groups`: 훅 Stop/UserPromptSubmit 수신 시 소속 그룹·역할로 라우팅. **소속 세션은
  그룹 설정이 알림을 결정**(일반 '작업 완료' 중복 방지, sub_stop 끄면 조용). 권한요청은 항상 일반.
  `[push] monitor=false` 면 그룹 알림을 끄고 일반으로 폴백. 페이로드에 `gid`.
- API 는 릴레이 api 프록시가 GET/POST 만 중계하므로 `…/{id}/notify`, `…/{id}/delete` POST 변형.
- 클릭 경로: sw.js `gid` → `#monitor=<gid>`(또는 열린 창에 `open-monitor` 메시지) → PWA 가 목록
  준비 후 저장 그룹의 관제 오버레이를 연다.
- PWA 피커: 저장된 그룹(열기·🔔 토글 3·삭제) + 이름 + "💾 저장하고 관전".

**검증**: 단위 18건(저장소 8·라우팅 8·API 2), 전체 275 passed. 푸시 실수신은 게시 후 폰으로.

## Phase 2-2. 하위 호출 실패 감지 (v0.8.2, 2026-09-12)

**훅의 사각지대**: 관리 에이전트의 `claude -p --resume <하위> "…"` 가 오류로 끝나면(세션 없음·
비-0 종료·타임아웃) 하위 세션의 Stop 훅은 오지 않는다 — 실패는 **관리 세션 jsonl 의 Bash
tool_result(is_error=true)** 에만 남는다. 오케스트레이션에서 제일 먼저 알고 싶은 사건이 이것.

**설계 — `monwatch.py`(Host 데몬 스레드 1개)**
- 대상 = 저장 그룹 중 `notify.error` 켜진 그룹의 **관리 세션 파일 하나씩만**(하위 N 개는 tail 안 함).
  2초 폴링, `Tailer.seek_end()` 부터(과거 오류 재알림 없음), 완성된 줄만 오프셋 증분(읽기 전용 —
  불가침 원칙). 그룹 목록은 폴링마다 다시 읽어 저장·삭제·플래그 변경이 즉시 반영. 파일이 아직
  없으면 30초마다 재탐색.
- 상관: tool_use(Bash) 의 **원본 command**(뷰어 절삭 없이)에서 `parse_calllines` 로 그룹 하위 UUID
  를 뽑아 `pending{tool_use_id → 하위}` 에 두고, 같은 id 의 tool_result 가 is_error 면
  `push.send("mon-error", 하위sid, "[그룹] 라벨 호출 실패", 오류 앞 120자, {gid})`. 그룹 밖 UUID·
  비-claude 명령의 오류는 무시(타임라인과 같은 우아한 저하). 60초 dedupe 는 push.send 공통.
- `notify.error` 기본 on(기존 저장 파일은 `norm_notify` 가 보정), PWA 피커 토글 '🔔 하위 호출 실패'.
  `[push] monitor` 마스터 스위치가 여기도 적용. 알림 클릭 → gid 딥링크(2-1 과 동일 경로).

**검증**: 단위 9건(`test_monwatch.py`), 전체 284 passed. 실제 jsonl 의 tool_result 블록은
`{type, tool_use_id, is_error(bool 항상 존재), content(str)}` 형태임을 로컬 세션 400개에서 확인.
