# ClewPath 개발 원칙

## 불가침 원칙: claude 자체의 동작에 영향을 주지 않는다 (2026-08-13, 사장님 확정)

ClewPath 는 claude 를 **관찰·중계·실행**하는 도구이지, claude 의 상태를 바꾸는
도구가 아니다.

1. **claude 가 사용하는 파일(`~/.claude` 아래 세션 jsonl, 설정, 잡 등록부 등)을
   임의로 수정하지 않는다.** 수정이 꼭 필요한 기능이라면, 반드시 사용자에게
   위험을 먼저 고지하고 **명시 승인을 받은 뒤에만** 수정한다(1회성 확인 포함).
2. **직접 수정이 없어도 claude 의 의도치 않은 동작을 유발할 위험이 있는 기능은
   아예 만들지 않는다.** 개발 중 그런 지점이 포착되면 구현 전에 **사장님과 먼저
   협의**한다.

전례(둘 다 원칙 위반으로 제거, 회귀 가드 테스트 존재):
- '은닉 유지'(피커 미표시 세션 재개 후 ai-title 레코드 자동 제거) → 대체 방식 =
  **재개 전 고지 + 사용자 승인**. 가드: `test_lifecycle.py::test_no_silent_modification_of_claude_files`
- 웹 재개 프롬프트의 `~/.claude/history.jsonl` 자동 append(터미널 ↑/↓ 이력 연동) →
  claude 소유 파일 무단 수정이라 제거. 가드: `test_webapi_history.py::test_no_silent_claude_history_append`

승인된 예외(2026-08-13 사장님 승인 — 모두 사용자의 명시 동작 경유, 삭제는 확인창 필수):
- 세션 삭제 → 확인창 → 휴지통 이동(원위치 복구 가능, 30일 보관)
- 이름 변경 → jsonl 에 custom-title 레코드 append (claude 네이티브 형식 준수)
  · **2026-10-15 사장님 승인·결정**: 이름을 바꿀 때 `agent-name` 한 줄도 함께 append — claude /rename 과 같은 짝
    (피어 이름(SendMessage·ListAgents)이 목록 이름과 갈리지 않게, 이름 없는 세션에 이름 붙이기 포함). 결과는 claude 의 /rename 과
    같아서 헤드리스(-p) 세션에 이름을 붙이면 claude 재개 피커에도 나타난다(사장님 감수). native_title.py, 가드: test_native_title.py
  · **2026-10-19 사장님 승인 — 통신 이름 맞추기**: 설정 → 🏷 통신 이름 맞추기(목록 + 확인창)로만, 제목(custom-title)은 있는데
    agent-name 이 비었거나 다른 세션에 agent-name **한 줄만** append(제목은 다시 쓰지 않음). 화면 이름이 ClewPath 라벨 이름뿐인
    세션은 이름 바꾸기와 같은 두 줄(custom-title + agent-name)을 적는다(0.23.7). 생성 중(busy)·자동 실행(-p)·이어받은 옛 줄은
    건너뜀, 로컬 전용. 떠 있는 세션은 다시 뜰 때 새 이름. 화면 이름과 통신 이름은 하나로 관리한다(별도 체계 두지 않음).
    native_title.agent_name_mismatches / sync_agent_names, 가드: test_native_title.py::test_agent_name_mismatch_list_and_sync
- 세션 이사(move_session) → jsonl 을 새 cwd 인코딩 폴더로 물리 이동 + cwd 치환
  (+ 옵션: 실제 작업 폴더 내용도 이동). 로컬 전용·실행중 차단·확인창.
  claude 미지원 폴더 이동을 대신 지원(2026-08-31 사장님 승인)
- 세션 가져오기 → 새 jsonl 파일 생성
- 워커 스킬 설치(2026-09-23 사장님 승인) → 설정 화면 "📦 워커 스킬 설치" 버튼 + 확인창으로만
  `~/.claude/skills/clewpath-workers/SKILL.md` **새 파일 생성**. 기존 파일 무접촉, 덮어쓰기 전 확인,
  자동 설치 없음. `~/.claude/sessions/*.json`(피어 레지스트리)은 **읽기만** 한다.
- 비서 스킬 설치(2026-10-01 사장님 승인, 개인 비서 단계 3) → 워커 스킬과 **같은 방식**(설정 📦 버튼 + 확인창,
  `~/.claude/skills/clewpath-assistant/SKILL.md` 새 파일 생성, 덮어쓰기 전 확인, 자동 설치 없음).
  MCP 서버 등록(claude 설정 파일)은 이 예외에 포함되지 않는다 — 명령 안내만.

- 이어하기 되돌리기(2026-10-06 사장님 승인 — 빈 입력칸 ← 백그라운드 사본 실사고) → 세션 상세
  **↩ 이어하기 되돌리기** 버튼 + 확인창(유지/삭제 대상·할 일·사본에만 있는 대화 경고)으로만.
  원래 세션 jsonl 에서 `continued-in`(대상 = 지우는 사본) **한 줄만** 제거, 원본은 사본의 휴지통 버킷에
  `backup_<원래id>.jsonl` 로 백업. 사본은 휴지통 이동, 원래 세션이 실행 중이면 거절, 사본 삭제 실패 시 원래 파일
  무접촉, 원격은 2FA 특권. revert.py, 가드 테스트: test_revert.py

- 모델 목록 화면 읽기(2026-10-12 사장님 승인 — 부작용 고지 후) → 정기 작업 「모델 목록 갱신」이 claude 가
  업데이트됐을 때(또는 7일마다) **숨은 터미널에 claude 를 한 번 띄워 `/model` 화면을 읽는다**(modelprobe.py).
  빈 세션 기록 1개 생성(미리 정한 --session-id, ClewPath 목록에서 숨김)·사용자 훅 1회 실행은 감수. 고르기 화면에선
  Enter 금지(아래 화살표·Esc 만, 기본 모델 불변), 시작 화면이 평소와 다르면(신뢰 질문 등) 아무것도 입력하지 않고 종료,
  신뢰 폴더 중 .mcp.json·프로젝트 훅 없는 곳에서만. `~/.claude.json` 은 폴더 신뢰 표시만 읽는다. 가드: test_modelprobe.py

- 비서 확장(2026-10-13 사장님 승인 — "일반 세션에 요청한 일도 다 파악해야 개인 비서") → ① 모든 일반 세션 jsonl 을
  **읽기만** 해서 요청·답 요약을 ClewPath 장부(`data_dir/worklog.db`)에 남긴다(worklog.py, 원본 무접촉·비밀값 가림).
  ② 비서가 일반 세션에 글을 보내는 것은 **전달마다 승인 요청(`session_send`)** → 사장님 승인(폰/2FA) 뒤에만, 꺼져 있으면
  자동 허용(yolo) 모드로 띄운다(2026-10-13 사장님 지시로 평소 권한 → yolo)(백그라운드 점유 중이면 거절 — 대화 분기 방지). 가드: test_worklog.py
- 비서 세션 이름 「개인비서」 고정(2026-10-13 사장님) → 비서 지정(승인) 때 위 이름 변경과 같은 custom-title(+agent-name, 2026-10-15) append
  (이미 그 이름이면 안 씀, 생성 중이면 건너뜀). ClewPath 목록은 지금 비서 세션을 항상 「개인비서」로 표시. 가드: test_approvals.py
- claude 보관 기간 예고 → `~/.claude/settings.json` 의 `cleanupPeriodDays` **읽기만**(retention.py). 값은 바꾸지 않는다.
- Claude 훅 자동 등록(2026-10-20 사장님 승인·기록 — 2026-08-10 도입, 이 원칙보다 먼저 생겨 목록에 없던 기존 동작) → Host 기동 때
  `~/.claude/settings.json` 의 hooks 에 ClewPath 상태 훅(SessionStart·UserPromptSubmit·Notification·Stop·SessionEnd, 로컬 Host 로 curl 한 줄)을
  **병합만** 한다: 다른 훅·설정 무접촉, 같은 명령이면 쓰지 않음, 포트가 바뀌면 그 명령만 갱신, 첫 수정 전 `settings.json.bak-clewpath` 백업 1회,
  파일이 깨져 있으면 건너뜀. 모든 OS(Windows·맥·리눅스) 같은 방식. **제거(uninstall) 때는 지우지 않고 남은 훅과 지우는 방법만 안내**(사장님 결정).
  hooks.py `ensure_registered`, 가드: test_hooks.py

이 목록에 없는 claude 파일 접근을 추가할 때는 위 1·2 를 적용할 것.

프로세스 정리 예외(2026-08-21 사장님 협의 — 실사용자 세션 잠금 사고로 승인):
- ClewPath 가 낳는 자식은 **Job Object(KILL_ON_JOB_CLOSE)** 에 편입 — 주인이
  어떻게 죽든 커널이 동반 종료(고아 원천 차단). jobguard.py
  · **2026-10-08 개정(사장님 승인 D1, docs/designs/pty-broker.md)**: 웹 터미널·워커 PTY 의 주인은
    Host 가 아니라 **터미널 관리 프로세스(브로커, `python -m session_manager.ptyd`)** 다. 브로커의 Job 이
    claude 를 묶는다 → 브로커가 죽으면 함께 종료, **Host 재기동·업데이트는 터미널을 죽이지 않는다.**
    브로커는 Host 잡에 넣지 않고 분리 기동하며, 업데이트 롤백의 python 일괄 종료에서 제외한다.
    브로커 무응답 시 터미널 열기 거부(D3), 일회성(fork)은 Host 스트림이 끊기면 즉시 종료(D4),
    브로커 교체는 터미널 0개면 자동·있으면 확인창(D2). 비상 스위치 `[terminal] broker = "off"` = Host 내장 PTY.
    웹 재개(`-p stream-json`)는 지금처럼 Host 잡. 가드 테스트: test_ptyd.py
- 구버전(≤0.3.24) 잔재는 부팅 시 소급 정리: '레거시 서명(--resume <UUID>
  --dangerously-skip-permissions) + 부모 사망' 이중 조건에서만. 픽커 재개·safe
  모드·bg 승격체(--bg-pty-host)·사용자 bg 에이전트는 계속 불가침.
  가드 테스트: test_pty_registry.py::test_legacy_orphan_signature_precision
- 세션 강제 종료(2026-09-30 사장님 요청, Host 0.10.1) → 세션 상세 **⛔ 세션 강제 종료** 버튼 +
  확인 시트로만. 대상은 피어 레지스트리(`~/.claude/sessions`, 읽기만) pid 중 **이름/명령줄에 claude 가
  있는 프로세스만** `taskkill /T /F`(Host 자신 제외, 레지스트리 `procStart` 와 프로세스 생성 시각이 다르면 pid 재사용으로 보고 **죽이지 않음**. `/T` 는 그 claude 가 띄운 자식 — MCP·도구 프로세스 — 도 끝낸다) + ClewPath PTY 는 stop. 파일(jsonl·레지스트리)
  무접촉, 원격은 2FA 특권. ClewPath 가 띄우지 않은 claude 도 끝낼 수 있어 원칙 2 의 승인 예외다.
  가드 테스트: test_kill_origin.py
