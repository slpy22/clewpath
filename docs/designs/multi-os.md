# 멀티 OS(Windows + macOS) 지원 설계

2026-10-15 사장님 결정: Host 의 OS 의존 부분을 모듈로 떼어 내고, 공통 기능은 그대로 쓴다.
맥 전용 부분은 실제 맥에서 시험하며 완성한다. 각 OS 의 설치 방법도 함께 설계한다.

릴레이·CP·PWA·폰 앱은 OS 와 무관하다. 바뀌는 것은 Host(006)와 설치 부트스트랩(007 `/get`)뿐이다.

## 단계

| 단계 | 어디서 | 내용 | 동작 변화 |
|---|---|---|---|
| 1 | Windows PC | `session_manager/osdep/` 경계를 만들고 Windows 코드를 그 뒤로 옮긴다 | Windows 없음 |
| 2 | Windows PC + 리눅스 `*-test` 컨테이너 | POSIX 기본 구현(pty·프로세스 그룹·psutil·경로), 테스트를 OS 별로 나눔 | Windows 없음 |
| 3 | 맥 | launchd·키체인·`install.sh`·`update_runner.sh`·CP `/get.sh`, 실제 claude 로 시험 | 맥 추가 |

브랜치: 1단계는 main 에서 배포. 2·3단계는 `macos` 브랜치에서 하고 합친다(그동안 Windows 핫픽스 가능).

## 1. OS 경계: `session_manager/osdep/`

```
osdep/__init__.py   공개 함수(아래 표). sys.platform 으로 _win / _posix 를 고른다
osdep/_win.py       지금 코드를 그대로 옮긴 Windows 구현
osdep/_posix.py     맥·리눅스. 1단계는 이식이 쉬운 것만(psutil·os.kill), 나머지는 2·3단계
```

공통 코드는 `sys.platform`·`winpty`·`ctypes.windll`·`powershell`·`taskkill` 을 직접 쓰지 않고 `osdep` 만 부른다.

| 함수 | Windows (지금) | macOS (계획) | 쓰는 곳 |
|---|---|---|---|
| `spawn_pty(argv, cwd, rows, cols, env=None)` | pywinpty(ConPTY) | 표준 `pty` 기반 같은 모양 객체(read·write·setwinsize·isalive·terminate·pid) | webterm, modelprobe, (ptyd) |
| `guard_child(pid)` | Job 객체(jobguard) | 프로세스 그룹 + 브로커 감시(맥엔 PDEATHSIG 없음) | webterm |
| `pid_alive(pid)` | OpenProcess/GetExitCodeProcess | `os.kill(pid, 0)` | liveness |
| `kill(pid)` / `kill_tree(pid)` | `taskkill /F` (`/T`) | psutil children + kill | webterm, webapi, lifecycle |
| `list_processes(name_like=None)` | PowerShell CIM | psutil | webterm·webapi 고아 청소 |
| `protect(data)` / `unprotect(data)` | DPAPI | 키체인(`security`) 또는 0600 파일 | e2ee |
| `NO_WINDOW` / `detached_popen_kwargs()` | creationflags | `start_new_session=True` | 하위 프로세스 기동 전부 |
| `ensure_launcher(...)` | `ensure_task.ps1`(예약 작업) | launchd plist 점검 | liveness |
| `open_resume_terminal(cmd, cwd)` | `cmd /c start` | Terminal.app(`osascript`) | resume |
| `hidden_dirs()` / `temp_claude_dir()` | AppData, LOCALAPPDATA\Temp\claude | ~/Library, $TMPDIR/claude | filebrowser, teamlog |
| `update_runner()` | `update_runner.ps1` + pwsh | `update_runner.sh` + /bin/sh | updater |

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

## 2. 설치·자동 기동·업데이트 (OS 별)

릴리스 zip 은 하나다(순수 파이썬 소스 + uv.lock). 매니페스트 서명·CP 보관·`/updates/*` 는 그대로 쓴다.
zip 안에 두 OS 의 스크립트를 함께 넣고, 설치본이 자기 OS 것만 쓴다.

| | Windows (지금) | macOS (3단계) |
|---|---|---|
| 한 줄 설치 | `irm https://clewpath.pyongso.com/get \| iex` → `get.ps1` | `curl -fsSL https://clewpath.pyongso.com/get.sh \| sh` → `get.sh` |
| 부트스트랩 | 매니페스트 조회 → zip 해시 대조 → `install.ps1` | 같음 → `install.sh` |
| 설치 위치 | `%LOCALAPPDATA%\ClewPath` | `~/Library/Application Support/ClewPath` |
| uv | 고정 버전 내려받아 SHA-256 대조 | 같음(darwin-arm64 / x86_64 자산) |
| 파이썬 | 서명된 시스템 파이썬 우선, 없으면 uv 런타임(SAC 대응) | uv 런타임(Gatekeeper 는 curl 로 받은 파일엔 격리 속성이 안 붙음) |
| 기동 스크립트 | `start-connector.ps1`(설정 생성) | `start-connector.sh` |
| 자동 시작·자기 회복 | 예약 작업: 로그온 + 5분 반복, 멱등 런처 | launchd `~/Library/LaunchAgents/com.clewpath.host.plist`: RunAtLoad + KeepAlive |
| 업데이트 적용 | `update_runner.ps1` (분리 pwsh) | `update_runner.sh` (분리 sh, `nohup`) |
| 제거 | 예약 작업 삭제 + 폴더 삭제 | `launchctl bootout` + 폴더 삭제 |
| 맥 전용 확인 | — | claude 설치 경로(`~/.local/bin`, Homebrew), 터미널 앱의 전체 디스크 접근 권한 필요 여부 |

CP(007) 변경(3단계, 서비스 모니터에 배포 의뢰): `/get.sh` 엔드포인트 추가, 랜딩 페이지에 OS 별 한 줄 설치.
매니페스트에 `platforms: ["win","mac"]` 표시를 넣어, 맥에서 아직 안 되는 릴리스를 맥 설치본이 받지 않게 한다.

## 3. 테스트

- 1단계: 기존 pytest·PWA 테스트가 그대로 통과해야 한다(동작 불변). `osdep` 공개 함수 목록·선택 로직 테스트 추가.
- 2단계: Windows 전용 테스트에 `skipif(sys.platform != "win32")`, POSIX 구현은 리눅스 컨테이너에서 실제 pty 로 검증.
- 3단계: 맥에서 전체 pytest + 실제 claude 기동·재접속·업데이트 왕복.
