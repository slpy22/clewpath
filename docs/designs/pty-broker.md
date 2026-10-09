# 설계: 터미널 관리 프로세스 분리 (PTY 브로커, 가칭 `clewpath-ptyd`)

- 상태: **구현 완료(Host 0.16.0, 2026-10-09)** — 설계·검토 2026-10-08, 구현 승인 2026-10-09(작업 중 세션 없음 확인)
- 요청: 2026-10-07 사장님 "클루패스가 띄운 세션이 서비스의 하위 프로세스로 뜨는데, 독자 프로세스로" → 선택지 A 승인
- 관련: CLAUDE.md 「프로세스 정리 예외」(2026-08-21), jobguard.py, webterm.py, docs/designs/workers-p2p.md

## 1. 문제

ClewPath 가 띄우는 세션(웹 터미널·워커)은 Host(python) 가 ConPTY 를 직접 열어 `claude --resume` 을 자식으로 실행한다.
Host 가 Job Object(KILL_ON_JOB_CLOSE) 로 자식을 묶어 두었으므로 **Host 가 재기동되면(업데이트마다) 모든 터미널이 죽는다.**

- 팀 KP 처럼 워커를 오래 살려 두는 운영에서 Host 업데이트 = 팀 전원 강제 종료(진행 중 턴 손실, 다음 배분 때 재기동 비용).
- 2026-10-06 실사고와도 닿아 있다: 응답 도중 종료 → '작업중' 잔상(0.15.1 에서 증상만 처리).

Job Object 는 2026-08-21 사고(Host 가 죽어 고아가 된 claude 를 Claude Code 데몬이 bg 에이전트로 승격 → 세션 잠금)의
대책이라 **그냥 빼면 사고가 재발한다.** 목표는 "Host 와 수명 분리" + "고아 0" 을 동시에 만족하는 것.

## 2. 목표 / 비목표

목표
- G1 Host 재기동·업데이트·크래시에도 ClewPath 터미널 세션이 계속 실행된다.
- G2 Host 가 다시 뜨면 그 세션들에 다시 붙는다(목록 🖥, 웹 화면 델타 재생, 화면 인계, 워커 SendMessage 그대로).
- G3 어떤 경우에도 **주인 없는 claude 가 남지 않는다**(8/21 형 고아 0).
- G4 터미널은 **화면에 창을 띄우지 않고 웹(로컬·릴레이)으로만** 본다. 원격 접근은 지금처럼 Host 인증·2FA 경유.

비목표(이번 사이클 아님)
- 웹 재개(`-p stream-json`, webapi) 프로세스 분리 — 턴 단위 짧은 수명이라 지금처럼 Host 잡에 둔다.
- 바탕화면 콘솔 창 표시(선택지 C) — 필요 시 Phase 2 옵션.
- Claude Code 자체 bg 데몬 사용(선택지 B) — 비권장(8/21·10/06 사본 문제와 같은 메커니즘).

## 3. 구조

```
            ┌──────────────── 이 PC (현재 사용자) ────────────────┐
 PWA/폰 ──▶ │ Host (python, 업데이트로 자주 재기동)               │
 (릴레이)    │   webterm 프록시 ── named pipe(사용자 전용 ACL+토큰) ─┐│
            │                                                     ▼│
            │ clewpath-ptyd (오래 사는 작은 프로세스, 별도 런타임)   │
            │   ├ 자체 Job(KILL_ON_JOB_CLOSE) ─ ConPTY ─ claude #1 │
            │   ├                              ─ ConPTY ─ claude #2 │
            │   ├ 링 버퍼(오프셋)·등록부·상한 12                     │
            │   └ 리스(Host 연결) 감시: 일회성(fork) 세션 정리        │
            └─────────────────────────────────────────────────────┘
```

- **브로커가 PTY 와 링 버퍼(누적 오프셋)를 소유**한다. 화면(screen) 소유권·인계·`mark_sent` 커서는 Host 쪽 로직으로 남기되,
  오프셋 기준은 브로커가 준다 → Host 가 재기동돼도 델타 재생(`tail_since`)이 끊기지 않는다.
- **Host 의 `webterm` 은 같은 공개 API 를 유지하는 프록시**가 된다(`has_terminal`, `screen_info`, `start_terminal`,
  `stop_terminal`, `write_to`, `run_terminal`). 호출자(server·lifecycle·revert·skill API)는 무변경이 목표.
- 브로커가 죽으면 자기 Job 이 자식을 모두 끝낸다 → **고아 0 은 지금과 같은 수준으로 유지**(주인이 Host → 브로커로 바뀔 뿐).

### 3.1 브로커 런타임 위치 (업데이트와 충돌 금지)

Host 업데이트는 `session_manager` 페이로드 교체 + `uv sync` 를 하고, 롤백 때는 **InstallRoot 아래 모든 python 을 강제 종료**한다
(update_runner.ps1:200-203). 브로커가 같은 venv·같은 경로면 ① 실행 중 python.exe/pywinpty .pyd 잠금으로 교체 실패 ② 롤백이 브로커를 죽임.
→ 브로커는 **버전 고정된 별도 디렉토리** `%LOCALAPPDATA%\ClewPath\ptyd\<프로토콜버전>\`(자체 venv: python + pywinpty 만)에서 돈다.
Host 업데이트는 이 디렉토리를 건드리지 않고, 롤백의 python 종료 규칙에서 제외한다.

### 3.2 기동·수명

- 기동 주체: **기존 런처(예약 작업 `ClewPathHost`, 5분 반복)가 Host 와 같은 방식으로 멱등 기동**(파이프 응답 있으면 패스).
  Host 가 필요할 때 직접 띄우는 경로도 두되, 그때는 Host 잡을 벗어나야 한다(`CREATE_BREAKAWAY_FROM_JOB` + `DETACHED_PROCESS`)
  — **예약 작업 잡이 breakaway 를 허용하는지 실측 필요(스파이크 S1).**
- 브로커는 세션이 0개여도 상주(작다: 메모리 수십 MB). 유휴 종료는 안 한다(재기동 경합 회피).
- `runtime-ptyd.json` 에 pid·프로토콜 버전·파이프 이름 기록(Host 의 runtime.json 과 같은 규칙).

### 3.3 IPC 프로토콜 (named pipe, 로컬 전용)

- 파이프: `\\.\pipe\clewpath-ptyd-<사용자 SID 해시>`, DACL = 현재 사용자만. 접속 직후 `hello{token, proto}` —
  토큰은 데이터 폴더의 사용자 전용 파일(브로커가 기동 때 생성). TCP 는 열지 않는다.
- 프레임: 4바이트 길이 + JSON(제어) / 출력 데이터는 JSON 안 문자열(지금 WS 가 텍스트라 동일).
- 요청: `spawn{key, sid, argv, cwd, cols, rows, persist}` → `{id, pid}` · `list` · `stop{key}` · `write{key, data}` ·
  `resize{key, cols, rows}` · `attach{key, from_off}` → 스트림(`out{key, data, end_off}` 연속) · `detach{key}`
- 이벤트: `exit{key, code}`(Host 는 `hooks.mark_gone` 호출), `dropped{key, base_off}`(링 버퍼 밀림).
- 버전: `proto` 불일치면 Host 는 **새 세션을 기존 브로커로 띄우지 않고** 안내(브로커 교체는 §5.3).

### 3.4 일회성(fork) 세션

지금 fork(`persist=False`)는 화면이 떨어지면 Host 가 즉시 정리하고 파일을 지운다(webapi). 브로커 구조에서는 Host 가 죽으면 정리할 주체가 사라진다.
→ `persist=False` 세션은 **Host 연결(리스)이 끊기면 브로커가 즉시 종료**. 파일 삭제는 Host 가 다음 기동 때 등록부의 고아 fork 를 정리(기존 sweep 확장).

## 4. 안전 설계 (8/21 재발 방지 + 숨은 프로세스 방지)

| 위험 | 대책 |
|---|---|
| 고아 claude → bg 승격 → 세션 잠금 | 부모(브로커)가 살아 있으면 고아가 아니다. 브로커가 죽으면 **브로커 Job 이 자식 동반 종료**. 브로커가 kill -9 돼도 커널이 처리 |
| 아무도 모르는 숨은 claude 가 비용을 씀 | 브로커 세션은 전부 목록에 🖥 로 보인다(Host 가 `list` 로 조인). 상한 12 는 **브로커가 최종 강제**. 강제 종료 버튼 그대로. (Phase 2: N시간 무입력·무출력 세션 알림) |
| 다른 사용자/프로세스가 파이프로 claude 조작 | 사용자 전용 DACL + 토큰. 원격은 Host 만 경유(Host 인증·디바이스·2FA 그대로) |
| 같은 세션에 프로세스 2개(대화 분기) | 스폰 전 검사(`continued`·`bg_hold`·피어 레지스트리 live)는 Host 에 그대로 + **브로커도 같은 sid 중복 스폰 거부**(이중) |
| Host 재기동 중 사용자가 다른 경로로 같은 세션 재개 | 지금도 있는 위험(피어 레지스트리로 감지). 변화 없음 — 브로커 세션은 레지스트리에 계속 보이므로 오히려 감지가 쉬워짐 |
| 출처 판정(🖥 클루패스) 붕괴 | `peers.classify_origin` 의 'ppid == Host' 를 'ppid ∈ {Host, 브로커}' 로 |
| 강제 종료가 브로커를 죽임 | `kill_session` 은 브로커 pid 를 Host pid 처럼 보호 목록에 |
| 롤백이 브로커를 죽임 | 롤백 python 종료 규칙에서 ptyd 디렉토리 제외 |

**CLAUDE.md 개정 필요(사장님 협의 사항)**: 「프로세스 정리 예외」의 "ClewPath 가 낳는 자식은 Host 와 함께 죽는다"를
"ClewPath 터미널은 **브로커**와 함께 죽는다(브로커 Job). Host 재기동은 터미널을 죽이지 않는다"로. 레거시 고아 정리·불가침 대상(픽커·safe·bg-pty-host·사용자 bg)은 그대로.

## 5. 업데이트·장애 시나리오

### 5.1 Host 업데이트(가장 흔함)
- 지금: lifespan `finally` 의 `shutdown_all` → 전부 종료. 자동 적용 경로는 60초 뒤 `Stop-Process -Force` → Job 이 종료.
- 변경: 브로커 모드에서 `shutdown_all` 은 **화면만 떼고(detach) 종료하지 않는다**. Host 가 어떻게 죽든 브로커 세션은 산다.
- 재기동 후: Host 가 `list` 로 세션을 복원 → 목록·화면 재접속·워커 주소 그대로. PWA 는 WS 가 끊겼다 다시 붙으며 델타 재생.

### 5.2 Host 크래시 / 강제 종료
- 5.1 과 같음(브로커 무관). 일회성(fork) 세션만 리스 끊김으로 브로커가 정리.

### 5.3 브로커 업데이트(드묾: 프로토콜·pywinpty 변경 때만)
- 새 버전 디렉토리에 설치 후 **세션이 0개일 때만 교체**. 세션이 있으면 PWA 설정에 "터미널 관리 프로세스 업데이트 대기 — 세션 N개가 끝나야 적용(지금 적용하면 N개 종료)" + 확인 버튼.
- 교체 동안 새 터미널 요청은 새 브로커로(프로토콜 버전 일치하는 쪽).

### 5.4 브로커 크래시
- 자식 전부 종료(지금 Host 크래시와 같은 결과). Host 는 파이프 끊김 감지 → 모든 세션 `mark_gone` + 런처/Host 가 브로커 재기동.
- 푸시 알림 1건("터미널 관리 프로세스가 재시작되어 터미널 N개가 종료됨").

### 5.5 PC 로그오프·재부팅
- 지금과 같음(전부 종료). 재부팅 후 자동 복원은 하지 않는다(워커는 다음 배분 때 start API 가 살림 — 지금 규칙 그대로).

## 6. 도입 방식

- 설정 `[terminal] broker = "on" | "off"`(clewpath.toml, 기본 on). on 인데 브로커가 응답하지 않으면 **터미널 열기를 거부**하고
  이유를 보여 준다(D3 결정: Host 내장 PTY 로 몰래 폴백하면 '업데이트에도 산다' 는 약속이 조용히 깨진다).
  거부 동안 런처/Host 가 브로커 재기동을 시도하고, 설정 화면에 상태·재시작 버튼. `off` 는 비상용 수동 스위치(지금 방식).
- 전환 릴리스 1회는 기존 Host 내장 PTY 가 종료된다(마지막 1회). 릴리스 노트·확인창으로 고지, 사장님 작업 없는 시간에 적용.
- 단계
  1. **S1 스파이크**(코드 반영 없음, 스크래치): 예약 작업 → python 의 잡 소속 확인, breakaway 가능 여부, 브로커 Job 안 ConPTY+claude 실행, Host 강제 종료 후 생존, 피어 레지스트리·SendMessage 정상.
  2. 브로커 본체 + 프로토콜 + 단위 테스트(가짜 PTY).
  3. Host 프록시(webterm API 유지) — 기존 webterm 테스트 32건을 프록시에도 통과시키는 어댑터.
  4. 런처·업데이트·롤백·출처 판정·kill 보호 반영.
  5. e2e: 워커 3개 기동 → Host 업데이트 적용 → 3개 생존·재접속 델타 재생·SendMessage 왕복 → 브로커 kill → 3개 종료·알림·`작업중` 잔상 없음.

## 7. 엔지니어링 검토 (2026-10-08)

| # | 지적 | 판정·반영 |
|---|---|---|
| R1 | Task Scheduler 가 실행한 프로세스는 잡 안일 수 있음 → Host 가 띄운 브로커도 잡에 묶여 같이 죽을 수 있음 | **S1 에서 실측**. 런처가 브로커를 직접 띄우는 경로를 1순위로(Host 의 자식이 아님) |
| R2 | 같은 venv 면 업데이트·롤백과 충돌 | §3.1 별도 버전 디렉토리 + 롤백 제외 규칙 |
| R3 | 화면 인계(screen_id)·델타 재생이 Host 메모리에 있음 → 재기동 시 커서 유실 | 누적 오프셋을 브로커가 소유, 커서는 Host 재기동 시 '처음 보는 화면' 으로 취급(tail+배너, 지금 재접속과 같은 동작) — 기능 퇴행 없음 |
| R4 | 첫 resize 강제 리페인트(cols-1) 트릭 | 브로커 `resize` 로 동일하게. 프로토콜에 순서 보장 필요(파이프 1개 직렬) |
| R5 | 출력 역압(지금 `send_text().result()` 로 PTY 읽기를 늦춤) | 브로커는 화면 없으면 버퍼만, 화면 있으면 파이프 쓰기 블록으로 역압 — Host 가 느린 화면을 끊어도 브로커는 링 버퍼로 계속 |
| R6 | 출처 판정·kill 보호·롤백 규칙이 pid 기반 | §4 표대로 3곳 수정 + 회귀 테스트 |
| R7 | `-p stream-json`(webapi) 은 여전히 Host 잡 → 업데이트 때 웹 재개 턴은 끊김 | 비목표로 명시. 영향: 진행 중 웹 재개 1턴. 필요 시 Phase 2 |
| R8 | 브로커 자체가 새 단일 장애점 | 실패 시 열기 거부 + 사유 표시(§6, D3), 크래시 알림, 런처 5분 자기 회복, 비상 스위치 `broker=off`. 워커 start API 는 409 `broker_down` → 스킬은 사용자에게 보고 |
| R9 | 숨은 프로세스 비용 | 상한·목록 노출·강제 종료 유지, Phase 2 장기 무활동 알림 |
| R10 | 불가침 원칙 | claude 파일 무접촉(프로세스 소유권만 바뀜). 「프로세스 정리 예외」 개정만 사장님 협의 |

작업량 추정: 사람 기준 2~3주 / CC 기준 스파이크 반나절 + 구현·테스트 2~3일 + e2e 반나절.

## 8. 사장님 결정 (2026-10-08)

- D1 CLAUDE.md 「프로세스 정리 예외」 개정 — **승인**. 문구 반영은 구현 릴리스와 같이(지금 바꾸면 규칙과 코드가 어긋남)
- D2 브로커 업데이트 — **세션 0개면 자동 교체, 있으면 설정 화면 확인 버튼**(§5.3)
- D3 브로커 무응답 — **터미널 열기 거부**(폴백 안 함, §6). 비상 스위치 `broker=off` 만 수동
- D4 일회성(fork) 세션 — **Host 연결이 끊기면 즉시 종료**(§3.4)
- D5 Phase 2 후보(미결): 장기 무활동 알림, 바탕화면 창 보기 옵션, 웹 재개 프로세스 분리

다음 단계: 작업 중 세션이 없을 때 S1 스파이크(스크래치, 운영 Host 무접촉) → 결과로 §3.2 기동 경로 확정 → 구현.

## 9. S1 스파이크 결과 (2026-10-09, 스크래치·운영 Host 무접촉)

| 확인 | 결과 |
|---|---|
| 운영 Host(pid 54144) 잡 소속 | `IsProcessInJob` = **True**(예약 작업/업데이트 러너 사슬에서 물려받음). 그러나 업데이트 러너가 띄운 새 Host 가 옛 Host 사망 뒤에도 사는 것으로 보아 바깥 잡은 KILL_ON_CLOSE 아님 |
| Host 흉내 → 브로커(Popen, DETACHED+NEW_GROUP, 잡 지정 없음) → ConPTY claude(브로커 잡) | 브로커 in_job=False, claude in_job=True, ppid 사슬 정상 |
| Host 흉내만 `taskkill /F`(트리 아님 — 실제 크래시·`Stop-Process -Force` 와 같음) | **브로커·claude 생존**(부모 사망 후에도) |
| 브로커 `taskkill /F` | **claude 3초 안 종료**(브로커 잡) → 고아 0 |
| 피어 레지스트리·SendMessage 주소 | 브로커 아래 claude 정상 등록(`kind interactive`, `cc-msg-…` 파이프). 출처는 `script` 로 오판 → R6 수정 필요 |
| `CREATE_BREAKAWAY_FROM_JOB` | 불필요 — 바깥 잡이 자식 동반 종료를 하지 않으므로 Host 의 자식으로 띄워도 독립 생존. §3.2 의 1순위를 **Host 가 필요할 때 기동**(런처 경유는 자기 회복 보조)으로 |

추가 발견
- **환경변수 오염**: claude 세션 안에서 띄운 프로세스는 `CLAUDECODE`·`CLAUDE_CODE_CHILD_SESSION`·`CLAUDE_CODE_SESSION_ID`·`CLAUDE_CODE_MESSAGING_*` 등을 물려받고,
  그 아래 claude 는 "Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION" 로 **기록 저장·레지스트리 등록이 꺼진다.**
  → 브로커는 claude 를 띄울 때 `CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_PID`, `CLAUDE_EFFORT` 를 지운 환경을 쓴다(Host 를 손으로 띄운 개발 환경 방어).
- **레지스트리 잔재**: 강제 종료된 claude 의 `~/.claude/sessions/<pid>.json` 이 남는다(지금 Host 강제 종료와 같음). `peers` 는 pid 생존을 보지 않아
  죽은 세션이 '실행 중' 으로 보일 수 있다 → peers 에 pid 생존 + `procStart` 일치 검사를 추가(파일은 읽기만, 정리는 claude 몫).

## 10. 구현 메모 (0.16.0)

- 브로커 `session_manager/ptyd.py`(stdlib `multiprocessing.connection` AF_PIPE + authkey HMAC, ConPTY 소유, 링 버퍼, 종료 표식으로
  '세션 종료' 와 '브로커 사망' 구분). Host 쪽 `ptyclient.py` 의 `BrokerProc` 가 winpty 와 같은 모양이라 webterm 의 버퍼·델타·인계 로직 무변경.
- **§3.1 변경**: 별도 venv 대신 **Host 와 같은 venv** 에서 `python -m session_manager.ptyd` 로 돈다. 근거 — ① `uv sync --frozen` 은 버전이 같은
  pywinpty 를 다시 설치하지 않아 .pyd 잠금 충돌이 없다(락 파일을 `--upgrade` 로 갱신할 때만 주의) ② .py 는 기동 때 읽힌 뒤라 페이로드 교체와 무관
  ③ 롤백의 python 일괄 종료는 명령줄 `session_manager.ptyd` 를 제외하도록 고쳤다(update_runner.ps1). 브로커 코드가 바뀌면 해시가 달라져 D2 흐름.
- 기동: 첫 터미널 스폰 때 Host 가 분리 기동(DETACHED·NEW_GROUP·NO_WINDOW, Claude 세션 환경변수 제거). Host 기동 시 `adopt_broker_sessions`.
- 테스트: test_ptyd.py 14(실제 파이프·authkey, 가짜 PTY) + PWA ptyd.test.mjs 2. 격리 e2e(실제 브로커·실제 claude): Host 크래시 후 생존·재접속 꼬리·
  피어 레지스트리 정상 / `/exit` 정상 종료 / 브로커 kill → claude 동반 종료·'브로커 사망' 판정.
