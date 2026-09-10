# TODOS — ClewPath

지속 백로그. office-hours·plan·document-release 가 공통으로 읽는다.
설계 근거: `docs/2026-09-03-multi-session-monitoring-design.md`,
`docs/2026-09-10-multi-terminal-tabs-design.md`

## 진행중 기능: 탭 전환형 멀티 터미널 뷰어 (v0.7.0)

여러 세션 터미널을 브라우저 탭처럼 열어두고 클릭 전환하며 **작업**(양방향).
설계 근거: `docs/2026-09-10-multi-terminal-tabs-design.md`.
실측 확정(2026-09-10): "1개 제한"은 **PWA UI 제약뿐** — 서버(webterm._ACTIVE
세션별 PTY dict)·릴레이(connector.streams[rid] 다중 스트림)는 이미 멀티 지원.
확정(plan-eng-review 2026-09-10, 교차검증 교정): 탭 전환(독립 세션) / 전경 1개 라이브
WS / **스위칭=단일 xterm 재접속**(보유 xterm은 Phase2). "서버 변경 0"은 **틀림** —
릴레이 stop 프레임·서버측 PTY 상한이 진짜 필수. 상세 §9.

### Phase 1 (초판) — 대기 (아키텍처 lock 완료, 개발 착수 가능)
- [ ] 전역 `_term` 단수 → **탭별 상태 dict**(구조 리팩터 먼저=1탭 기존동작 무변경, 그 뒤 멀티탭·Beck)
- [ ] **단일 xterm 재접속** 스위칭(대상 세션으로 1개 WS 재지향, 서버 detach+tail 리플레이 재사용)
- [ ] 탭바 UI: 데스크톱 상단 탭 / 모바일 가로 칩 (세션색 점+이름+×, 우측 `+`=피커 재사용)
- [x] **릴레이 rid stop 프레임**(2026-09-10): connector `stream_close` 핸들러(rid 단위 task.cancel→로컬 WS close→화면 detach, PTY persist) + e2ee require 게이트 포함 + 클라 `close()`가 프레임 전송. 테스트 3건(test_connector_stream_close.py) + 전체 239 passed. 로컬모드는 실 WS close라 무관.
- [ ] **터미널 재접속 스토리**: 릴레이 재연결·visibilitychange 시 전경 라이브 재프로브+재접속 (+`_pipe_terminal` CancelledError→eof 검토)
- [x] **서버측 PTY 소프트 상한**(2026-09-10): webterm `_max_live_pty()`(SM_MAX_TERMINALS 기본 8)+`_live_count()`, run_terminal **새 스폰만** 상한(재접속 무관). 테스트 4건(test_webterm_cap.py).
- [x] **stop 경로 2FA 게이트**(2026-09-10): connector `_handle_api` 가 특권 경로(`/terminal/stop`)에 start 와 대칭으로 `_priv_ok` 요구(릴레이 경유 한정, 소유자 로컬 무영향). 테스트 4건(test_connector_stop_2fa.py). [탭 닫기 detach/종료 선택 UI 는 Lane B]
- [ ] 탭 닫기 UI: '화면만 닫기(PTY 유지)' vs '세션 종료(stop, OTP 동반)' 선택 [Lane B]
- [ ] dedupe: `session_id` **및 `fork_id`**(포크 탭) → 이미 열린 탭으로 포커스
- [ ] `CURRENT_TERM_SID` **단수→집합**(멀티탭 알림 선점/오억제 방지)
- [ ] localStorage 복원: 신선 세션목록 fetch→`live_terminal` stale 판정, 삭제/이사(404) 처리, 전경만 attach
- [ ] 로컬(iframe/terminal.html)·relay 양쪽 동작 + node --check + 브라우저 검증(elementFromPoint 클릭검증)
- [ ] 회귀 가드(CRITICAL): 1탭=기존 동작, 멀티탭 jsonl 무수정, 배경 PTY persist, dedupe 동작

### Phase 2 (고도화) — 대기 (절대 잊지 말 것)
- [ ] **보유 xterm/즉시 전환**(서버 '리플레이 억제' 플래그 + 숨김 xterm fit/focus/wakeLock) — Decision1에서 이월
- [ ] 백그라운드 탭 활동 배지 `●` — **모니터 tail 스트림 재사용**(라이브 WS N개 없이, 총 WS 2개)
- [ ] eager 모델 검토(모든 탭 라이브 WS) — 모바일 연결수 실측 후 판단
- [ ] 탭 드래그 재정렬 / 모바일 좌우 스와이프 전환
- [ ] 전경 탭 자동 재부착·하트비트(현재 모니터에만 있음)
- [ ] 탭 + 분할 혼합(2개 나란히 — 기각했던 분할뷰를 옵션으로 흡수)

### Deferred (수요 확인 후)
- [ ] 탭 그룹(워킹셋) 이름 저장·재사용 — labels sidecar 확장(모니터 그룹저장과 공통)
- [ ] 세션 간 드래그로 프롬프트/텍스트 복사
- [ ] 완료/에러 알림에서 해당 탭으로 점프
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
- [ ] 개입: 관전 중 관리 에이전트에 사이드 프롬프트 주입 (⚠️ 불가침 원칙 사전 검토)
- [ ] 오케스트레이션 그룹 저장·재사용 (labels sidecar 확장)
- [ ] 새 이벤트/완료/에러 알림 (FCM 재사용)
- [ ] 타임라인 필터(에이전트별 접기, 호출선만 보기)

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
