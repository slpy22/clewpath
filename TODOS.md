# TODOS — ClewPath

지속 백로그. office-hours·plan·document-release 가 공통으로 읽는다.
설계 근거: `docs/2026-09-03-multi-session-monitoring-design.md`,
`docs/2026-09-10-multi-terminal-tabs-design.md`

## 진행중 기능: 워커 세션 분배 — start API + SendMessage + 동봉 스킬 (v0.9.0)

스파이크 `docs/2026-09-23-worker-dispatch-spike.md`(2026-09-23) 로 확정. 상위 세션이 하위 워커들에 오래
일감을 나눠 주는 운영을 claude 표준 세션 간 통신(`ListAgents`/`SendMessage`)으로 하고, ClewPath 는
**"세션을 살려 둔다"** 하나만 맡는다. **기존 `-p --resume` 경로는 무변경(추가만)**, **세션 하나는 한 방식만**
(워커로 띄운 세션에 `-p --resume` 금지 — 프로세스 2개 = 대화 분기).

### Phase 1 (이번 사이클) — 구현·게시(0.9.0 + 핫픽스 0.9.1)·이 PC e2e 완료(2026-09-23)
- [x] `POST /api/sessions/{id}/terminal/start` — `webterm._spawn`(스폰 본체를 run_terminal 과 공유) + `start_terminal` **멱등**. 로컬 전용, 409 cap/bg_hold·404 no_cwd. e2e: started(pid 90684) → 재호출 already_live(같은 pid) → 7초 내 피어 등장(`live_terminal` true) → stop → 피어 소멸 → 재기동 OK
- [x] 라이브 판정 확장 `peers.py`: `~/.claude/sessions/*.json` 읽기 전용, 2초 캐시 → 세션 목록 `peer{name,status,pid,socket}`, PWA 목록 `🔗 이름 · 작업 중` 칩, `externallyActive` 1차-b 근거. `resolve(to)`: 이름 / `이름 [ref]` / `uds:파이프`(이스케이프 변형 정규화)
- [x] 관제 호출선: `SendMessage` → `calls_out{via:"message"}`(PWA `✉ →`), 수신 래퍼 → `from_peer`/`from_peer_session_id` + 본문 정리(PWA `📨 발신자`). e2e(설치본 0.9.0, 모니터 WS 클라): 관리 `✉ → e2e워커` / 워커 `📨 021-3-ax-65` 본문 정리 / 워커 답장 `✉ →` 관리 — 3건 모두 표시
- [x] monwatch 실패 감지: pending 에 SendMessage 대상 추가, `"success":false` 도 실패. **e2e 가 잡은 버그**: 죽은 워커는 레지스트리에서 사라져 이름을 못 풀어 푸시 0건 → 0.9.1: peers 가 마지막으로 본 이름/파이프→세션 기억(살아 있는 동명 우선) + 그룹 라벨 폴백. 재검증: `[monwatch] [e2e-workers] e2e워커 호출 실패 감지 → 알림 1건`
- [x] 스킬 `session_manager/skills/clewpath-workers/SKILL.md` 동봉(패키지 zip 포함 확인). 실측 반영: 파생 이름은 프로세스마다 바뀜(worker2-7f → 40 → 94) → 매번 레지스트리에서 읽고 등록부는 UUID 만
- [x] 스킬 설치: `skillinstall.py` + `GET /api/owner/skills/workers` / `POST …/install`(로컬 전용, overwrite 재확인) + 설정 "📦 워커 스킬 설치" 확인창. CLAUDE.md 예외 등록. 가드 `test_no_skill_install_on_startup`(기동·상태 조회는 파일 생성 없음). ⚠ 실제 설치는 사장님이 설정 화면에서 버튼으로(원칙상 제가 대신 누르지 않음) — 설치본 상태 조회 `bundled:true, installed:false` 확인
- [x] e2e 전체 통과. 테스트 11건(`test_workers_dispatch.py`), 전체 296 passed. 정리: e2e 그룹 ab7832fe 삭제, 워커 세션 휴지통
- [x] 실사용 1회(2026-09-28 19:23, 사장님 설치 → 이 세션이 관리 세션으로 `/clewpath-workers`): `claude -p` 로 워커 생성($0.33) → 등록부 `.clewpath/workers.json`(gitignore) → start API `started` → **6초** 뒤 피어 `worker-test-50` → 관제 그룹 `85d91464` 저장 → SendMessage 일감 1(인벤토리) 답장 검증 → 의존 일감 2(pytest 7건) 답장·독립 재실행 통과. 스킬 절차 이탈 0. 관찰: 유휴 통지가 일감 2 발송 뒤에 도착(지연) → 스킬에 '답장을 믿고 통지는 참고' 명시 필요 / 등록부 콘솔 출력 cp949 깨짐(파일은 정상)

## 참고(완료): 이어하기 되돌리기 + '작업중' 잔상 (Host 0.15.1, 2026-10-06)

실사고 2건(팀 KP): ① 빈 입력칸 ← 로 논문 세션이 백그라운드 사본(92a84e39)으로 복사 — 명부가 사본을 따라가 중복 사용량 116건,
원래 세션엔 '이어받음' 표시 ② 관리 세션이 RESULT 직후 포털 터미널을 내려 Stop 훅 미도착 → 몇 시간 '작업중'.
- [x] `revert.py` + `GET …/revert-continuation/preview`·`POST …/revert-continuation`(원격 2FA): 사본 중지(bg=`claude stop`/PTY/강제) → 휴지통 → 명부 원복(`team.revert_continuation`) → 원래 파일 continued-in 한 줄 제거(백업). CLAUDE.md 승인 예외
- [x] PWA 상세 '↩ 이어하기 되돌리기'(원래·사본 양쪽) + 확인창(유지/삭제·할 일·사본에만 있는 대화 경고)
- [x] `hooks.mark_gone`(PTY 종료·강제 종료 시) + 목록 `hooks.effective`(프로세스 없음 + 15분 무기록 → 종료로 보정)
- [x] 워커 스킬: 터미널은 구성원 `status=idle` 일 때만 내린다(재설치 필요)
- [ ] 보류(사장님 2026-10-06 "더 수정 안 함"): 자동 압축 때 오는 SessionStart(source=compact)가 phase 를 idle 로 덮어 턴 도중 '작업중' 이 사라짐(삼흥_데이터추출 15:39 실측). 고친다면 compact 는 phase 유지 + 배지가 레지스트리 busy 도 근거로
- [ ] 후속: 이어받은 세션이 이미 휴지통이면 되돌리기 버튼이 안 뜸(기타 d56d9dca 는 수동 처리) — 마크만 지우는 모드
- [ ] 후속: 원래 세션도 실행 중인 채 ← 로 사본이 생기는 경우(지금은 '원래 세션 종료 후' 안내만), 사본 휴지통 복구 시 명부 재연결

## 참고(완료): 이어받기(continued-in) 대응 (v0.9.9, 2026-09-29)

타 PC 실사고 분석(사장님 전달) 검증: ①③④ 사실, ⑤ 부분 사실, ② "재개 시 이어받은 세션을 데몬으로 띄우고 앞 프로세스 종료" 는
이 PC(CLI 2.1.284) 헤드리스 실측에서 **재현 안 됨**(옛 id 그대로 재개됨 = 두 갈래는 맞음). 한 번의 옛 줄 재개가 `total_cost_usd 98.78` 로 찍힘(누적치 여부 불명, 비용 주의).
- [x] ① 스캐너 `continued-in` 파싱 → `SessionMeta.continued_in`, 목록 API 노출, PWA `⏩ 이어받음` 칩 + dim, 상세 카드 한 줄
- [x] ② `scanner.latest_session_id()`(체인·순환·누락 파일 방어) → `webterm._spawn`·`webapi.run_resume_api` 가 옛 줄이면 `continued` 로 거부(포크는 통과), PWA 는 열기/재개를 이어받은 세션으로 보냄(`continuedTarget`)
- [x] ③ `lifecycle.delete_session` 살아 있는 세션(우리 PTY 또는 claude 레지스트리) → `session_live`, API 409, PWA 안내. 설치본 실측: 이 세션 삭제 시도 409
- [x] ④ `monwatch` 가 manager 파일의 `continued-in` 을 보면 `mongroups.set_manager` 로 갱신 + 새 파일 tail
- [x] ⑤ 목록 `🏷 agent_name` 칩(제목과 다를 때). 파생 이름 충돌 자체는 claude 규칙이라 스킬은 UUID 기준(기존)
- [x] 테스트 9(`test_continued_in.py`) + 하네스 1 → 361 passed. 0.9.9 게시·적용. ⚠ 이어받은 파일이 없는 옛 줄(다른 PC 에서 이어짐)은 여전히 재개 허용(설계: 마지막 확인 id) — 실측에서 d56d9dca 가 그 케이스
- [x] 포크 점검(0.9.10, 사장님 질문 "포크가 데몬으로 뜨면?"): 실측 — 헤드리스 `-p` 도 실행 중엔 `~/.claude/sessions` 에 등록(kind interactive) → 0.9.9 삭제 보호가 **포크 자동 정리**(webapi 종료 시 delete, terminal.html 🗑)를 막을 수 있었음. 수정: 프로세스 `wait`/`kill` 뒤 `delete_session(..., wait_live_s=3)`(레지스트리 소멸을 최대 3초 대기), 그래도 살아 있으면(데몬 승격) **보류하고 안내**(전엔 살아 있는 채 파일을 옮겨 분기). API `wait_live_s`(상한 5초), terminal.html 은 409 를 안내로 표시. 재개 시 bg_hold 검사는 포크 id 에도 적용됨(기존)
- [ ] 후속: 하위(subs) 세션의 continued-in 추적, 그쪽 PC 의 `claude --version`·agents 근거 확보 후 ② 데몬 주장 재검토

## 참고(완료): 세션 출처 마크 + 강제 종료 (Host 0.10.1, 2026-09-30)
- [x] 목록 칩 아이콘 = 출처(⌨ 터미널 / 🧬 클로드가 띄움 / 🤖 헤드리스 / 🧩 SDK / 🐍 스크립트 / 🖥 클루패스), `peers.origin_of`(psutil, pid 부모·명령줄, 캐시) → `peer.origin`
- [x] 상세 '⛔ 세션 강제 종료' → `POST /api/sessions/{id}/kill`(PTY stop + 레지스트리 pid `taskkill /T /F`, claude 프로세스만·Host 자신 제외, 원격 2FA `_PRIV_API_SUFFIXES`)
- [x] 외부 접속 설정 화면: 로컬 전용 항목 숨김(🔒 폐지). 0.10.1 게시·적용(13:44). 주의: apply 재기동이 incidents 5 로 기록됨(표준 경로 확인 필요 — 다음 릴리스 때 관찰)

## 대기: 강제 종료 pid 재사용 방어 (Codex 문서 리뷰 2026-09-30)
- [x] (2026-09-30 완료: `peers.same_process` — procStart↔create_time 2초 이내, 없으면 startedAt 기준. 다르면 `pid_reused` 로 거부) `lifecycle.kill_session` 이 레지스트리 `startedAt`/`procStart` 와 프로세스 생성 시각(psutil `create_time`)을 대조해 **다르면 죽이지 않는다**(낡은 레지스트리의 pid 를 다른 claude 가 재사용한 경우). `/T` 자식 범위도 재검토(그 claude 가 띄운 MCP·도구 프로세스까지 끝남 — 의도라면 확인 시트 문구에 명시). 시작점 `session_manager/lifecycle.py` kill_session, 가드 `tests/test_kill_origin.py`. S

## 참고(완료·사고): 출처 마크 미표시 — 설치본 psutil 누락 (Host 0.10.6, 2026-09-30)
- [x] 원인: pyproject 에 psutil 추가 후 **uv.lock 미갱신** → 업데이트 러너 `uv sync --frozen` 이 새 의존성을 설치하지 않음(0.10.1~0.10.5 모두 🔗). 조치: uv.lock 갱신·psutil 없을 때 PowerShell CIM 폴백·`release.ps1` 에 `uv lock --check` 게이트. 교훈: **의존성을 추가하면 `uv lock` 을 같이 커밋**(게이트가 막아 줌)

## 참고(완료): 원격 Host 업데이트 + 로컬 다른 PC 목록 (Host 0.10.5, 2026-09-30)
- [x] `update/apply` 를 로컬 전용 덴리스트에서 빼고 특권(2FA, `_PRIV_API_SUFFIXES`)으로 — 폰 설정 '업데이트' 가 원격에서도 보이고 ensurePriv 뒤 적용(사장님 요청). 실행 코드는 CP 서명 패키지뿐이라 위험은 재기동에 국한. 0.9.8 리뷰 결정(로컬 전용)을 사장님 결정으로 덮음
- [x] 로컬 헤더 '다른 PC 목록': Chrome 서드파티 iframe 저장소 분리 → 세션당 1회 왕복(`?pcexport`→`#pcs=`), 왕복은 iframe 이 '응답은 했는데 빈 목록' 일 때만(불통이면 안 함 — 로컬 화면 이탈 방지), '🔄 PC 목록 새로고침'

## 진행중 기능: 페어링 UX 통일 — 대칭 페어링 모델 (Host 0.9.8 핫픽스 + 0.10.0 + 007 + 앱, 2026-09-29 기획)

`/plan-ceo-review`(SELECTIVE EXPANSION, Codex 외부 시각) 산출: `docs/designs/pc-pairing-ux.md`(정본), 입력 인벤토리
`docs/2026-09-29-pc-pairing-ux-plan.md`. `/plan-eng-review` 완료(2026-09-30, Codex 7건 수용) → 설계 문서 §엔지니어링 리뷰에
**2단계 릴리스(0.10.0 UI/Host 무서버 → 0.11.0 self-revoke·동기화·CP)** + 아키텍처 규칙 13조 + 작업 T1~T15. 다음 게이트: `/plan-design-review` → 구현.
아래 T3~T8 은 eng 리뷰의 T1~T15 로 대체(설계 문서 §Implementation Tasks 가 정본).

### Phase 0 — P1 보안 핫픽스 (선행, Host 0.9.8)
- [x] T1 (0.9.8, 2026-09-29) 커넥터 프록시 `X-ClewPath-Via: relay` 헤더 + 덴리스트(`/api/owner/devices*` 전 동사, `2fa/(provision|toggle)`·`skills/*/install`·`trash/*`·`update/apply`·`sessions/*/terminal/start` POST → `local_only`), 서버 `_is_local` 은 헤더면 False. 설치본 실측: via-relay POST 403 / 로컬 200 / `2fa/status` GET 200 유지. 테스트 25건(`test_connector_local_only.py`)
- [x] T2 (0.9.8) `connector.drop_device` + `request_drop_device`(서버 스레드에서, delete/reissue/revoke 핸들러가 호출) → 그 기기 cid 의 스트림 cancel·authed·enc_cids 정리 / `_handle_stream_in` 은 enforced 면 살아 있는 인증(`devices.is_active`)만 통과, 아니면 파이프 해체 / `_on_connected()` 가 authed·enc_cids·req_cid 초기화

`/plan-design-review` 완료(2026-09-30, 4/10→9/10, 크로스체크 Gemini·Codex 8건 채택) → 설계 문서 §디자인 리뷰(위계·상태 표·스토리보드·5기호 1칩·confirmSheet·문구·E-2·ITP 안내·QR 연결됨 전환·a11y) + DT1~DT7(T5/T6/T11 에 흡수). 승인 목업 `~/.gstack/projects/006_session_manager/designs/*-20260930/`.

### Phase 1-a — Host 0.10.0 (UI·용어·Host 내부 준비, 007 무변) — eng T1~T7 + design DT1~DT7
- [x] T1 devices `_mutate`+RLock·`clean_name`·`set_name_if_empty`·`client_scoped` / T2 `Connector._auth()`+폰 이름 보고 / T3 재발급 재정렬(issue→revoke)·CP 설정 시 폴백 금지(503)·구형 배지 필드 — 2026-09-30, 테스트 +20(383)
- [x] T4 `connect()` auth 결과·페어링 실패 시 이전 자격 복원·폰 이름 보고·pcsSetName(2026-09-30) / T5+DT1~4 `#pair` 상황 줄 1개·행 어휘(pairRow/statusChip 5기호)·confirmSheet(4곳)·해제 진입점(행 [페어링 해제]·설정 [이 기기 초기화], 로그아웃 선택 모달 삭제)·헤더 select ≤1 숨김·`sm_pcs_relay`/`#btn-pcs` 제거(**0.10.2~0.10.4 에서 사장님 요청으로 되돌림**: 헤더 🖧 버튼·로컬 다른 PC 목록 복구)·이름 없이 QR·🩺 줄·a11y(44px·focus-visible·dialog·ESC·aria-live) — PWA 테스트 53
- [x] T6 브리지 `?relay=` 파싱·`persistSync`·`deviceName` + E-2 '앱에서 열기'(fragment 소거 전 보관·pagehide 타이머 취소·웹으로 계속) / DT5 QR 모달 ✅ 연결됨 전환(발급 시각 기준·늦은 응답 폐기·닫히면 중단) / DT7 iOS Safari 본체 안내(.firstrun 1회 + 설정 1줄) — PWA 테스트 58, pytest 383 (2026-09-30). 앱 반영은 다음 TestFlight(맥 지시서 갱신 필요)
- [x] T7 007 `test_cp_client_credentials.py` 5건(revoke 소유권 경계·재폐기·미인증·폐기 뒤 401·발급 room 결합) — 007 커밋 7d2cda1, 배포 없음
- [x] 회귀 테스트 5건(잠금·재정렬·브리지 relay·로컬 select·pcs 이름; notice 순서는 0.11.0 T10) → **release.ps1 0.10.0 게시·적용 완료(2026-09-30 10:41, Host 0.9.10→0.10.0, 태그 v0.10.0)**. 사장님 수동 확인: 폰 페어링 화면·해제 시트·PC 📱 목록 ✅ 연결됨·Safari '앱에서 열기'(앱은 다음 TestFlight)

### Phase 1-b — Host 0.11.0 + 007 (self-revoke·devsync·CP) — eng T8~T15
- [x] T8 007 `/client/status`(일괄·소유권 필터·200 상한)·`/client/revoke-self`(secret 검증·멱등) — 007 커밋 14b7841, **CP 배포는 /deploy-request 로 아직 안 함**(nginx limit_req 에 status 포함해 같이 의뢰)
- [x] T9 `devsync.py`(기동+60s·5분·kick·최소 30s·잠금 밖 조회·(device_id,cpub) 재대조·cp_pending 재폐기·pending_cpub 정리·delete_pending 행 제거·meta.cp_synced·📵 웹푸시) + 📱 목록 즉시 응답+kick + `bye_device` / T10 `drop_device(notice=)` wait_for 1s+finally·request_drop_device 로그·삭제 불명→행 유지(pending)·재발급 pending_cpub·PROTOCOL_VERSION 2 — 2026-09-30, 테스트 +25
- [x] T12 `jsonl_log.py`(잠금+8MB 회전 1세대) → policy.audit 이전 + `pairing-audit.jsonl`(first_auth·delete·reissue·revoke·self_revoke[bye_device/cp_sync]) + E-3 ① 첫 접속 웹푸시(재접속 제외) — 테스트 +5
- [x] T14 `tests/e2e/test_pairing_flow.py`(`@e2e`, `SM_E2E_CP_URL` 있을 때만, `SM_E2E_ADMIN_TOKEN` 으로 정리) + pyproject `addopts -m 'not e2e'` + release.ps1 e2e 단계. **실 CP 에 T8 엔드포인트가 배포된 뒤에야 통과** — 0.11.0 릴리스 전 /deploy-request 후 `SM_E2E_CP_URL=https://clewpath.pyongso.com/cp` 로 1회 실행
- [x] T11 PWA 폰: `PV=2`, 폐기 큐 `{cp,public_id,secret,room,ts}`·persistSync 순서·항목 cp 로만 플러시·복원 병합, `unpairPc` 온라인=bye_device→revoke-self / 오프라인=큐, B-7 성공 후 옛 cpub 큐, pv≥2 게이팅(구 Host 는 D4 문구·'CP 폐기됨(PC 미반영)'), notice(device_removed/reissued)·CP 401·auth_invalid → ⛔[다시 페어링][지우기]·🔄 배지, resetDevice 큐 보존, PC 📱 목록 📵/⏳ 칩(revoked_by/cp_pending)·🩺 cp_synced / T13 💤 90일 / T15 앱 `#cb-pair` 오버레이 폐기 → TestFlight — 2026-09-30 구현(tests/pwa/revoke.test.mjs 9건, PWA 76). 앱 반영은 mac-build-tasks 업데이트 8(TestFlight 대기)
- [x] 0.11.0 릴리스(2026-09-30): ① 서비스 모니터가 CP 재시작 + nginx `sm_cp_client` 30r/m burst 20(실측 21번째 429) ② 실 CP e2e 2/2 ③ Host 0.11.0 게시·적용(34f2efc, v0.11.0)
- [ ] 앱 TestFlight — mac-build-tasks 업데이트 7+8 동반(맥 측)
- [x] 사장님 실사용 확인: 폰에서 PC 해제 → PC 📱 목록 📵 → PC 에서 삭제(0.11.1 수정 후 확인, 2026-09-30)
- [ ] 사장님 실사용 확인: PC 에서 기기 삭제·QR 재발급 → 폰 ⛔·🔄 + [다시 페어링][지우기] / 오래 연 탭의 새로고침 배너

### Phase 2 (고도화) — 대기
- [ ] **E-1 양쪽 목록 상태 표시**(폰: PC 켜짐/꺼짐·마지막, PC: 기기 접속 중) — 왜: 열어보기 전에 상태를 앎. 이번엔 연기(Codex: '켜짐' 은 데이터가 보증 못 함). 설계 필수 항목: 릴레이→CP 즉시 상태 보고(seq, 알림 grace 와 분리), CP TTL·역전 방지, 폰 갱신 주기, 마지막 성공값+'확인 N분 전'·1시간 넘으면 숨김, Host `authed` 재접속 초기화(T2 선행). M→S
- [ ] **E-6 데모 둘러보기**(PC 0대 빈 상태) — 스토어 제출 사이클(M4)에서, 화면 확정 뒤. 완전 목 데이터(`demoT`), Host/CP 호출 0(불가침). M→S
- [ ] **X-1 6자리 코드 페어링**(PC 가 코드 표시, 폰은 입력, CP 60초 코드↔room 중개) — 카메라·링크·앱 무관. CP 임시 보관 = 무저장 예외 확장이라 C 와 함께. M→S
- [ ] **C) CP 를 페어링 정본으로**(목록·이름·상태를 CP 에, 양쪽이 같은 API 렌더, 팀 공유 PC) — 외부 사용자 생긴 뒤. 로컬 전용 보안 설계 재검토 필요. L→M
- [ ] **폰 해제 결과 문구를 실제 서버 폐기 결과로**(2026-09-30 document-release) — 왜: 0.10.x 때 페어링한 PC 는 폰에 pv=1 로 저장돼, PC 를 0.11 로 올린 뒤 연결 없이 해제하면 '이 폰에서만 제거 — PC 에서도 삭제하세요' 가 뜬다(실제로는 CP 폐기·PC 📵 반영됨). 무엇: `unpairPc` 의 sub/토스트·sm_pair_reason 을 `serverUnpairable`(저장된 pv) 대신 플러시 결과(done 에 room 포함)로 결정, 확인 시트 문구는 '서버 폐기 시도' 로 중립화. 시작점: `pwa/index.html` unpairPc·serverUnpairable. S
- [ ] **`policy.usage_summary` 회전 파일 이어 읽기**(eng E-D20) — 왜: T12 로 `api_audit.jsonl` 이 8MB 에서 `.1` 로 회전하면 회전 직후 '최근 n건' 목록이 짧아진다(오늘 한도는 별도 usage 파일이라 무영향). 무엇: `.1` 이 있으면 이어 읽어 recent_n 을 채운다(10줄). 시작점: `session_manager/policy.py:167`. 의존: T12. S
- [ ] **DESIGN.md 생성(`/design-consultation`)**(design DR-11) — 왜: 디자인 시스템 정본이 없어 리뷰마다 `index.html:16-27` 토큰을 사실상 기준으로 삼는다. 무엇: :root 토큰·타이포·컴포넌트(.btn/.devrow/.schip/openModal/confirmSheet) 어휘를 DESIGN.md 로. 다음 사이클. S
- [ ] **접근성 규칙 전 화면 적용 + confirm() 나머지 9곳 confirmSheet 이전**(design DR-13/DR-7) — 왜: 이번엔 페어링 화면 3개만 44px·focus-visible·dialog·inert·aria-live 를 적용. 무엇: 세션 목록·터미널 탭·관제·설정에 같은 규칙, `grep confirm(` 9곳을 confirmSheet 로. 의존: DT2/DT4. M
- [x] **iOS Safari(홈 화면 미설치) ITP 7일 저장소 삭제 안내** — **Completed:** 2026-09-30 (0.10.0 DT7 `maybeShowIosHint`·설정 1줄)(eng E-D21 → design DR-12 로 자리·문구 확정: 페어링 직후 .firstrun 1회 + 설정 1줄, '앱처럼 열어보세요') — 왜: 웹 탭은 localStorage 뿐이라 7일 미사용이면 폐기 큐·`sm_pcs` 가 통째 사라져 유령 cpub(E-5 90일)·'페어링이 저절로 사라짐' 이 된다. 무엇: 페어링 완료 화면/설정에 `navigator.standalone` 아니면 '홈 화면에 추가하면 알림과 페어링이 보존됩니다' 한 줄(푸시 안내와 같은 자리) + docs 에 제약 기록. 문구·위치는 /plan-design-review 에서. 의존: 없음(T5 B-2 화면 작업 때 후보). S

## 진행중 기능: 모바일 앱 — 앱 안 QR 스캔으로 PC 추가 (앱 1.6(6), 2026-09-29)

사장님 실기: 폰 카메라로 QR 을 찍으면 Safari(웹 릴레이)가 열려 네이티브 앱엔 PC 를 못 넣는다. 앱의 네이티브
스캐너는 첫 실행 오버레이에만 있었다. Universal Links(카메라 스캔만으로 앱 열기)는 Phase 2.

### Phase 1 (이번 사이클) — 코드 완료, 맥 빌드 대기
- [x] `native-bridge.js`: `pairFromUrl`(=handlePairingUrl: https 만·릴레이 ws 유도·fragment→해시→리로드→부트 파서 `pcsUpsert` 로 목록에 추가)·`pairErrorText` 공개. 웹 기본형은 null/''
- [x] `index.html`: 페어링 화면 `#p-scan`(앱이면 노출) + ⚙ PC 전환·관리에 `📷 QR 스캔으로 PC 추가`(앱이면 버튼, 웹은 기존 문구) → `appScanPair()`(취소 조용히, 오류는 브리지 문구 toast). 브리지 캡슐화 유지(앱 여부는 브리지 함수 유무로만)
- [x] 테스트 `tests/pwa/app-scan.test.mjs` 5건 — **native-bridge.js 첫 헤드리스 부트**(Capacitor 플러그인 프록시 스텁): pairFromUrl 계약·오버레이 조건·웹/앱 입구. 하네스 `load({setup})` + `$('#id')` 가 등록 요소를 찾음. node 43건
- [x] pbxproj 1.6(6) 고정 + `docs/mac-build-tasks.md` 업데이트 5(Podfile 에 CapacitorBarcodeScanner 확인 필수)
- [x] 맥: Podfile 에 CapacitorBarcodeScanner 반영 커밋(cf40dfe) + TestFlight **1.9** 배포(2026-09-29)
- [ ] 사장님 실기: 앱 📷 로 두 번째 PC 추가 → PC 목록에 추가·기존 유지 (위 1-b 실기와 함께)

### Phase 1-b — Universal Links / App Links (같은 QR 을 카메라 앱으로 찍어도 앱이 열림, 2026-09-29 승격)
- [x] 007 CP: `GET /.well-known/apple-app-site-association`(applinks+webcredentials, `TEAM.com.pyongso.clewpath`, paths `/relay/app`) · `assetlinks.json`(SHA-256 지문). 설정은 `control_plane/applinks.json`(공개값, env `SM_CP_APPLE_TEAM_ID`/`SM_CP_ANDROID_SHA256` 우선). **미설정이면 404**(잘못된 파일을 주면 OS 가 실패를 캐시). 테스트 3건
- [x] nginx(호스트 `D:\docker\infra
ginx\default.conf`): clewpath 블록에 `location ^~ /.well-known/` → CP. 재생성 없이 reload
- [x] iOS: `App.entitlements`(`applinks:clewpath.pyongso.com`) + pbxproj `CODE_SIGN_ENTITLEMENTS` · Android: `autoVerify` https intent-filter(`/relay/app`). 앱 쪽 링크 처리는 기존 `appUrlOpen → handlePairingUrl`
- [x] **Apple Team ID** `7789R34LHR`(맥 보고, 2026-09-29) → `applinks.json` 기입(007 push) → CP restart → AASA **200** `appID 7789R34LHR.com.pyongso.clewpath` 확인. 맥 쪽은 `applinks:clewpath.pyongso.com` 을 Xcode 에 적용해 **1.9 로 TestFlight 배포**(맥이 자체 버전업 — 저장소 pbxproj 1.6(6) 은 더 이상 정본 아님)
- [ ] 사장님 실기(TestFlight 1.9): ① 앱 헤더 🖧 → 📷 로 PC 추가(0.10.3~, 이전엔 ⚙ 설정 → PC 전환·관리) ② 카메라 앱으로 같은 QR → ClewPath 앱이 열리는지. 함정: AASA 는 Apple CDN 캐시(최대 1일)·앱 설치 시점 검증 → 안 되면 앱 삭제·재설치. 1.9 가 Team ID 반영(02:31) **전에** 설치됐으면 재설치가 거의 확실히 필요
- [ ] Android: Play 서명 키 SHA-256 을 `applinks.json` 에(배포 때)

### Phase 2 (고도화) — 대기
- [x] 저장소 `Podfile` 동기화 — 맥 cf40dfe
- [ ] 앱 버전 정본을 맥으로: 맥이 업로드한 버전(1.9)을 pbxproj 에 커밋해 두도록 지시서에 규칙화(저장소 1.6(6) 은 stale)

## 진행중 기능: '다시 연결됨' 알림에 끊김 이력 (서버 007, 2026-09-28)

사장님 실기 ①: down/up 이 같은 tag 라 복구 알림이 '연결 끊김'을 조용히 대체 → 끊김을 못 본 사람은 무슨 일이
있었는지 모른다. 태그를 나누면 복구 뒤에도 '끊김'이 남으니, 대신 **up 본문에 이력**을 싣는다.

### Phase 1 (이번 사이클)
- [x] relay: `_DOWN[room].since`(단절 시작 epoch, down 을 이미 알린 뒤 재예약돼도 최초 시각 유지) → agent-down 에 `since`, agent-up 에 `down_since` 동봉
- [x] CP: `notify_room(..., down_since)` → up 본문 "…다시 붙었습니다. (11:59 부터 6분 끊김)" — `outage_phrase`(Asia/Seoul, `SM_CP_NOTICE_TZ`, 분 반올림, 1시간 이상은 시간·분). 구 릴레이가 since 를 안 주면 `last_down_at - grace` 폴백. **스키마 무변경**
- [x] 테스트: CP 2건(문구·API 왕복+폴백) + relay 1건(since 유지) → 007 92 passed
- [x] 배포(2026-09-28 17:20): 007 main 484907a push → `docker restart util-session-cp util-session-relay` → Host 1초 만에 재접속(host.log `relay 연결됨 … e2ee=on`, grace 안이라 무알림). ⚠ 본문 실확인은 다음 실제 단절 때(쿨다운 6h 라 인위 e2e 불가) — 기대 문구 "…다시 붙었습니다. (HH:MM 부터 N분 끊김)"

## 진행중 기능: 로컬 모드 화면 인계 UX (v0.9.6)

사장님 실기(2026-09-28): PC **로컬 127.0.0.1:5100** + 폰이 실제 주 사용 형태인데, 로컬은 iframe(terminal.html)이라
0.9.4 의 인계 UX(점선 칩·확인창)가 없고 서버 통지 문구만 보였다. 릴레이와 같은 결과를 iframe 경유로 만든다.

### Phase 1 (이번 사이클)
- [x] `static/terminal.html`: 서버 `TAKEOVER_NOTE` 감지 → 상태 "다른 화면이 보는 중" + 부모에 `postMessage({type:'clewpath-term', event:'taken', id, fork}, origin)`(붙을 때 `attached` 도) → 끊길 때 노란 "다른 기기에서 보는 중 — 탭/재연결" 안내(회색 '연결 끊김' 대신)
- [x] `pwa/index.html` 로컬: `onLocalTermMessage`(같은 origin 만) → 탭 `taken` + 점선 칩 / `shouldConfirmTakeover` 는 로컬에서 **통지로만** 판단(목록 `screen` 은 내 iframe 인지 못 가림) / 확인 후 `attach(..., wasTaken)` → iframe 리로드로 재접속(서버 tail 리플레이). 자동 되찾기 없음(핑퐁 차단). 릴레이 경로 무변경
- [x] 테스트 `tests/pwa/local-takeover.test.mjs` 3건(terminal.html 을 부모 있는 샌드박스에 부트 + 앱 로컬 흐름), 하네스 `__winListeners`. node 37건
- [x] 릴리스 0.9.6(2026-09-28 15:41, `-Publish -Apply` 27초, 조용한 재기동 incidents 4 유지, 설치본 `/app`·`/terminal` 새 코드 확인). 007 동기화는 **불필요** — `007/pwa/index.html` 은 006 파일의 하드링크(git 미추적)라 릴레이가 요청마다 읽어 이미 라이브에 반영됨(`onLocalTermMessage` 확인)
- [x] 사장님 재실기(2026-09-28 16:00): PC 로컬 + 폰 왕복 — 노란 줄·확인창·되찾기 ✅. 발견: 전경 탭의 `.sel` 실선 outline 이 1px 점선 테두리를 가려 **점선이 안 보임** → 0.9.7: `.taken.sel` outline 을 주황 점선으로, 글자 주황 + 👀 접두, CSS 가드 테스트(node 38건). 게시·적용 완료
- [ ] 사장님 육안: 0.9.7 에서 빼앗긴 탭이 한눈에 구분되는지(PC 로컬 새로고침 후)

## 진행중 기능: 자기 회복 Phase 2 — 릴레이 heartbeat 단절 알림 (Host 0.9.5 + 서버 007)

사장님 결정(2026-09-28): **A) Host 가 웹푸시 구독을 CP 에 미러**. 폰 알림은 Host 웹푸시(구독·VAPID 키가 Host 에만)라
Host 가 죽으면 알릴 주체가 없었다. 내용 없는 고정 문구 알림이라 blind 원칙과 무관. 의도된 단절(업데이트·종료)은
Host 가 `bye` 프레임으로 예고해 오탐을 막는다.

### Phase 1 (이번 사이클)
- [x] **007 CP**: `webpush.py` — pywebpush 없이(컨테이너 재빌드 금지) RFC 8291 aes128gcm + RFC 8292 VAPID 를 cryptography·pyjwt 로 구현, **RFC 8291 부록 A 벡터 통과**. `WebPushMirror` 테이블(room 당 PEM+구독), `POST /push/webpush-mirror`(agent JWT), `POST /internal/rooms/{room}/agent-down|agent-up`. down 은 6h 쿨다운, up 은 down 뒤 1회, 404/410 구독 청소. 테스트 5건
- [x] **007 relay**: agent 가 `bye` 없이 끊기면 `RELAY_DOWN_GRACE_S`(600) 뒤에도 없을 때만 CP 에 down 통지, 복귀 시 down 을 보냈으면 up 통지. bye 프레임은 중계하지 않고 표식만. 릴레이 재시작으로 전원 끊겨도 grace 뒤 재접속돼 있으면 무알림. 테스트 4건. 007 전체 89 passed. main a683fab push·`docker restart` 완료(테이블 create_all 확인)
- [x] **006 Host**: `push.mirror_to_cp()`(구독 추가/삭제·기동 시, 스레드, CP 미설정 no-op) / `connector.bye()` + `request_bye()`(lifespan 종료·updater.apply 에서) / 테스트 4건, 324 passed. Host 0.9.5 `release.ps1 -Publish -Apply` 22초 → 기동 로그 `CP 미러 ok count 1`, CP DB room 행 1(구독 Windows)
- [x] 배포·e2e(2026-09-28): 007 restart(테이블 create_all) → Host 0.9.5 → CP DB 미러 1행 → maintenance.flag + taskkill 11:59:55 → 릴레이 `→ 600s 뒤 단절 확인` → CP `last_down_at`=**12:09:55(정확히 +600s)**, `down_pending` true(= '연결 끊김' 웹푸시 발송) → 플래그 제거 12:13 → 12:16 복구 → `down_pending` false(= '다시 연결됨' 발송). 0.9.4→0.9.5 업데이트 재기동은 옛 Host 라 bye 없이 끊겼지만 20초 내 복귀라 무알림(grace 설계대로). ⚠ 확인: 이 PC Chrome 에 '연결 끊김'(12:10)·'다시 연결됨'(12:16) 알림 **(사장님)** / 다음 업데이트(0.9.5→)부터 bye 로 릴레이 감시 자체가 안 걸리는지
- [x] 한계 기록(설계 문서 §Phase 2): PC 전원 꺼짐도 bye 없이 끊기면 10분 뒤 알림 1건(쿨다운 6h) — 정상 종료는 lifespan bye 로 억제. 릴레이 자체 사망은 못 알림(운영측 모니터 몫). maintenance+taskkill 은 알림 나감

## 참고(완료): 화면 소유권 인계 UX (v0.9.4)

크로스체크 3순위(D). 지금은 다른 기기가 같은 세션 터미널을 열면 이전 화면이 **말없이** 끊긴다(`webterm` 점유 규칙:
마지막 접속이 이김). 게다가 끊긴 쪽이 화면 복귀(`visibilitychange`)로 자동 재접속하면 다시 빼앗아 **핑퐁**이 난다.

### Phase 1 (이번 사이클) — 구현·게시(0.9.4)·e2e 완료(2026-09-28)
- [x] 서버 `webterm._push_out`: 새 화면이 붙을 때 이전 화면에 `TAKEOVER_NOTE` 한 줄(2초 타임아웃) → 종료. `screen_info` → 세션 목록 `screen{attached, screen_id}`
- [x] PWA: 통지 감지 → 탭 `taken`(점선 칩 + 툴팁 "[다른 기기에서 보는 중 — 누르면 가져오기]"), eof 를 `disconnected` 로 안 봐 `reattachForegroundTab` 이 되빼앗지 않음(핑퐁 차단), 배너. `shouldConfirmTakeover`: taken 탭 또는 남의 화면이 붙어 있으면 확인창, 내 화면(`screen_id` == 내 탭 screenId)이면 그냥 재접속. `switchTab` 조기 반환에 `!t.taken`
- [x] 로컬 모드(iframe): 통지 문구만 보임(자동 재접속 없음) — 범위 밖
- [x] 테스트: `test_webterm_takeover.py` 3건(통지 후 종료·전송 실패해도 종료·screen_info) + 하네스 `takeover.test.mjs` 3건(판정·통지→taken→eof 비장애·재접속 제외·확인창 취소/수락). 하네스 보강: `<head>` script onload 발화, window 이벤트·ResizeObserver, Proxy xterm → `loadTerminal→attach` 실경로 헤드리스 검증. 전체 320 passed
- [x] 릴리스 0.9.4: `-PrepareChangelog` → bump 커밋 → **`-Publish -Apply` 첫 실사용 49초 exit 0**(pytest 320 → 패키지 675KB → 서명 → 게시 sha 일치 → apply → 0.9.4 → 조용한 재기동 incidents 3 유지)
- [x] 설치본 e2e(WS 2개): A 붙음 → `screen{attached:true, screen_id:screenA}` → B 붙음 → A 가 통지 1줄 수신 후 `closed code=1000` → `screen_id:screenB` → B 닫음 → `attached:false`. ⚠ 미검증: 폰+PC 실기기 왕복(확인창 문구·점선 칩 육안)

## 미검증 항목 모음 (retro 개선 #3, 2026-09-28 — 각 사이클 앞머리에서 2개씩 소화)

흩어져 있던 "⚠ 미검증"을 한 곳에. 통과하면 여기서 지우고 원 항목에 한 줄 남긴다.
- [x] 자기 회복: `maintenance.flag` 실기(2026-09-28) — 플래그 두고 taskkill 11:12:13 → 330초 동안 안 살아남(11:15 틱이 플래그를 존중) → 플래그 제거 11:17:51 → 다음 틱 11:20:54 기동, 11:20:57 health OK(launcher.log "이전 pid 95824 없음 → 기동")
- [x] 자기 회복: 정기 업데이트 재기동이 조용한지 — 0.9.3 적용에서 확인(incidents 불변)
- [x] 릴리스 스크립트 `-Publish -Apply` 경로 첫 실사용 — 0.9.4 에서 49초 exit 0(2026-09-28)
- [x] 화면 인계 실기(2026-09-28 사장님): PC **로컬 127.0.0.1:5100** + 폰 → PC 에 노란 통지만 보이고 점선 칩·툴팁·확인창 없음, 창 오갈 때 소유자 불변(핑퐁 없음 ✅). = 로컬 iframe 모드의 알려진 한계가 **실제 주 사용 형태**(PC 로컬 + 폰)에서 그대로 노출 → 다음 사이클 "로컬 모드 화면 인계 UX" 로 승격(아래 진행중 기능)
- [ ] 알림→탭 점프: 실제 서비스워커 알림 클릭 경로 — **iPhone 은 Safari → 공유 → 홈 화면에 추가 → 홈 아이콘으로 실행해야 웹푸시 가능**(iOS 16.4+, 브라우저 탭에서는 불가 → ⚙ 설정 → 알림에 "지원하지 않습니다" 표시). 🔔 은 상단이 아니라 ⚙ 설정 → 알림 안 **(사장님)**
- [x] 자기 회복 푸시 수신(2026-09-28 사장님): '비정상 종료 복구'·'다시 연결됨' 은 봄, '연결 끊김' 은 못 봄 — 원인: down/up 이 같은 `tag:"host-down"` 이라 12:16 up 이 12:10 down 을 **조용히 대체**(renotify:false, 설계상 '끊김' 이 복구 뒤 남지 않게). 개선 후보: up 본문에 끊긴 시각·지속시간을 넣어 이력이 보이게(007 `webpush.notify_room`, 소) → Deferred
- [ ] 모바일 터미널 모드 하단 입력 영역 높이 이상(사장님, 2026-09-28) — 다음에 스크린샷과 함께
- [x] 관제 타임라인 필터: 실시간 증분 행에 필터 적용 — 헤드리스 하네스로 `openMonitor` 를 실제로 열어 스냅샷 3행 → '오류' 필터 → 증분 2행(텍스트 숨김·오류 표시) → 세션 숨김 토글 → 복원까지 검증(`tests/pwa/monitor.test.mjs`, 2026-09-28)
- [x] 탭 배지 주기 재렌더(2026-09-28, 하네스 `tests/pwa/unverified.test.mjs`): 터미널 뷰 마운트 + 탭 2개 fast-path 상태에서 20초 틱 3회 → 목록은 3회 재렌더, 스트립은 300ms 코얼레싱으로 1회, 활성 칩 `scrollIntoView` 0(사용자 스크롤 보존), xterm 포커스 0·인스턴스/스트림 동일·전송 0. 하네스에 `setInterval` 기록(`__intervals`) 추가
- [x] 즉시 전환(2026-09-28): 버퍼 초과 폴백을 실제 `/ws/terminal?screen=` 라우트로(`test_webterm_overflow_route.py` 2건: tail+'생략' 안내 / 정확한 델타·배너 없음 / 처음 보는 화면 tail+재연결 배너) · 로컬 모드(/app)는 하네스 `MODE=local` 부트로 탭별 iframe 보유·전환 시 `.on` 토글만(src 재대입 0)·dead 탭만 리로드·닫으면 about:blank 검증. ⚠ 남은 것: 로컬 /app 육안(iframe 그리드) — 낮음
- [x] 2FA on OTP 경로(2026-09-28): PWA `ensurePriv`(취소 null·OTP→grace 캐시·재요청 없음·불일치 toast·`2fa_invalid` 시 grace 소거) + 터미널 스트림 2fa 오류 → grace 소거·안내 문구 + 커넥터 실 TOTP 비밀로 grant→grace→terminal start 통과/거부·비밀 재설정 시 grace 무효(`test_connector_stop_2fa.py`). ⚠ 실기 OTP 입력(폰 인증앱)은 이 Host 2FA 를 켤 때 — **(사장님)** 선택
- [ ] 모바일 실기기 육안: 탭 밑줄·필터 바·💾·재개 버튼 상태 아이콘(🖥/⌨/🧬/🤖 — 0.10.7 에서 🔗 이름 칩 대체) **(사장님)**
- [x] 워커 스킬 실사용 1회 — 2026-09-28 완료(위 워커 분배 섹션). 폰 알림은 iPhone 홈 화면 추가 뒤

## 참고(완료): CHANGELOG.md 도입 (retro 개선 #2, 2026-09-28)

릴리스 이력이 CP DB(매니페스트 notes)와 커밋 메시지에만 있었다. 서명 매니페스트 41개에서 notes 를 복원해
`CHANGELOG.md` 를 만들고, release.ps1 이 "이 버전 항목이 있어야 릴리스" 게이트 + `-PrepareChangelog` 로 항목을 써 준다.

### Phase 1 (이번 사이클) — 완료(2026-09-28)
- [x] `CHANGELOG.md`: 0.3.0 → 0.9.3 전 버전 41개 — 매니페스트 notes 우선(서명 JWT 페이로드 디코드), 비어 있던 0.3.20~0.6.6 은 커밋 제목으로 복원(0.6.3/0.6.5 는 상세 기록 없음 표기)
- [x] `release.ps1`: 사전점검 맨 앞에 "CHANGELOG.md 에 `## <ver>` 항목 존재" 게이트. `-PrepareChangelog` 모드(`-Notes` 필수, 중복 거부, 맨 위 삽입 후 종료). `-Notes` 생략 시 CHANGELOG 항목 본문이 매니페스트 notes 가 됨(한 문장을 한 곳에만)
- [x] 테스트 3건(`test_changelog.py`): 현재 버전 항목+본문, 41개 이상·내림차순·중복 없음·끝은 0.3.0, 게이트가 bump 확인보다 앞. 전체 317 passed
- [x] 실행 검증: 항목 없는 버전 → FAIL 게이트 / Prepare → 항목 삽입(복원) / 중복 Prepare → FAIL / 0.9.3 드라이런에서 매니페스트 notes == CHANGELOG 본문
- [x] ops/README 절차 갱신: `-PrepareChangelog` → bump 와 커밋 → `-Publish -Apply`

## 참고(완료): 릴리스 스크립트 통합 (retro 개선 #1, 2026-09-28)

손으로 7단계(package → rename → sign → verify → publish → apply → poll)를 11회 반복 — 순서·이름 규칙·토큰
취급이 매번 손에 달려 있었다. `ops/release.ps1 -Version X.Y.Z -Notes "…" [-Publish] [-Apply]` 하나로 고정.

### Phase 1 (이번 사이클) — 완료(2026-09-28)
- [x] `ops/release.ps1`: 사전점검(버전 형식·pyproject 일치·작업 트리 깨끗·서명 키 존재·pytest) → 패키징+이름 규칙+zip 안 pyproject 버전 확인 → 서명·verify → `-Publish`(토큰은 env 파일에서 읽어 인자로만, 출력 마스킹, 게시 sha == 로컬 sha) → `-Apply`(latest==버전 확인 → apply → 150초 폴링 → apply 로그 → incidents 불변=조용한 재기동 확인). 로그 `ops/logs/release-<ver>.log`
- [x] 안전 게이트: 같은 날 같은 버전 재패키징 거부, `-Apply` 는 `-Publish` 필수, 실패 시 즉시 중단(exit 1)
- [x] 테스트 `tests/test_release_script.py` 3건: 단계 순서·게이트 계약, 토큰이 Log/Write-Host 에 안 실림(+마스킹·소거), pwsh 파서 통과 + 잘못된 버전 거부 실행. 전체 314 passed
- [x] 드라이런: 0.9.3 을 임시 OutDir 에 패키지+서명+검증 5초, exit 0(게시 없음). `-Publish -Apply` 경로는 다음 실제 릴리스에서 첫 사용
- [x] ops/README.md 한 방 절차 추가, 메모리 "릴리스는 release.ps1 로만"

## 참고(완료): PWA 자동 테스트 인프라 (retro 레벨업 #1, 2026-09-27)

`pwa/index.html` 3,800줄 단일 파일에 자동 테스트 0 — 14회 수정이 전부 `node --check` + 수동 e2e 였다. 파일을
쪼개면 릴레이 라이브 서빙(index.html+sw.js)과 충돌하므로 **제품 코드 무변경**으로 로더 쪽에서 해결한다.

### Phase 1 (이번 사이클) — 완료(2026-09-27), 제품 코드 무변경
- [x] `tests/pwa/harness.mjs`: 인라인 스크립트를 `node:vm` 에 통째로 부트 — Proxy 기반 DOM 스텁(어떤 프로퍼티든 읽기 가능·대입 기억·classList/style/dataset 실동작), localStorage/location/history/fetch/WebSocket/Notification/Terminal 스텁. 최상위 `let/const` 는 `ev(ctx, code)` 로 같은 컨텍스트에서 평가. 첫 시도에 3,256줄 스크립트가 예외 없이 부트
- [x] `tests/pwa/pwa.test.mjs`(node:test) 8건: 부트·sessionBadge 신선도·externallyActive 순서·tabEvKind/relIso·jumpToSession 라우팅·openSessionById(재조회/무동작/보류)·tabBadge._on(전경·구독 전 무시, 건수·종류)·알림 기본값 — 0.5초
- [x] `tests/test_pwa_js.py`: pytest 가 `node --test --test-reporter=tap` 을 감싼다(node 없으면 skip) → `pytest -q` = 311 passed(Python 310 + JS 묶음 1)
- [x] 함정 기록: vm realm 이 달라 `deepStrictEqual` 이 프로토타입에서 갈림(JSON 비교) / `node --test 디렉토리/` 는 Windows 에서 경로로 해석돼 실패(파일 지정) / 기본 리포터가 TTY 여부로 spec·tap 갈림(`--test-reporter=tap` 고정) / `const` 바인딩은 `ctx.x` 로 안 보임(`ev`)

### Phase 2 (고도화) — 완료(2026-09-28), 제품 코드 무변경, node 23건
- [x] `rowMatches` 는 끌어내지 않아도 됨 — `monitor.test.mjs` 가 openMonitor 를 스텁 DOM 에 실제로 열어 스냅샷·증분·세션 숨김까지 검증(미검증 소화 때 해결)
- [x] `tests/pwa/phase2.test.mjs` 5건: `saveTabs/restoreTabsOnce`(저장 형태·삭제 세션 버림·PTY 없으면 dead·활성 소실·1회 가드·깨진 JSON) / `#monitor=` 콜드 스타트 → `consumePendingMonitor` 1회 / `openMonitorGroupById`(관리 앞·중복 제거·gid 갱신 모드·없으면 toast·관제 중이면 무시) / `#open=` → `consumePendingOpen` 접두 일치·없으면 버림 / `renderTabBar` 칩 클래스 `sel/st/stpulse/dead/taken`·`--st` 색·툴팁·`lact`·`+ 탭`·활성 변경 때만 scrollIntoView
- [x] `tests/pwa/unverified.test.mjs` 4건(미검증 소화): 20초 틱 재렌더 무해성·2FA OTP 경로·로컬 iframe 즉시 전환. 하네스: `__intervals`(setInterval 기록)·`load({location})` 로 MODE 전환
- [x] 함정 추가: 하네스 `$('#paneDetail')` 은 매번 새 스텁이라 `termView.mounted` 가 참이 안 됨 → 테스트에서 `document.querySelector` 를 그 id 만 고정 / attach 의 resize 타이머(80·300ms)가 `tc.send` 를 부르니 전송 카운터는 400ms 뒤 리셋 / vm 배열은 JSON 비교(`same`)

### Phase 3 — 완료(2026-09-28), `tests/pwa/phase3.test.mjs` 4건, node 27건
- [x] `renderRows` 목록 카드: 폴더 그룹 최신순·건수·대표 cwd(최단) / 상태 점(pulse·색)·문구 / 🔗 피어 칩은 `peer && !live_terminal` 일 때만(우리 PTY 면 없음) / 🤖 배경 에이전트·⚙ 피커 미표시(`picker_expose` 면 없음)·#라벨·🧠 모델(`set`) / 🖥 vs ▶ / 권한 대기 → ✋ + 클릭 가능 문구 / msgs·bytes·상대시간 / 경로 줄은 대표와 다를 때만 / 접기(검색 중 무시)·제목/경로 검색·라벨 필터·빈 문구 2종. (목록엔 `screen` 칩이 없다 — 화면 점유는 터미널 탭·확인창에서만)
- [x] `sw.js` 를 vm 에 부트: push → `showNotification(title 기본값·tag·data{sid,kind,gid})`, 깨진 JSON/빈 data 도 알림. notificationclick → 같은 origin 창 focus + `postMessage({type:'open-session',sid}|{type:'open-monitor',gid})`, 다른 origin 건너뜀, focus 실패 시 다음 창→`openWindow`, 창 없음 → `/relay/app#open=…`·`#monitor=`(encodeURIComponent)·로컬 `/sw.js` 는 `/#open=`. 페이지 쪽: `navigator.serviceWorker` message → `openSessionById`/`openMonitorGroupById`, 그 외 무시 — **sw ↔ 페이지 계약 양쪽 고정**(하네스 `load({navigator})`)

### Phase 4 — 완료(2026-09-28), `tests/pwa/phase4.test.mjs` 7건, node 34건
- [x] `Conn`(릴레이 WS RPC) 을 이벤트 구동 WebSocket 스텁으로: connect/hello → 상태 콜백·cid / req id 매칭(순서 뒤바뀜·모르는 id 무시)·연결 전 즉시 `disconnected`(seq 미소모) / `api()` HTTP 상태 → 예외 / 터미널·모니터 스트림 라우팅(safe→skip:false, screen 커서 동봉, eof+error, stream_in/add/remove, 터미널 close → `stream_close`) / 소켓 끊김 → waiter 전부 disconnected + 스트림 전부 eof + `_scheduleReconnect(code)` / 기기 인증(hello 뒤 auth 완료까지 connect 보류, ver 표시, pv 상위 경고 1회, `auth_required` → onauth(false)) / 재연결 백오프 1·2·4·8·16·30·30s(6회 상한)·타이머 중복 없음·4403/4426 중단·4402 는 JWT 강제 갱신·성공 시 `loadList`+`reattachForegroundTab`·로그아웃 뒤 무예약 / **E2EE**: 룸 키 로드 → `_tx` 는 `a1` 봉투만(평문 메서드 유출 없음), 봉투 수신 복호 → waiter, 키 없이 온 봉투 무시
- [x] `loadDetail`: 정보 카드 kv(ID 8자·폴더·메시지·크기·생성·마지막(상대)) / ⚙ 안내 카드 토글 → `POST picker-expose {expose}` → 문구·toast 반전 / 통계 카드 성공·실패 문구 / 버튼 8개 문구·순서, 실행 중이면 `🖥 터미널로 돌아가기` / 상세 진입 시 `CURRENT_TERM_SID` 해제
- [x] `showResumeChooser`: 실행 중 → 바로 터미널 / ⚙ 사전 확인(취소면 모달 없음, `picker_expose` 면 생략) / bg 잠금 → `showBgHold` / 피어 활동 → ⚠ 동시 재개 경고 / 기본 web+YOLO → `loadResume(s,true,false)`, term+일반 → `loadTerminal(s,true)`, 세그먼트 버튼으로 바꾼 조합이 `sm_resume_pref` 에 저장
- 함정: 수신 디스패치는 `_rxq` 직렬화(비동기) → `push` 뒤 한 틱 기다릴 것 / `setTimeout` 을 컨텍스트에서 바꿔치기해 백오프를 즉시 검증(끝나면 복원)

### Phase 5 (대기)
- [ ] `loadResume`(웹 재개 스트림) 프레임 렌더·입력·eof 처리 / `showApprove` 원격 승인 모달
- [ ] 설정 화면(`showSettings`)의 🔔 구독 토글·📦 스킬 설치 확인창 흐름

## 참고(완료): 세션 목록 API 성능 — 증분 파싱 (v0.9.3)

크로스체크(2026-09-27)로 자기 회복 다음 사이클 확정. 실측: 이 세션 jsonl 63MB 가 바뀔 때마다(매 턴) 목록 API
첫 호출 2.9초(전체 재파싱), 이후 0.02초(캐시). 원격 UI 의 20초 폴링·알림 점프·새로고침이 전부 그만큼 느림.

### Phase 1 (이번 사이클)
- [x] `scanner._Fold` 누적기 리팩터 + 캐시(상태·오프셋·직전 64B 서명) → 자란 파일은 새 바이트만 접기(완성된 줄만, 부분 줄은 다음 스캔). 바이너리 1MB 청크 읽기라 **전체 파싱 자체도 2.9s → 0.31s**(텍스트 모드 줄 반복이 병목이었음)
- [x] 방어: 축소·서명 불일치 → 전체 재파싱 폴백. `scanner.invalidate()` 를 lifecycle 의 재작성 2곳(cwd 경로 갱신·세션 이사)에 명시 호출. `STATS{incremental, full}` 진단 카운터
- [x] 테스트 5건(`test_scanner_incremental.py`): append·부분 줄·완성 뒤 동치 == 전체 파싱, 축소/앞부분 재작성 폴백, invalidate, 무변경 시 무읽기, 6MB 성능. 전체 310 passed
- [x] 실파일 벤치(이 세션 63.7MB, 12,654줄): 전체 0.31s → 파일이 17KB 자란 뒤 **증분 4ms**, 전체 파싱과 동치 확인
- [x] 이 PC 실측(설치본 0.9.3): 재기동 직후 콜드 1회 3.8s(전 세션 전체 파싱, 기동 시 data-check 스레드가 백그라운드로 워밍) → 이후 활성 세션이 자랄 때마다 **0.03s**(이전 2.9s). 0.9.2→0.9.3 업데이트 재기동은 조용함(incidents 2→2, 표식 소비) — 자기 회복의 남은 확인 항목 통과

## 사고 기록: Host 무음 종료·미복구 (2026-09-23 13:06) — 자기 회복 0.9.2 로 해결 ✅

0.9.1 적용 후 1시간 33분 만에 Host 프로세스(pid 81060)가 흔적 없이 사라짐(트레이스백·정상 종료 로그·
로그오프/앱오류 이벤트·세션의 kill 명령 모두 없음 → 외부 강제 종료 또는 무음 크래시, 원인 미확정).
런처 `ClewPathHost` 작업이 **로그온 시 1회**뿐이라 21:01 사장님이 발견할 때까지 8시간 다운.
svcmon(사장님 모니터)이 13:07 `up → down` 을 기록했으나 복구 액션은 없음.

### Phase 1 (이번 사이클, 크로스체크로 확정 2026-09-27) — 자기 회복 0.9.2
크로스체크: Gemini=B(목록 성능) 우선, Codex·Claude=A(자기 회복) 우선, Copilot 무응답 → A 먼저, B 는 바로 다음 사이클.
- [x] `start-connector.template.ps1`(v2, 정본) 멱등화: health 3초 → ok 면 종료, runtime pid 가 python 으로 살아 있으면 종료(죽이지 않음·launcher.log 기록), `maintenance.flag` 존중, `launcher.lock`(2분), 기동 사유 `launcher.log`. install.ps1 은 템플릿에서 생성(옛 zip 은 v1 폴백)
- [x] `ClewPathHost` 작업 5분 반복 트리거(로그온 유지, IgnoreNew) — install.ps1(v2 일 때만) + 기존 설치는 `ensure_task.ps1`(런처 v2 재생성 **후에만** 트리거 추가)을 Host 가 기동 시 버전당 1회 실행. 이 PC: 적용 직후 `launcher: regenerated (v2) | task: repetition trigger added (5m)`, 트리거 = 로그온 + PT5M
- [x] `liveness.py`: runtime pid 소멸 + `shutdown.json` 표식 없음 → 의심 기록(incidents.jsonl, 마지막 로그 10줄 ANSI 제거) + 푸시 `host-recovered`. 표식은 lifespan 종료(normal)·updater.apply(update). 다른 인스턴스 생존 시 제외. 1회성 오탐: 0.9.1→0.9.2 재기동(옛 프로세스가 표식을 모름) — 이후 업데이트는 조용
- [x] health 경로 확인: `/api/health` 는 상수 응답(스캔 무관) — 성능 이슈가 오탐을 만들지 않음
- [x] 검증(이 PC, 설치본 0.9.2): `taskkill /F` 18:45:18 → 반복 트리거 18:45:56 기동(launcher.log "이전 pid 100768 없음 → 기동") → 18:46:01 health OK = **43초 복구**, incidents.jsonl 기록 + 복구 푸시 1건 / 살아 있는 Host 앞에서 런처 실행 → 중복 기동 0 / 단위: 표식 있으면 조용·다른 인스턴스 제외. ⚠ 미검증: maintenance.flag 실기(코드 경로 단순), 다음 정기 업데이트가 조용한지(새 updater 표식 — 0.9.3 적용 때 확인)
- [x] 이 PC 조치: 0.9.2 적용 시 Host 가 자동으로 런처 v2 재생성 + 작업 5분 반복 등록(로그온 + PT5M, IgnoreNew)
- [x] 한계 기록(설계 문서 §한계): Host 가 못 뜨거나 PC 가 꺼지면 알림 주체 없음 → Phase 2 릴레이 heartbeat; 스케줄러 차단 PC(HKCU Run 폴백)는 로그온 1회뿐

### Phase 2 (고도화) — 대기
- [x] 릴레이가 Host heartbeat 단절을 감지해 폰 알림 — 0.9.5 자기 회복 Phase 2 로 완료(2026-09-28)
- [x] (B) 목록 API 성능 — 0.9.3 증분 파싱으로 완료(2026-09-27). 원 메모: 요약 메타 **증분 갱신**(mtime 캐시는 활성 세션에서 매번 무효화 — Codex), 파일 축소·교체·미완성 마지막 행 처리(st_size 검증 → 전체 재파싱 폴백 — Gemini), 6초 작업이 다른 요청을 막는지 확인

### Phase 2 (고도화) — 대기
- [ ] 원격(릴레이) 경로에서 start API — stop 과 대칭 2FA
- [ ] 관제 화면에 워커 큐 상태(대기/실행/실패) 패널
- [ ] 기존 `-p` 관리 세션의 새 방식 이전 가이드(세션별 선택)

### 팀·에이전트 명부 + 워커 직접 통신(P2P) — 개인 비서 인프라 (2026-10-01 기획, 정본 docs/designs/workers-p2p.md)
목적: 일반 프로젝트 협업 + 세션·에이전트를 사장님 작업 이력으로 축적 → 나중에 개인 비서 에이전트가 대신 관리. **결정(2026-10-01 사장님): Host 전역 명부부터**(프로젝트 파일 등록부 안 함). 에이전트(영속) ≠ 세션, 팀 안 평면·팀 위 1단(비서), 작성자는 Host API 하나.
#### 단계 1 — 명부 + 스킬 v2 (M) · eng review CLEAR(2026-10-01, E-1~E-13: SQLite·전부 로컬 전용·uds 주소·상태기계·증거·기한) → 구현 T1~T7
- [x] Host `team` 모듈(2026-10-01): SQLite team.db(팀·에이전트·소속·세션 이력·일감·이벤트) + 상태기계·idem_key·assignment_ver·제출 증거·기한 알림 + `/api/v1/team` 로컬 전용(서버·커넥터 이중) — tests/test_team.py 17건
- [x] 관제 그룹 자동 동기화(구성원·라벨만, 이름·알림 보존, pending 재시도, 워커 교체 추적) + 관제 상한 12·저장 그룹 화면이 구성원 변경을 따라감(gid)
- [x] 스킬 v2(관리/워커 모드·P2P 기준·`[cw]` 규약·uds 주소·팀 API 표·제출 증거) + 문서↔라우트 대조 테스트
- [x] v1 `.clewpath/workers.json` 가져오기(`POST /api/v1/team/import`)
- [ ] Host 릴리스 + 설정 📦 워커 스킬 덮어쓰기 설치(사장님 버튼)
- [x] 실사용 1회(2026-10-01, 0.12.0, 팀 P2PT): 워커끼리 직접 4건(ASK/ANSWER·REVIEW 2스레드), 관리 수신 RESULT 1건, 장부 assigned→submitted(sha256·git HEAD)→accepted, 관제 워커↔워커 호출선 확인. 발견: ① 새 폴더는 '폴더 신뢰' 질문에서 워커가 멈춤(레지스트리 미등록) ② Git Bash curl 인자 한글은 CP949 로 깨져 400 → stdin ③ 주소는 전체 문자열 — 스킬 문서 반영, 팀 보관 API 추가(0.12.1)
- [ ] v1/v2 비교(같은 일감, 전체 토큰·시간·반려) — 실제 프로젝트 일감으로
#### 단계 2 — 장부 자동 채움·경력 화면 (M) · 구현 2026-10-01(설계 docs/designs/workers-p2p.md §단계 2, Codex 10건·S2-1~S2-5)
- [x] teamlog: 팀 세션 원본 보존(gzip 청크·세대·삭제 전 최종 수집·가입 이후부터) + 사람 입력·메시지·전달 실패·외부 수신·토큰(message.id 최댓값) 색인 + 비밀값 가림
- [x] 위반 감지 4종(턴 초과·전달 사슬·무응답 30분·순환) + 위반별 폰 푸시(재시도 3회)
- [x] 읽기 API(이력·전체 경력·일감 타임라인·토큰·사람 입력 FTS 검색·위반·보존 원본) + '이 팀 보존본 지우기'
- [x] PWA ⚙ → 👥 팀 · 경력(로컬 전용): 팀 → 구성원/일감/위반 → 일감 타임라인·에이전트 경력·사람 입력 검색
- [x] 0.13.0 게시 + 실데이터 확인(2026-10-01): 이 세션(92MB)을 팀에 붙였을 때 가입(15:53) 이후만 보존(gz 324KB)·사람 입력 2·메시지 2(잘린 주소 전송은 전달 실패로)·토큰 86건 색인. 확인 뒤 보존본 지움·P2PT 보관
- [x] 버그: 관리자 등록 실패 시 반쪽 팀이 남음 → 원자적 생성 + 이미 팀에 있는 세션은 같은 에이전트로 다른 팀에도 소속(한 관리자 여러 팀). 다음 릴리스에 포함
- [ ] 한계: 한 세션이 여러 팀에 소속되면 수집 이벤트는 처음 바인딩된 팀에 귀속(agent_sessions.team_id 1개) — 비서 단계에서 [cw] task 의 팀 코드로 재귀속 검토
#### 단계 3 — 개인 비서 (L) · 구현 2026-10-01(설계 §단계 3, S3-1~S3-4, Codex 10건 → 서버 강제 승인 A-1~A-10)
- [x] 승인 요청 approvals(폰 탭 = 커넥터 메모리 비밀+활성 기기 / PC = 2FA, 1회 실행·인자 고정·30분) + 팀 만들기·가져오기·보관·보존본 지우기는 승인으로만
- [x] 비서: 포트폴리오 팀(수집·보존 대상)·1명 고정·다시 지정하면 같은 비서에 새 세션·새 팀 parent·원격 순수 읽기 GET /api/owner/assistant
- [x] 사장님 프로필(제안 → 승인으로 확정/거절/수정/삭제, 거절 재제안 차단, 보존본 지우기 시 근거 제거) · 전체 팀 검색
- [x] PWA: 헤더 🧑‍💼(PC·폰) · ✅ 승인 대기/승인 시트(#approval= 알림 딥링크) · 프로필 화면 · 보관/지우기 승인 흐름 · 📦 비서 스킬 설치
- [x] 스킬: clewpath-assistant(새, 불가침 예외 승인) · clewpath-workers(팀 만들기 승인 흐름) · MCP 읽기+승인 요청/상태/실행(직접 쓰기 도구 없음)
- [x] 0.14.0 게시·실사용(2026-10-02): 비서 생성(2FA 승인, 첫 요청 1건 30분 만료) → 첫 브리핑 정확(팀 3·막힌 일감 1·제안 3) → 제안 2건 승인 요청 → 사장님 2FA 승인 → 실행(P2PT-T1 취소·INGT 보존본 삭제)
- [x] 실사용 발견 2건 → 0.14.1: 일감 취소(cancel, 반려는 제출된 일감만이라 비서 제안이 실패했을 것) · claude -p 프롬프트(turnOrigin=sdk)를 사장님 입력이 아닌 🤖 프로그램 입력(prompt_in)으로
- [ ] 비서 위임 1회(실제 업무 팀) — 사장님이 맡길 일이 생기면
- [x] 브리지 → 팀 전환(2026-10-05): 원인 조사(개발관리 세션의 session_bridge.py 가 resume-inplace stream-json 으로 하위 5개를 띄워 웹 터미널 없음) → 0.15.0 편집 범위 감시(🚨 경계 이탈 대체, 1시간 묶음 알림)·터미널 상한 12·사람 입력 판정 수정(src=pwa·릴레이 증명) → 팀 KP 생성(승인 ap_9e3dac453f2f, 실폴더 기준 write_scope) → 개발관리에 전환 지시+시험 일감
- [ ] KP 시험 일감 결과 확인 후 브리지 사용 중단 확정, 표시 개선(브리지 같은 stream-json 프로세스는 🛰 '다른 세션이 조종 중' + 누르면 실시간 보기)
- [x] 폰 '웹 재개'(resume-inplace, API 토큰 없음 = PWA)로 친 말: ClewPath 가 보낸 프롬프트 해시를 기억(web_prompts, 7일)해 수집기가 sdk 기록을 사장님 입력(origin=web)으로(2026-10-02). fork 재개(/api/v1/resume)는 원본 세션에 안 쓰므로 무관
- [ ] (남은 틈, A-4) 세션이 일상 API(일감 배정·승인/반려·구성원 추가)를 직접 부르는 것은 막지 않음 — 업그레이드 조건(팀 토큰) 그대로
- [ ] **(조건부) 팀별 관리자 토큰 강제**(eng E-11, 결정 로그 e2457b66) — 지금은 '관리 세션만 쓴다' 가 프롬프트 약속이고 작성자는 자기 신고. **언제**: 비서가 사람 승인 없이 여러 팀을 관리하거나 같은 PC 에 다른 사용자 세션이 생길 때. 무엇: 팀 생성 시 토큰 발급·쓰기 필수, 워커는 자기 일감 제출만. 시작점: team.py 쓰기 함수 + server 팀 라우트. S

## 참고(완료): 알림 → 탭 점프 (v0.8.6 PWA)

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
- [x] **보유 xterm/즉시 전환** — v0.8.0 탭별 xterm + 화면 커서 델타 리플레이로 완료
- [x] 백그라운드 탭 활동 배지 `●` — v0.7.2 모니터 tail 스트림 재사용으로 완료
- [ ] eager 모델 검토(모든 탭 라이브 WS) — 모바일 연결수 실측 후 판단
- [x] relay `Conn.req()` 강화 — 닫힌 소켓이면 즉시 `{ok:false,error:'disconnected'}`(구현 완료, PWA 테스트 Phase 4 로 고정 2026-09-28)
- [ ] 탭 드래그 재정렬 / 모바일 좌우 스와이프 전환
- [x] 전경 탭 자동 재부착 — `reattachForegroundTab`(재연결·화면 복귀, v0.7.x) 완료. 하트비트는 릴레이 ping 으로 대체
- [ ] 탭 + 분할 혼합(2개 나란히 — 기각했던 분할뷰를 옵션으로 흡수)

### Deferred (수요 확인 후)
- [x] (e2e 발견 2026-09-15) 세션 목록 API 거대 활성 세션 재파싱 → v0.9.3 증분 파싱으로 해결(2026-09-27)
- [ ] 탭 그룹(워킹셋) 이름 저장·재사용 — labels sidecar 확장(모니터 그룹저장과 공통)
- [ ] 세션 간 드래그로 프롬프트/텍스트 복사
- [x] 완료/에러 알림에서 해당 탭으로 점프 — v0.8.6 사이클로 승격(2026-09-15, 위 진행중 섹션)
- [x] **화면 소유권 인계 UX** — v0.9.4(릴레이) + v0.9.6/0.9.7(로컬 iframe) 완료

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
- [x] 오케스트레이션 그룹 저장·재사용 — v0.8.1 관제 그룹 저장으로 완료
- [x] 새 이벤트/완료/에러 알림 — v0.8.1 관제 알림 + v0.8.2 호출 실패 감지로 완료(웹푸시)
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
