# 멀티 OS(Windows + macOS) 지원 설계

2026-10-15 사장님 결정: Host 의 OS 의존 부분을 모듈로 떼어 내고, 공통 기능은 그대로 쓴다.
맥 전용 부분은 실제 맥에서 시험하며 완성한다. 각 OS 의 설치 방법도 함께 설계한다.
리눅스는 맥과 같은 POSIX 스크립트를 쓰지만 **시험용**이다(정식 지원·systemd 자동 시작은 결정 전).

릴레이·CP·PWA·폰 앱은 OS 와 무관하다. 바뀌는 것은 Host(006)와 설치 부트스트랩(007 `/get`)뿐이다.

## 단계

| 단계 | 어디서 | 내용 | 동작 변화 | 상태 |
|---|---|---|---|---|
| 1 | Windows PC | `session_manager/osdep/` 경계를 만들고 Windows 코드를 그 뒤로 옮긴다 | Windows 없음 | 완료 — 0.23.0 |
| 2 | Windows PC + 리눅스 `*-test` 컨테이너 | POSIX 기본 구현(pty·프로세스 그룹·psutil·경로), 테스트를 OS 별로 나눔 | Windows 없음 | 완료 — main 합침 0.28.0(2026-10-20) |
| 3a | Windows PC + 리눅스 `*-test` 컨테이너 | 맥·리눅스 공통 설치·업데이트: `get.sh`·`install.sh`·`uninstall.sh`·멱등 런처 `start-connector.sh`·`update_runner.sh`·`hostctl`, 매니페스트 `platforms`, 릴리스 게이트, 컨테이너 e2e 왕복 — 명세 `docs/designs/posix-install.md` | Windows 는 OS 무관 수정만(명세 Q14). 릴리스는 `platforms=["win"]` 이라 맥·리눅스 설치본은 아직 받지 않는다 | 구현·시험 완료 — `posix-install` 브랜치(2026-10-21), main 합침 대기 |
| 3b | 맥 | launchd 자동 시작·자기 회복, 키체인, Terminal.app 재개, launchd 최소 환경(PATH·LANG), 맥 실기(3a 의 e2e 시나리오를 맥에서)·실제 claude 왕복, CP `/get.sh` 공개·랜딩 OS 별 한 줄(서비스 모니터 의뢰), 릴리스 `platforms` 에 mac | 맥 추가 | 대기 |

브랜치: 1단계는 main 에서 배포. 2단계는 `macos` 브랜치에서 하고 합쳤다(그동안 Windows 핫픽스 가능).
2단계는 2026-10-20 main 에 합쳐 0.28.0 으로 배포(재검증: 리눅스 컨테이너 612 통과·Windows 620 통과). 터미널 관리 프로세스(ptyd) 코드가 바뀌어
각 PC 에서 브로커 교체가 한 번 필요하다 — 열린 터미널이 없으면 자동, 있으면 설정의 확인창(D2).
3a 는 `posix-install` 브랜치(scratchpad 작업 트리)에서 만들었다. 상세 칸 바로가기(`quick-switch`)와 섞이지 않게 차례로 합치고,
합칠 때마다 Windows 전체·리눅스 컨테이너를 다시 시험한다. `get.sh` 는 CP 공개 전까지 006 `ops/get.sh` 에 둔다 —
007 `control_plane` 은 운영 CP 가 bind-mount 로 바로 서빙하므로 공개할 때 옮기고 라우트를 더한다(명세 7절).

## 1. OS 경계: `session_manager/osdep/`

```
osdep/__init__.py   공개 함수(아래 표). sys.platform 으로 _win / _posix 를 고른다
osdep/_win.py       지금 코드를 그대로 옮긴 Windows 구현
osdep/_posix.py     맥·리눅스. 1단계는 이식이 쉬운 것만(psutil·os.kill), 2단계 pty, 3a 러너·런처·포트, 3b 키체인·launchd·재개 창
```

공통 코드는 `sys.platform`·`winpty`·`ctypes.windll`·`powershell`·`taskkill` 을 직접 쓰지 않고 `osdep` 만 부른다.

| 함수 | Windows (지금) | macOS (계획) | 쓰는 곳 |
|---|---|---|---|
| `spawn_pty(argv, cwd, rows, cols, env=None)` | pywinpty(ConPTY) | 표준 `pty` 기반 같은 모양 객체(read·write·setwinsize·isalive·terminate·pid) | webterm, modelprobe, (ptyd) |
| `guard_child(pid)` | Job 객체(jobguard) | 프로세스 그룹 + 브로커 감시(맥엔 PDEATHSIG 없음) | webterm |
| `pid_alive(pid)` | OpenProcess/GetExitCodeProcess | `os.kill(pid, 0)` | liveness |
| `kill(pid)` / `kill_tree(pid)` | `taskkill /F` (`/T`) | psutil children + kill | webterm, webapi, lifecycle |
| `list_processes(name_like=None)` | PowerShell CIM | psutil | webterm·webapi 고아 청소 |
| `protect(data)` / `unprotect(data)` | DPAPI | 키체인(`security`, 3b) — 그 전엔 None 이라 호출자가 데이터 폴더(0700) 안 파일에 둔다 | e2ee |
| `NO_WINDOW` / `detached_popen_kwargs()` | creationflags | `start_new_session=True` | 하위 프로세스 기동 전부 |
| `ensure_launcher(...)` | `ensure_task.ps1`(예약 작업) | `start-connector.sh` 를 템플릿으로 다시 렌더(3a) + launchd plist 점검(3b) | liveness |
| `open_resume_terminal(cmd, cwd)` | `cmd /c start` | Terminal.app(`osascript`) — 3b 전엔 False(명령어 안내) | resume |
| `hidden_dirs()` / `temp_claude_dir()` | AppData, LOCALAPPDATA\Temp\claude | ~/Library, $TMPDIR/claude | filebrowser, teamlog |
| `update_runner_argv(...)` | `update_runner.ps1` + pwsh | `update_runner.sh` + /bin/sh(3a) | updater |
| `port_free(host, port)` | SO_REUSEADDR 없이 bind | 같고, 막히면 듣는 소켓이 없을 때만 SO_REUSEADDR 로 다시(TIME_WAIT 오판 → 5101 폴백 방지, 3a) | server |
| `CURL` | `curl.exe`(PowerShell 별칭 회피) | `curl`(3a) | hooks |

## 2단계 결과 (2026-10-15, `macos` 브랜치)

- 터미널: `ptyprocess.PtyProcessUnicode`(pywinpty 와 같은 계열 — read→str·write·setwinsize·isalive·terminate·pid), UTF-8 깨짐은 replace.
- 자식 동반 종료: pty 자식은 새 세션의 리더라 주인(브로커)이 죽어 master 가 닫히면 커널이 SIGHUP → 끝남(Job 대신).
  웹 재개(`-p` 파이프 자식)는 Host 가 죽으면 stdin EOF 로 끝나고, 남은 것은 기동 청소가 줍는다 — 고아 판정에서
  init(pid 1)이 입양한 프로세스는 '부모 없음'(`osdep.ADOPTER_PIDS`). pid_alive 는 좀비를 죽은 것으로 본다.
- 브로커: `ptyd.FAMILY` = AF_UNIX, `data_dir/ptyd.sock`(0600, 100바이트 넘으면 임시 폴더), 죽은 브로커의 소켓은 기동 때 지움.
  POSIX 도 브로커 기본 켜짐. **ptyd.py 가 바뀌었으므로 main 에 합쳐 배포하면 Windows PC 들도 '브로커 업데이트 대기'(D2)를 한 번 거친다.**
- 범위 감시(teamlog): 맥·리눅스에선 `/…`·`~/…` 절대 경로도 파괴적 Bash·리다이렉트 대상으로 본다(Windows 는 그대로).
- 검증: 리눅스 컨테이너(`python:3.11-slim`, `--init`, 일반 사용자) 전체 pytest 571 통과·14 건너뜀(Windows 전용·3단계),
  브로커 통합 확인(실제 소켓·pty, Host 분리 후 재접속, 브로커 kill -9 → 자식 종료, 재기동). Windows 579 통과.
- 테스트 의존성에 pytest-asyncio 를 명시(이 PC 엔 깔려 있었지만 선언이 없었다).

원칙
- 기존 테스트의 바꿔치기 지점(`peers._proc_info`, `ptyd._pty_spawn`, `e2ee._dpapi`, `winpty.PtyProcess.spawn`, `subprocess.run`)은 유지한다.
- `ptyd.py` 는 1단계에서 건드리지 않았다(해시가 바뀌면 모든 PC 에 '브로커 업데이트 대기'). 2단계에서 POSIX pty 와 함께 한 번에 바꿨다.
- `jobguard.py`·`peers._cim_query` 처럼 이미 Windows 전용으로 갈라진 모듈은 그대로 두고 `_win` 이 부른다.

## 3a 결과 (2026-10-21, `posix-install` 브랜치) — 맥·리눅스 공통 설치·업데이트

명세 `docs/designs/posix-install.md`. 결정(2026-10-20 사장님): 훅 자동 등록은 CLAUDE.md 승인 예외로 기록(모든 OS 같은 방식),
제거 때 훅은 안내만, 그 밖의 질문(Q1~Q5·Q8~Q21)은 권장값. 맥과 리눅스가 같은 파일을 쓴다.

- 스크립트(`#!/bin/sh`·LF·BOM 없음·POSIX sh 만 — sh·dash·`bash --posix` 문법 검사와 shellcheck 를 시험이 돌린다):
  - `ops/get.sh`: 매니페스트 → zip SHA-256·크기 대조 → 항목 이름 검사(절대 경로·`..`·역슬래시·제어문자·심볼릭 링크·같은 이름 등)
    → 설치 폴더 옆 임시 stage → `install.sh --from-stage`(exec). 첫 설치의 신뢰는 TLS(`get.ps1` 과 같은 모델), 서명 검증은 설치 뒤 업데이트부터.
  - `install.sh`: uv(고정 0.12.1·SHA-256) → 파이썬 → 클로드 폴더 → `.venv`(uv.lock) → 설정·권한·런처·설치 기록·자격증명 → (재)기동·확인.
    대화형 입력 없음, root 거부, 토큰은 env 로만, 설치하는 동안 `update.inprogress`(by=install)를 쥔다.
  - `session_manager/start-connector.template.sh` → 설치본 `start-connector.sh`: 멱등 런처(`start`·`stop [--hold]`·`resume`·`status`·
    `watch`·`paths`·`foreground`). `maintenance.flag`·업데이트 표식을 지키고, Host 는 명령줄로 알아본다(pid 재사용·좀비는 죽은 것).
    Host 는 사용자의 umask·env 를 그대로 물려받는다(claude 표식만 뺀다 — 브로커와 같은 규칙).
  - `session_manager/update_runner.sh`: 같은 파일 시스템 안 rename + 저널 교체 → uv sync(안 되면 `uv pip install -e`, 둘 다 실패면 곧바로
    롤백) → 재기동(`CLEWPATH_UPDATER=1`) → 헬스(기대 버전) → 커밋 | 롤백. 어디서 끊겨도 `-RecoverOnly` 가 되돌리고, 브로커는 건드리지 않는다.
  - `uninstall.sh`: 확인(`/dev/tty`) → 표식 → watch·Host·브로커 정지 → 설치 폴더 삭제(선택 `--purge-data`·`--purge-uv`).
    클로드 파일은 읽기만 — 남은 훅은 개수와 지우는 방법만 알려 준다.
- 파이썬: `osdep._posix`(`update_runner_argv`·런처 렌더·`port_free`·`CURL`), `session_manager/hostctl.py`(install.sh 도우미 — 경로·설정
  규칙은 Host 코드가 원본), updater(PAYLOAD 에 install.sh·uninstall.sh, `platforms` 판정, `maintenance.flag` 면 적용 거부).
- Windows 에도 가는 OS 무관 수정(명세 Q14, 각각 회귀 시험): 포트 판정 `port_free`, 루프백 확인 요청의 프록시 우회, 커넥터가 실제로 연
  포트로 프록시, 자격증명 발급의 표시 이름, 유지보수 표식이 있으면 적용 거부, update/status 의 `unsupported`, `update_runner.ps1` 의
  `$Payload` 한 줄. install.ps1·런처 템플릿(ps1)·ensure_task.ps1 은 바꾸지 않았다.
- 릴리스 도구: `sign_release --platforms`(기본 win)·`release.ps1 -Platforms`(win 필수, mac·linux 는 같은 HEAD 의 POSIX e2e 기록 필요),
  패키지에 `.sh` 두 개와 형식 게이트(LF·BOM 없음·`#!/bin/sh`), zip 내용 게이트.
- 검증(9단계 끝, 실패 0): Windows 전체 934 통과·221 건너뜀(Git for Windows 의 sh·dash·bash 를 PATH 에 두면 셸 문법 계약까지),
  기본 PATH 836 통과·285 건너뜀. 리눅스 컨테이너(`python:3.11-slim`, 일반 사용자, `--network none`) 전체 1226 통과·24 건너뜀
  (pwsh·node·git 이 없어서 등). 컨테이너 e2e 왕복 186/186(8단계 61372eb — 9단계는 문서만 바꿔 다시 돌리지 않았다), 진짜 uv 왕복
  34/34(4단계). 일부러 버그를 넣은 변이 시험 — 런처 24·러너 47·설치·제거·hostctl 43·get.sh·서명 54·릴리스 ps1 18·e2e 12·문서 32종이
  모두 겨냥한 단정에서 잡혔다.
- 실측으로 바뀐 것(자세히는 명세의 단계별 구현 메모): 리눅스는 재기동 직후 5100 에 TIME_WAIT 가 남아 옛 판정이 5101 로 밀었다 →
  `port_free`. `trap '' HUP INT` 는 exec 로 브로커와 claude 까지 물려가 `trap ':'` 로. `:` 에 붙인 리디렉션이 실패하면 dash·`bash --posix`
  가 스크립트를 통째로 끝낸다 → `true >`. `uv python find` 는 작업 폴더의 `.venv`·`VIRTUAL_ENV` 를 먼저 돌려준다 → 늘 `--system`.
  설치 루트에 `clewpath_host.egg-info` 가 생긴다(uv sync 의 편집 설치).
- 남은 일: TODOS 「멀티 OS」의 3a(합침·후속)·3b, 「별건: Windows 기존 결함」(명세 8.1 R15). 맥 실기 전에는 릴리스 `platforms` 에
  mac 을 넣지 않는다(R1).

## 2. 설치·자동 기동·업데이트 (OS 별)

릴리스 zip 은 하나다(순수 파이썬 소스 + uv.lock). 매니페스트 서명·CP 보관·`/updates/*` 는 그대로 쓴다.
zip 안에 두 OS 의 스크립트를 함께 넣고, 설치본이 자기 OS 것만 쓴다.

| | Windows (지금) | 맥·리눅스 공통 (3a, 시험용) | 맥 전용 (3b) |
|---|---|---|---|
| 한 줄 설치 | `irm https://clewpath.pyongso.com/get \| iex` → `get.ps1` | `curl -fsSL https://clewpath.pyongso.com/get.sh \| sh` → `get.sh` — **공개 전**(지금은 006 `ops/get.sh`) | CP `/get.sh` 공개 |
| 부트스트랩 | 매니페스트 조회 → zip 해시 대조 → `install.ps1` | 같음 + 크기·항목 이름 검사 → 설치 폴더 옆 임시 stage → `install.sh --from-stage` | — |
| 설치 위치 | `%LOCALAPPDATA%\ClewPath` | [mac] `~/Library/Application Support/ClewPath` · [linux] `${XDG_DATA_HOME:-~/.local/share}/clewpath` — 그 아래 `app/`(설치 루트)·`bin/`(uv)·`python/`(uv 관리 런타임), `CLEWPATH_HOME` 으로 바꾼다 | — |
| uv | 고정 버전 내려받아 SHA-256 대조 | 같음(0.12.1 — linux·darwin × x86_64·aarch64 해시를 소스에) | — |
| 파이썬 | 서명된 시스템 파이썬 우선, 없으면 uv 런타임(SAC 대응) | [linux] 시스템 파이썬(3.11 이상) 우선, 없으면 uv 관리 런타임 · [mac] uv 관리 런타임만(CLT 설치 대화상자 회피, Gatekeeper 는 curl 로 받은 파일엔 격리 속성이 안 붙음) | 맥 실기로 확인 |
| 기동 스크립트 | `start-connector.ps1`(템플릿 렌더) | `start-connector.sh`(템플릿 `start-connector.template.sh` 를 렌더한 멱등 런처) | — |
| 자동 시작·자기 회복 | 예약 작업: 로그온 + 5분 반복, 멱등 런처 | 등록하지 않고 안내만, 임시 감시 `watch` | launchd `~/Library/LaunchAgents/com.clewpath.host.plist`: RunAtLoad + KeepAlive(`foreground`) · [linux] systemd `--user` 는 결정 전 |
| 업데이트 적용 | `update_runner.ps1` (분리 pwsh, 복사 교체) | `update_runner.sh` (분리 `/bin/sh`·새 세션, rename + 저널 교체, 의존성 동기화가 두 번 실패하면 즉시 롤백, 브로커 보존) | — |
| 업데이트 받는 OS | 매니페스트 `platforms` 클레임(값 `win`·`mac`·`linux` = `osdep.NAME`, 없으면 win 전용) | 같음 — 이번 출시는 `["win"]` 이라 받지 않는다 | 릴리스에 mac 을 넣는다 |
| 제거 | 예약 작업 삭제 + 폴더 삭제(README 8절) | `uninstall.sh`(watch·Host·브로커 정지 → 설치 폴더, 선택 `--purge-data`·`--purge-uv`, 클로드 훅은 안내만) | + `launchctl bootout` |
| 비밀 보관 | DPAPI | 데이터 폴더 0700·설정 파일 0600 | 키체인(`security`) |
| 맥 전용 확인 | — | — | claude 설치 경로(`~/.local/bin`, Homebrew), 터미널 앱의 전체 디스크 접근 권한 필요 여부, bash 3.2 의 sh 모드·BSD 도구(`ps`·unzip) |

CP(007) 변경(3b, 서비스 모니터에 배포 의뢰 — 명세 7절): `ops/get.sh` 를 007 `control_plane/get.sh` 로 옮기고 `/get.sh` 라우트·
007 `.gitattributes`·nginx `location = /get.sh`, 랜딩 페이지에 OS 별 한 줄 설치(리눅스는 '시험용'으로 표시하거나 넣지 않는다).
매니페스트 `platforms` 는 3a 에서 넣었다 — 맥에서 아직 안 되는 릴리스를 맥 설치본이 받지 않는다.

## 3. 테스트

- 1단계: 기존 pytest·PWA 테스트가 그대로 통과해야 한다(동작 불변). `osdep` 공개 함수 목록·선택 로직 테스트 추가.
- 2단계: Windows 전용 테스트에 `skipif(sys.platform != "win32")`, POSIX 구현은 리눅스 컨테이너에서 실제 pty 로 검증.
- 3a: 세 층 — 정적 계약(모든 OS, `tests/test_posix_scripts.py`), POSIX 실행 시험(리눅스 컨테이너의 전체 pytest — 런처·러너·설치·제거·
  get.sh), 리눅스 `*-test` 컨테이너 e2e 왕복(`python -m pytest -m posix_e2e tests/test_posix_e2e.py`, 가짜 CP·시험 키). 문서 계약
  `tests/test_docs_multi_os.py`(README 맥·리눅스 절·이 단계 표·TODOS).
- 3b: 맥에서 전체 pytest + 3a 의 e2e 시나리오(명세 6.3) + 실제 claude 기동·재접속·업데이트 왕복.
