# TODOS — ClewPath

지속 백로그. office-hours·plan·document-release 가 공통으로 읽는다.
설계 근거: `docs/2026-09-03-multi-session-monitoring-design.md`,
`docs/2026-09-10-multi-terminal-tabs-design.md`

## 진행중 기능: 알림 → 탭 점프 (v0.8.6 PWA)

멀티탭 Deferred 승격(2026-09-15). 지금은 알림 클릭이 세션 **상세 화면**으로 가고(탭이 이미 있으면 그 탭),
거기서 "🖥 터미널로 돌아가기"를 한 번 더 눌러야 한다. 우리 PTY 가 살아 있는 세션(`live_terminal`)이면
알림 클릭 한 번으로 그 터미널 탭까지 간다. 서버 변경 0.

### Phase 1 (이번 사이클) — 구현·릴레이 검증 완료(2026-09-15)
- [x] `jumpToSession(s)`: 탭 있음 → `switchTab` / `live_terminal` → `loadTerminal(s)`(탭 추가) / 그 외 → `loadDetail(s)`(재개 방식·충돌 경고는 상세에서 — 알림으로 새 claude 를 띄우지 않는다). `openSessionById` 는 결정 전에 목록을 새로 받아(stale live_terminal 20초 방지) 판단, `consumePendingOpen`(콜드 스타트 `#open=`)도 같은 경로
- [x] 결정 근거: 설계 §9 "복원 시 터미널 자동 열기 금지"는 **자동** 복원 얘기 — 알림 클릭은 사용자의 명시 동작이라 '🖥 터미널로 돌아가기' 버튼과 같은 등급. 관제 알림(gid)은 기존대로 관제 오버레이
- [x] e2e 발견 버그 수정: `switchTab` 이 "탭이 활성이고 termView.sid 같음"이면 조기 반환 → 상세/목록으로 나간 뒤(탭은 남음) 알림을 누르면 아무 일도 안 일어남. 조건에 `term-mode 실제 표시 중` 추가(화면 없으면 재구축)
- [x] 검증: jscheck 통과. 릴레이 e2e(임시 페어링·던질 세션 `claude -p` 로 생성 후 휴지통): 비실행 세션 → 상세 화면(🖥 돌아가기 버튼 없음·▶ 재개) / 터미널 열어 PTY 스폰 → 탭 제거 → 상세 → 알림 점프 → term-mode·탭 생성·CURRENT_TERM_SID(7초, 대부분 목록 6초) / 재로드 후 탭 복원 상태에서 점프 → 0.95초 전환 / 이미 보는 중 → 즉시 무동작. 정리 후 streams 0·탭 0. ⚠ 미검증: 실제 sw 알림 클릭 경로(`open-session` 메시지 → 같은 함수), 폰
- [x] (발견) 목록 API 6초 — 멀티탭 Deferred 에 기록(거대 활성 세션 재파싱), 이번 사이클 범위 밖

## 참고(완료): 관제 오버레이 안 💾 저장 (v0.8.5 PWA)

모니터링 Phase 2 마지막 UI 항목 승격(2026-09-12). 피커 밖에서 연 그룹(또는 관전 중 세션을 더하고 뺀 그룹)을
오버레이 헤더의 💾 로 Host 에 저장/갱신 — 알림의 근거인 저장 그룹을 관전 중에 바로 만든다.

### Phase 1 (이번 사이클) — 구현·릴레이 검증 완료(2026-09-12)
- [x] 헤더 💾 버튼(`.mon-save`, 34px 아이콘 버튼): 미저장 → 이름 모달(기본: 관리 라벨) → `POST /api/owner/monitor/groups` → 저장됨(강조색). 저장 그룹으로 연 경우(`openMonitorGroup`/피커 "💾 저장하고 관전" 이 `{gid,name}` 전달) → 세션 추가/제거(`group` 메시지) 시 주황 점(`.dirty::after`; 글자 `*` 는 버튼 안에서 줄바꿈돼 CSS 점으로) → 클릭 시 같은 `id` 로 갱신(서버 `mongroups.save(gid=)` 기존 지원, 서버 변경 0)
- [x] 그룹 구성은 오버레이의 현재 `group` 맵(관리 + 나머지 하위, 라벨은 서버가 확정한 세션 라벨) — 관리가 제거됐으면 첫 세션을 관리로
- [x] 검증: jscheck 통과. 릴레이 앱(임시 데스크톱 페어링 → 폐기): ad hoc 2세션 관전 → 💾 → 모달 기본 이름 '관리 라벨' → 저장 → 버튼 saved·툴팁 그룹명·Host 목록에 `e2e-overlay-save`(notify 4개 기본값) → 닫고 저장 그룹으로 재개 → `+ 세션 추가` 로 세션 추가 → 1초 내 dirty 점(8px 주황, 툴팁 "구성이 달라졌습니다") → 💾 → "관제 그룹 갱신됨", subs 에 추가 세션 반영 → 재로드 후 재현. 테스트 그룹 삭제·콘솔 오류 0

## 참고(완료): 관제 타임라인 필터 (v0.8.4 PWA)

모니터링 Phase 2 승격(2026-09-12). 서버 변경 0 — 행은 이미 DOM 에 있으므로 행에 `data-sid`·종류 클래스
(`has-call/has-text/has-tool/has-err`)를 달고 필터는 `.fhid` 토글(재렌더 없음, 새 행은 렌더 시 즉시 적용).

### Phase 1 (이번 사이클) — 구현·릴레이 검증 완료(2026-09-12)
- [x] 필터 바(범례 아래 `.mon-filter`): 종류 단일 선택 `전체 / → 호출선 / 💬 대화 / ⚠ 오류` + 세션별 👁 토글(숨김은 취소선·흐림, 색 점·★ 유지) + 카운터(`전체 M` / 필터 중 `표시 N / 전체 M`). 행은 렌더 시 `data-sid`·`has-call/has-text/has-tool/has-err` 표식 + 즉시 `.fhid` 판정
- [x] 필터는 표시만 거른다 — 자동 스크롤(nearBottom 유지)·우측 뷰어·자동 추적·활동점 무영향. `renderLegend()` 가 `renderFilterBar()` 를 불러 세션 추가/제거·색·라벨 변동 반영. 모바일 칩 36px
- [x] 검증: jscheck 통과. 릴레이 앱(임시 데스크톱 페어링 → 검증 후 폐기)에서 저장 그룹 `048f244c` 오버레이: 실제 스냅샷 46행 — 호출선 0(프라임 tail 에 호출 없음)·대화 10·오류 2 가 각각 클래스와 1:1 일치, 관리 세션 숨김 → 11행, 복원 → 46행, 카운터 문구 일치, 콘솔 오류 0, 스크린샷. ⚠ 미검증: 실시간 증분 행에 대한 필터 적용(코드 경로는 renderEvent 동일), 모바일 실기기

## 참고(완료): 탭 배지 상태 색 + 이벤트 툴팁 (v0.8.3)

멀티탭 Phase 2 승격(2026-09-12, 관제 알림 2-2 다음 우선순위). 서버 변경 0 — 목록의 훅 상태
(`s.runtime` → `sessionBadge()`)와 배지 구독의 모니터 이벤트(role/tools/tool_results)를 탭 칩에 재사용.

### Phase 1 (이번 사이클) — 구현·릴레이 검증 완료(2026-09-12)
- [x] 칩 상태 밑줄: `sessionBadge(최신 SESSIONS 객체)` 색/펄스를 칩 `::after` 밑줄(3px)로 — 작업 중 파랑 깜빡임·권한/입력 대기 주황 깜빡임·완료 초록. 세션 식별색(.ldot)·중지 탭(상태 없음)은 그대로. 탭 칩만 아래 여백 +2px(밑줄 자리). 20초 목록 폴링·loadList 뒤 term-mode 면 `renderTabBarSoon()`(300ms 코얼레싱), 활성 칩 스크롤은 활성 키가 바뀐 렌더에서만(`strip._sel`)
- [x] 툴팁: `상태: …` + 배경 탭 `● 새 출력 N건 · 최근: 도구 실행(Bash, Read, Edit 외)/응답/도구 결과/프롬프트 입력 (방금)`. `tabBadge._on` 이 `t.actN/t.last` 갱신, 전환 시 loadTerminal 이 소거. 모바일은 title 툴팁이 안 뜨므로 색 밑줄만(툴팁은 데스크톱용)
- [x] 검증: jscheck 통과. 릴레이 앱(작업트리 PWA, 데스크톱 임시 페어링 → 검증 후 폐기) 에서 실제 훅 데이터로 JS 구동 검증: thinking 세션 → `st stpulse` `--st:#2f81f7` "상태: 작업 중"; 합성 모니터 이벤트 2건 → `.lact` + "● 새 출력 2건 · 최근: 도구 결과 (방금)"; 메모리 패치로 waiting/ready → 주황 깜빡임/초록 고정, `::after` 3px·stblink; 배지 구독은 termView.main 없으면 생성 안 됨(subs 0); 콘솔 오류 0; 줌 스크린샷으로 밑줄 육안 확인. ⚠ 미검증: 실제 터미널 탭 마운트 상태에서의 주기 재렌더(코드 경로는 renderTabBar 동일), 모바일 실기기 육안
- [x] 로컬 모드(/app)는 Host 0.8.3 묶음 릴리스에 포함(2026-09-12 게시·이 PC 적용)

## 참고(완료): 관제 그룹 저장 + 관제 알림 (v0.8.1)

모니터링 Phase 2 승격(2026-09-12, 우선순위 1 "알림"). 알림은 앱이 닫혀 있어도 와야 하므로 Host 가
그룹을 알아야 한다 → "그룹 저장" 이 기반. 알림 소스 = 이미 모든 세션에서 오는 훅(Stop/Notification)
우선 재사용, 에러는 관제 tail(tool_result is_error) 보강. 발송은 push.py(웹푸시 직발송·dedupe) 재사용.

### Phase 1 (이번 사이클) — 구현 완료(2026-09-12), 푸시 실수신 e2e 는 게시 후
- [x] **실증**: `claude -p` 헤드리스 세션도 훅(UserPromptSubmit→thinking, Stop→ready, SessionEnd→ended)이 Host 에 도달(hooktest 2건) → 하위 "응답 완료"는 tail 없이 훅으로 정확
- [x] 저장소 `mongroups.py`: `monitor_groups.json`(labels 사이드카 패턴, 원자 저장, 손상 내성) — {id, name, manager, subs[], labels{}, notify{manager_stop✓, sub_stop✓, sub_start✗}, created/updated_at}, 상한 50. 테스트 8건
- [x] API(relay 프록시가 GET/POST 만이라 POST 변형): `GET/POST /api/owner/monitor/groups`, `POST …/{id}/notify`, `POST …/{id}/delete`. 테스트 2건
- [x] 알림 엔진 `push._notify_groups`: 소속 세션은 그룹 설정이 결정(관리 Stop→`[그룹] 관리 에이전트 턴 종료`, 하위 Stop→`[그룹] X 응답 완료`, 하위 UserPromptSubmit→`작업 시작`(기본 off)), 일반 '작업 완료' 중복 방지, 권한요청은 항상 일반, `[push] monitor=false` 면 일반으로 폴백. payload `gid`. 테스트 8건
- [x] PWA: 피커에 저장된 그룹(열기·🔔 토글 3개·삭제) + 이름 입력 + "💾 저장하고 관전". sw.js `gid`→`#monitor=<gid>` 딥링크·`open-monitor` 메시지, PWA `PENDING_OPEN_GID`→`consumePendingMonitor`(목록 준비 후 오버레이)
- [ ] 푸시 실수신 e2e(사장님 확인 대기): 0.8.1 적용 후 테스트 그룹 `048f244c` 생성 → 합성 Stop 훅 2건 발송 완료(구독 1개="Windows"=이 PC Chrome, 폰 구독 없음). 확인할 것: 이 PC 알림 2건·클릭 시 `#monitor=` 관제 오버레이·폰 🔔 구독 후 재발송. 끝나면 테스트 그룹 삭제

### 2-2 호출 실패 감지 (v0.8.2) — 진행중
- [x] `monwatch.py`: Host 백그라운드 워처 1스레드. 저장 그룹 중 `notify.error` 켜진 그룹의 **관리 세션 jsonl 만** Tailer 로 2초 폴링(seek_end 부터 — 과거 오류 재알림 없음). tool_use(Bash `claude -p --resume <하위>`, parse_calllines, 원본 command 무절삭) 를 pending{tool_use_id→target} 에 두고, 같은 id 의 tool_result 가 is_error 면 push `mon-error` "[그룹] X 호출 실패"(본문 앞 120자). 그룹 목록은 폴링마다 재로드(저장·삭제·플래그 즉시 반영), 파일 없음→30초 재탐색, 회전 내성(Tailer)
- [x] `notify.error` 플래그(기본 on, 기존 저장 파일은 norm_notify 로 보정) — mongroups DEFAULT_NOTIFY·PWA 토글 라벨 '하위 호출 실패'. server lifespan 에서 start()/stop() (daemon, 기동 비블로킹)
- [x] 테스트 `test_monwatch.py` 9건: 오류→1건(gid·대상·본문), 정상→무발송, 그룹 밖 UUID·비-claude 오류 무시, 과거 이력 미재생, 그룹 없음/플래그 off/삭제 시 tail 없음, 파일 없다가 생김, 3000자 넘는 프롬프트 뒤 UUID, 페이로드+마스터 스위치, 스레드 start/stop. 전체 284 passed
- [x] 실측: 실제 jsonl 의 `claude --resume` 실패 tool_result = `{is_error:true, content:"Exit code 1\nNo conversation found with session ID: …"}` 확인(로컬 세션 400개 + 직접 발생). 0.8.2 게시·이 PC 적용(헬스체크 통과)
- [ ] 푸시 실수신(사장님 확인 대기): 임시 그룹 `ecdec71f`(관리=이 세션 97aeef21, 하위=없는 UUID, 알림은 error 만) 만들고 없는 UUID 호출 → Host 로그에 push 실패 없음. 이 PC Chrome 에 "[e2e-호출실패] 없는하위 호출 실패" 알림 떴는지 확인 후 그룹 삭제
- [x] e2e 발견 버그 수정(Host 0.8.3 에 포함, 2026-09-12 게시): 그룹 API 가 저장 형태 그대로 반환 → 옛 그룹은 error 키 없어 PWA 토글이 꺼진 듯 표시. 읽기 경로 3곳 norm_notify 사본 반환(파일 무수정) + PWA 기본값 폴백(즉시 라이브)

### Phase 2 (고도화) — 대기
- [x] 관제 오버레이 안에서 "💾 저장" 버튼(피커 밖에서 열었을 때) — v0.8.5 사이클로 승격(2026-09-12, 위 진행중 섹션)

## 참고(완료): 탭 즉시 전환 — 탭별 xterm 보유 + 화면 커서 델타 리플레이 (v0.8.0)

멀티탭 Phase 2 승격(2026-09-11, 우선순위 1). 목표: 탭 전환이 즉시(깜빡임·재그리기 없음), 스크롤백
보존, 배경 탭이 놓친 출력은 정확히 이어 붙음. 라이브 WS 는 여전히 전경 1개(모바일 안전).
핵심 결정: "리플레이 억제 플래그" 대신 **screen_id 별 전송 커서**를 서버가 기억 → 재접속 시
못 본 델타만 리플레이(버퍼 초과 시 기존 전체 tail+배너 폴백). 구버전 클라(screen 없음)는 기존 동작.

### Phase 1 (이번 사이클) — 구현·격리 relay e2e 완료(2026-09-11)
- [x] webterm: `_TermSession` `total_len/base_off/screens/client_screen`, `tail_since(off)`, `mark_sent()`(단조·상한 16), reader 가 전송 성공 시 커서 전진. `run_terminal(..., screen_id)`: 커서 있으면 델타만(배너 없음·빈 델타 무전송), 없으면 기존 tail+배너, 버퍼 초과면 tail+"생략" 안내. 새 스폰은 커서 0 등록
- [x] server `/ws/terminal/{sid}?screen=` + connector `_start_terminal` `screen` 전달(urlencode). 테스트 8건(test_webterm_delta.py: 회계·경계·폴백·커서 단조/상한·재접속 선택·reader 전진), 전체 257 passed
- [x] PWA `termView` 탭별 인스턴스(wrap 안 `.thost` 절대배치, `.on` 만 표시): 전환 = 이전 tc 닫기·숨김 → 대상 표시·fit·`screen` 으로 tc. wakeLock/ResizeObserver 뷰당 1개(wrap 관찰), onData/보조키는 전경만, 탭 닫기 `closeTabInstance`. eof 시 핸들러 `close()`(안 하면 `conn.streams` 누수 — e2e 발견)
- [x] 로컬 모드: 탭별 iframe 보유, 표시 시 resize 이벤트. (⚠ 로컬 e2e 미실시 — 코드 검토만)
- [x] screenId 는 **페이지 수명 동안만**(sm_tabs 에 저장 안 함 — 새로고침 뒤엔 버퍼가 비어 전체 tail 이 맞음). 새 스폰 판정 = 복원 시점 `dead`(loadTerminal 이 지우기 전 `wasDead` 포착) 또는 서버의 '세션 종료' 텍스트(`t.ended`). ⚠ `SESSIONS.live_terminal` 로 판정하면 20초 폴링 stale 로 살아있는 탭까지 비움(e2e 실측) — 금지
- [x] e2e(격리 Host 5199 + `SESSION_MANAGER_CLAUDE_HOME` + A/B jsonl 복사 + CP 새 room, 데스크톱 페어링): A/B 왕복·재연결 내내 screenId 불변·**배너 0**·마커 중복 0, 배경 답변이 복귀 시 델타로만 덧붙음(마커 0→2), 정리 후 `conn.streams` 빈 것 확인, 콘솔 오류 0. ⚠ 미검증: 버퍼 초과 폴백(단위테스트만), 모바일 실기기 육안, 로컬 모드

### Phase 2 (고도화) — 대기
- [ ] 배경 탭 메모리 상한(스크롤백 줄 수 조정) 및 탭 수 상한 조정
- [ ] 로컬 모드도 델타 커서 사용(iframe 보유 대신 단일 iframe+델타) — 메모리 절약 필요 시

## 참고(완료): 배경 탭 활동 배지 + relay req 강화 (v0.7.2)

멀티탭 Phase 2 에서 승격(2026-09-11, devflow STEP 6→2). 설계 근거: 멀티탭 설계 문서 §4
(모니터 tail 스트림 재사용 — 라이브 터미널 WS 1 + 모니터 WS 1 = 총 2, 서버 변경 0).

### Phase 1 (이번 사이클) — 구현·relay e2e 완료(2026-09-11)
- [x] 배지 구독기 `tabBadge`: `renderTabBar()` 끝에서 `sync()` — 없으면 `T.monitor` 1개 생성, 있으면 탭 집합과 차분 `add/remove`, eof(재연결) 뒤 다음 sync 가 재구독, `termView.dispose` 에서 close. e2e: streams=2(터미널+모니터), 탭 닫으면 host.log 에 `mon` rid 해체
- [x] 이벤트 필터: webmonitor 는 `add` 직후에도 과거 이벤트를 `events` 로 리플레이(webmonitor.py:80-85)하므로 **구독 시각(CLOCK_SKEW 보정) 이후 ts** 만 인정, snapshot/heartbeat/group/error·전경 탭 무시. e2e: 프라임·add 리플레이로 점등 없음, 배경 B 활동 시 `●`(.lact 7px monpulse), 전환 시 소거
- [x] relay `Conn.req()`: 소켓 미개방 시 즉시 `{ok:false,error:'disconnected'}`. e2e: 끊김 중 `T.list` 0ms 실패(전엔 45s hang)
- [x] relay e2e(데스크톱 페어링, Host 0.7.1 + 작업트리 PWA): 전 시나리오 통과, 콘솔 오류 0. 로컬 모드는 동일 JS(`localT.monitor` 는 관제 오버레이로 기검증)라 별도 e2e 생략
- [x] 회귀: 배지 구독은 자체 handle(`tabBadge.h`)이라 관제 오버레이 handle 과 독립 — 코드 검토로 확인(동시 사용 e2e 는 미실시)

- [x] 후속(2026-09-11, 폰 실기기 피드백 "활성 탭이 너무 작아 터치 어려움"): `@media (max-width:879px), (pointer: coarse)` 에서 탭 칩 44px·글자 .92rem·× 히트 32×38·활성 칩 굵기/배경 강조·`+ 탭` 40px. 파일 규칙 주입 실측으로 검증. 릴레이 앱엔 즉시 라이브, 로컬 /app 은 다음 Host 릴리스에 포함

### Phase 2 (고도화) — 대기
- [x] 배지에 상태 색(작업 중/입력 대기) — v0.8.3 사이클로 승격(2026-09-12, 위 진행중 섹션)
- [x] 배지 이벤트 종류 표시(도구 실행/응답 완료) 툴팁 — v0.8.3 사이클로 승격

## 참고(완료): 탭 전환형 멀티 터미널 뷰어 (v0.7.0/0.7.1)

여러 세션 터미널을 브라우저 탭처럼 열어두고 클릭 전환하며 **작업**(양방향).
설계 근거: `docs/2026-09-10-multi-terminal-tabs-design.md`.
실측 확정(2026-09-10): "1개 제한"은 **PWA UI 제약뿐** — 서버(webterm._ACTIVE
세션별 PTY dict)·릴레이(connector.streams[rid] 다중 스트림)는 이미 멀티 지원.
확정(plan-eng-review 2026-09-10, 교차검증 교정): 탭 전환(독립 세션) / 전경 1개 라이브
WS / **스위칭=단일 xterm 재접속**(보유 xterm은 Phase2). "서버 변경 0"은 **틀림** —
릴레이 stop 프레임·서버측 PTY 상한이 진짜 필수. 상세 §9.

### Phase 1 (초판) — 구현 완료·로컬 e2e 검증(2026-09-10). 남은 것: relay e2e(페어링 기기 필요)·모바일 실기기 육안·배포
- [x] 전역 `_term` 단수 → **`TABS`+`termView`**(mount/attach/detach/dispose). Step1 구조 리팩터(1탭 무변경) → Step2 멀티탭. `_term` 잔존 0
- [x] **단일 xterm 재접속** 스위칭: relay=xterm 1개 `term.reset()`+tc 교체 / local=iframe 1개 `src` 재지향(terminal.html 무수정). e2e: 전환 시 iframe 1개 유지, "다시 연결했습니다" 리플레이 확인
- [x] 탭바 UI: `.mon-legend.term-tabs` — 모니터 범례 칩 CSS 재사용(lchip/ldot/lx/ladd/sel), 가로 스크롤 스트립, hash→hsl 세션색, `+ 탭`=`showAddSessionPicker(opts)` 재사용(열린 탭 제외). 실제 클릭+elementFromPoint 검증
- [x] **릴레이 rid stop 프레임**(2026-09-10): connector `stream_close` 핸들러(rid 단위 task.cancel→로컬 WS close→화면 detach, PTY persist) + e2ee require 게이트 포함 + 클라 `close()`가 프레임 전송. 테스트 3건(test_connector_stream_close.py) + 전체 239 passed. 로컬모드는 실 WS close라 무관.
- [x] **터미널 재접속 스토리**(relay): `Conn.onclose`가 streams에 eof 통지+clear(죽은 핸들러 방치 금지) → `termView.disconnected` → 재연결 성공·`visibilitychange` 시 `reattachForegroundTab()`. 서버 CancelledError→eof는 재연결 클라(다른 커넥션)에 안 닿아 보류. ⚠ relay e2e 미실시(단위테스트+코드검토)
- [x] **서버측 PTY 소프트 상한**(2026-09-10): webterm `_max_live_pty()`(SM_MAX_TERMINALS 기본 8)+`_live_count()`, run_terminal **새 스폰만** 상한(재접속 무관). 테스트 4건(test_webterm_cap.py).
- [x] **stop 경로 2FA 게이트**(2026-09-10): connector `_handle_api` 가 특권 경로(`/terminal/stop`)에 start 와 대칭으로 `_priv_ok` 요구(릴레이 경유 한정, 소유자 로컬 무영향). 테스트 4건(test_connector_stop_2fa.py). [탭 닫기 detach/종료 선택 UI 는 Lane B]
- [x] 탭 닫기 UI(`closeTab`): 모달 '화면만 닫기(PTY 유지)' / '⏹ 세션 종료'. 활성 탭 닫으면 이웃 탭 자동 전환, 없으면 상세로. `stopSessionTerminal()`이 바 ⏹종료와 공유 — **원격은 ensurePriv grace/otp 동반**(`conn.api`가 grace/otp 전달하도록 확장; 없었으면 Lane A 게이트에 원격 종료가 깨짐). e2e: 화면만닫기→PTY 유지, 세션종료→live=false
- [x] dedupe: 탭 key=`fork_id||session_id`(webterm `_ACTIVE` 키와 동일 규칙). `loadTerminal`이 같은 key면 기존 탭 갱신·전경, 피커는 열린 탭 제외(e2e: A 제외 확인)
- [x] 알림 클릭 멀티탭 대응: `CURRENT_TERM_SID`=전경 탭(유지) + `openSessionById`가 `TABS.list`(집합)를 먼저 봐 배경 탭이면 `switchTab` (새 화면 생성 없음)
- [x] localStorage(`sm_tabs`) 복원 `restoreTabsOnce()`: `loadList` 후 1회, SESSIONS로 존재 검증(삭제/이사 세션 폐기), `live_terminal`→dead 표시, **터미널 자동 열기 없음**(다른 기기 화면 탈취 방지). e2e: 토스트·view=list·iframes=0 확인
- [x] 로컬 모드 e2e(리포 서버 5199 + 실제 `claude --resume` 2세션): 추가/전환/화면만닫기/세션종료/복원 전부 실제 클릭+elementFromPoint 검증, 인라인 JS `node --check` 통과. ⚠ **relay 모드 e2e 미실시**(릴레이+페어링 기기 필요: OTP 동반 stop, stream_close 경유 detach, 재연결 재접속) — connector 단위테스트로만 커버. ⚠ 모바일 실기기 육안 미확인
- [x] **relay 모드 e2e**(2026-09-10, 데스크톱 Chrome을 테스트 기기로 페어링, Host 0.7.0): 탭 추가/전환 시 host.log `stream_close rid=…`(배경 detach, PTY 유지) · 릴레이 끊김(`conn.ws.close`)→eof→자동 재연결→**전경 탭 자동 복원**(리플레이 배너) · 배경 B 화면만닫기(live 유지) · 재추가 · 활성 B 세션종료(relay api 프록시+2FA 게이트, live=false) · 마지막 탭 종료 후 상세 복귀 — 전부 통과. 2FA는 이 Host에서 off(required:false)라 OTP 프롬프트 경로는 미검증.
  - 🐛 e2e가 잡은 relay 버그 2건(수정·재검증 완료, 0.7.1): ① `reattachForegroundTab`이 `mounted()`를 요구해 재연결 흐름(`loadList`→pane 리셋) 뒤 복원 불가 → mounted 요구 제거(재구축 허용) + `loadList`가 term-mode pane을 비우지 않게 가드. ② 탭 전환 fast-path가 `showDetailPane()`을 안 불러 `data-view=list` 상태(재연결·알림 클릭)에서 **보이지 않는 pane에 재접속** → fast-path에 `showDetailPane()`.
- [x] **0.7.1 핫픽스(업데이터 포트)**(2026-09-10): 0.7.0 적용 중 stale `runtime.json`(5199 테스트 서버 잔재)로 runner가 엉뚱한 포트를 헬스체크해 헛롤백. 수정: `updater.BOUND_PORT`(server.main 세팅) 권위·runtime.json 폴백, runner `Test-Healthy` runtime.json 포트 보조 확인+기대 버전 검사, 테스트 2건(전체 249). ⚠ runner 개선은 0.7.1 **이후** 업데이트부터 유효(runner는 현재 설치본에서 복사됨). 교훈: 테스트용 두 번째 Host는 `SESSION_MANAGER_CLAUDE_HOME` 격리 필수.
- [x] 회귀 가드(CRITICAL): 1탭=기존 동작(구조 리팩터만, 동일 버튼/xterm/핸들러) · jsonl 무수정(터미널은 `claude --resume`만, 새 claude 파일 접근 0) · 배경 PTY persist(e2e live=true) · dedupe — e2e로 검증. ⚠ PWA JS 자동화 테스트 인프라는 없음(수동 e2e 의존) → Deferred 후보

### Phase 2 (고도화) — 대기 (절대 잊지 말 것)
- [ ] **보유 xterm/즉시 전환**(서버 '리플레이 억제' 플래그 + 숨김 xterm fit/focus/wakeLock) — Decision1에서 이월
- [ ] 백그라운드 탭 활동 배지 `●` — **모니터 tail 스트림 재사용**(라이브 WS N개 없이, 총 WS 2개)
- [ ] eager 모델 검토(모든 탭 라이브 WS) — 모바일 연결수 실측 후 판단
- [ ] relay `Conn.req()` 강화: 소켓이 닫힌 동안 보낸 요청이 waiter로 영원히 대기(재연결 후에도 미해소). 닫힘 상태면 즉시 reject 또는 재연결 후 재전송. (relay e2e 프로브가 이 경로에 걸려 발견)
- [ ] 탭 드래그 재정렬 / 모바일 좌우 스와이프 전환
- [ ] 전경 탭 자동 재부착·하트비트(현재 모니터에만 있음)
- [ ] 탭 + 분할 혼합(2개 나란히 — 기각했던 분할뷰를 옵션으로 흡수)

### Deferred (수요 확인 후)
- [ ] (e2e 발견 2026-09-15) 세션 목록 API 가 거대 활성 세션(56MB·1.1만 msgs)이 있으면 6초+ — 스캐너가 mtime 변경마다 jsonl 전체를 다시 파싱하는 듯. 원격 UI 의 모든 목록 호출(20초 폴링·알림 점프)이 그만큼 느려짐 → 증분 파싱(마지막 오프셋부터) 또는 메타 캐시 검토
- [ ] 탭 그룹(워킹셋) 이름 저장·재사용 — labels sidecar 확장(모니터 그룹저장과 공통)
- [ ] 세션 간 드래그로 프롬프트/텍스트 복사
- [x] 완료/에러 알림에서 해당 탭으로 점프 — v0.8.6 사이클로 승격(2026-09-15, 위 진행중 섹션)
- [ ] **화면 소유권 인계 UX** — 다중 기기 "세션당 화면 1개" 탈취전 완화(현재는 마지막 접속이 이김, webterm.py:407)

## 참고(완료): 멀티 세션·멀티 에이전트 모니터링 (v0.6.0)

오케스트레이션 타임라인 — 관리 에이전트가 하위 세션들을 부리는 소통을
실시간 관전. 확정 결정: 타임라인 형태 / 관리+지정하위 병합 / 호출선 포함 완본.

### Phase 1 (초판) — 대기 (plan-eng-review 확정, 아래 순서로 개발)
확정: WS 재사용 / Host 서버측 병합 / 오프셋 폴링 / viewer 파서 추출+tool_use_id 상관 / 링버퍼
- [x] **[최선두] 호출선 파싱 실측 스파이크 (2026-09-03 완료)**: 스키마·정규식 확정, 리스크 제거. 설계 문서 §9 참조. 하위 세션 tail=기존 parse_record 그대로, 호출선=관리 tool_use command 정규식
- [x] viewer.py 라인 파서를 공유 `parse_record(obj)` 로 추출 (2026-09-03 완료, DRY). 배치 read_conversation 이 이를 호출, 계약 테스트 7건(test_parse_record.py). 전체 193 passed.
- [x] monitor.py (2026-09-03 완료): Tailer(바이트 오프셋 tail·부분줄 보류·트렁케이트 리셋·멀티바이트) + MonitorGroup(시간축 merge·세션 태깅·호출선 calls_out·링버퍼) + parse_calllines + build_group. 순수 동기(WS 없이 테스트). test_monitor.py 18건, 전체 211 passed.
- [x] server.py `/ws/monitor` WS 라우트 + webmonitor.py 러너 (2026-09-03 완료): 터미널 동일 인증, ?ids=콤마목록&manager=UUID, prime()스냅샷→0.4s 폴링 push, 5s 하트비트, 끊김 감지, 읽기전용 단방향. test_webmonitor.py 3건, 전체 214 passed.
- [x] connector.py `monitor` relay 터널 (2026-09-03 완료): method 디스패치 + `_start_monitor`(ids/manager→/ws/monitor URL) + `_pipe_monitor`(읽기전용 단방향 local→client, E2EE·blind 그대로). test_connector_monitor.py 3건, 전체 217 passed.
- [x] PWA UI (2026-09-03 완료): 헤더 🎬 버튼 → 그룹 피커 모달(관리 라디오+하위 체크) → 전체화면 타임라인 오버레이. T.monitor 로컬(/ws/monitor 직결)+relay(monitor 메서드) 이중 구현. 스냅샷+증분 렌더, 세션별 색·이름, 호출선(→)+프롬프트(라벨 선점), 하위 들여쓰기, 도구칩/tool_result, 자동스크롤, ● Live 배지/하트비트. 격리 목서버로 브라우저 end-to-end 검증(스크린샷). node --check 통과.
- [x] 매핑 실패 시 화살표 생략(우아한 저하) — monitor.parse_calllines + UI 반영
- [x] 회귀 가드: jsonl 무수정(오프셋 tail 읽기전용), 파싱 실패 폴백, 관전-점유 무충돌 — test_monitor/test_webmonitor 커버

### Phase 2 (고도화) — 대기 (절대 잊지 말 것)
- [x] ~~개입: 관전 중 관리 에이전트에 사이드 프롬프트 주입~~ — **안 함(사장님 결정 2026-09-14)**: 불가침 원칙과 정면 충돌(세션 대화 흐름 변경), 동시 입력 충돌·관리 에이전트가 모르는 변화·원격 인증 등급 문제. 필요하면 세션을 터미널 탭으로 열어 직접 입력(기존 경로). 관제는 읽기전용으로 확정
- [ ] 오케스트레이션 그룹 저장·재사용 (labels sidecar 확장)
- [ ] 새 이벤트/완료/에러 알림 (FCM 재사용)
- [x] 타임라인 필터(에이전트별 접기, 호출선만 보기) — v0.8.4 사이클로 승격(2026-09-12, 위 진행중 섹션)

### 별도 발견 (모니터링 무관, 2026-09-03 검증 완료 — 회귀 아님)
- [x] scanner.picker_hidden 검증: 순수 `-p`(재개 없음)=mode 0개→picker_hidden=True(정확, 회귀 없음). `mode` 는 `--resume` 가 남기는 것이지 `-p` 산물 아님. 헤드리스 재개된 세션만 picker_hidden=False(저영향, 재개 가능하니 타당할 수 있음). 스캐너 규칙 건재.

### Deferred (수요 확인 후)
- [ ] E2EE 룸 하의 원격 관제 정합성 (기기 페어링 보안 연계)
- [ ] 완전 자동 발견 모드 (claude agents --json 기반 그룹 자동 추정)
- [ ] 관계 그래프 뷰 (노드-엣지 토폴로지)

## 완료된 최근 기능
- [x] v0.5.0 세션 이사(작업 폴더 이동 + 콘텐츠 이동 옵션)
- [x] v0.4.7 픽커 판정 agent-name 반영
- [x] v0.4.6 세션 삭제 시 라벨 동반 정리
