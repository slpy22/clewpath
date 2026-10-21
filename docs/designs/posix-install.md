# 맥·리눅스 공통 설치·업데이트 스크립트 구현 명세 (v1, 결정 반영)

> 상태: 2026-10-20 작성 → 1~9단계 구현·시험 완료(`posix-install` 브랜치, 2026-10-21), main 합침·맥 단계(3b) 대기.
> 진행 표는 `docs/designs/multi-os.md`, 남은 일은 TODOS 「멀티 OS」·「별건: Windows 기존 결함」
> 범위: 멀티 OS 계획(`006/docs/designs/multi-os.md`) 가운데 맥·리눅스 공통 설치·업데이트 단계와 리눅스 `*-test` 시험장
> 근거: 읽기 전용 조사 세 건(설치 부트스트랩, 자동 업데이트, 기동·자기 회복)과 이 문서를 쓰면서 원본을 다시 읽어 확인한 내용. 006·007·nginx·compose 파일은 읽기만 했고 아무것도 바꾸지 않았다. `ops/release-keys/private.pem`은 열지 않았다.
> 표기:
> - `006/` = `F:\021_3_AX 기술스택\006_session_manager`
> - `007/` = `F:\021_3_AX 기술스택\007_clewpath_server`
> - `파일:줄`은 오늘 작업 트리 기준이다. 앞에 저장소 표시가 없으면 006이다.
> - **[mac]**·**[linux]**는 OS에 따라 갈리는 줄이다.
> - "(권장)"은 이 명세의 기본값이다. 확정은 8절에서 한다.

## 결정 (2026-10-20 사장님)

- **Q7 훅 자동 등록: 승인·기록.** 기존 Windows 동작(2026-08-10 도입)을 CLAUDE.md 불가침 원칙의 승인 예외로 기록했고, 맥·리눅스도 같은 방식이다.
- **Q6 제거할 때 훅: 안내만.** uninstall.sh 는 claude 의 settings.json 을 건드리지 않고, 남은 훅과 지우는 방법만 보여 준다.
- **그 밖의 Q1~Q5·Q8~Q21: 아래 8.2 의 권장값 그대로.**
  - 리눅스 위치는 `${XDG_DATA_HOME:-~/.local/share}/clewpath`, 맥은 `~/Library/Application Support/ClewPath`.
  - 이번 출시는 `platforms=["win"]` 이다. 맥·리눅스 설치는 시험장에서만 한다.
  - CP `/get.sh` 공개는 맥 단계와 함께 한다.
  - 작고 명백한 OS 무관 수정은 이번에 넣는다.
  - 의존성 동기화가 두 번 실패하면 즉시 롤백한다. POSIX 만 rename·저널로 교체한다.
- **구현 위치 조정:** `get.sh` 는 CP 공개 전까지 006 의 `ops/get.sh` 에 둔다. 007 의 `control_plane` 은 운영 컨테이너가 bind-mount 로 즉시 서빙하므로, 이번 단계에서는 007 을 건드리지 않는다. 공개할 때 007 로 옮기고 라우트를 추가한다(7절).
- **작업 방식:** `posix-install` 브랜치(scratchpad 작업 트리)에서 만든다. 상세 칸 바로가기(`quick-switch`)와 섞이지 않게 차례로 합친다. 합칠 때마다 Windows 전체·리눅스 컨테이너를 다시 시험한다.

## 0. 한눈에

```
curl -fsSL …/get.sh | sh              get.sh(CP 서빙): 매니페스트 → zip SHA-256·크기 대조 → 이름 검사 → 임시 stage 에 풀기
  └ exec /bin/sh stage/install.sh --from-stage …   설치 루트로 옮김 → exec /bin/sh ROOT/install.sh (pid 유지)
       └ uv → 파이썬 → claude 폴더 → .venv(uv sync) → 설정·런처·자격증명 → (재)기동 → 확인
            └ ROOT/start-connector.sh   멱등 런처: start | stop [--hold] | resume | status | watch | paths | foreground
                 └ .venv/bin/python -m session_manager.server   (적용 시 updater.apply)
                      └ /bin/sh DATA/updates/update_runner.sh …   대기 → 백업 → rename 교체(저널) → uv sync → 재기동 → 헬스 → (롤백)
sh ROOT/uninstall.sh                   멈춤(watch·Host·브로커는 확인 후) → 설치 폴더 삭제(데이터·uv 는 선택)
```

원본을 다시 읽으면서 새로 확인한 사실(조사 결과 보강):

1. **nginx `/get`은 고정된 주소로 넘긴다.** `proxy_pass http://$cp_get:5200/get`(`D:\docker\infra\nginx\default.conf:251-258`)이다. 정규식에 `get\.sh`만 더하면 `/get.sh` 요청에 PowerShell 스크립트가 나간다. 그래서 location을 따로 둬야 한다(7절).
2. **CP 컨테이너는 007 소스를 그대로 붙여 쓴다.** util-session-cp가 `007/control_plane`을 읽기 전용 bind-mount 한다(`E:\018_whisper\util\docker-compose.yml`). get.ps1은 요청이 올 때마다 디스크에서 읽으므로(007 `api.py:510-526`) 저장하는 순간 운영에 반영된다. 이번 단계에서는 get.ps1을 건드리지 않는다.
3. **리눅스에서는 재기동 직후 포트가 5101로 밀린다.**
   - uvicorn은 쉬고 있는 keep-alive 연결을 5초 뒤 서버 쪽에서 닫는다(`timeout_keep_alive=5`). 그래서 5100번 포트에 서버 쪽 TIME_WAIT가 쌓인다.
   - `_port_free`(`server.py:2101-2114`)는 SO_REUSEADDR 없이 bind를 시험하므로, 재기동 직후 5100을 "사용 중"으로 보고 5101로 폴백한다. uvicorn 자신은 POSIX의 asyncio 기본값(reuse_address)으로 문제없이 bind 한다. 틀리는 것은 사전 확인뿐이다.
   - 폴백이 일어나면 훅 명령의 포트가 바뀌어 `~/.claude/settings.json`을 다시 쓰고(`hooks.py:163-172`), 커넥터는 옛 포트로 프록시한다(`connector.py:181-182`). 4.4절의 필수 수정이다.
4. **`sign_release.py keygen`의 기본 출력 폴더가 운영 키 폴더다**(`ops/tools/sign_release.py:58-59`, `:185`). 시험 하네스는 keygen을 부르지 않고 키를 프로세스 안에서 만든다. `sign --key`는 이미 있다(`:190`).
5. **루프백 확인 요청이 프록시로 샐 수 있다.** POSIX에서 urllib는 `http_proxy`를 따르므로 `no_proxy`가 없으면 127.0.0.1 요청도 프록시로 보낸다. 이 때문에 `_clewpath_already_there`(`server.py:2117-2131`)가 오판할 수 있다.
6. **Host를 띄우는 프로세스의 umask·env는 브로커와 그 안의 claude까지 내려간다.** `ptyclient.py:92-99`는 env만 정리한다. 런처에서 `umask 077`을 걸면 claude가 사용자 프로젝트에 만드는 파일이 0600이 된다. 이는 불가침 원칙 위반이므로, 비밀은 폴더·파일 권한으로 지킨다.
7. **POSIX에서는 명령줄(argv)이 같은 PC의 다른 사용자에게 보인다**(`/proc/<pid>/cmdline`). 따라서 토큰은 argv가 아니라 env로 받는다.

## 1. 범위

### 1.1 이번에 하는 것

1. POSIX 공통 스크립트 5종을 만든다. 맥과 리눅스가 같은 파일을 쓴다.
   - `get.sh`
   - `install.sh`
   - `session_manager/start-connector.template.sh`(설치본에서는 `start-connector.sh`)
   - `session_manager/update_runner.sh`
   - `uninstall.sh`
2. 파이썬 쪽 연결부를 고친다.
   - `osdep._posix`의 `update_runner_argv`·`ensure_launcher`(렌더 방식)를 구현한다.
   - `session_manager/hostctl.py`(설치 도우미 CLI)를 새로 만든다.
   - POSIX에서 바로 깨지는 곳을 고친다: 훅의 `curl.exe`, TIME_WAIT 포트 오판, 폴백 포트를 모르는 커넥터, 발급 표시 이름.
   - `updater`에 PAYLOAD 추가, `platforms` 판정, 유지보수 표식(maintenance.flag) 가드를 넣는다.
3. 릴리스 도구를 고친다.
   - 패키지 화이트리스트에 `.sh` 두 개를 넣고 `.sh` 형식 게이트를 둔다.
   - `sign_release --platforms`, `release.ps1 -Platforms`, zip 내용 게이트를 더한다.
4. 테스트를 세 층으로 둔다.
   - 정적 계약 테스트(모든 OS)
   - POSIX 실행 테스트(컨테이너 pytest)
   - 리눅스 `*-test` 컨테이너 e2e 왕복(가짜 CP, 시험 키)
5. 문서를 고친다: README의 맥·리눅스 절(시험용 표시), multi-os.md 단계 표, CHANGELOG, TODOS.

### 1.2 다음 단계로 미루는 것

- **[mac] 전용 기능**
  - launchd LaunchAgent 등록·점검(`ensure_launcher`의 예약 부분)
  - 키체인(`protect`/`unprotect`)
  - Gatekeeper 격리 속성
  - Terminal.app 재개
  - launchd 최소 환경에 맞춘 PATH·LANG 보강
  - 맥 실기 시험(bash 3.2의 sh 모드, BSD 도구, APFS 대소문자, 공백 경로)
- **[linux] 전용 기능**: systemd `--user` 유닛, `systemd-run --scope`. 리눅스 정식 지원 자체도 다음 단계다.
- **CP `/get.sh` 공개**: 라우트·nginx·랜딩 페이지(7절, 서비스 모니터에 의뢰). 이번 시험장은 가짜 CP를 쓴다.
- **Windows 스크립트 동작 변경**: 하지 않는다. 예외는 `update_runner.ps1`의 `$Payload` 한 줄(PAYLOAD 동기화)뿐이다. 발견된 Windows 기존 결함은 8절에 별건으로 적었다.
- **기타**: musl(Alpine)·FreeBSD, 작업 폴더 정리 정책, 러너 env 상속 정리, apply.lock의 O_EXCL 원자화.

### 1.3 이번 단계의 기본값

| 항목 | 결정 (권장) |
|---|---|
| 설치 위치 | `CLEWPATH_HOME` 아래에 `app/`(설치 루트), `bin/`(uv), `python/`(uv 관리 런타임)을 둔다.<br>[mac] `~/Library/Application Support/ClewPath`(multi-os.md:71)<br>[linux] `${XDG_DATA_HOME:-~/.local/share}/clewpath`<br>`CLEWPATH_HOME` env로 바꿀 수 있다. |
| install.sh·uninstall.sh 위치 | 설치 루트(install.ps1 옆). PAYLOAD 네 곳에 추가한다. |
| 파이썬 | [linux] 시스템 파이썬(>=3.11)을 먼저 쓰고, 없으면 uv 관리 런타임.<br>[mac] uv 관리 런타임만(CLT 스텁 대화상자 회피).<br>관리 런타임은 `UV_PYTHON_INSTALL_DIR=$CLEWPATH_HOME/python`에 둔다. |
| 런처 | 스스로 완결된 sh와 venv 파이썬 인라인 판정. 하위 명령을 둔다. 판정 코드는 런처 파일 안에 있으므로 롤백 뒤 버전이 어긋나도 깨지지 않는다. |
| 업데이트 중 표식 | `DATA/update.inprogress`를 러너·설치 스크립트가 쥔다. `CLEWPATH_UPDATER=1`은 러너 자신의 재기동만 통과시킨다. |
| 교체 방식 | 같은 파일 시스템 안에서 rename과 저널로 교체한다. 헬스체크 통과 전에 중단되면 롤백으로 본다. |
| platforms 클레임 | 값은 `osdep.NAME`(`win`·`mac`·`linux`)이다. 이번 단계 릴리스는 `["win"]`로 서명한다(리눅스는 시험용, 맥은 미완). 클레임이 없으면 `win` 전용으로 본다. |
| 자동 시작 | 등록하지 않고 안내만 한다. 시험과 임시용으로 `watch`를 둔다. |
| 비밀 보호 | `DATA_DIR` 0700, 비밀 파일 0600. umask는 바꾸지 않는다. |
| 토큰 전달 | env `CLEWPATH_CLIENT_TOKEN`·`CLEWPATH_AGENT_TOKEN`. argv로 받으면 경고하고 env로 옮긴다. |

### 1.4 구현 순서 (권장)

1. 파이썬 연결부와 정적 테스트
2. 런처 템플릿·렌더러와 런처 실행 테스트
3. update_runner.sh와 러너 실행 테스트
4. install.sh와 hostctl
5. uninstall.sh
6. get.sh(007의 새 파일. 라우트가 없으니 아직 서빙되지 않는다)
7. 패키징·서명·릴리스 게이트
8. 컨테이너 e2e
9. 문서

릴리스는 Windows 전체 회귀와 컨테이너 e2e를 통과한 뒤 `platforms=["win"]`로 낸다. Windows 사용자에게도 배포되는 릴리스다.

**구현 메모 (9단계 문서, 2026-10-21)**
- README: 상세 레퍼런스에 「맥·리눅스 (시험용 — 한 줄 설치는 아직 공개 전)」 절을 두고 사용자 매뉴얼 1절(준비물)에서 가리킨다.
  번호 절(1~9)은 Windows 그대로라 'README 8절' 같은 참조가 바뀌지 않는다. 공개 주소(`…/get.sh`)는 적지 않았다 — 지금 붙여 넣으면
  안 되는 명령이라서. 업데이트 교체 목록을 PAYLOAD 와 맞췄다(전엔 LICENSE 도 빠져 있었다).
- multi-os.md: 단계 표의 3 을 3a(이번, 공통 설치·업데이트)·3b(맥 실기)로 나누고 상태 칸을 더했다. 「3a 결과」 절.
- TODOS: 멀티 OS 3a 진행(1~9)·합침·후속, 3b, 리눅스 정식 지원(결정 전), 양 OS 별건(Q5·Q15·Q16·R6), 그리고 8.1 R15 의 Windows
  기존 결함 ①~⑦ 을 별건 절로(건마다 왜·무엇·시작점).
- CHANGELOG 는 손대지 않았다(1.1-5 와 다름) — 릴리스 때 `release.ps1 -PrepareChangelog` 가 쓴다.
- 문서와 코드가 어긋나지 않게 `tests/test_docs_multi_os.py`: README 가 보여 주는 명령·옵션·환경변수·저장소 경로가 스크립트에 있는지,
  사용자가 쓸 런처 하위 명령·제거 옵션이 빠짐없는지, 설치 위치·파이썬 최소 버전·자동 시작 없음·'Windows 전용 릴리스' 말이 코드와
  같은지, README 에 공개 전 get.sh 주소가 없는지, 교체 목록 = PAYLOAD, 단계 표, TODOS 의 R15 ①~⑦ = 이 문서 8.1.
  CP `/get.sh` 를 열 때 그 시험('공개 주소 없음')도 함께 바꾼다.
- (검토 반영) 이 시험은 기본 pytest(= release.ps1 의 게이트)에 들어가므로 문서를 코드와 묶을 뿐 진행 상태(할 일 체크·단계 상태
  칸·고친 R15 항목 지우기)에는 묶지 않는다 — 그런 편집을 사본에 해 보고 여전히 통과하는지 보는 시험(`*_survives_*`)을 함께 둔다.

## 2. 파일 목록

### 2.1 새 파일

| 파일 | 위치 | zip | PAYLOAD | 비고 |
|---|---|---|---|---|
| `install.sh` | 006 루트 | 포함 | **추가** | install.ps1에 대응. `#!/bin/sh`, LF, BOM 없음 |
| `uninstall.sh` | 006 루트 | 포함 | **추가** | README 8절 수동 제거 절차의 스크립트판 |
| `session_manager/start-connector.template.sh` | 006 | 포함(폴더째) | (session_manager) | 런처의 원본. 자리표시 `{{DATA_DIR}}`·`{{CONF_FILE}}`·`{{PORT}}` |
| `session_manager/update_runner.sh` | 006 | 포함 | (session_manager) | 이름은 이미 `_posix.py:18`에 있다 |
| `session_manager/hostctl.py` | 006 | 포함 | (session_manager) | `python -I -m session_manager.hostctl`. 설치 때만 쓴다 |
| `start-connector.sh` | 설치 루트(생성물) | 제외 | 아님 | 렌더 결과, 0755. 이름은 `_posix.py:17`에 있다 |
| `.clewpath-install` | 설치 루트(생성물) | 제외 | 아님 | 설치 기록(`key=value`). 러너·제거 스크립트가 읽는다 |
| `.clewpath-swap.<pid>/` | 설치 루트(일시) | 없음 | 없음 | 러너의 교체 저널. 성공하거나 복구한 뒤 지운다 |
| `control_plane/get.sh` | **007** 새 파일 | (CP가 서빙) | 없음 | 라우트가 생기기 전에는 서빙되지 않는다(7절) |
| `tests/test_posix_scripts.py` | 006 | 없음 | 없음 | 정적 계약. 모든 OS에서 돈다 |
| `tests/test_hostctl.py` | 006 | 없음 | 없음 | 렌더·설정·경로 |
| `tests/test_launcher_sh.py` | 006 | 없음 | 없음 | POSIX 실행 시험(win32에서는 건너뜀) |
| `tests/test_update_runner_sh.py` | 006 | 없음 | 없음 | POSIX 실행 시험 |
| `tests/posix_e2e/` (`build_fixtures.py`, `fake_cp.py`, `e2e_driver.py`, `Dockerfile`, `warm_cache.sh`, `README.md`) | 006 | 없음 | 없음 | 컨테이너 e2e 재료 |
| `tests/test_posix_e2e.py` | 006 | 없음 | 없음 | `-m posix_e2e`. 기본 실행에서는 빠진다 |
| `tests/test_docs_multi_os.py` | 006 | 없음 | 없음 | 문서 계약(9단계) — README 맥·리눅스 절·multi-os 단계 표·TODOS 가 코드·이 문서와 맞는지. 모든 OS |

### 2.2 바꿀 파일

| 파일 | 바꿀 것 |
|---|---|
| `session_manager/osdep/_posix.py` | `update_runner_argv` 구현<br>`ensure_launcher`(렌더 → 비교 → 원자 교체)<br>`render_launcher`, `port_free`, `CURL="curl"`<br>머리말 갱신 |
| `session_manager/osdep/_win.py` | `port_free`(지금 `server._port_free` 본문을 그대로 옮김), `CURL="curl.exe"` |
| `session_manager/osdep/__init__.py` | `port_free`·`CURL` 공개, 머리말 갱신 |
| `session_manager/updater.py` | PAYLOAD에 `install.sh`·`uninstall.sh` 추가<br>platforms 판정<br>maintenance.flag가 있으면 apply 거부<br>머리말 교체 목록 정정(`:21-23`이 실제 PAYLOAD와 다르다) |
| `session_manager/update_runner.ps1` | `$Payload`에 `install.sh`·`uninstall.sh` 추가(`:40-41`, BOM과 PS5.1 문법 유지) |
| `session_manager/server.py` | `_port_free`가 `osdep.port_free`를 부르게<br>`_clewpath_already_there`가 프록시를 쓰지 않게<br>lifespan이 `relay_config_from_env(BOUND_PORT)`를 부르게 |
| `session_manager/connector.py` | `relay_config_from_env(bound_port=None)`: 폴백 포트를 반영 |
| `session_manager/hooks.py` | `hook_command`의 curl 이름을 `osdep.CURL`로 |
| `session_manager/cp_client.py` | provision 표시 이름 기본값을 `appconfig.machine_name()`으로 |
| `session_manager/appconfig.py` | `render_template` 확장(client_token·agent_token·config_dir, TOML 이스케이프). POSIX toml의 원본이 된다 |
| `session_manager/liveness.py` | `mark_shutdown(reason, pid=None)`<br>(선택) `log_tail`이 마지막 런처 구분줄 앞까지만 읽기 |
| `ops/package-for-user.ps1` | `$include`에 `install.sh`·`uninstall.sh`<br>제외 목록에 `start-connector.sh`·`.clewpath-install`<br>비밀 스캔 확장자에 `.sh`<br>`.sh` 형식 게이트 |
| `ops/tools/sign_release.py` | `sign --platforms`(기본 `win`), verify 출력에 표시 |
| `ops/release.ps1` | `-Platforms`(기본 `win`) 전달, zip 내용 게이트 |
| `.gitignore` | `start-connector.sh`, `.clewpath-install`, `.clewpath-swap.*` |
| `.gitattributes` | `*.sh text eol=lf` 명시(`* text=auto eol=lf`로도 LF지만 의도를 고정) |
| `pyproject.toml` | 마커 `posix_e2e` 추가, addopts를 `-m 'not e2e and not posix_e2e'`로 |
| 기존 테스트 | `test_osdep`(argv 예외 제거), `test_update_safety`(`_WIN_RUNNER` 해제), `test_updater`, `test_hooks`, `test_liveness`, `test_appconfig`, `test_release_key_safety`, `test_release_script` (4.7절) |
| 문서 | README, multi-os.md, CHANGELOG, TODOS |

다음 파일은 바꾸지 않는다: install.ps1, start-connector.template.ps1, ensure_task.ps1, 007 get.ps1.

### 2.3 설치 후 레이아웃 (POSIX)

```
$CLEWPATH_HOME/
├─ app/                          설치 루트(ROOT). get.sh --dir 로 바꿀 수 있다
│  ├─ session_manager/ pwa/ pyproject.toml uv.lock install.ps1 install.sh uninstall.sh README.md LICENSE   ← PAYLOAD
│  ├─ .venv/                     uv sync 로 맞춘다(업데이트는 교체하지 않고 sync)
│  ├─ clewpath_host.egg-info/    uv sync 의 편집 설치(setuptools)가 만든다 — PAYLOAD 밖, sync 가 버전마다 다시 쓴다(8단계 실측)
│  ├─ start-connector.sh         렌더 결과(0755)
│  └─ .clewpath-install          설치 기록
├─ bin/uv, bin/uvx               고정 버전, 0755
├─ python/                       uv 관리 런타임 — [mac] 늘, [linux] 시스템 파이썬이 없을 때만
└─ (일시) ../.clewpath-get.XXXXXX/   get.sh 임시 폴더(설치 루트의 부모 = 같은 파일 시스템)
<claude_home>/session_manager/   DATA_DIR(0700) — Windows 와 같은 파일 + update.inprogress, watch.lock.d/,
                                 launcher.lock/(폴더), updates/runner.lock.d/, updates/update_runner.sh(복사본)
```

`.clewpath-install`의 형식은 다음과 같다. 소스로 읽어 실행하지 않고 sed로만 읽는다.

```
# clewpath-install v1 — install.sh 가 쓴다. 손대지 마세요
home=/home/u/.local/share/clewpath
uv=/home/u/.local/share/clewpath/bin/uv
python=/usr/local/bin/python3.11
python_pref=only-system            ([mac] only-managed)
python_install_dir=/home/u/.local/share/clewpath/python
data_dir=/home/u/.claude/session_manager
conf_file=/home/u/.claude/session_manager/clewpath.toml
installed_version=0.29.0
installed_at=1760900000
```

## 3. 스크립트별 상세 동작

### 3.0 공통 규칙 (다섯 스크립트 모두)

**셸 문법**
- `/bin/sh`(POSIX)만 쓴다. [linux]는 dash, [mac]은 bash 3.2의 sh 모드다.
- 쓰지 않는 문법: `[[ ]]`, 배열, `function` 키워드, `$'..'`, `<<<`, `source`, `[ a == b ]`, `pipefail`, `local`(함수 변수는 접두사로 구분), `echo -e`/`-n`(printf만 쓴다).
- `:`에 리디렉션을 붙이지 않는다(`: >f` 대신 `true >f`). `:`는 특수 내장이라 리디렉션이 실패하면 dash·`bash --posix`가 `|| …`로 넘어가지 않고 스크립트를 통째로 끝낸다(4단계 실측). 같은 이유로 `exec 3</dev/tty` 같은 특수 내장 리디렉션은 하위 셸 `( … )` 안에서만 시험한다.
- 쓰지 않는 GNU 전용 옵션: `sed -i`, `readlink -f`, `stat -c`/`-f`, `date -d`, `cp -T`/`mv -T`.
- [mac]에 없는 도구: `timeout`, `flock`, `setsid`.
- [linux] slim 이미지에 없는 도구(실측): `ps`, `pgrep`, `pkill`, `curl`, `unzip`. 이것들에 혼자 기대지 않는다.
- GNU와 BSD에 다 있는 것은 쓴다: `find -maxdepth`·`-mmin`, `mktemp -d 템플릿`, `ls -A`, `awk`, `sed -n 's///p'`, `tr`, `wc -c`, `kill -0`, `date +%s`.

**파일 형식**
- LF, BOM 없음, 1행 `#!/bin/sh`.
- 실행 비트에 기대지 않는다. zip에 유닉스 권한이 없고(0.28.1 실측 `create_system=0`, `external_attr=0`), `zipfile`·`copy2`도 0644 그대로 둔다. 그래서 늘 `/bin/sh 파일`로 부른다. 생성물인 `start-connector.sh`만 0755로 둔다.

**실패 처리**
- get.sh·install.sh·uninstall.sh: `set -u`를 쓰고, 명령마다 결과를 확인한다(`… || die "…"`).
- update_runner.sh: `set -e`·`set -u`를 쓰지 않는다. 중간에 죽으면 반쯤 교체된 설치 폴더가 남는다(update_runner.ps1:17-18 원칙).
- pipefail이 없으므로 파이프 종료코드는 믿지 않는다. 중요한 명령은 출력을 임시 파일로 받고 종료코드를 직접 본다.

**경로**
- 변수를 펼칠 때는 늘 큰따옴표로 감싼다. [mac] `Application Support` 같은 공백 경로와 한글 경로를 시험한다.
- 경로에 줄바꿈이나 제어문자가 있으면 거부한다.
- 인자 목록을 다시 만들 때는 배열 대신 `set -- "$@" …`로 쌓는다.

**출력**
- 접두는 `[OK]`·`[WARN]`·`[FAIL]`이다(install.ps1:110-112와 같은 말). 색은 쓰지 않는다.
- 토큰과 비밀값은 출력하지 않는다.
- `set -x`와 env 덤프는 금지한다. 런처와 러너는 Host의 env(`SM_PASSWORD`·`SM_API_TOKEN`·`SM_RELAY_*`)를 물려받는다.

**umask와 env**
- Host를 띄우는 모든 경로는 사용자의 umask·HOME·TMPDIR·PATH를 바꾸지 않는다. 해당 경로: install.sh 첫 기동, 런처, 러너 재기동, get.sh(exec가 umask를 물려줌).
- 비밀 파일은 `( umask 077; … )` 하위 셸에서만 만든다.

**JSON과 HTTP**
- jq는 쓰지 않는다. JSON은 venv 파이썬으로 읽는다(`-I`, 표준 라이브러리와 psutil).
- get.sh만 예외다. 아직 venv가 없으므로 압축된 ASCII JSON을 sed·awk로 읽고 형식을 검증한다.
- 루프백 HTTP는 프록시를 쓰지 않는다. 파이썬은 `build_opener(ProxyHandler({}))`(httpx 는 `trust_env=False`, websockets 는 `proxy=None`), curl은 `--noproxy '*'`(따옴표를 못 쓰는 훅 명령은 `--noproxy 127.0.0.1`).

**공통 경로 블록**
get.sh·install.sh·uninstall.sh·update_runner.sh에 똑같이 넣는다. 정적 테스트로 네 사본이 같은지 확인한다.

```sh
# >>> clewpath-home (get.sh·install.sh·uninstall.sh·update_runner.sh 공통 — 바꾸면 넷 다)
case "$(uname -s)" in
  Darwin) CLEWPATH_HOME="${CLEWPATH_HOME:-$HOME/Library/Application Support/ClewPath}" ;;
  *)      _xdg="${XDG_DATA_HOME:-}"; case "$_xdg" in /*) ;; *) _xdg="$HOME/.local/share" ;; esac
          CLEWPATH_HOME="${CLEWPATH_HOME:-$_xdg/clewpath}" ;;
esac
# <<< clewpath-home
```

**데이터 폴더**
- 원본은 파이썬이다(config.py:8-23·69-74): `SESSION_MANAGER_CLAUDE_HOME`이 있으면 그것, 없으면 `CLAUDE_CONFIG_DIR`, 없으면 `~/.claude`. 그 아래 `session_manager`다.
- 셸에서는 venv가 생기기 전에만 같은 식으로 계산한다. 그 뒤로는 `hostctl paths`와 `start-connector.sh paths`를 믿는다.

**표식과 잠금 (공통 모양)**
- `maintenance.flag`: 사람이 만들거나 `stop --hold`가 만든다. 있으면 런처·watch·foreground가 아무것도 띄우지 않는다.
- `update.inprogress`: 내용은 `pid=<pid>`, `at=<epoch>`, `by=runner|install`이다.
  - pid가 살아 있고 30분이 안 됐으면 "진행 중"으로 본다.
  - 그 밖이면 버려진 표식(시체)으로 보고 지운 뒤 기록한다.
- 잠금 폴더(`launcher.lock/`, `watch.lock.d/`, `updates/runner.lock.d/`):
  - `mkdir`로 원자적으로 잡고, 안에 `pid` 파일을 둔다.
  - pid가 죽었으면 시체다. pid 파일이 없고 폴더가 2분 넘게 묵었으면(`find -maxdepth 0 -mmin +2`)도 시체다.
  - 시체면 지우고 한 번만 다시 잡는다. 치우는 것은 옆 폴더 `<잠금>.reclaim`(mkdir)을 쥔 하나만 — 쥔 채 다시 보고 지우고
    새로 잡은 뒤 놓는다(둘이 같은 시체를 따로 지우면 늦은 쪽이 먼저 잡은 쪽의 새 잠금을 지워 둘 다 잡았다고 믿는다).
    권리 폴더가 2분 넘게 남았으면 치우다 죽은 것이다(검토 반영).

### 3.1 get.sh (`007/control_plane/get.sh`, CP가 서빙)

**한 줄 설치**

```
curl -fsSL https://clewpath.pyongso.com/get.sh | sh
curl -fsSL …/get.sh | sh -s -- --port 5123                       # 옵션
curl -fsSL …/get.sh | CLEWPATH_CLIENT_TOKEN=… sh                 # 토큰은 env(argv 는 다른 사용자에게 보임)
```

**머리말에 적을 신뢰 모델** (get.ps1:13-19와 같다)
- 첫 설치의 신뢰 근거는 TLS다.
- 매니페스트 서명은 여기서 검증하지 않는다. 서버가 뚫리면 스크립트째 바뀌기 때문이다.
- zip의 SHA-256과 크기만 매니페스트와 대조한다.
- 설치가 끝난 뒤 업데이트부터는 코드에 박힌 공개키로 서명을 검증한다.

**인자**

| 인자 | 뜻 |
|---|---|
| `--cp-url URL` | 기본 `https://clewpath.pyongso.com/cp` |
| `--dir DIR` | 기본 `$CLEWPATH_HOME/app` |
| `--port N` | 1-65535, 기본 5100 |
| `--relay-url URL`, `--app-url URL` | install.sh로 전달 |
| `--client-token T` | 받기는 하되 `[WARN]` 후 env로 옮긴다 |
| `--no-autostart` | 전달만(이번 단계는 의미 없음) |
| `--no-provision` | 전달(오프라인·시험용) |
| `--allow-http` | 시험 전용. http CP·URL을 허용하고 `[WARN]` |
| `--allow-root` | 시험 전용 |
| `-h` | 도움말 |
| 모르는 인자 | `[FAIL]` 후 종료코드 2 |

**구조**
본문 전체를 `main() { … }`에 넣고 마지막 줄을 `main "$@"`로 한다. `curl | sh`가 중간에 끊겨도 반쪽 스크립트가 실행되지 않는다.

**단계**

1. **G0 환경 점검**
   - `uname -s`로 OS를 정한다: Darwin은 mac, Linux는 linux, 그 밖은 `[FAIL] 지원하지 않는 OS`.
   - [linux]면 `[WARN] 리눅스는 시험용입니다(공식 지원 아님)`.
   - `id -u`가 0이고 `--allow-root`가 없으면 `[FAIL]`.
   - `$HOME`이 절대 경로인지 본다.
   - 필요한 도구: `curl`(한 줄 설치 명령 자체가 curl이므로 wget 폴백은 두지 않는다), `mktemp`, `tr`, `sed`, `awk`, `wc`, `find`, `dirname`. SHA-256 도구는 `sha256sum` → `shasum -a 256` → `openssl dgst -sha256 -r` 순으로 찾는다. base64 디코드는 `base64 -d` → `base64 -D` → `openssl base64 -d -A` 순으로 찾는다. zip 도구는 `unzip`, 없으면 [linux]만 `python3 -I`로 대신한다. [mac]에서는 python3를 부르지 않는다(CLT 스텁).
   - 도구가 없으면 무엇을 설치하라고 말하고 종료코드 1로 끝낸다.
2. **G1 경로 정리**
   - 공통 경로 블록을 실행한다. `DIR`의 기본값은 `$CLEWPATH_HOME/app`이다.
   - CP 주소 끝의 `/`는 반복해서 지운다(get.ps1:38).
   - CP가 `https://`로 시작해야 한다(`--allow-http`면 `http://`도 허용).
3. **G2 매니페스트**
   - `curl --proto '=https' --proto-redir '=https' --tlsv1.2 -sS --max-time 20 -o "$tmp/m" -w '%{http_code}' "$CP/updates/manifest"`
   - 404: `[FAIL] 아직 게시된 릴리스가 없습니다. 운영자에게 문의하세요.`
   - curl 실패나 200이 아닌 응답: `[FAIL] 릴리스 서버에 닿지 못했습니다: …`
   - 둘 다 종료코드 1이다. Windows의 `return`과 달리 종료코드에 의미가 있다.
   - JWT 모양을 확인한다: 점으로 나뉜 세 조각, 각 조각은 `[A-Za-z0-9_-]`만.
4. **G3 내용 읽기**
   - 두 번째 조각에 `tr '_-' '/+'`를 하고, 길이를 4로 나눈 나머지에 맞춰 `=`를 붙인다(나머지가 1이면 실패). 그다음 base64로 디코드한다.
   - 결과는 압축된 ASCII JSON이다. 한글 notes는 `\uXXXX`로 이스케이프돼 있다(0.28.1 실측).
   - 필드 추출: `sed -n 's/.*[{,]"sha256":"\([0-9a-f]*\)".*/\1/p'` 식으로 뽑는다. 키 앞에 `{`나 `,`를 요구하므로 notes 안의 `\"` 이스케이프와 헷갈리지 않는다.
   - 각 키가 정확히 한 번 나오는지 `awk '{n=gsub(/[{,]"sha256":/,"&")} END{print n}'`로 센다.
   - 검증(하나라도 어긋나면 `[FAIL]` 후 종료코드 1):
     - `ver`가 `^[0-9]+\.[0-9]+\.[0-9]+$`
     - `sha256`이 `^[0-9a-f]{64}$`
     - `size`가 `^[0-9]+$`이고 0보다 크다
     - `url`이 `^https://[^"[:space:][:cntrl:]]+$`(`--allow-http`면 http도)
   - platforms 확인:
     - `"platforms":[…]`가 있으면 그 안에 `"$OS"`가 있어야 한다.
     - 없으면 legacy(win 전용)로 본다.
     - 어긋나면 `[FAIL] 이 릴리스(<ver>)는 맥·리눅스 설치를 지원하지 않습니다.` 지금 운영 중인 0.28.1이 바로 이 경우다.
5. **G4 임시 폴더**
   - `mkdir -p "$(dirname "$DIR")"`
   - `tmp=$(mktemp -d "$(dirname "$DIR")/.clewpath-get.XXXXXX")`
   - 설치 루트와 같은 파일 시스템에 만들어야 이후의 mv가 rename이 된다.
   - `trap 'rm -rf "$tmp"' EXIT INT TERM HUP`
6. **G5 내려받기와 무결성**
   - `curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --max-time 300 -o "$tmp/clewpath-host.zip" "$url"`
   - SHA-256(소문자)이 `sha256`과 같은지, `wc -c`가 `size`와 같은지 본다.
   - 다르면 get.ps1과 같은 문구(`SHA-256 불일치 - 받은 파일을 버립니다. 다시 시도하세요.` / `크기 불일치 - 다시 시도하세요.`)를 내고 종료코드 1.
7. **G6 이름 검사 후 압축 해제**
   - 목록은 `unzip -Z1`, 또는 [linux] `python3 -I` zipfile의 namelist로 얻는다.
   - 다음 이름이 하나라도 있으면 거부한다:
     - 빈 이름, `/`로 시작, `\` 포함, 제어문자 포함
     - `..`이 경로 조각으로 들어 있음(`..`, `../*`, `*/../*`, `*/..`)
     - 최상위 이름이 `.`로 시작(예: `.venv`)
     - 최상위 이름이 `start-connector.sh`
   - 심볼릭 링크 항목도 거부한다. `unzip -Z`의 권한 열이 `l`로 시작하는지 보거나, zipfile `external_attr>>16`이 S_IFLNK인지 본다.
   - `"$tmp/stage"`에 푼다(`unzip -q -o -d`, 또는 [linux] `python3 -I` zipfile extractall).
   - 사후 확인:
     - `find "$tmp/stage" -type l`이 비어 있어야 한다.
     - `stage/session_manager/__init__.py`와 `stage/install.sh`가 있어야 한다. 없으면 G3과 같은 "지원하지 않습니다" 문구.
8. **G7 출력**: 설치 위치, 최신 버전, `[OK] 무결성 확인 (SHA-256)`, `[OK] 압축 해제`.
9. **G8 넘기기**
   - `trap - EXIT INT TERM HUP`. 임시 폴더는 install.sh가 지운다.
   - 토큰은 env로 export한다.
   - 다음을 실행한다. 인자는 `set -- …`로 쌓는다.
     ```
     exec /bin/sh "$tmp/stage/install.sh" --from-stage "$tmp/stage" --dir "$DIR" --cp-url "$CP" --port "$PORT" [전달 인자…] </dev/null
     ```
   - 표준 입력은 이미 다 읽힌 파이프다. 뒤따르는 스크립트는 대화형 입력을 받지 않는다.

**종료코드**: get.sh 자신은 1(실패)과 2(인자 오류)만 낸다. 성공하면 exec 하므로 install.sh의 종료코드가 그대로 사용자에게 간다.

**get.ps1과의 1:1 대응**

| get.ps1 | get.sh | 차이와 이유 |
|---|---|---|
| :5-7 `irm \| iex`, `[scriptblock]::Create` 옵션 | `curl … \| sh`, `\| sh -s -- …` | |
| :24-32 인자 | 위 표의 인자. `-NoWinget`은 없다 | 토큰은 env |
| :34 EAP=Stop | `set -u`, 명령마다 확인, `main()` 감싸기 | 끊긴 실행 방지 |
| :35 TLS1.2 | `--proto '=https' --proto-redir '=https' --tlsv1.2` | 리다이렉트로 http 강등 차단 |
| :37 `%LOCALAPPDATA%\ClewPath\app` | `$CLEWPATH_HOME/app` | |
| :38 CpUrl 끝 `/` 제거 | 반복 제거와 https 검사 | |
| :47-58 매니페스트, 404 구분, `return` | 같은 문구, `exit 1` | 종료코드에 의미 |
| :59-62 base64url 디코드, ConvertFrom-Json | tr·패딩·base64, sed·awk, 키 1회·형식·platforms 검증 | jq·python 무의존 |
| :65-69 `%TEMP%\clewpath-get-<PID>`, IWR 300초 | 설치 루트 옆 `mktemp -d`, curl 300초 | mv가 rename이 되게 |
| :71-81 SHA-256·크기 | sha256sum, shasum, openssl / `wc -c` | |
| :88-90 설치 폴더 아래 python 일괄 `Stop-Process -Force` | **하지 않는다.** install.sh `--from-stage`가 런처 stop(SIGTERM)을 부른다 | 브로커·터미널 보존, 정상 종료 표식 |
| :91-93 `Expand-Archive -Force`(제자리) | 이름·링크 검사, stage에 풀기, install.sh가 최상위 항목별로 교체 | zip-slip, 반쪽 덮어쓰기 감소 |
| :97-108 install.ps1 실행(종료코드 무시) | `exec /bin/sh …/install.sh --from-stage …` | 종료코드 전달 |

**구현 메모 (6단계, 2026-10-21 — 위 설계와 다른 점과 뒤 단계가 맞출 약속)**
- 위치: 결정대로 006 의 `ops/get.sh`(라우트가 없으니 서빙되지 않는다). CP 공개 때 007 `control_plane/get.sh`로 옮긴다 — 시험은 env `CLEWPATH_GET_SH`로 그 파일을 가리킬 수 있다.
- 순서: 받는 것(매니페스트·zip·이름 목록)은 `${TMPDIR:-/tmp}/clewpath-get.XXXXXX`(작업 폴더)에 두고, 설치 루트 옆 `.clewpath-get.XXXXXX/stage`는 zip 해시·크기와 이름 검사를 통과한 뒤에야 만든다. 그래서 거부될 때(S1) 설치 폴더 옆에는 아무것도 생기지 않는다(설계는 G4 임시 폴더를 G2 전에 만들고 zip 도 거기 받았다). 작업 폴더는 exec 직전에 지운다.
- 받기 전에 설치 폴더를 본다(install.sh 서막과 같은 거절 — 폴더가 아닌 것·개발 체크아웃(.git·ops)·비어 있지 않은 남의 폴더). 받은 뒤 거절하면 헛걸음이고 임시 폴더만 남는다. `--dir`은 절대 경로만(상대·`.`·`..` 조각·`/`·홈은 2). 설치 폴더가 심볼릭 링크면 물리 경로로 넘긴다 — install.sh 가 `pwd -P`로 맞춰 보는 자리 옆에 stage 가 있어야 옮긴 뒤 치울 수 있다.
- curl 은 `g_curl` 한 곳: `--proto`·`--proto-redir`(`=https`, `--allow-http`일 때만 `=http,https`)·`--tlsv1.2`, 매니페스트 `--max-filesize 65536`·20초, zip `--max-filesize <size>`(넘으면 curl 63 → "크기 불일치")·300초. 프록시는 사용자 설정을 따르고 이 PC(127.0.0.1·localhost·[::1])로 가는 요청만 `--noproxy '*'` — 설계의 `--noproxy '*'`를 늘 주면 사내 프록시가 필요한 PC 에서 CP 에 닿지 못한다.
- 매니페스트: 64KB 이하, 세 조각, 내용은 한 줄 인쇄 가능 ASCII(`tr -d ' -~'`가 빈 결과)이고 `{…}`. `ver`·`sha256`·`size`·`url`은 값 자리(앞이 `{`·`,`)에 정확히 한 번. `size`는 9자리까지. `url`에 공백·제어문자·따옴표·역슬래시가 있으면 거부(이스케이프를 풀지 않는다). platforms 목록의 이름은 `[a-z0-9_-]`(앞으로의 이름은 그냥 고르지 않을 뿐), 목록이 아니거나 모양이 이상하면 아무 OS 도 받지 않는다(updater 와 같다).
- zip 이름 규칙에 더한 것(실측 근거): `^`가 든 이름 — unzip 목록은 제어문자를 `^A`·`^J`로 보여 주고 풀 때 그 글자를 지운다(`\001../x`가 `^A../x`로 보이다가 `../x`가 된다). 같은 이름(대소문자 무시 — APFS) — `unzip -o`는 뒤의 것을 조용히 남긴다. 빈 zip. 심볼릭 링크는 `unzip -Z`의 권한 열(`l`), [linux] python 길은 `external_attr`.
- 푼 뒤 `chmod -R u+rwX stage`(내 권한만, 링크는 따라가지 않는다)를 사후 확인보다 먼저 한다 — zip 에 유닉스 권한이 적혀 있으면 unzip 이 그대로 쓴다(권한 0 폴더면 사후 확인이 그 안을 못 본다). 배포 zip 은 Windows 에서 만들어 권한이 없다(`create_system=0`·`external_attr=0`). 실패 정리도 권한을 채운 뒤 지운다.
- 없는 도구는 한 번에 모두 알린다(curl·mktemp·tr·sed·awk·wc·find·dirname·sort·uniq, SHA-256 도구, base64 도구, unzip — [linux]만 python3 대역). 리눅스 경고는 get.sh 와 install.sh 가 한 번씩 낸다. `--agent-token` 인자는 없다(env `CLEWPATH_AGENT_TOKEN`은 그대로 넘어간다).
- install.sh(4단계 파일): 서막이 stage 를 확인하고 설치 폴더를 정한 뒤로는 실패해도 get.sh 의 임시 폴더를 지운다(`GET_TMP` — 이름이 `.clewpath-get.*`이고 설치 루트 옆일 때만). 전에는 성공했을 때만 지워, 업데이트 중 거절 같은 실패마다 받은 payload 가 숨은 폴더로 남았다. 손으로 푼 stage·이름만 닮은 다른 곳의 폴더는 지우지 않는다(시험으로 고정).
- 알려진 한계: `curl | sh`가 정확히 마지막 줄의 `main` 바로 뒤에서 끊기면 인자 없이(기본값으로) 돈다 — 모든 `curl | sh` 설치기의 공통 한계이고 결과는 기본 설치다. [mac] 셸(bash 3.2 sh 모드)·BSD 도구는 실기 확인 전이다(R1). Git for Windows 의 dash 는 UTF-8 이어지는 바이트(0x80-0x9F)를 `[[:cntrl:]]`로 보지만(시험 하네스 사정) 리눅스 dash·`bash --posix`는 아니다.
- 시험: `tests/test_get_sh.py` — 함수 시험(마지막 줄을 뺀 사본을 source, sign_release 가 만든 진짜 매니페스트, 모든 OS)과 실행 시험(가짜 CP, zip 안의 install.sh 는 받은 인자·env·pid·표준 입력을 적는 대역, S1 거부 22가지, 끊긴 다운로드, 파이프 실행, 도구 대역으로 [mac]·옛 맥 base64·shasum·openssl·python 길). `tests/test_install_sh.py::test_get_sh_one_liner_installs`가 `curl …/get.sh | sh` → 진짜 install.sh → Host 기동까지 잇는다. 정적 계약(`test_posix_scripts`): 함수 밖에는 주석·`set -u`·값 대입·함수 정의뿐, curl 은 한 곳, 머리말의 신뢰 모델. 리눅스 컨테이너에서 get.sh·install.sh·sign_release 에 버그를 하나씩 넣어(54종) 모두 겨냥한 단정에서 잡히는 것을 확인했다.

### 3.2 install.sh (zip 루트, install.ps1에 대응)

**인자**

| 인자 | 뜻 |
|---|---|
| `--cp-url` | 끝 `/` 제거, https 확인 |
| `--relay-url` | 기본 `wss://clewpath.pyongso.com/relay/ws` |
| `--app-url` | 기본 `https://clewpath.pyongso.com/relay/app` |
| `--port` | 기본 5100 |
| `--python` | 기본 3.11 |
| `--uv-version` | 기본 0.12.1. install.ps1의 `$UvDefault`와 같아야 한다(테스트로 고정) |
| `--system-uv`, `--reinstall`, `--no-autostart`, `--dry-run` | install.ps1과 같은 뜻 |
| `--no-provision`, `--allow-root`, `--allow-http` | 시험·오프라인용 |
| `--from-stage DIR --dir DIR` | get.sh가 쓴다 |
| `--client-token`, `--agent-token` | 경고 후 env로 옮긴다 |

**env**

| env | 뜻 |
|---|---|
| `CLEWPATH_CLIENT_TOKEN`, `CLEWPATH_AGENT_TOKEN` | 토큰 |
| `CLEWPATH_UV_ARCHIVE` | 로컬 uv tar.gz. 고정 해시표와 대조하며, 표에 없는 버전이면 거부 |
| `CLEWPATH_HOME`, `XDG_DATA_HOME` | 경로 규칙 |
| `SESSION_MANAGER_CLAUDE_HOME`, `CLAUDE_CONFIG_DIR` | 데이터 폴더 규칙 |
| `UV_SYSTEM_CERTS` | 없으면 1 |

**단계**

**I0. 사전 점검**
- 인자를 해석한다. root면 거부한다(`--allow-root`가 있을 때만 허용).
- [linux]면 시험용 경고를 낸다.
- `ROOT=$(cd "$(dirname "$0")" && pwd -P)`. 물리 경로를 쓰는 이유는 파이썬 `install_root()`가 `resolve()`로 구하는 경로와 맞추기 위해서다.
- `"$ROOT/session_manager"`가 없으면 install.ps1:200-203과 같은 문구로 `[FAIL]` 후 종료코드 1.
- ROOT에 `.git`이나 `ops/`가 있으면 `[WARN] 개발 체크아웃 — 자동 업데이트는 꺼진 채로 동작`.
- `UV_SYSTEM_CERTS`가 없으면 1로 둔다.
- 필요한 도구: `curl`(uv 내려받기와 dry-run healthz에만. `CLEWPATH_UV_ARCHIVE`나 `--system-uv`면 없어도 된다), `tar`, `gzip`, SHA-256 도구, `mktemp`.
- 명령 출력은 임시 로그로 받고, 실패할 때만 `tail -n 25`를 보여 준다(install.ps1:90-109에 대응).

**I1. `--from-stage` 서막**
1. stage 확인: `STAGE/session_manager/__init__.py`와 `STAGE/install.sh`가 있어야 한다.
2. DIR 확인: 절대 경로여야 하고, `/`나 `$HOME`이 아니어야 하며, DIR에 `.git`이나 `ops/`가 있으면 거부한다(개발 트리 위에 설치하지 않음).
3. `DATA_DIR` 추정: `DIR/start-connector.sh`가 있으면 `sh "$DIR/start-connector.sh" paths`의 `data_dir`, 없으면 셸 규칙.
4. 표식 획득: `update.inprogress`가 살아 있고 그 pid가 `$$`가 아니면 `[FAIL] 업데이트·설치가 진행 중입니다(pid N)` 후 종료코드 1. 아니면 `pid=$$ by=install`로 쓴다(하위 셸에서 `umask 077`).
5. 기존 Host 정지: `DIR/start-connector.sh`가 있으면 `CLEWPATH_UPDATER=1 /bin/sh "$DIR/start-connector.sh" stop`(SIGTERM, 브로커 보존). 없으면 `stop_host_fallback`을 쓴다. 이 함수는 `runtime.json`의 pid를 보고, 명령줄에 `"$DIR/.venv/bin/"`와 `session_manager.server`가 둘 다 있을 때만 TERM을 보내고 15초 뒤 KILL 한다. 명령줄은 [linux] `/proc/<pid>/cmdline`, [mac] `ps -p <pid> -o command=`로 읽는다.
6. 배치: stage 최상위 항목마다 이름을 다시 검사한 뒤 옛 항목을 `"$DIR/.clewpath-old.<pid>/"`로 rename 하고 새 항목을 제자리로 rename 한다(항목이 비는 틈은 rename 두 번 사이뿐). 옮기는 동안 INT·TERM·HUP 은 미뤘다가 다 옮긴 뒤 끝낸다. 다 놓으면 옛 사본을 지운다(지난번에 남은 `.clewpath-old.*`는 먼저 치운다). 실패하면 `[FAIL]` 후 종료코드 1이고, 같은 명령을 다시 치라고 안내한다. zip에 없는 기존 항목은 남긴다(Windows `Expand-Archive`와 같음). (검토 반영 — 전엔 `rm -rf` 뒤 `mv`라 그 사이 신호로 끊기면 session_manager 가 빠진 채 남아 install.sh·uninstall.sh 가 거부했다)
7. 임시 폴더 정리: `rm -rf "$(dirname "$STAGE")"`. 이름이 `.clewpath-get.*`이고 그 부모가 `dirname "$DIR"`일 때만 지운다.
8. `exec /bin/sh "$DIR/install.sh" <--from-stage·--dir 를 뺀 인자> </dev/null`. pid가 그대로이므로 표식도 그대로 유효하다.

**I2. 표식 (dry-run이면 건너뜀)**
- 서막을 거치지 않고 바로 실행됐으면 `update.inprogress`를 `pid=$$ by=install`로 잡는다. 다른 살아 있는 pid가 쥐고 있으면 거부한다.
- `trap`: 끝날 때 내용의 pid가 `$$`인 표식만 지우고, 임시 로그를 지운다.

**I3. [1/6] uv**
- `--system-uv`: `command -v uv`를 쓰고 `[WARN] 검증하지 않습니다`. 없으면 종료코드 1.
- `"$CLEWPATH_HOME/bin/uv"`가 있고 `--version`이 고정 버전이면 재사용한다. Windows는 버전을 보지 않지만, POSIX는 다르면 다시 설치한다.
- 그 밖이면 대상을 정한다.
  - [linux] `uname -m`이 x86_64/amd64면 `x86_64-unknown-linux-gnu`, aarch64/arm64면 `aarch64-unknown-linux-gnu`. musl(`ldd --version`에 musl, 또는 `/lib/ld-musl-*`)이면 `[FAIL] 범위 밖`.
  - [mac] `sysctl -n hw.optional.arm64`가 1이면 `aarch64-apple-darwin`(Rosetta 셸 대비), 아니면 `x86_64-apple-darwin`.
- `https://github.com/astral-sh/uv/releases/download/$V/uv-$T.tar.gz`를 180초 안에 받는다. `CLEWPATH_UV_ARCHIVE`가 있으면 그 파일을 쓴다.
- 해시 대조:
  - 스크립트에 박은 표 `uv_pinned_sha256 "$V" "$T"`와 비교한다. 다르면 `uv_checksum_mismatch` 후 종료코드 1.
  - 표에 없는 버전은 `[WARN]` 후 같은 서버의 `.sha256`과 대조한다(install.ps1:164-176과 같음). 로컬 파일이면 거부한다.
- `tar -xzf` 후 이름이 `uv`·`uvx`인 파일을 찾는다. 폴더 구조에 기대지 않는다. `bin/.uv.tmp`에 복사하고 `chmod 755`한 뒤 mv로 교체한다.
- 해시표(구현할 때 채움, 추측 금지):
  ```sh
  uv_pinned_sha256() {   # $1=버전 $2=대상 — 값은 공식 릴리스의 .sha256 과 직접 계산한 값이 같을 때만 기록
    case "$1|$2" in
      "0.12.1|x86_64-unknown-linux-gnu")  echo "<64hex>" ;;
      "0.12.1|aarch64-unknown-linux-gnu") echo "<64hex>" ;;
      "0.12.1|x86_64-apple-darwin")       echo "<64hex>" ;;
      "0.12.1|aarch64-apple-darwin")      echo "<64hex>" ;;
      *) echo "" ;;
    esac
  }
  ```

**I4. [2/6] 파이썬**
- 관리 런타임 위치: `export UV_PYTHON_INSTALL_DIR="$CLEWPATH_HOME/python"`.
- `uv python install`에는 `--no-bin`을 준다. uv 0.8 이후 `~/.local/bin`에 실행 파일을 두는 동작이 있으므로, 0.12.1의 `--help`로 지원 여부를 확인한다. 지원하지 않으면 `UV_PYTHON_BIN_DIR="$CLEWPATH_HOME/python/bin"`으로 대신한다.
- [linux]
  1. `UV_PYTHON_PREFERENCE=only-system uv python find ">=$PY"`로 찾고 스모크 시험을 한다.
  2. 실패하면 `uv python install "$PY"`, 이어 `only-managed`로 find 후 스모크.
  3. 끝내 실패하면 배포판 python3.11+ 설치나 네트워크·프록시 확인을 안내하고 종료코드 1.
- [mac] `only-managed`로만 install과 find를 하고 스모크한다. 시스템 탐색을 하지 않는 이유는 uv가 후보를 실행해 보다가 `/usr/bin/python3` 스텁을 건드리기 때문이다.
- 스모크: `"$py" -c 'import ssl, ctypes, sqlite3, sys; assert sys.version_info >= (3, 11)'`(install.ps1:240-249).
- SAC·winget 단계는 없다.
- 고른 선호를 `python_pref`로 기록한다. 러너가 같은 값으로 uv를 부른다.

**I5. [3/6] claude 폴더**
- `command -v claude`가 없으면 `[WARN]`.
- `${SESSION_MANAGER_CLAUDE_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}/projects`가 없으면 `[FAIL]`과 install.ps1:350-357과 같은 안내를 내고 종료코드 1. `--dry-run`이면 계속한다.
- `projects`는 있는지만 읽는다.

**I6. [4/6] 의존성**
- `--reinstall`이면 가드를 거친 뒤 `rm -rf "$ROOT/.venv"`.
- `"$ROOT/.venv/bin/python"`이 없으면 스모크를 통과한 경로로 `uv venv --python "$PYEXE" "$ROOT/.venv"`. 버전 문자열은 넘기지 않는다(install.ps1:376-382).
- venv 스모크가 실패하면 `--reinstall`을 안내하고 종료코드 1.
- `(cd "$ROOT" && UV_PROJECT_ENVIRONMENT="$ROOT/.venv" "$UV" sync --frozen --no-dev)`
- 실패하면 `[WARN] 잠금 파일로 설치 실패 — pyproject 기준으로 재시도` 후 `"$UV" pip install --python "$ROOT/.venv/bin/python" -e "$ROOT"`.
- 그것도 실패하면 마지막 25줄과 원인(`.venv` 손상, PyPI 차단)을 보여 주고 종료코드 1. 이 경우 Host는 다시 띄우지 않고 "원인을 해결한 뒤 같은 명령을 다시"라고 안내한다.

**I7. 경로 확정 (파이썬 규칙)**
- `eval`은 쓰지 않는다. `"$VPY" -I -m session_manager.hostctl paths`의 `data_dir=`·`conf_file=` 줄을 sed로 읽는다.
- I2에서 셸로 추정한 `DATA_DIR`과 다르면 `[WARN]`을 내고, 표식을 새 위치로 옮긴다.
- `SESSION_MANAGER_CLAUDE_HOME`이 있으면 `[WARN] 시험 격리용 변수 — 기동할 때도 같은 값이 필요합니다`.

**I8. [5/6] 설정·권한·런처·기록·자격증명**
1. `hostctl secure-data-dir`: `DATA_DIR`을 0700으로 만들고, 그 아래 파일·폴더에서 group·other 권한을 뺀다(심볼릭 링크는 건너뜀).
2. `hostctl write-config --port --cp-url --relay-url --app-url [--claude-config-dir "$CLAUDE_CONFIG_DIR"]`. 토큰은 env로만 넘긴다. 파일이 이미 있으면 손대지 않는다(`config=kept`). 새로 만들 때는 0600, BOM 없음, tomllib 검증을 통과한 뒤 원자적으로 쓴다.
3. `hostctl render-launcher --root "$ROOT" --data-dir --conf-file --port`로 `start-connector.sh`를 만든다(0755, 원자적). 템플릿이 없으면 `[FAIL]`이다. POSIX에는 v1 폴백이 필요 없다.
4. `.clewpath-install`을 기록한다(printf로 임시 파일에 쓰고 mv).
5. 자격증명(install.ps1:535-570에 대응):
   - 파일이 있으면 재사용한다.
   - `--dry-run`이면 `curl --noproxy '*' … "$CP/healthz"`만 부른다.
   - `--no-provision`이면 건너뛴다.
   - 그 밖이면 `hostctl provision --cp-url "$CP"`를 부른다. 저장은 `cp_client.save_creds`가 맡는다(0600, 원자적, 키 이름 일치).
   - 실패(429 포함)는 `[WARN]`만 낸다(fail-open).

**I9. [6/6] 자동 시작 자리와 (재)기동**
- `autostart_install()`은 이번 단계에서 안내만 한다: `[WARN] 자동 시작은 아직 없습니다 — 로그인할 때마다 아래 명령으로 켜거나 watch 를 쓰세요`. `--no-autostart`면 메시지도 생략한다. 다음 단계에서 [mac] launchd, [linux] systemd를 여기에 넣고, 그때도 "런처 렌더 성공 뒤에만" 등록한다.
- `sh start-connector.sh status`가 running이면 `CLEWPATH_UPDATER=1 sh start-connector.sh stop`으로 정상 종료한다.
- `maintenance.flag`가 있으면 `[WARN] maintenance.flag 가 있어 기동하지 않습니다(… resume 으로 해제)`를 내고 기동을 건너뛴다.
- 자기 표식을 지운 뒤 `sh "$ROOT/start-connector.sh" start`.
- `hostctl wait-ready --port "$PORT" --tries 20`(install.ps1:643-695에 대응):
  - 1초 간격으로 `runtime.json`의 port를 반영한다. 다르면 `[WARN] N 이 사용 중이라 M 번 포트로 열렸습니다`.
  - `/api/owner/connection`이 응답하면 `/api/owner/diagnostics`를 불러 세션 수와 claude 실행 파일을 확인한다.
  - 연결 모드를 출력한다. fallback이면 경고한다.
  - 실패하면 `host.err.log`(비어 있지 않으면) 또는 `host.log` 마지막 8줄을 보여 준다. 둘 다 없으면 "런처가 실행되지 못했습니다".
  - 모두 경고만 하고 종료코드는 0이다.

**I10. 안내**
- 열 주소: `http://127.0.0.1:<port>/`
- 폰 페어링: 로컬 웹 오른쪽 위 기기 버튼 → 새 기기 추가 → QR
- 서비스 관리:

  | 동작 | 명령 |
  |---|---|
  | 시작 | `sh "<ROOT>/start-connector.sh"` |
  | 상태 | `… status` |
  | 멈춤 | `… stop --hold` (다시 켜기: `… resume`) |
  | 임시 감시 | `nohup sh "<ROOT>/start-connector.sh" watch >/dev/null 2>&1 &` |
  | 제거 | `sh "<ROOT>/uninstall.sh"` |

- 설정 파일, 자격증명, `runtime.json`, uv의 위치를 알려 준다.

**종료코드**: install.ps1과 같다. 설치 폴더·uv·파이썬·claude 폴더·venv·의존성 실패와 동시 설치는 1이다. 발급·기동 확인 실패는 경고만 하고 0이다.

**install.ps1과의 1:1 대응**

| install.ps1 | install.sh | 차이와 이유 |
|---|---|---|
| :57-86 인자, EAP, TLS, UV_SYSTEM_CERTS | I0 (`--from-stage`·`--no-provision` 등 추가) | |
| :90-112 Invoke-Native, Show-LastNative, Ok/Warn/Fail | 임시 로그와 `tail -n 25`, 같은 접두 | PS5.1 stderr 문제가 없음 |
| :114-128 uv 고정 버전·해시(win 2대상), `%LOCALAPPDATA%\ClewPath\bin` | POSIX 4대상 해시표, `$CLEWPATH_HOME/bin` | 해시는 구현 때 고정 |
| :130-135 PROCESSOR_ARCHITECTURE | `uname -s/-m`, [mac] `sysctl hw.optional.arm64` | |
| :137-190 Install-Uv(zip, Expand-Archive) | tar.gz, `tar -xzf`, 이름으로 찾기, 0755, 원자 교체 | |
| :200-203 폴더 확인 | I0 | |
| :208-216 옛 control_plane·relay 정리 | 생략 | POSIX에는 옛 설치가 없음 |
| :218-234 SystemUv, 재사용(버전 무시), 설치 | I3, 재사용할 때 버전 확인 | |
| :236-332 파이썬(시스템 → 관리 → SAC → winget) | I4, [linux] 시스템 → 관리, [mac] 관리만 | SAC·winget 해당 없음 |
| :334-362 claude 폴더 | I5 (`SESSION_MANAGER_CLAUDE_HOME`도 본다) | 파이썬 규칙과 일치 |
| :364-418 .venv, `uv sync --frozen`, 폴백 | I6 | |
| :425-429 데이터 폴더(`CLAUDE_CONFIG_DIR` 누락) | I7 `hostctl paths` | 파이썬이 원본 |
| :431-491 toml 인라인 템플릿 | `hostctl write-config` (`appconfig.render_template`) | 셋째 사본을 만들지 않음 |
| :493-534 런처 치환, v1 폴백, `$launcherV2` | `hostctl render-launcher` (`shlex.quote`, 원자적), 폴백 없음 | sed 치환의 경로 문제 회피 |
| :535-570 자격증명 직접 POST, Set-Content | `hostctl provision` (`save_creds` 0600) | 키 이름·권한 일치 |
| :572-641 예약 작업, HKCU Run 폴백 | `autostart_install` 자리만 | 다음 단계 |
| :643-695 Start-Process, 20회 폴링, 진단, 로그 꼬리 | 런처 start, `wait-ready`. 이미 떠 있으면 stop 후 start | install.ps1은 재실행해도 옛 Host 유지 |
| :698-717 안내 | I10 | |

**구현 메모 (4단계, 2026-10-20 — 위 설계와 다른 점과 뒤 단계가 맞출 약속)**
- uv 해시표: 0.12.1 네 대상(x86_64·aarch64 리눅스 gnu, x86_64·aarch64 맥)을 GitHub 공식 릴리스의 `.sha256`과 직접 내려받아 계산한 값(sha256sum·파이썬 hashlib)이 같은 것을 확인하고 적었다. Windows 고정값도 같은 릴리스의 `.sha256`과 같다.
- `uv python find`에는 늘 `--system`을 준다. 0.12.1은 작업 폴더의 `.venv`나 `VIRTUAL_ENV`가 있으면 그 파이썬을 먼저 답한다(실측) — 설치 루트에서 재설치하면 자기 venv를 '시스템 파이썬'으로 집는다. `--system`이어도 관리 런타임(`UV_PYTHON_INSTALL_DIR`)은 찾는다(실측). 찾기는 임시 폴더에서 `VIRTUAL_ENV`·`CONDA_PREFIX`·`UV_PYTHON`을 빼고 부른다. 관리 런타임은 이미 받아 둔 것이 있으면 다시 받지 않는다.
- uv 설정 env(`UV_SYSTEM_CERTS`·`UV_PYTHON_INSTALL_DIR`·`UV_PYTHON_PREFERENCE`·`UV_PROJECT_ENVIRONMENT`)는 uv를 부르는 하위 셸에만 준다. 명세의 `export UV_PYTHON_INSTALL_DIR`처럼 스크립트 전체에 export 하면 기동하는 Host와 그 안의 claude까지 물려받는다(S38). 토큰도 hostctl write-config 에만 env로 넘기고, Host를 띄울 때는 뺀다.
- `uv venv`는 폴더가 있으면 거부한다(실측). 파이썬이 없는 `.venv`는 가드를 거쳐 지운 뒤 다시 만든다. `--reinstall`은 지우기 전에 Host를 정상 종료한다(돌고 있는 Host의 파이썬을 지우지 않게).
- `--from-stage`: stage는 이 install.sh가 있는 폴더여야 한다. 최상위 이름 검사는 아무것도 바꾸기 전에 한다(점으로 시작·`start-connector.sh`·제어문자·역슬래시·심볼릭 링크). `--dir`이 비어 있지 않은데 ClewPath 설치본(`.clewpath-install`, 또는 session_manager와 이름이 clewpath-host인 pyproject)이 아니면 거부한다(남의 폴더의 같은 이름 항목을 지우지 않게). `--dry-run`과 함께 쓰면 거부(2). 옮긴 뒤 교체 잔재(`.clewpath-swap.*`)는 버린다 — 받은 파일이 교체 대상을 모두 새로 덮었으므로, 남은 저널을 되돌리면 방금 놓은 파일이 옛 것으로 돌아간다(시험으로 고정). 지우지 못하면 그 자리에서 멈춘다.
- 교체 잔재가 있는 채로 `sh install.sh`(stage 없이)를 다시 치면 — 런처가 "install.sh 로 복구하세요"라고 안내하는 길 — 표식을 잡기 전에 Host를 멈추고 러너(데이터 폴더 사본 → 설치 루트 → 잔재 안의 것 순)를 임시 사본으로 `-RecoverOnly` 한다. 자기 표식(서막이 잡고 exec 로 이어 온 같은 pid)이 있으면 먼저 내려놓는다 — 러너가 '설치 중'으로 보고 비켜서면(3) 아무도 되돌리지 못한다. 3이면 60초까지 다른 복구가 끝나기를 기다리고, 끝내 남으면 get.sh 재설치를 안내하고 1.
- 표식은 데이터 폴더가 없고 클로드 `projects`도 없으면 잡지 않는다(그 설치는 [3/6]에서 멈춘다 — 클로드 폴더를 우리가 만들지 않게). 표식의 `at`은 앞자리 0을 받지 않는다(`$(( ))`가 8진수로 읽어 셸이 끝난다).
- 기존 설정 파일이 있으면 그 파일의 포트(없거나 읽을 수 없으면 Host 기본값 5100)를 런처 렌더·기동 확인·안내에 쓴다. `--port`가 다르면 경고만 한다.
- 이미 떠 있는 Host는 `running`뿐 아니라 응답 없는 Host(`starting`)도 멈춘다 — status 의 `pid=`(살아 있는 Host 만)로 판단한다. 우리 표식이 있으면 status 는 응답 없는 Host 를 `updating`으로 보이기 때문이다. maintenance.flag 가 있으면 멈추지도 띄우지도 않는다.
- DryRun 의 컨트롤플레인 도달 확인은 루프백만 프록시를 건너뛴다(`--noproxy 127.0.0.1,localhost,::1`) — 원격 CP 는 사내 프록시가 필요할 수 있다.
- 인자는 `--opt 값`과 `--opt=값`을 다 받는다. 모르는 위치 인자는 값을 화면에 찍지 않는다(토큰을 잘못 붙였을 수 있다).
- (검토 반영) `--client-token`·`--agent-token`을 받으면 경고 한 번 뒤 토큰을 env 로 옮기고 그 인자만 뺀 채 자기 자신을 exec 한다(pid 그대로,
  인자 해석 바로 뒤). 전엔 서막을 거치는 길만 옮겨, `sh install.sh --client-token …`로 바로 부르면 설치 내내(수 분) `/proc/<pid>/cmdline`(ps)에
  토큰이 남았다. 스크립트를 파일로 다시 읽을 수 없으면(표준 입력으로 돌림) 경고만 하고 잇는다.
- 시험: `tests/test_install_sh.py`(진짜 install.sh·uninstall.sh·런처·hostctl·Host·브로커, 가짜 uv 하나), `tests/test_hostctl.py`. 리눅스 컨테이너에서 install.sh·uninstall.sh·hostctl 에 버그를 하나씩 넣어(43종) 모두 겨냥한 단정에서 잡히는 것을 확인했다. `bash --posix`(맥 sh 근사)로 stage 없는 설치·제거도 돈다. 진짜 uv(고정 해시 tarball)·오프라인 `uv sync --frozen`으로 설치 → 상태·멈춤·기동 → 재설치 → 제거 → `--purge` 왕복도 컨테이너에서 확인했다.
- 뒤 단계가 맞출 것: get.sh 는 `--dir`을 절대 경로로, stage 를 `dirname DIR` 아래 `.clewpath-get.*/stage`에 두고 `exec /bin/sh "$tmp/stage/install.sh" --from-stage "$tmp/stage" --dir …`로 넘긴다. 토큰은 env 로만 넘긴다.

### 3.3 start-connector.template.sh → start-connector.sh (멱등 런처)

**머리**
- 1행: `#!/bin/sh`
- 2행: `# clewpath-launcher v2 (posix) — 자동 생성됨(install.sh / Host). 업데이트 뒤 Host 가 템플릿으로 다시 만듭니다. 직접 고치지 마세요.`
- 3행: `# 설정은 이 파일이 아니라 {{CONF_FILE}} 을 고치세요.`
- Windows 템플릿 1행의 "업데이트 때 덮어써집니다"는 사실과 다르지만, POSIX에서는 `ensure_launcher`가 실제로 다시 만든다.

**자리표시 (`shlex.quote`로 넣음)**

```sh
DATA_DIR={{DATA_DIR}}
CONF_FILE={{CONF_FILE}}
DEFAULT_PORT={{PORT}}
ROOT=$(cd "$(dirname "$0")" && pwd -P) || exit 1
PY="$ROOT/.venv/bin/python"
```

설치 루트를 박지 않고 `$0`에서 구하므로 파일을 옮겨도 동작한다. 파이썬 `install_root()`와 같은 물리 경로다.

**하위 명령** (기본은 start)

| 명령 | 동작 | 종료코드 |
|---|---|---|
| `start` | 멱등 기동(아래) | 0 떠 있음·기동함·보류 / 1 오류 / 2 venv 없음 |
| `stop [--hold]` | `--hold`면 먼저 `maintenance.flag`(`by=stop`)를 만든다.<br>runtime.json의 pid가 `$ROOT/.venv/bin/` 실행이고 `session_manager.server`(또는 `clewpath-host`)일 때만 SIGTERM을 보낸다.<br>0.5초 간격으로 최대 15초 기다린다(`_shutdown_soon`의 15초와 같음).<br>남아 있으면 SIGKILL과 `launcher.log 'stop: forced'`. 이때 `liveness.mark_shutdown("forced-stop", pid=N)`을 부르고 실패는 무시한다.<br>브로커는 건드리지 않는다. 떠 있지 않으면 `not running`. | 0 |
| `resume` | `maintenance.flag`를 지우고 start | start와 같음 |
| `status` | `state=running|starting|stopped|maintenance|updating`, `pid=`, `port=`, `url=`, `broker_pid=`, `broker_alive=`(ptyd.json), `watch_pid=` | 0 health 정상 / 1 아님 / 3 유지보수 |
| `watch [초=60, 최소 10]` | 상주 감시자(Windows 5분 트리거의 임시 대용).<br>`watch.lock.d`로 한 개만 돈다(살아 있는 감시자가 있으면 즉시 0).<br>틱마다 `( cmd_start )`를 하위 셸로 실행한다.<br>`sleep N & wait $!`로 신호에 바로 반응한다.<br>TERM·INT·HUP를 받으면 sleep을 죽이고 잠금을 풀고 끝낸다. Host는 분리돼 있어 계속 산다.<br>연속 3틱 dead면 간격을 최대 300초까지 두 배로 늘리고 `launcher.log 'backoff'`. | 0 |
| `paths` | `root=`, `data_dir=`, `conf_file=`, `default_port=`, `python=` | 0 |
| `foreground` | 다음 단계용. 이번에는 정의만 한다.<br>사전 점검 후 maintenance면 0. 아니면 정리된 env로 `os.execve(PY, [PY,'-m','session_manager.server'])`.<br>launchd `KeepAlive{SuccessfulExit=false}`나 systemd `Restart=on-failure`와 짝을 이룬다. | |

**start 순서** (Windows 템플릿 :20-56에 새 단계를 더함)

```sh
cmd_start() {
  [ -x "$PY" ] || { llog "fail: venv 없음"; say "venv 가 없습니다 — sh install.sh --reinstall"; exit 2; }
  [ -d "$DATA_DIR" ] || mkdir -p "$DATA_DIR"; chmod 700 "$DATA_DIR" 2>/dev/null   # 폴더 권한만(umask 는 그대로)
  [ -e "$DATA_DIR/maintenance.flag" ] && { [ -t 2 ] && say "maintenance.flag — resume 으로 해제"; exit 0; }
  if update_in_progress && [ "${CLEWPATH_UPDATER:-}" != 1 ]; then llog "skip: 업데이트 진행 중"; exit 0; fi
  recover_swap_leftovers || exit 1           # 아래 설명
  set -- $(probe)                            # 출력: "<state> <pid> <port>" (공백 없는 토큰 3개)
  case "$1" in ok) exit 0 ;; busy) llog "skip: pid $2 살아 있음(health 없음 — 기동 중이거나 응답 불능)"; exit 0 ;; esac
  lock_acquire "$DATA_DIR/launcher.lock" || exit 0
  trap 'lock_release "$DATA_DIR/launcher.lock"' EXIT; trap 'exit 1' INT TERM HUP
  set -- $(probe); case "$1" in ok|busy) exit 0 ;; esac      # 잠금 안에서 한 번 더(Windows 에 없는 보강)
  rotate "$DATA_DIR/host.log"; rotate "$DATA_DIR/host.err.log" # 5242880 바이트 초과면 mv -f → .1 (기동 직전에만)
  why="이전 pid $2 없음(health 없음)"; [ "$2" = 0 ] && why="runtime.json 없음"
  llog "start: $why → 기동"; printf '=== launcher start %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$DATA_DIR/host.log"
  pid=$(spawn) || { llog "fail: spawn rc=$?"; exit 1; }; llog "spawned pid $pid"
}
```

**`probe`** (`"$PY" -I -c "$PROBE_PY" "$DATA_DIR" "$DEFAULT_PORT"`, 표준 라이브러리와 psutil)
- `runtime.json`(`utf-8-sig`)의 port와 pid를 읽는다.
- 프록시 없는 opener로 `GET http://127.0.0.1:<port>/api/health`(3초)를 보내 `{"status":"ok"}`가 오면 `ok`.
- 그렇지 않으면 psutil로 pid를 본다. 존재하고, 좀비가 아니고, 내 uid 이고, cmdline 인자 하나가 `$ROOT/.venv/bin/`으로 시작하고 다른 하나가 `session_manager.server`이거나 `*/clewpath-host`로 끝나면 `busy`(이 설치본의 Host — S27).
- `AccessDenied`면 보수적으로 `busy`(겹쳐 띄우지 않음).
- psutil을 import할 수 없으면 `os.kill(pid,0)`과 [linux] `/proc/<pid>/cmdline`, `/proc/<pid>/stat`의 상태 Z로 대신한다.
- 나머지는 `dead`(pid 있음) 또는 `none`이다.
- Windows는 프로세스 이름에 python이 들어 있는지만 보지만, POSIX는 cmdline으로 판정해 pid 재사용과 좀비를 걸러낸다.

**`spawn`** (`"$PY" -I -c "$SPAWN_PY" "$ROOT" "$DATA_DIR" "$CONF_FILE"`)
- env를 정리한다: `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `CLAUDE_CODE_*`(`ptyd.clean_env`와 같은 규칙, ptyd.py:38-44), 그리고 `CLEWPATH_UPDATER`를 뺀다.
- 더하는 env: `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8`, `CLEWPATH_CONFIG=$CONF_FILE`. 마지막은 toml 위치를 고정해 Host의 `data_dir`과 런처의 `DATA_DIR`이 갈리지 않게 한다.
- 실행:
  ```
  subprocess.Popen([PY, "-m", "session_manager.server"], cwd=ROOT, start_new_session=True,
                   stdin=DEVNULL, stdout=open(host.log, "ab"), stderr=open(host.err.log, "ab"), close_fds=True, env=env)
  ```
- 로그는 이어 쓰기(`ab`)다. liveness가 죽은 프로세스의 꼬리를 읽을 수 있다. Windows의 Start-Process 리다이렉트는 덮어쓴다(실측).
- 출력은 pid다. spawn 하는 파이썬이 곧 끝나므로 Host는 init에 입양된다. 이중 분리라 좀비가 생기지 않는다.
- PATH·HOME·TMPDIR·LANG·umask는 손대지 않는다.
- 띄우기 전에 HUP·INT·QUIT 을 기본 처리로 되돌린다(검토 반영). README 의 `nohup sh start-connector.sh watch &` 처럼 nohup(HUP)·스크립트의 `&`(INT·QUIT)가 둔 '무시'는 exec 로 Host → 브로커 → pty 안의 프로그램까지 물려가, 브로커가 죽어도 pty 자식이 SIGHUP 으로 끝나지 않는다. Host 는 새 세션이라 터미널 신호는 오지 않는다. exec 모드(foreground)는 부른 쪽의 처리를 그대로 둔다.

**인라인 파이썬 규칙**
- 작은따옴표로 감싼 셸 변수에 넣는다. 그래서 파이썬 코드에는 작은따옴표를 쓰지 않는다(정적 테스트로 확인).
- `session_manager`를 import 하지 않는다. 런처가 렌더된 버전과 설치된 코드의 버전이 달라도(롤백 직후) 동작해야 하기 때문이다. 예외는 stop 강제 종료 때의 표식 남기기뿐이며 실패는 무시한다.

**`update_in_progress`**
- `update.inprogress`의 pid를 `kill -0`으로 보고, `at`이 30분 안인지 본다.
- 시체면 지우고 `llog 'stale update marker 제거'`.

**`recover_swap_leftovers`**
- `"$ROOT"/.clewpath-swap.*` 폴더가 있고 `updates/runner.lock.d`의 pid가 죽어 있으면 러너 사본으로 복구한다:
  ```
  /bin/sh "$DATA_DIR/updates/update_runner.sh" -RecoverOnly -InstallRoot "$ROOT" -LogFile "$DATA_DIR/updates/recover.log"
  ```
- 사본이 없으면 `llog 'blocked: 중단된 업데이트 잔재'`, 표준오류로 "sh install.sh 로 복구하세요"를 내고 종료코드 1.

**Windows 템플릿·ensure_task와의 1:1 대응**

| Windows | POSIX | 차이와 이유 |
|---|---|---|
| template:1 v2 표식(1행) | 2행 표식과 내용 비교 렌더 | shebang이 1행 |
| :13 `Set-Location $PSScriptRoot` | `ROOT=$(cd "$(dirname "$0")" && pwd -P)` | |
| :14-18 경로, LLog | 자리표시 3개, printf `launcher.log` | |
| :20 maintenance.flag | 같음 | |
| 없음 | `update.inprogress`, 교체 잔재 복구 | 감독자가 업데이트 중에 띄우지 않게(새 불변식) |
| :23-30 runtime.json port, `/api/health` | `probe`(프록시 무시) | |
| :31-36 pid와 프로세스 이름 python | psutil 존재·좀비 아님·cmdline 모듈 | pid 재사용, 좀비 |
| :39-41 잠금 파일 120초(비원자) | mkdir 잠금과 pid(2분) | 원자적 |
| 없음 | 잠금 안 재판정 | 경합 축소 |
| :43-44 env | 같음, claude 표식 정리, `CLEWPATH_CONFIG` | 불가침, 데이터 폴더 일치 |
| :46-48 5MB 회전 | 같음 | |
| :49-50 기동 사유 | 같음과 host.log 구분줄 | |
| :51-53 Start-Process(덮어씀) | Popen `start_new_session`, 이어쓰기 | 사고 꼬리 보존 |
| :54-56 finally 잠금 삭제 | trap | |
| ensure_task:15-31 v2가 아니면 재생성 | `_posix.ensure_launcher`: 렌더 결과가 다르면 재생성 | 템플릿 변경도 전파 |
| ensure_task:33-52 5분 반복 트리거 | 없음(watch는 사람이 켬) | 다음 단계에서 재생성 성공 뒤에만 |

**구현 메모 (2단계, 2026-10-20 — 뒤 단계가 맞출 약속)**
- `start`는 띄운 뒤 runtime.json에 새 pid가 적히거나(`up`) 끝날 때까지 최대 15초 잠금을 쥔다. 그 사이 온 다른 `start`가 "runtime.json 없음"을 죽음으로 읽고 겹쳐 띄우지 않게 하려는 것이다. 기동 직후 끝나면 `fail: 기동 직후 종료`를 남기고 종료코드 1.
- 잠금(`launcher.lock`·`watch.lock.d`·`updates/runner.lock.d`)과 `update.inprogress`의 pid는 명령줄까지 본다. 잠금은 런처 파일 이름, 러너 잠금과 `by=runner` 표식은 `update_runner`, `by=install` 표식은 `install.sh`가 명령줄에 있어야 산 것이다(pid 재사용 방지). `by=`가 없으면 pid만 본다. pid 없는 표식은 1분, pid 없는 잠금은 2분까지 "쓰는 중"으로 본다. 러너·install.sh는 표식을 임시 파일에 쓰고 mv 한다.
- 교체 잔재가 있고 러너가 살아 있으면 `CLEWPATH_UPDATER=1`(러너 자신의 재기동)만 통과하고 나머지는 보류(0)한다. 러너 사본의 `-RecoverOnly` 종료코드: 0이면 복구(잔재가 남으면 실패로 봄), 3이면 다른 러너가 도는 중이거나 이 설치본의 Host 가 떠 있어(3단계) 보류, 그 밖은 막힘(1).
- probe는 pid가 좀비이거나, 내 uid가 아니거나, 명령줄이 이 설치본의 Host(설치 경로 + 모듈)가 아니면 죽은 것으로 본다(맥 재부팅 뒤 남의 프로세스가 같은 pid를 받는 경우). (검토 반영: 처음엔 runtime.json 의 `started_at`보다 5초 넘게 뒤에 생긴 프로세스도 죽은 것으로 봤다 — 리눅스 psutil 의 create_time 은 지금의 부팅 시각으로 계산해 시계가 앞으로 뛰면(WSL2·VM 재개·NTP) 같이 밀려, 잠깐 응답 없는 산 Host 를 시체로 보고 두 번째 Host 를 띄웠다. 시각 대신 설치 경로로 가린다)
- `watch`의 틱은 `( cmd_start )` 대신 `/bin/sh "$SELF" start` 새 프로세스로 돈다. 잠금 pid가 진짜 pid가 되고, 다시 렌더된 런처를 쓴다. Host가 살아 있으면(ok·busy) start를 부르지 않는다.
- 출력: `stop`은 `not running` / `stopped (pid N)` / `stopped (pid N, forced)`. `status`는 위 키에 `maintenance=0|1`을 더하고, venv가 없거나 판정이 안 되면 `venv=missing|broken`을 붙인다. 사람용 안내는 터미널일 때만 표준오류로 낸다(실패는 늘 표준오류).
- 렌더러: `osdep._posix.write_launcher(root, data_dir=, conf_file=, port=)` → `created|updated|unchanged`(원자 교체, 0755). `hostctl render-launcher`(4단계)와 `ensure_launcher`가 함께 쓴다. `DEFAULT_PORT=` 줄은 그 줄만 있어야 `ensure_launcher`가 기존 포트를 다시 읽는다(시험으로 고정).
- 시험: `tests/test_launcher_sh.py`(L①~L⑪, 판정 세 갈래 psutil·/proc·ps). 리눅스 컨테이너에서 템플릿에 버그를 하나씩 넣어 시험이 잡는지 확인했다(24종 모두 잡힘).

### 3.4 update_runner.sh (`session_manager/`, 설치 폴더 밖 사본으로 실행)

**호출** (`osdep._posix.update_runner_argv`)

```
/bin/sh <DATA>/updates/update_runner.sh -InstallRoot R -StageDir S -BackupDir <WORK>/backup-<현재> -WaitPid <Host pid>
        -Port P -Version X.Y.Z -LogFile <WORK>/apply-<X.Y.Z>.log [-RestartScript R/start-connector.sh] [-WaitSeconds 60]
/bin/sh … -RecoverOnly -InstallRoot R -LogFile <WORK>/recover.log          (런처·다음 러너가 잔재 복구에 씀)
```

- 플래그 이름은 ps1과 같다. 그래서 `test_update_safety`의 `a.index('-Port')`가 그대로 통과한다.
- 파생 경로:
  - `WORK=$(dirname "$LogFile")`
  - `DATA=$(dirname "$WORK")`
  - `VENV_PY="$InstallRoot/.venv/bin/python"`
  - `UV`: `.clewpath-install`의 `uv=`가 먼저, 없으면 공통 경로 규칙의 `$CLEWPATH_HOME/bin/uv`, 그다음 `command -v uv`. launchd나 분리 기동에서는 XDG 변수가 없을 수 있으므로 기록 파일을 먼저 본다.
  - 기록의 `python_pref`·`python_install_dir`을 `UV_PYTHON_PREFERENCE`·`UV_PYTHON_INSTALL_DIR`로 export 한다.

**PAYLOAD** (네 곳 일치, 테스트로 고정)

```
PAYLOAD="session_manager pwa pyproject.toml uv.lock install.ps1 install.sh uninstall.sh README.md LICENSE"
```

**헬퍼**
- 시작할 때 `$WORK/.runner-helper.$$.py`에 쓰고, `"$HPY" -I -S`로 부른다. 표준 라이브러리만 쓰며 설치 폴더 코드를 import 하지 않는다.
- `HPY`는 `VENV_PY`가 쓸 만하면 그것, 아니면 [linux] `python3`. [mac]에서는 시스템 python3를 부르지 않는다.
- 둘 다 없으면 셸 경로로 대신한다: curl 헬스는 문자열 검사, 프로세스 목록은 `/proc` 또는 `ps`, JSON 이스케이프는 sed.
- 하위 명령:
  - `waitpid N 초`
  - `cmdline N`
  - `procs ROOT`: 실패본 정지 대상. 명령줄에 `"$ROOT/.venv/bin/"`가 있고 `session_manager.server`나 `clewpath-host`가 있는 프로세스. `session_manager.ptyd`, 자기 자신, 부모는 뺀다. [linux] `/proc/*/cmdline`의 NUL을 인자 단위로 비교, [mac] `ps -axww -o pid= -o command=`.
  - `health 포트… --runtime FILE --expect V --seconds 45`: 0.7초 간격, 프록시 없음, `/api/owner/diagnostics`에 `claude_home`이 있고 version이 없거나 V와 같으면 통과.
  - `jsonstr`

**셸 규칙**
- `set -e`·`set -u`·`set -x`를 쓰지 않는다.
- `trap '' HUP INT`. TERM은 `STOP_REQ=1`만 세운다. 교체가 시작되기 전에 받았으면 중단 처리(옛 버전 재기동, ok=false)하고, 교체가 시작된 뒤면 끝까지 진행한다.
- `rm -rf`를 쓰기 전 가드:
  - 대상이 비어 있지 않고, `/`로 시작하고, `/`나 `$HOME`이 아니다.
  - 지우는 이름은 `$BackupDir*`, `$SWAP`, `"$InstallRoot/<PAYLOAD 고정 이름>"`뿐이다.
  - `InstallRoot/session_manager`가 있어야 한다.

**단계** (로그 문구는 ps1과 같게. release.ps1 `-Apply`가 꼬리 2줄을 보여 준다)

**0. 인자 검증**
- 필수 6개 인자가 있어야 한다. 경로는 절대 경로여야 한다.
- 다음이 있어야 한다: `InstallRoot/session_manager`, `StageDir/session_manager`, `StageDir/pyproject.toml`.
- 스테이지 pyproject의 version이 `-Version`과 같아야 한다.
- `InstallRoot`에 `.git`이나 `ops/`가 있으면 거부한다(이중 방어).
- 실패하면: 로그를 남기고, `last_apply` ok=false, `apply.lock`을 지우고, 종료코드 2. 설치 폴더는 건드리지 않는다.

**1. 잠금과 표식**
- `mkdir "$WORK/runner.lock.d"`로 잠그고 pid를 쓴다. 살아 있는 러너가 있으면 종료코드 3(이때 `apply.lock`은 건드리지 않음).
- `$DATA/update.inprogress`에 `pid=$$ by=runner`를 쓴다. 파이썬이 Host 종료를 최소 1.5초 늦추므로(server.py:237-253) 이 표식이 먼저 생긴다.
- 지난 실행의 `.clewpath-swap.*`가 남아 있으면 먼저 복구한다(아래 '복구').
- 단계마다 `touch "$WORK/apply.lock"`(하트비트). 긴 uv sync 중에 파이썬의 300초 만료가 지나지 않게 한다.
- 사용자의 `maintenance.flag`는 건드리지 않는다. 파이썬 apply가 미리 거부한다(4.2절).

**2. 본체 종료 대기**
- 로그: "본체(PID N) 종료 대기..."
- `waitpid`로 `WaitSeconds`(60)만큼 기다린다.
- 남아 있으면 cmdline이 `session_manager`인지 확인한다(pid 재사용 방지). 맞으면 "시간 내에 안 죽어서 강제 종료합니다."를 남기고 TERM을 보낸다.
- 10초 뒤에도 남아 있으면 KILL. 아니면 죽은 것으로 보고 진행한다.
- 파일 핸들 대기(ps1:160-163)는 필요 없다.

**3. 백업**
- `rm -rf "$BackupDir.tmp.$$"`
- `InstallRoot`에 있는 PAYLOAD 항목만 `cp -pR` 하고, 매번 종료코드를 확인한다.
- `session_manager/__init__.py`와 `pyproject.toml`이 있는지 본다.
- `rm -rf "$BackupDir"` 후 `mv "$BackupDir.tmp.$$" "$BackupDir"`. 완성된 백업만 정식 이름을 갖는다. 로그: "백업 완료: …"
- 실패하면 "[FAIL] 백업 실패 - 되돌릴 수 없으므로 여기서 멈춥니다"를 남기고 옛 버전을 재기동한 뒤 정리(아래 9·10)하고 종료코드 1. ps1과 달리 잠금과 결과도 정리한다.

**4. 교체** (`SWAP="$InstallRoot/.clewpath-swap.$$"`. 설치 루트 안이므로 mv가 곧 rename(2)이다)
- 저널 형식(v1, 바꿀 때는 하위 호환):

  | 파일 | 내용 |
  |---|---|
  | `info` | `format=1 runner_pid version from started` |
  | `journal` | 각 mv 직전에 한 줄: `old <이름>` 또는 `new <이름>` |
  | `new/<이름>` | 스테이지에서 복사한 새 항목 |
  | `old/<이름>` | 옮겨 둔 옛 항목 |
  | `committed` | 헬스 통과 뒤 생성 |

- 4a. 스테이지의 PAYLOAD 항목을 `SWAP/new`로 `cp -pR` 한다. 스테이지는 데이터 폴더라 다른 파일 시스템일 수 있다. 실패하면 SWAP을 지우고(설치 폴더는 그대로) 재기동한 뒤 ok=false.
- 4b. 항목마다:
  - 기존 항목이 있으면 `echo "old x" >> journal` 후 `mv ROOT/x SWAP/old/x`.
  - 이어 `echo "new x" >> journal` 후 `mv SWAP/new/x ROOT/x`.
  - 실패하면 '복구'와 같은 되돌림을 하고 재기동한 뒤 ok=false. 로그: "[FAIL] 교체 중 오류: …"
- 성공 로그: "파일 교체 완료"
- 스테이지에 없는 기존 항목은 남긴다(Windows와 같음).

**복구** (저널을 거꾸로 읽는다. `-RecoverOnly`와 롤백에서 공통)
- `committed`가 있으면 되돌리지 않고 SWAP만 지운다.
- `new x`: `SWAP/new/x`가 아직 있으면 옮기지 않은 것이므로 할 일이 없다. 없으면 `rm -rf ROOT/x`.
- `old x`: `SWAP/old/x`가 있으면 `ROOT/x`를 비운 뒤 `mv SWAP/old/x ROOT/x`. 없으면 옮기지 않은 것이다.
- 끝나면 uv sync를 하고(dist-info 복원), `last_apply`에 ok=false(그 버전)를 쓰고, SWAP을 지운다.

**5. 의존성**
- `(cd "$InstallRoot" && UV_PROJECT_ENVIRONMENT="$InstallRoot/.venv" UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-1}" "$UV" sync --frozen --no-dev)`. 출력은 줄마다 `  uv: …`로 로그에 남긴다.
- 실패하면 `"$UV" pip install --python "$VENV_PY" -e "$InstallRoot"`(로그 접두 `  pip: …`). 종료코드는 그대로 기록한다.
- 둘 다 실패하면 "[FAIL] 의존성 동기화 실패(새 버전 V)"를 남기고 곧바로 8번으로 간다(권장. Windows는 결과와 무관하게 '완료' 후 헬스에 맡김).
- uv나 venv가 없으면 로그만 남기고 진행한다. 버전 불일치로 헬스가 실패해 롤백된다.

**6. 재기동**
- `CLEWPATH_UPDATER=1 /bin/sh "$RestartScript" start </dev/null >>"$LogFile" 2>&1`. 런처는 곧 끝나므로 동기 호출이다.
- `RestartScript`가 없으면 "재기동 스크립트가 없어 건너뜁니다".

**7. 헬스**
- `health "$Port" --runtime "$DATA/runtime.json" --expect "$Version" --seconds 45`
- 통과: `touch SWAP/committed`, 로그 "헬스체크 통과 - 업데이트 성공 (V)", SWAP 삭제.
- 실패: "헬스체크 실패 (port=…, runtime.json port=…)"

**8. 롤백**
- 로그: "롤백합니다."
- a. 실패본 보관: `$BackupDir-failed-$Version`에 PAYLOAD를 `cp -pR` 한다.
- b. `procs`로 찾은 실패한 Host와 runtime.json pid(같은 조건)에 TERM을 보내고 5초 뒤 KILL. `session_manager.ptyd`는 빼낸다(ps1:208-214와 같은 예외).
- c. 복원: SWAP이 있으면 저널을 되돌린다. 없으면 BackupDir을 원본으로 4a·4b를 다시 한다. 로그: "이전 버전 복원 완료"
- d. uv sync(롤백). dist-info를 옛 버전으로 되돌린다(ps1:218-220의 이유).
- e. 재기동 후 헬스. 이번에는 백업 pyproject의 옛 버전을 기대한다. 옛 코드가 새 버전을 보고하는 어긋남도 잡는다.
  - 통과: "롤백 후 정상 동작 확인"
  - 실패: "[FAIL] 롤백 후에도 응답이 없습니다 - 수동 확인이 필요합니다." 버전만 어긋나면 "… 버전 표기가 어긋납니다 — sh install.sh 로 다시 설치하세요"를 덧붙인다.

**9. 결과 기록**
- `$WORK/last_apply.json`을 임시 파일에 printf로 쓰고 mv로 교체한다.
- 내용: `{"version":"V","ok":true|false,"finished_at":<date +%s>,"log":"<이스케이프한 경로>"}`
- UTF-8, BOM 없음. `updater.recently_failed`가 `json.loads`로 읽는다.

**10. 정리**
- `rm -f "$WORK/apply.lock"`, `update.inprogress`(내 pid일 때만), 헬퍼, `runner.lock.d`를 지운다. 모든 종료 경로에서 지운다.
- 마지막 로그: "=== 종료 (성공: true|false) ==="

**종료코드**: 0 성공, 1 실패(롤백 수행), 2 인자·사전 조건 오류(무변경), 3 다른 러너 실행 중.

**update_runner.ps1과의 1:1 대응**

| ps1 | sh | 차이와 이유 |
|---|---|---|
| :24-35 param | 같은 플래그와 `-RecoverOnly` | 파이썬 argv 공유 |
| :37 EAP Continue | `set -e`·`-u` 없음 | 스스로 죽지 않음 |
| :40-41 `$Payload` | PAYLOAD 문자열 | 네 곳 일치 |
| :43-46 Log | printf 이어쓰기 | |
| :48-57 Copy-Payload(지우고 복사, 오류 삼킴) | new/ 준비 후 항목별 rename과 저널, 종료코드 확인 | 원자성, 감지 |
| :59-88 Sync-Deps(결과와 무관하게 '완료') | 같은 순서, 종료코드 기록, 둘 다 실패면 즉시 롤백 | |
| :63-67 `%LOCALAPPDATA%` uv, PATH | 기록 파일, 경로 규칙, PATH | launchd 최소 환경 대비 |
| :68 `.venv\Scripts\python.exe` | `.venv/bin/python` | |
| :90-102 Get-RuntimePort | helper | |
| :104-124 Test-Healthy | helper(45초, 0.7초, 버전) | |
| :126-144 Start-Host2(pwsh, CreateNoWindow) | `CLEWPATH_UPDATER=1 /bin/sh "$RestartScript" start` | |
| :150-163 대기, 강제 종료, 핸들 대기 2+2초 | `kill -0` 대기, cmdline 확인, TERM 10초, KILL, 핸들 대기 없음 | pid 재사용 |
| :165-174 백업(실패 시 정리 없이 exit) | `.tmp` 후 rename, 실패 시 정리 후 exit 1 | ps1 결함 보완 |
| :176-184 교체 | 4a/4b | |
| :189-195 재기동, 헬스 | 같음과 커밋 표식 | |
| :197-227 롤백 | 같음, 저널 우선, 옛 버전 기대 헬스 | 어긋남 감지 |
| :229-238 last_apply(PS5.1은 BOM) | printf와 mv, BOM 없음 | |
| :240-242 apply.lock 삭제 | 모든 경로와 하트비트 | |
| 없음 | runner.lock.d, update.inprogress, 잔재 복구 | 겹침, 감독자 경합 |

**구현 메모 (3단계, 2026-10-20 — 위 설계와 다른 점과 뒤 단계가 맞출 약속)**
- 되돌림은 지우지 않고 반대로 옮기기만 한다. `new x`는 `ROOT/x`를 `SWAP/new/x`로 되돌리고(명세의 `rm -rf ROOT/x` 대신), `old x`는 `SWAP/old/x`를 제자리로 옮긴다. 되돌리다 죽어도 다시 돌리면 같은 결과다(멱등). 설치 폴더의 항목은 어떤 길에서도 rm 하지 않는다.
- 저널이 없거나 비어 있으면 아무것도 옮기지 않은 것이라 치우기만 한다. 저널 폴더를 지울 때는 `journal` 파일부터 지운다. 롤백·복구는 되돌림 → uv sync → 저널 정리 순서라, 그 사이에 죽어도 다음 복구가 다시 맞춘다. 저널을 rename 직전에 쓰는 순서가 이 규칙의 전제다(시험: rename 마다 SIGKILL).
- 지난 교체 잔재 복구는 1단계(잠금·표식)가 아니라 2단계(본체 종료 대기) 뒤, 백업 앞에서 한다. 잔재가 있는데 Host 가 떠 있다면 그 Host 가 잔재 트리로 돌고 있으므로, 내려간 뒤에 되돌린다. 복구가 실패하면 재기동하지 않고 끝낸다(install.sh 안내).
- `-RecoverOnly`: `InstallRoot/session_manager`가 없어도 교체 저널(`info`)이 있으면 받는다(교체 도중 죽으면 `SWAP/old`에 있다). apply.lock 은 건드리지 않고, 일하는 동안 잠금과 표식을 쥔다. 커밋된 저널은 치우기만 한다(uv sync·last_apply 없음). 되돌렸으면 uv sync 와 `last_apply`(info 의 version, ok=false). 잔재가 있는데 이 설치본의 Host 가 떠 있으면(러너가 재기동 뒤 헬스 전에 죽어 그 Host 가 잔재 트리로 돌고 있다) 되돌리지 않고 3 — 런처는 확인 전에 복구를 부르므로 그대로 보류(0)한다(런처 로그 문구 `skip: 복구 보류(rc=3 …)`). Host 가 내려간 뒤의 start·다음 업데이트가 되돌린다. 종료코드 0 됨·잔재 없음 / 1 실패 / 2 인자 / 3 다른 러너·떠 있는 Host.
- `info`는 `key=value` 줄(`format=1`, `runner_pid`, `version`, `from`, `started`). `format`이 1이 아니거나 저널에 PAYLOAD 밖 이름·모르는 줄이 있으면 아무것도 옮기지 않고 실패한다.
- 신호: `trap ':' HUP INT`(빈 처리). 명세의 `trap '' HUP INT`(무시)는 exec 로 uv·런처·재기동 Host·그 Host 가 새로 띄우는 브로커와 claude 까지 물려가, 브로커가 죽어도 claude 가 SIGHUP 으로 끝나지 않는다. `test_posix_scripts`에 `trap ''` 금지 규칙을 더했다.
- (검토 반영) 런처(`start-connector.sh`)는 PAYLOAD 밖이지만 새 버전 Host 가 뜨자마자(헬스 판정 전) 자기 템플릿으로 다시 렌더한다. 교체 전 런처를 저널(`SWAP/launcher`)과 백업(`BackupDir/start-connector.sh`)에 함께 두고, 되돌림(롤백·`-RecoverOnly`·저널 없는 백업 복원)이 제자리로 돌린다. 되돌린 버전의 `launcher-ensured-<버전>` 표식도 지운다(그 버전을 다시 받으면 그때 새 템플릿으로 렌더하게). 전엔 롤백한 옛 버전이 실패한 릴리스의 런처로 재기동되고(옛 버전의 표식이 있어 다시 렌더하지 않는다) 그 뒤 모든 기동도 그랬다 — 템플릿이 문제인 릴리스면 옛 버전이 영영 못 뜬다. 저널 형식은 그대로 v1(옛 러너는 모르는 파일을 무시한다).
- (검토 반영) 러너 잠금의 시체 치우기는 런처와 같은 권리 폴더(`runner.lock.d.reclaim`)로 한다 — 둘이 동시에 치우면 같은 교체 저널을 두 러너가 동시에 되돌렸다(`runner_lock_take`).
- 헬퍼(`HELPER_PY`, `-I -S`)는 `health`만 한다. 부를 때마다 venv 파이썬 → [linux] python3 → curl 문자열 검사 순으로 고른다(업데이트 중 venv 가 바뀔 수 있어서). waitpid·cmdline·procs·JSON 이스케이프는 셸 한 길(`/proc`, 맥은 `ps -A -ww -o pid= -o command=`, sed)로 한다.
- `-HealthSeconds`(기본 45)를 더했다. 파이썬은 넘기지 않는다(시험과 느린 PC 용).
- 재기동이 실패하면(런처 종료코드가 0이 아님 — 기동 직후 종료·막힘) 헬스 45초를 기다리지 않고 곧바로 롤백한다.
- 사전 조건 오류(종료코드 2)라도 설치 루트가 멀쩡하면 옛 버전으로 계속한다. 본체를 죽이지 않고 `WaitSeconds`까지 스스로 내려가기를 기다렸다가(수동 적용은 1.5초 뒤 내려간다) 런처 start 를 부르고, 안 내려가면(재기동 자동 적용) 그대로 둔다. 인자가 망가졌으면(상대·빈 경로, 제어문자) 아무것도 하지 않는다. 교체 전 중단(TERM·백업 실패·스테이지 복사 실패·교체 도중 실패 후 되돌림)도 같은 길이다. 이때는 잠금과 표식을 먼저 풀고 `CLEWPATH_UPDATER` 없이 부른다. 그래야 런처가 평소대로(남의 잔재가 있으면 -RecoverOnly 포함) 판단한다.
- `last_apply`(ok=false)는 롤백을 시작할 때 먼저 쓴다. 다시 띄운 옛 Host 의 재기동 자동 적용이 같은 버전을 또 받지 않게 하려는 것이다. 살아 있는 설치 표식(`by=install`)도 종료코드 3이다.
- 인자 검증: `-BackupDir`는 로그 폴더 바로 아래, `-Version`은 `[0-9][0-9A-Za-z.+-]*`(64자까지 — 파일 이름과 JSON 에 들어간다), 숫자 인자는 앞자리 0 금지(`$(( ))`가 8진수로 읽는다), 경로는 절대 경로이고 제어문자·`.`·`..` 조각이 없어야 한다. rm -rf 는 `BackupDir`·`BackupDir.tmp.*`·`BackupDir-failed-*`·`InstallRoot/.clewpath-swap.*`·`WORK/runner.lock.d`에만 한다.
- 스크립트 전체를 `main()`에 넣었다. updater.apply 의 `copy2`가 같은 자리에 덮어써도, 돌고 있는 sh 는 이미 다 읽은 함수만 쓴다.
- 시험: `tests/test_update_runner_sh.py`(R①~R⑮, rename 마다 SIGKILL 17곳, curl 길, uv 찾는 순서, 런처 + 진짜 러너 복구, 업데이트 중 감독자 대기, Host 가 떠 있을 때 복구 보류), `test_update_safety`(러너 사본 실행). 리눅스 컨테이너에서 러너에 버그를 하나씩 넣어 시험이 잡는지 확인했다.
- (4단계에서 고침) 커밋 표식은 `true >"$SWAP/committed"`로 쓴다. `: >…`(특수 내장)는 쓰기가 실패하면 dash·`bash --posix`가 러너를 정리 없이 끝냈다(잠금·표식·apply.lock 이 남음). `test_posix_scripts`가 `:` 리디렉션을 금지한다.
- 뒤 단계가 맞출 것: install.sh 는 `.clewpath-install`에 `uv=`·`python_pref=`·`python_install_dir=`를 쓴다(러너가 읽는다). install.sh 의 표식은 `by=install`이고 명령줄에 `install.sh`가 있어야 러너가 산 것으로 본다(그동안 러너는 종료코드 3). uninstall.sh 는 살아 있는 러너 표식(`by=runner`)이 있으면 끝나기를 기다리거나 거부하는 편이 안전하다(러너가 끝에 Host 를 다시 띄운다 — 5단계에서 정할 것).

### 3.5 uninstall.sh (설치 루트)

**인자**: `--yes`, `--purge-data`, `--purge-uv`, `--purge`(둘 다). `--remove-hooks`는 사장님 승인 전까지 만들지 않는다(8절 Q6).

**순서**

1. **확인**
   - `ROOT`(물리 경로)에 `session_manager/`와 `pyproject.toml`이 있어야 한다.
   - `ROOT`가 `/`나 `$HOME`이면 거부한다. `.git`이나 `ops/`가 있으면 거부한다(개발 트리).
   - 안전을 위해 자신을 `mktemp`로 복사해 `--root "$ROOT"`와 함께 다시 실행한다. sh는 열린 fd로 읽으므로 이 복사는 이중 안전장치다.
2. **정보 수집**
   - `DATA_DIR`: `sh start-connector.sh paths`, 없으면 `.clewpath-install`, 없으면 경로 규칙.
   - `CLEWPATH_HOME`: 기록 파일, 없으면 경로 규칙.
   - 지울 목록을 만든다.
3. **사용자 확인** (`--yes`가 없을 때)
   - `/dev/tty`로 묻는다.
   - 지울 목록을 보여 주고 "열린 ClewPath 터미널(브로커 안 claude)이 모두 끝납니다"를 알린다. `--purge-data`면 "기기 페어링·자격증명이 모두 무효"도 알린다.
   - tty가 없고 `--yes`도 없으면 종료코드 2.
4. `maintenance.flag`(`by=uninstall`)를 만든다. watch가 다시 띄우지 못하게 하려는 것이다.
5. watch를 멈춘다: `watch.lock.d/pid`의 cmdline에 `start-connector`와 `watch`가 있으면 TERM.
6. Host를 멈춘다: `sh start-connector.sh stop`, 없으면 `stop_host_fallback`.
7. 브로커를 멈춘다: `ptyd.json`의 pid가 `session_manager.ptyd`이고 `"$ROOT/.venv/bin/"` 실행이면 TERM, 5초 뒤 KILL. 남은 소켓(`ptyd.json`의 address)을 지운다.
8. 자동 시작 해제 자리. 다음 단계에서 [mac] `launchctl bootout gui/$(id -u)/com.clewpath.host`와 plist 삭제, [linux] `systemctl --user disable --now`를 넣는다.
9. `rm -rf "$ROOT"`(가드 거침).
10. `--purge-uv`면 `$CLEWPATH_HOME/bin`과 `$CLEWPATH_HOME/python`을 지우고, `CLEWPATH_HOME`이 비면 rmdir. uv 캐시는 안내만 한다.
11. `--purge-data`면 `DATA_DIR`을 지운다. 가드: 이름이 `session_manager`이고 `~/.claude` 자체가 아니어야 한다. 아니면 4단계에서 만든 `maintenance.flag`만 지운다. 남겨 두면 재설치 첫 기동이 막힌다.
12. `~/.claude/projects`는 어떤 경우에도 건드리지 않는다.
13. `~/.claude/settings.json`은 읽기만 한다. ClewPath 훅(`/api/hooks/event`)이 남아 있으면 개수와 수동 삭제 방법을 안내한다. `settings.json.bak-clewpath`도 안내만 한다.
14. 남은 것을 보고한다: 프로세스(cmdline에 ROOT 포함), ROOT, (purge 시) 데이터·bin. 하나도 없으면 종료코드 0, 남았으면 1. 각 단계는 실패해도 `[WARN]` 후 계속한다.

**README 8절과의 1:1 대응** (README.md:189-201)

| README(Windows 수동) | uninstall.sh | 차이 |
|---|---|---|
| `Unregister-ScheduledTask`, HKCU Run 삭제 | 8단계 자리(다음 단계) | |
| 설치 폴더 삭제 | 9단계(가드) | |
| `%LOCALAPPDATA%\ClewPath`(uv) 선택 삭제 | `--purge-uv` | |
| `~/.claude/session_manager` 선택 삭제 | `--purge-data`(경고, 확인) | |
| `~/.claude/projects` 유지 | 같음 | |
| 없음 | 표식, watch·Host·브로커 정지 | README에는 정지 단계가 없다 |
| 없음 | 훅 안내(읽기만) | 불가침 원칙 |

**구현 메모 (5단계, 2026-10-20)**
- 결정 Q6 대로 훅은 지우지 않는다. 데이터 폴더의 부모(= Host 가 훅을 적은 클로드 폴더)의 `settings.json`에서 `/api/hooks/event`가 든 줄 수를 세어 남은 개수·지우는 법·`settings.json.bak-clewpath`(참고용, 통째로 되돌리지 말 것)를 안내한다.
- 업데이트·설치가 진행 중이면(러너 잠금, 또는 by=runner·by=install 의 살아 있는 표식) 확인을 묻기 전에 거부한다(1) — 러너는 끝에 Host 를 다시 띄우고, 설치는 파일을 놓는 중이다(3단계 메모의 열린 질문).
- 설치 루트는 session_manager 와 이름이 `clewpath-host`인 pyproject 가 있어야 한다(`--root`로 남의 폴더를 가리켜도 지우지 않게).
- `--purge-uv`는 폴더째 지우지 않는다: `bin`에서는 `uv`·`uvx`만 지우고 비면 rmdir, `python`은 uv 관리 런타임 모양(`cpython-*`·uv 의 잠금·임시)만 있을 때 통째로 — `CLEWPATH_HOME`을 남과 같이 쓰는 폴더로 정했어도 남의 파일을 지우지 않는다. 끊긴 get.sh 의 `.clewpath-get.*`도 이때 치운다. uv 캐시는 안내만.
- 남은 프로세스 확인은 후보(`/proc/*/cmdline` grep)를 다 모은 뒤 하나씩 다시 본다 — 찾는 grep 자신이 그 글자를 인자로 가져 '남은 프로세스'로 잡히던 경합(시험에서 느린 grep 으로 늘 재현)을 막는다. 명령줄이 나와 똑같은 프로세스(내 하위 셸 — 하위 셸 안에서도 `$$`는 본 셸의 pid)도 뺀다. 설치 폴더 안의 uninstall.sh 를 `--root`로 직접 부르면 스크립트 경로에 설치 폴더가 들어 있어 제 하위 셸을 남은 것으로 보던 문제(`bash --posix` 시험에서 발견).
- 확인 질문은 `/dev/tty`가 열릴 때만(하위 셸에서 시험), 표준 입력이 파이프여도 사람에게 묻는다. `y`·`yes` 외에는 취소(1).
- 남은 것이 없으면 0, 프로세스·설치 폴더·(purge 때) 데이터·uv 가 남으면 1.

## 4. 파이썬 쪽 변경

### 4.1 osdep

```python
# _posix.py
CURL = "curl"

def update_runner_argv(runner: Path, *, install_root: Path, stage_dir: Path, backup_dir: Path, wait_pid: int,
                       port: int, version: str, log_file: Path, restart_script: str = "") -> list[str]:
    a = ["/bin/sh", str(runner), "-InstallRoot", str(install_root), "-StageDir", str(stage_dir),
         "-BackupDir", str(backup_dir), "-WaitPid", str(wait_pid), "-Port", str(port),
         "-Version", str(version), "-LogFile", str(log_file)]
    return a + (["-RestartScript", restart_script] if restart_script else [])

def render_launcher(template: str, *, data_dir: Path, conf_file: Path, port: int) -> str:
    # 경로에 NUL·줄바꿈이면 ValueError, 1<=port<=65535, 값은 shlex.quote, 렌더 뒤 '{{' 가 남으면 ValueError
    ...

def ensure_launcher(root: Path, data_dir: Path, conf_file: Path, port: int) -> str | None:
    # 템플릿 없음 → "launcher: template missing — skip"
    # 기존 start-connector.sh 의 DEFAULT_PORT 를 우선(폴백 포트 BOUND_PORT 로 덮지 않게)
    # 같으면 "launcher: ok (posix v2)", 다르면 tmp → os.replace, chmod 0o755 → "launcher: regenerated (posix v2)"
    # 예외 → "launcher: regenerate failed: …". 자동 시작 점검은 다음 단계(그때도 '렌더 성공 뒤에만')
    ...

def port_free(host: str, port: int) -> bool:
    # ① SO_REUSEADDR 없이 bind → 되면 True
    # ② 실패면 루프백(host 가 0.0.0.0/'' 이면 127.0.0.1)에 connect(0.5초):
    #    거절(ECONNREFUSED)일 때만 = 듣는 소켓 없음(TIME_WAIT 뿐) → SO_REUSEADDR 로 bind 재시도 → 되면 True
    # ③ 그 밖 False. [mac] 은 SO_REUSEADDR 가 와일드카드 리스너 옆 bind 를 허용하므로 ② 의 connect 확인이 필수
```

- `_win.py`: `CURL = "curl.exe"`, `port_free`는 지금 `server._port_free` 본문을 그대로 옮긴다(Windows의 SO_REUSEADDR는 의미가 달라 쓰지 않음).
- `__init__.py`: `CURL`과 `port_free`를 공개 목록에 더하고 머리말을 "업데이트 러너·런처 렌더는 공통 단계에서 구현"으로 고친다.
- `test_osdep.py:42`의 `update_runner_argv` 시그니처 예외를 지운다.

### 4.2 updater.py

- `PAYLOAD = ("session_manager", "pwa", "pyproject.toml", "uv.lock", "install.ps1", "install.sh", "uninstall.sh", "README.md", "LICENSE")`.
  - 옛 설치본의 `stage()`는 새 최상위 항목을 버리므로(:256-259) Windows 설치본에는 다음다음 업데이트부터 들어간다. Windows에서는 쓰지 않으니 상관없다.
  - POSIX 첫 설치는 get.sh가 전체를 받으므로 문제가 없다.
- `check()`의 platforms 판정(verify 뒤):
  ```python
  plats = m.get("platforms", ["win"])
  if not (isinstance(plats, list) and all(isinstance(p, str) for p in plats)):
      plats = []                                         # 모양이 이상하면 fail-closed
  if osdep.NAME not in plats:
      return write_state(last_check=…, available=False, error=None, current=cur, latest=m["ver"],
                         notes=m.get("notes", ""), manifest=None,
                         unsupported=f"이 릴리스({m['ver']})는 {osdep.NAME} 를 지원하지 않습니다")
  ```
  Windows는 legacy(클레임 없음) 판정이 그대로다.
- `apply()` 맨 앞, 잠금 전에: `config.data_dir()/"maintenance.flag"`가 있으면 `UpdateError("유지보수 표식(maintenance.flag)이 있어 적용하지 않습니다 — 재기동이 막혀 롤백됩니다. 표식을 지운 뒤 다시 하세요.")`.
  - 자동 경로(server.py:276-290)는 예외를 로그로만 남긴다.
  - OS와 무관하므로 Windows 동작도 바뀐다(개선).
- 머리말 교체 목록(:21-23)을 실제 PAYLOAD에 맞게 고친다.
- (선택) 러너 사본을 `chmod 0o700`, apply.lock을 `os.open(O_CREAT|O_EXCL)`과 pid로. 둘 다 별건이다.

### 4.3 hostctl.py (새 파일, install.sh 전용 CLI)

`python -I -m session_manager.hostctl <명령>`. 표준출력은 `key=value` 줄이고, 사람용 메시지는 표준오류로 낸다. 종료코드는 0 성공, 1 오류, 2 사용법이다.

| 명령 | 동작 | 출력 |
|---|---|---|
| `paths` | `config.data_dir()`, `appconfig.config_path()`, `config.claude_home()` | `data_dir=`, `conf_file=`, `claude_home=`, `projects_dir=` |
| `secure-data-dir` | `DATA_DIR`을 0700으로, 그 아래 파일·폴더에서 go-rwx 제거(lstat로 링크 건너뜀) | `changed=N` |
| `write-config --port --cp-url --relay-url --app-url [--claude-config-dir]` | 있으면 그대로. 없으면 `appconfig.render_template(...)`, tomllib 검증, tmp에 0600으로 쓰고 `os.replace`. 토큰은 env에서만 읽는다 | `config=created|kept`, `path=` |
| `render-launcher --root --data-dir --conf-file --port` | `_posix.render_launcher`, 원자 교체, 0755 | `launcher=created|updated|unchanged` |
| `provision --cp-url` | `systls.use_system_trust()`, `SM_CP_URL` 설정, 있으면 재사용, 없으면 `cp_client.provision(httpx.Client(), appconfig.machine_name())` | `creds=kept|created|failed`, `connector=`, `reason=` (fail-open이라 0) |
| `wait-ready --port --tries 20` | install.ps1:643-695와 같은 폴링, 진단 요약 | `state=up|down`, `port=`, `mode=`, `session_count=`, `claude_exe_found=` |

- 맨 위 import는 표준 라이브러리만 쓴다. 나머지는 명령 안에서 늦게 import 한다.
- 런처와 러너는 hostctl을 쓰지 않는다. 버전 어긋남과 교체 중 상태를 피하기 위해서다.
- 구현 메모(4단계): `paths`는 Host 가 기동할 때와 같은 순서로 구한다 — 설정 파일 자리(`appconfig.config_path()`, 런처가 `CLEWPATH_CONFIG`로 고정할 값)를 먼저 정하고, 그 파일을 `apply_env()`로 펼친 뒤(`[claude] config_dir`) 데이터 폴더를 정한다. 같은 프로세스에서 부르는 시험이 물려받지 않게 env 는 끝나면 통째로 되돌린다. 상대 경로·제어문자가 든 경로는 거부한다(1). `write-config`는 설정 파일 자리만 보며 `port=`(유지면 그 파일의 포트)를 함께 낸다. `secure-data-dir`·`wait-ready`는 `--data-dir`, `write-config`는 `--conf-file`을 선택 인자로 받는다(install.sh 는 `paths` 결과를 넘긴다). `provision`은 `reason=`을 낸다(`provision_rate_limited` 등). 발급 로그(room 포함)는 버리고 connector id 만 보여 준다. key=value 값은 파일 시스템 바이트 그대로(`os.fsencode`) 낸다.

### 4.4 그 밖의 파이썬

**필수** (POSIX에서 바로 깨지는 곳)
- `server._port_free`는 `osdep.port_free`를 부르는 얇은 함수로 남긴다. `test_appconfig`의 monkeypatch 지점을 유지하기 위해서다.
- `_clewpath_already_there`(server.py:2117-2131)는 `build_opener(ProxyHandler({}))`를 쓴다.
- `connector.relay_config_from_env(bound_port=None)`은 `local_base` 포트를 `bound_port or SM_PORT`로 정한다. lifespan(server.py:397)이 `_updater_mod.BOUND_PORT`를 넘긴다. OS와 무관한 결함이며 Windows의 폴백 포트에도 해당한다. `SM_PORT` env는 바꾸지 않는다. 바꾸면 러너를 거쳐 재기동 Host로 이어져 포트가 계속 밀린다.
- `hooks.hook_command`(hooks.py:114)의 `"curl.exe"`를 `osdep.CURL`로 바꾼다. 셸 공통 조건(`||` 금지 등)은 그대로다. 맥·리눅스는 `--noproxy 127.0.0.1`을 더한다(검토 반영 — 훅은 claude 의 env 에서 돌아 curl 이 루프백에도 http_proxy·ALL_PROXY 를 써, 프롬프트가 든 훅 내용이 프록시로 새고 Host 에는 닿지 않았다). Windows 명령은 글자까지 그대로(settings.json 을 다시 쓰지 않게).
- 커넥터가 로컬 Host 로 가는 길(`local_base`의 httpx 클라이언트, 터미널·재개·관제 WS)은 프록시를 쓰지 않는다(`trust_env=False`, websockets 15+ `proxy=None`)(검토 반영 — 사람 증명 헤더·세션 내용이 프록시로 새고 원격 접속이 끊겼다). 릴레이로 나가는 연결은 사용자의 프록시를 그대로 따른다. OS 무관(Windows 의 시스템 프록시 설정에서도 같은 결함).
- 훅 첫 등록 때 남기는 `settings.json.bak-clewpath`는 0600 으로 만든다(검토 반영 — env 비밀이 그대로 든 사본이 umask 0644 로 생겼다).
- `cp_client.provision`(:127)의 표시 이름 기본값을 `appconfig.machine_name()`으로 바꾼다. Windows는 COMPUTERNAME이 먼저라 결과가 같다.

**권장 (작음)**
- `appconfig.render_template(port, cp_url, relay_url, app_url, client_token=None, agent_token=None, claude_config_dir=None)`
  - 값은 `json.dumps(s, ensure_ascii=False)`로 따옴표째 넣는다. TOML 기본 문자열과 호환된다.
  - 토큰과 config_dir가 없으면 주석 줄로 둔다.
  - 머리 주석의 "start-connector.ps1 에 $env:…"를 OS 공통 표현으로 바꾼다.
  - install.ps1 인라인 템플릿(:443-484)과 섹션·키 집합이 같은지 테스트로 묶는다.
- `liveness.mark_shutdown(reason, pid=None)`: 런처의 강제 stop이 대상 pid로 표식을 남긴다.
- (선택) `liveness.log_tail`이 마지막 `=== launcher start` 줄 앞까지만 읽는다. 새 프로세스 첫 줄이 섞이지 않게 하려는 것이다.

### 4.5 릴리스 도구

**package-for-user.ps1**
- `$include`에 `install.sh`, `uninstall.sh`를 더한다.
- `$excludePatterns`에 `start-connector.sh`, `.clewpath-install`을 더한다. 부분일치이지만 `start-connector.template.sh`는 걸리지 않는다.
- 비밀 스캔 확장자(:124)에 `.sh`를 더한다.
- 새 게이트: 스테이지의 모든 `.sh`가 CR 없음, BOM 없음, 1행 `#!/bin/sh`이어야 한다. 아니면 중단한다.
  - 이 게이트가 필요한 이유: 편집기가 CRLF로 저장해도 `eol=lf` 정규화 때문에 `git status`는 깨끗하게 보인다. 그래서 release.ps1의 "깨끗한 트리" 게이트가 이를 못 잡는다.

**sign_release.py**
- `sign --platforms win[,mac][,linux]`를 더한다. 기본은 `win`이고, 허용 값 밖이면 거부한다.
- 클레임 `"platforms": [...]`를 넣는다. verify 출력에 표시한다.
- 하네스용 규칙: keygen은 쓰지 않는다. 시험 키는 `--key 임시경로`로만 넘긴다.

**release.ps1**
- `-Platforms "win"`(기본)을 sign에 넘기고 로그에 남긴다.
- zip 게이트(:107-115 옆):
  - `install.sh`, `uninstall.sh`, `session_manager/update_runner.sh`, `session_manager/start-connector.template.sh`, `session_manager/hostctl.py`가 있어야 한다.
  - 항목 이름에 `\`가 없어야 한다(Compress-Archive 대비).
  - zip 안 `updater.py`의 `RELEASE_KEYS` kid 집합이 저장소와 같아야 한다. 시험 빌드 게시를 막는다.
- (권장, 맥 단계 전에 확정) `-Platforms`에 mac이나 linux가 들어가면, 같은 HEAD에서 통과한 POSIX e2e 기록(`ops/logs/posix-e2e-<ver>.json`)이 있어야 게시한다.

### 4.6 매니페스트 platforms

- 값은 `osdep.NAME`과 같다(`win`·`mac`·`linux`, multi-os.md:81 표기).
- 클레임이 없으면 `["win"]`로 본다. get.sh와 updater가 같은 규칙을 쓴다.
- CP는 보관과 배포만 하므로 007 변경이 필요 없다. 다만 CP는 최신 한 건만 준다(007 api.py:543-548). 최신이 어떤 OS를 빼면 그 OS는 그동안 설치·업데이트를 받지 못한다(8절 R6).
- 이번 단계 릴리스는 `["win"]`이다. 맥 단계가 끝나면 `["win","mac"]`, 리눅스 정식 지원 때 `"linux"`를 더한다. 시험 빌드는 `["win","mac","linux"]`로 서명한다.

**구현 메모 (7단계, 2026-10-21 — 4.5·4.6 과 다른 점과 뒤 단계가 맞출 약속)**
- `sign_release.py sign --platforms`: argparse 형식 검사라 모르는 이름·빈 목록은 서명 전에 종료코드 2. 쉼표·공백·대소문자·겹침을 받고 클레임은 늘 `win,mac,linux` 순서로 쓴다. 서명 출력 줄(`ver=…`)에 `platforms=`를 붙였다(release.ps1 이 그 줄을 로그에 남긴다). `verify`는 배포본(updater)이 받아들이는지 본 뒤 `platforms=win,mac` 한 줄을 찍는다. 클레임이 없으면 `platforms=(클레임 없음 — win 전용으로 읽힌다)`, 모양이 이상하면(문자열·빈 목록·모르는 이름·문자열이 아닌 항목) `[OK]`를 찍지 않고 1.
- `release.ps1 -Platforms`: `[string[]]`(기본 `win`) — `-File`로 준 `win,mac`과 PowerShell 안의 배열을 다 받는다. 버전 형식 바로 다음에 본다(다른 사전점검보다 먼저). **win 은 늘 들어가야 한다**(4.5 에 없음) — CP 는 최신 한 건만 주므로(R6) win 이 빠진 릴리스가 최신인 동안 Windows 사용자 전체가 업데이트를 못 받고, get.ps1 은 platforms 를 보지 않아 그 zip 을 Windows 에 그대로 깐다. verify 출력의 `platforms=` 줄을 `-Platforms`와 대조한다.
- (검토 반영) `sign_release.py sign --platforms`도 win 이 없으면 서명 전에 거부(2)하고, `verify`는 win 이 없는 클레임에 `[FAIL]`(1)을 낸다 — 'win 필수'가 release.ps1 에만 있어, 문서에 있는 수동 package→sign→publish 길로 Windows 업데이트가 조용히 끊길 수 있었다.
- (권장 항목을 이번에 넣음) mac·linux 가 들어가면 `ops/logs/posix-e2e-<ver>.json` = `{"version": "<ver>", "head": "<git rev-parse HEAD>", "ok": true}`가 있어야 한다. 8단계 e2e 가 통과하면 이 모양으로 남길 것. `.gitignore`에 넣어 '깨끗한 트리' 게이트에 걸리지 않게 했다.
- zip 내용 게이트는 함수 `Test-ReleaseZip`(시험이 AST 로 꺼내 pwsh 로 돌린다): 맥·리눅스 다섯 파일, 이름에 `\` 없음(Windows PowerShell 5.1 의 Compress-Archive 가 만든다 — pwsh 7.6 의 Archive 1.2.6 은 `/`, 실측), `session_manager/updater.py`의 `RELEASE_KEYS` kid 집합이 저장소와 같음(주석 줄의 옛 키는 세지 않는다). 서명 도구가 아니라 여기에 둔 이유: 시험 하네스도 `sign`으로 시험 키 빌드를 서명한다.
- package-for-user.ps1: `.sh` 형식 게이트는 비밀 스캔 다음·zip 전. 파일마다 이유 한 줄(`(BOM)`·`(CR - 줄끝이 CRLF)`·`(1행이 #!/bin/sh 가 아님)`)을 찍고 1. 1행 비교는 대소문자를 가린다.
- 드라이런(작업 트리, 출력·스테이지 모두 scratchpad): 98 항목(파일 97 + 하위 폴더만 든 `session_manager/skills/`의 폴더 항목 — Compress-Archive 가 만든다), 역슬래시 0, `create_system=0`·`external_attr=0`, `.sh` 넷 모두 LF·BOM 없음·`#!/bin/sh`, `start-connector.sh`·`.clewpath-install` 없음, `Test-ReleaseZip` 문제 0. 게시·적용은 하지 않았다.
- 시험: `tests/test_sign_release.py`(새), `tests/test_package_for_user.py`(새 — 가짜 저장소에서 진짜 스크립트를 돌린다, pwsh 가 없으면 건너뜀), `test_release_script`·`test_release_key_safety`·`test_updater`(패키지 대조를 엄격하게 — 넷이 같아야). Windows 에서 두 ps1 에 버그를 하나씩 넣어(18종) 모두 잡히는 것을 확인했다.

### 4.7 테스트 변경 목록

**기존 테스트 수정**
- `test_osdep`:
  - 42행 예외를 지운다.
  - `test_posix_update_runner_argv_shape`: 앞이 `/bin/sh, runner`, `-RestartScript`는 조건부, 키워드 시그니처가 `_win`과 같음.
  - `CURL`·`port_free` 공개를 확인한다.
- `test_update_safety:49`의 `_WIN_RUNNER` skip을 지운다. 세 건이 POSIX에서도 돈다.
- `test_updater`:
  - `test_payload_lists_agree`: `updater.PAYLOAD`, ps1 `$Payload`, sh `PAYLOAD`, package `$include`가 같다.
  - platforms가 없거나 win만이면 POSIX에서 available=False.
  - maintenance.flag가 있으면 apply가 거부한다.
- `test_hooks:45`: `osdep.CURL`로 바꾸고, POSIX에서는 `curl.exe`가 없어야 한다.
- `test_liveness`:
  - POSIX 템플릿 계약(5절 S41과 6.1절).
  - `mark_shutdown(pid=)`.
- `test_appconfig`: 렌더 이스케이프(따옴표, 역슬래시, 한글, 공백)와 tomllib 통과, install.ps1과 키 집합 일치.
- `test_release_key_safety`: 스캔 확장자에 `.sh`, 제외에 `start-connector.sh`.
- `test_release_script`: `-Platforms` 전달, zip 게이트 문구.

**새 테스트**: 6.1과 6.2절.

## 5. 보안 불변식 체크리스트

| # | 불변식 | 지키는 곳 | 검증 |
|---|---|---|---|
| **신뢰·무결성** | | | |
| S1 | 업데이트 매니페스트는 오프라인 Ed25519 서명이다. 공개키는 코드에 고정하고, env·네트워크·파일로 키를 더하는 경로를 만들지 않는다. 시험 키는 임시 빌드 사본에만 있다 | updater.py:48-51 | test_updater, 정적 가드(코드에 키 주입 경로 없음) |
| S2 | 검증: alg는 EdDSA 고정, iss·aud, exp(leeway 60), kid 화이트리스트, 필수 클레임, sha256 64자 | updater.py:139-169 | 기존 10건 |
| S3 | 저장된 토큰을 쓰기 직전마다 다시 검증한다 | updater.py:370, server.py:284·302·793 | 기존 |
| S4 | 내려받기: 크기 상한, 정확한 크기, SHA-256, `.part`에서 원자 교체 | updater.py:210-237 | 기존 |
| S5 | 엄격히 더 새 버전만 적용한다(다운그레이드 차단, TTL 120일) | updater.py:204 | 기존 |
| S6 | platforms: 자기 OS가 없으면 설치도 적용도 하지 않는다. 클레임이 없으면 win 전용이고, 모양이 이상하면 거부한다 | get.sh G3, updater.check | 6.1, 6.3 S1 |
| S7 | get.sh: https 강제(`--proto`, `--proto-redir`, TLS1.2), SHA-256과 크기 대조, 값 형식과 키 1회 검증, `main()` 감싸기, 서명을 검증하지 않는다는 사실을 머리말에 적는다 | get.sh | 6.1, e2e S1 |
| S8 | zip 이름 검사: 절대 경로, `..`, `\`, 제어문자, 점으로 시작하는 최상위 이름, 예약 이름, 심볼릭 링크를 거부한다. 임시 stage에 푼 뒤 사후 확인하고 배치한다 | get.sh G6, install.sh I1 | e2e S1 |
| S9 | 스테이징: zip-slip 거부, `session_manager` 필수, PAYLOAD 밖 최상위 항목 삭제. 런처를 zip으로 심을 수 없다 | updater.py:240-260 | test_stage_* |
| S10 | uv: 고정 버전과 소스에 박은 대상별 SHA-256. 표에 없는 버전은 경고 후 배포처 해시, 로컬 tarball은 표에 있어야 한다. `--system-uv`는 경고, 재사용할 때 버전 확인 | install.sh I3 | 6.1 해시표, e2e |
| S11 | 의존성: `uv.lock --frozen --no-dev`가 원칙이고, 잠금 밖 폴백은 경고와 로그를 남긴다 | install.sh I6, 러너 5 | e2e |
| **권한·경계** | | | |
| S12 | root 실행 거부(`--allow-root`는 시험 전용). 사용자 영역에만 설치하고 sudo를 쓰지 않는다 | get.sh, install.sh | 6.1, e2e |
| S13 | Host는 기본으로 127.0.0.1에 바인드한다. 시험 컨테이너도 포트를 밖으로 열지 않는다 | toml, 6.3 | e2e |
| S14 | 업데이트 적용은 로컬 루프백이거나 릴레이를 거친 2FA 요청일 때만 | server.py:777-779 | 기존 |
| S15 | 개발 체크아웃(`.git`·`ops/`) 보호: 자동·수동 적용 거부, 러너가 다시 거부, uninstall 거부, get.sh 배치 거부 | updater.py:71-79, 러너 0, uninstall 1 | 6.2 |
| **비밀** | | | |
| S16 | `DATA_DIR` 0700(install과 매 기동). 기존 비밀 파일과 toml은 0600. 자격증명은 `save_creds`로 0600·원자 저장 | hostctl, 런처, cp_client.py:79-88 | e2e S4⑦ |
| S17 | 토큰은 argv로 넘기지 않는다(ps 노출). get.sh에서 install.sh, hostctl까지 env로 전달한다 | 3.1, 3.2 | 6.1 정적(hostctl에 토큰 인자 없음) |
| S18 | `set -x`와 env 덤프 금지. 토큰은 출력·로그에 남기지 않는다 | 다섯 스크립트 | 6.1 정적 |
| S19 | 릴리스 키: 하네스는 `ops/release-keys`를 읽지 않고 keygen을 부르지 않는다. 빌드 컨텍스트는 화이트리스트 산출물이며 `*.pem`이 있으면 중단한다 | posix_e2e | 6.1 정적 가드 |
| S20 | 패키지 비밀 스캔에 `.sh`를 더한다. 키 파일이 있으면 중단한다(기존) | package-for-user.ps1 | test_release_key_safety |
| **프로세스 안전** | | | |
| S21 | 멱등 런처 순서: maintenance → update 표식 → 잔재 복구 → health → pid(좀비·cmdline) → 잠금 → 재판정 → 기동. health 실패만으로 죽이거나 겹쳐 띄우지 않는다 | 런처 start | 6.2, e2e S4 |
| S22 | 반복 실행 주체(watch, 다음 단계의 launchd·systemd)는 멱등 런처를 확인한 뒤에만 둔다 | 3.3, 다음 단계 | 6.1 |
| S23 | maintenance.flag를 존중한다(start·watch·foreground). 표식이 있으면 apply를 거부한다(재기동이 막혀 롤백되는 것 방지) | 런처, updater.apply | e2e S4③, test_updater |
| S24 | 러너와 설치가 도는 동안 감독자는 기동하지 않는다(`update.inprogress`). `CLEWPATH_UPDATER=1`은 러너 자신의 재기동만 통과한다 | 런처, 러너 1, install I2 | 6.2, e2e S4⑪ |
| S25 | 정상 종료는 SIGTERM이다(normal 표식, bye, 브로커 세션 분리). KILL은 15초(러너는 10초) 뒤에만 쓰고 표식을 남긴다 | 런처 stop, 러너 2 | e2e S4⑤ |
| S26 | 브로커(`session_manager.ptyd`)는 stop·install·get·update·rollback에서 빠진다. uninstall만 확인을 거친 뒤 멈춘다 | 3.3–3.5 | 6.2⑤, e2e S5·S6 |
| S27 | 프로세스는 명령줄(설치 경로와 모듈)로 식별한다. venv python은 심볼릭 링크라 실행 파일 경로·이름으로 고르지 않는다. pid 재사용을 검사한다 | 런처, 러너, 설치 | 6.2 |
| S28 | Host의 두 번째 방어선(같은 포트에 ClewPath가 있으면 exit 1)을 유지한다. 루프백 확인은 프록시를 쓰지 않는다 | server.py:2160-2163 | test_appconfig |
| S29 | 러너는 스스로 죽지 않는다. 교체는 rename과 저널이고, 헬스 통과 전 중단은 롤백이며, 통과하면 커밋 표식을 남긴다. 저널 형식은 하위 호환한다 | 러너 | 6.2⑦⑬ |
| S30 | `rm -rf` 가드: 절대 경로, `/`·`$HOME`이 아님, 고정 이름만, InstallRoot 확인. 빈 인자나 상대 경로 인자에서는 지우지 않는다 | 러너, uninstall, install | 6.2⑫ |
| S31 | 러너 잠금(`runner.lock.d`)과 apply.lock 하트비트, 모든 종료 경로에서 해제 | 러너 1·10 | 6.2⑥ |
| S32 | 러너는 설치 폴더 밖 사본으로 돈다. 스테이지의 스크립트는 실행하지 않는다. 재기동은 설치본 고유의 start-connector.sh(PAYLOAD 밖)로 한다 | updater.py:292-302 | 6.2 |
| S33 | 헬스는 BOUND_PORT와 runtime.json, 기대 버전으로 판정한다. scan_all이 없는 진단 엔드포인트를 쓴다. 롤백 헬스는 옛 버전을 기대한다 | updater.py:304-318, 러너 7·8 | 6.2②⑩ |
| S34 | 롤백 반복 차단: `last_apply.json`은 BOM 없이 쓰고 `recently_failed`가 읽는다 | 러너 9 | e2e S6, 6.2⑨ |
| S35 | 의도된 종료 표식(update, normal, forced-stop)으로 incidents 오탐을 막는다 | updater.py:326-338, liveness | e2e S5 |
| **claude 불가침** | | | |
| S36 | `~/.claude/projects`는 존재 확인만 한다. uninstall도 절대 건드리지 않는다 | install I5, uninstall 12 | e2e S9(해시 비교) |
| S37 | settings.json 훅: 첫 기동 때 병합 등록(백업)은 기존 동작이고, POSIX에서는 curl로만 바꾼다. 제거할 때 자동으로 지우지 않고 안내만 한다 | hooks.py, uninstall 13 | e2e S9 |
| S38 | Host를 띄우는 모든 경로는 사용자의 umask·HOME·TMPDIR·PATH를 바꾸지 않는다. claude 자식(터미널·재개)이 물려받기 때문이다 | 다섯 스크립트 | 6.1 정적(umask 명령 없음), e2e S2(`/proc/<pid>/status` Umask) |
| S39 | claude 상속 표식(`CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `CLAUDE_CODE_*`)은 Host를 띄울 때 지운다. 규칙은 `ptyd.clean_env`와 같고, 범위는 Q5에서 정한다 | 런처 spawn | 6.1(목록 일치), e2e S4⑥ |
| **형식·호환** | | | |
| S40 | `.sh`는 LF, BOM 없음, `#!/bin/sh`, POSIX 문법이고 실행 비트에 기대지 않으며 경로를 늘 큰따옴표로 감싼다 | 3.0 | 6.1 |
| S41 | 생성 파일은 BOM 없는 UTF-8로 원자적으로 쓴다(tmp 후 mv·replace) | hostctl, 러너, 런처 | 6.1, 6.2 |
| S42 | PAYLOAD 네 곳이 같아야 한다 | 4.2, 4.5, 3.4 | test_payload_lists_agree |
| S43 | 설정과 자격증명은 덮어쓰지 않는다. cp_url은 정규화(끝 `/` 제거)한다 | hostctl, install I0 | e2e S3 |
| **시험 격리** | | | |
| S44 | 운영 CP의 `/provision/anonymous`를 부르지 않는다(conftest 가드, `--network none`, 가짜 CP) | 6장 | e2e 요청 로그 |
| S45 | 컨테이너 이름은 `*-test`로 끝나고, 운영 네트워크·볼륨을 쓰지 않으며, 일반 사용자와 `--init`으로 돈다. install·get·uninstall 실행 시험은 컨테이너 안에서만 한다(환경 표식으로 확인) | 6장 | 하네스 가드 |
| S46 | 시험 산출물은 게시할 수 없다: 버전 90.x와 시험 kid, release 게이트가 zip의 RELEASE_KEYS를 운영 kid와 대조 | release.ps1 | test_release_script |
| **운영 반영** | | | |
| S47 | CP·nginx 변경과 CP 재시작은 서비스 모니터에 `/deploy-request`로 의뢰한다. 007 control_plane은 bind-mount라 get.ps1 수정이 즉시 운영에 반영되므로 이번 단계는 get.ps1을 건드리지 않는다. util-session-cp는 restart만 하고 재생성하지 않는다 | 7장 | 의뢰서 |

## 6. 리눅스 컨테이너 시험 계획 (운영 반영 아님)

### 6.0 원칙

- 실제 릴리스 키와 운영 CP는 쓰지 않는다.
  - 시험 키쌍은 하네스가 `cryptography`로 프로세스 안에서 만들어 임시 폴더(저장소 밖)에만 둔다.
  - 서명은 `sign_release.cmd_sign`을 `key=<임시>/test-key.pem`으로 불러, 운영과 같은 클레임 형식을 쓴다.
  - Host가 시험 서명을 믿게 하는 방법: 시험 zip을 만들 때 `updater.py`의 `RELEASE_KEYS`를 시험 공개키 하나로 바꾼다. 운영 코드에는 키 주입 경로를 만들지 않는다.
- 실행 단계는 `docker run --network none`이다. 네트워크는 이미지 빌드(apt, uv 캐시 데우기)에만 쓴다. 그래서 운영 CP·릴레이·GitHub에 닿을 수 없다.
- 컨테이너 이름은 `clewpath-posix-e2e-test`와 `clewpath-pytest-test`다. `--rm --init`, 일반 사용자 `tester`(uid 1000)로 돌고, 볼륨·운영 네트워크를 쓰지 않는다. 재료는 이미지에 COPY 한다.
- 빌드 컨텍스트는 `build_fixtures.py`가 만든 임시 폴더다. 저장소 루트를 컨텍스트로 쓰지 않는다. 컨텍스트에서 `*.pem`이나 `release-keys`가 발견되면 중단한다.
- install·get·uninstall을 개발 PC에서 직접 실행하는 시험은 없다. 실제 `~/.local/share`나 5100에 설치되기 때문이다. 하네스는 `CLEWPATH_E2E_CONTAINER=1`이 없으면 시나리오를 거부한다.

### 6.1 정적 계약 테스트 (모든 OS, Windows 포함)

`tests/test_posix_scripts.py`

1. 다섯 스크립트(get.sh는 경로를 env로 받고 없으면 skip): CR 없음, BOM 없음, 1행 `#!/bin/sh`, 템플릿 2행에 `clewpath-launcher v2`.
2. bash 전용 문법 정규식 스캔: `[[`, `function `, `$'`, `<<<`, `source `, `pipefail`, `local `, `echo -e`, 배열 대입, `[ … == … ]`. 금지 명령: `umask`(S38), `set -x`, `sed -i`, `readlink -f`, `flock`, `setsid`, `timeout`.
3. 런처 템플릿에 반드시 들어 있어야 하는 문자열: `maintenance.flag`, `update.inprogress`, `launcher.lock`, `runtime.json`, `/api/health`, `launcher.log`, `{{DATA_DIR}}`, `{{CONF_FILE}}`, `{{PORT}}`, `start_new_session`, `"ab"`, `CLEWPATH_UPDATER`, `CLEWPATH_CONFIG`, `session_manager.server`.
4. 인라인 파이썬에 작은따옴표가 없다.
5. 런처의 정리 목록이 `ptyd._SCRUB_EXACT`와 `CLAUDE_CODE_` 접두와 같다.
6. 공통 경로 블록이 네 스크립트에서 바이트까지 같다.
7. PAYLOAD 네 곳이 같다. 러너에 `session_manager.ptyd` 제외 규칙이 있다(test_ptyd:268-273의 sh 판).
8. uv 해시표: 대상 4개, 값은 64자 hex, 버전은 install.ps1 `$UvDefault`와 같다.
9. 렌더 시험: 공백·한글·작은따옴표·`&`가 든 경로로 렌더하면 자리표시가 0개다. `sh`가 있으면 `sh -n`, `bash --posix -n`, `dash -n`을 통과한다(Windows Git Bash로도 가능).
10. 하네스 가드: `tests/posix_e2e/*`에 `release-keys`·`keygen`·`pyongso.com`이 없다(거부 단정문 빼고).
11. hostctl에 토큰 인자가 없다(env 전용).

### 6.2 POSIX 실행 테스트 (`tests/test_launcher_sh.py`, `tests/test_update_runner_sh.py`, win32에서는 건너뜀)

**재료**
- 가짜 설치 루트: `python -m venv --without-pip .venv`에 현재 site-packages를 가리키는 `.pth`를 둬서 psutil을 쓸 수 있게 한다.
- 가짜 `session_manager/server.py`: `runtime.json`을 쓰고, `/api/health`와 `/api/owner/diagnostics`(version은 가짜 dist-info 파일에서 읽음)를 서빙하는 작은 서버.
- 가짜 `uv`: `sync` 때 dist-info를 pyproject 버전으로 맞춘다. env로 실패를 주입할 수 있다.
- 가짜 브로커: `-m session_manager.ptyd`로 잠만 잔다.
- 포트는 임의로 고르고, `HOME`과 데이터 폴더는 tmp다.

**런처**
- L① start를 두 번 해도 서버는 하나다.
- L② `kill -9` 후 start하면 새 pid가 뜨고, host.log가 이어 쓰기라 옛 줄이 남는다.
- L③ maintenance.flag가 있으면 띄우지 않고, `resume`하면 뜬다.
- L④ 살아 있는 잠금이면 보류하고, 시체 잠금(pid가 죽음, 또는 2분 넘음)이면 넘어간다.
- L⑤ stop은 SIGTERM이고 브로커는 살아남는다. TERM을 무시하는 가짜는 15초 뒤 KILL되고 `forced-stop` 표식이 남는다.
- L⑥ 살아 있는 pid의 update 표식이면 skip, `CLEWPATH_UPDATER=1`이면 기동, 죽은 pid의 표식이면 지우고 기동.
- L⑦ 좀비 pid(부모가 거두지 않음)는 죽은 것으로 판정한다.
- L⑧ 런처 env 정리, `CLEWPATH_CONFIG`가 설정된다.
- L⑨ watch: 한 개만 돌고, kill 뒤 간격 안에 되살리고, TERM이면 잠금을 풀고 끝나며 서버는 산다.
- L⑩ 교체 잔재가 있으면 `-RecoverOnly`를 호출하고, 러너 사본이 없으면 blocked.
- L⑪ `paths`·`status` 출력 형식.

**러너**
- R① 성공: 교체, ok=true, apply.lock·runner.lock.d·표식·SWAP이 없음, 백업이 있음.
- R② 헬스 버전 불일치: 롤백, `-failed-<ver>` 보관, ok=false, 옛 파일 복원, 새로 생긴 항목 제거.
- R③ uv가 두 번 다 실패하면 즉시 롤백.
- R④ WaitPid가 TERM을 무시하면 KILL. pid가 재사용된 가짜(cmdline이 다름)는 죽이지 않는다.
- R⑤ 롤백 때 ptyd 더미는 살고 server 더미는 죽는다.
- R⑥ 두 번째 러너는 종료코드 3.
- R⑦ 교체 중 실패 주입(`chmod 500 ROOT/pwa`로 디렉터리 rename 실패): 앞서 옮긴 항목이 되돌아간다.
- R⑧ 공백·한글 경로(`My Apps/클루패스 app`).
- R⑨ `last_apply.json`을 `updater.recently_failed`가 읽는다.
- R⑩ runtime.json 포트 폴백으로 헬스가 통과한다.
- R⑪ `.git`이 있으면 종료코드 2, 아무것도 바꾸지 않는다.
- R⑫ 빈·상대 경로 인자면 종료코드 2, rm이 없다.
- R⑬ 저널 잔재(`old`만 옮긴 상태, `new`까지 옮긴 상태, committed)를 각각 정확히 복구한다.
- R⑭ 교체 전 TERM이면 중단(ok=false, 옛 버전 재기동).
- R⑮ `bash --posix update_runner.sh`로 R①·R②를 반복한다([mac] sh 근사).

**osdep**: `port_free`의 TIME_WAIT 재현. 서버 쪽에서 먼저 닫으면 일반 bind는 실패하지만 `port_free`는 True여야 한다. 듣는 소켓이 있으면 False여야 한다.

**실행 위치**: 6.4절의 `clewpath-pytest-test` 컨테이너에서 전체 pytest와 함께 돈다. 2단계 기준선은 리눅스 612 통과다.

### 6.3 e2e 왕복 (`-m posix_e2e`, 가짜 CP, 시험 키)

**재료 만들기** (`build_fixtures.py`, 호스트에서 실행, 산출물은 저장소 밖 임시 폴더)

- 기본 zip:
  - pwsh가 있으면 진짜 `package-for-user.ps1 -OutDir <임시>`로 만든다. CR·BOM 게이트와 화이트리스트를 함께 시험할 수 있다.
  - 없으면 같은 목록의 파이썬 판으로 만들고, 그 목록이 ps1과 같은지 테스트로 묶는다.
- 변형 zip(zipfile로 항목만 바꿔 씀, 구분자는 `/`):

  | 이름 | 버전 | 바꾼 것 | 용도 |
  |---|---|---|---|
  | A | 90.0.0 | pyproject와 uv.lock 루트 패키지 version, RELEASE_KEYS를 시험 키로 | 첫 설치 |
  | B | 90.0.1 | A와 같고, README 첫 줄 표식, 런처 템플릿 주석 한 줄 변경 | 재렌더 확인 |
  | C | 90.0.2 | `server.main` 첫 줄에 `raise SystemExit("e2e: 깨진 빌드")` | 롤백 |
  | D | 90.0.3 | 정상 | 재기동 자동 적용 |
  | L | 91.0.0 | install.sh 없음, platforms 없음(0.28.1 모양) | 미지원 거부 |
  | P | 91.0.1 | `platforms=["win"]` | 거부 |
  | Z | 91.0.2 | `../evil`, 절대 경로, 심볼릭 링크 항목 | 거부 |
  | T | 91.0.3 | 매니페스트 sha256 또는 size 변조 | 거부 |
  | M | 없음 | 내용이 깨진 JWT | 거부 |

- 매니페스트 url은 `http://127.0.0.1:8099/cp/updates/download/<ver>`이고 TTL은 1일이다. platforms는 L·P를 빼면 `["win","mac","linux"]`다.
- 그 밖의 재료:
  - uv tar.gz(호스트 아키텍처). install.sh 해시표와 대조해 넣는다.
  - get.sh. 경로는 env `CLEWPATH_GET_SH`로 받고, 기본은 `007/control_plane/get.sh`.
  - 시드 세션 `~/.claude/projects/x/<uuid>.jsonl`.

**이미지** (`tests/posix_e2e/Dockerfile`, 개략)

```dockerfile
FROM python:3.11-slim                        # /bin/sh = dash (실측)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*              # procps(ps·pgrep)는 일부러 넣지 않는다
RUN useradd -m -u 1000 tester
COPY --chown=tester:tester ctx/ /opt/e2e/    # build_fixtures.py 산출물만
USER tester
RUN sh /opt/e2e/warm_cache.sh                # 빌드 때만 네트워크: A 트리로 uv sync --frozen --no-dev 1회(setuptools 포함) 뒤 venv 삭제
ENV UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never CLEWPATH_E2E_CONTAINER=1 \
    CLEWPATH_UV_ARCHIVE=/opt/e2e/fixtures/uv-x86_64-unknown-linux-gnu.tar.gz
CMD ["python3", "-I", "/opt/e2e/e2e_driver.py"]
```

**가짜 CP** (`fake_cp.py`, 시스템 python3의 표준 라이브러리, 127.0.0.1:8099, tester로 실행)

| 요청 | 응답 |
|---|---|
| `GET /cp/healthz` | `{"ok":true}` |
| `GET /cp/updates/manifest` | `state/current.jwt`(`application/jwt`), 없으면 404 `{"error":"no_release"}` |
| `GET /cp/updates/download/<ver>` | zip 또는 404 `{"error":"not_found"}` |
| `GET /cp/get.sh` | 원본 바이트 그대로 |
| `POST /cp/provision/anonymous` | 가짜 자격증명, 횟수 기록, env로 429 주입 가능 |
| `POST /cp/connector/token` | 503(Host fail-open) |
| 그 밖 | 404 |

모든 요청은 `state/requests.log`에 남긴다. 최신 버전 전환은 `current.jwt`를 바꿔치기해서 한다.

**공통 설치 명령** (드라이버가 `sh -c`로 실행)

```
curl -fsS http://127.0.0.1:8099/cp/get.sh | sh -s -- --cp-url http://127.0.0.1:8099/cp --allow-http \
  --relay-url ws://127.0.0.1:9/relay/ws --app-url http://127.0.0.1:9/app
```

**시나리오** (드라이버 `e2e_driver.py`는 표준 라이브러리만 쓰고, 단계별 성공·실패와 근거를 JSON으로 낸다)

**S0 정적**: 컨테이너 안에서 `dash -n`, `bash --posix -n`을 다섯 스크립트와 렌더 결과에 돌린다.

**S1 get.sh 거부** (설치 전, 매번 DIR이 생기지 않았고 `.clewpath-get.*`가 없는지 확인)
- current 없음: 404 문구, 종료코드 1.
- T(sha·size): 종료코드 1.
- `--allow-http` 없이 http CP: 거부.
- Z: 거부, 바깥에 파일이 없음.
- L: "지원하지 않습니다".
- P: 거부.
- M: 종료코드 1.

**S2 설치(A)**
- 종료코드 0.
- 레이아웃이 2.3절과 같다. `start-connector.sh`는 0755, `bin/uv`는 0755, `.clewpath-install`이 있다.
- `DATA_DIR` 0700, toml과 `cp_credentials.json`은 0600이고 BOM 없음, tomllib 통과, cp_url은 가짜 CP.
- `runtime.json`, `/api/health`는 ok, 진단 version은 90.0.0.
- `~/.claude/settings.json` 훅 명령이 `curl -s -m 3 …`이다(`curl.exe` 아님).
- Host의 `/proc/<pid>/status` Umask가 드라이버의 umask와 같다(S38).
- 요청 로그는 get.sh, manifest, download 90.0.0, provision 1회, 그리고 Host의 token·manifest 호출뿐이다.

**S3 재실행 멱등**: `sh $APP/install.sh …`를 다시 실행한다.
- toml·자격증명의 해시가 그대로다.
- `.venv/bin/python` inode가 그대로다(재사용).
- 런처는 내용이 같다.
- Host는 새 pid(stop 후 start)이고, `shutdown.json`에 옛 pid의 normal 표식이 있으며, incidents는 그대로다.

**S4 런처** (6.2의 L① ~ L⑪을 실제 Host로)
- ⑤ 브로커는 ptyclient와 같은 명령으로 띄운다.
- ⑥ `CLAUDECODE=1 CLAUDE_CODE_X=1 CLAUDE_PID=7`로 start 하면 `/proc/<pid>/environ`에 셋 다 없다.
- ⑦ 권한을 확인한다.
- ⑧ 5100을 더미 리스너로 막으면 5101로 뜨고, 런처는 runtime.json으로 찾고, host.log의 `[relay] … (local http://127.0.0.1:5101)`(4.4 수정)을 확인한다.
- ⑨ TIME_WAIT: keep-alive 요청 후 6초 기다렸다가 stop하고 곧바로 start하면 5100이어야 한다(4.1 수정 확인).
- ⑩ watch 10초.
- ⑪ 업데이트 표식.

**S5 수동 적용 성공(B)**
- current를 B로 바꾸고 `POST /api/owner/update/apply`(루프백)를 보내면 `applied:true`.
- 150초 안에 진단 version이 90.0.1.
- `last_apply.json`은 ok=true이고 BOM 없음.
- 잠금·표식·SWAP이 없다. `backup-90.0.0`이 있다. README 표식이 B다.
- `start-connector.sh`가 B 템플릿으로 다시 렌더됐고 `launcher-ensured-90.0.1`이 있다.
- incidents는 그대로이고, 브로커 pid가 같고 살아 있다.
- `apply-90.0.1.log`에 `=== 종료 (성공: true) ===`가 있다.

**S6 실패와 롤백(C)**
- current를 C로 바꾸고 apply 하면 150초 안에 version이 90.0.1로 돌아온다.
- `last_apply`는 `{version: 90.0.2, ok: false}`이고 `backup-90.0.1-failed-90.0.2`가 있다.
- README 표식이 B로 돌아왔고, venv python으로 `recently_failed("90.0.2")`가 True다.
- 실패 버전의 server 프로세스가 0개이고 브로커는 살아 있다.
- 이어서 재기동하면 20초 뒤 host.log에 "90.0.2 는 방금 실패해 롤백됨 - 재적용 건너뜀"이 남는다.

**S7 재기동 자동 적용(D)**
- current를 D로 바꾸고 `stop` 후 `start` 한다.
- 약 20초 뒤 자동 적용이 시작되고, 러너가 60초 기다린 뒤 TERM을 보낸다. 로그에 "시간 내에 안 죽어서 강제 종료합니다."가 남는다.
- version 90.0.3, ok=true, incidents는 그대로다(update·normal 표식).

**S8 제거**
- `sh $APP/uninstall.sh --yes`: 종료코드 0. server·ptyd·watch 프로세스가 0개(`/proc` 스캔). `$APP`이 없다. 데이터 폴더는 남아 있고 `maintenance.flag`는 없다.
- `~/.claude/projects` 해시가 그대로이고, `settings.json`은 제거 전후 해시가 같으며 출력에 남은 훅 안내가 있다.
- 다시 설치(S2 명령)한 뒤 `--purge --yes`를 실행하면 데이터 폴더와 `bin/`·`python/`까지 사라지고 projects는 그대로다.

**S9 sh 근사**: `bash --posix`로 런처 start·stop과 러너 R①을 한 번 더 돌린다.

**합격 기준**
- S0 ~ S9 전부 통과.
- 요청 로그에 예상 밖 경로가 없다.
- 실패하면 드라이버가 host.log·host.err.log·launcher.log·apply 로그의 꼬리를 표준출력으로 내보낸다(`--rm`이라 남지 않으므로).

### 6.4 실행 방법

```powershell
# 0) 재료(호스트, 저장소 밖 임시 폴더) — 네트워크: GitHub(uv) 만
python tests/posix_e2e/build_fixtures.py --out "$env:TEMP\clewpath-posix-e2e" --get-sh "..\007_clewpath_server\control_plane\get.sh"
# 1) e2e 이미지(빌드 때만 네트워크: apt·PyPI)
docker build -t clewpath-posix-e2e:local "$env:TEMP\clewpath-posix-e2e"
# 2) 실행 — 이름은 -test 로 끝나야 이 PC 의 배포 가드 규칙을 통과한다
docker run --rm --init --network none --name clewpath-posix-e2e-test clewpath-posix-e2e:local
# 3) 컨테이너 전체 pytest(6.2 포함): 컨텍스트 = git ls-files -co --exclude-standard 사본(ops/release-keys 는 .gitignore 라 빠짐, *.pem 발견 시 중단)
docker run --rm --init --network none --name clewpath-pytest-test -u 1000 clewpath-pytest:local python -m pytest -q
```

pytest 연결: `tests/test_posix_e2e.py`(`@pytest.mark.posix_e2e`, docker가 없으면 skip)가 위 0~2단계를 감싸고 드라이버 JSON을 단정한다. 기본 실행에서는 addopts로 빠진다.

**구현 메모 (8단계, 2026-10-21 — 6.3·6.4 와 다른 점과 뒤 단계가 맞출 약속)**
- 이름: 이미지도 `clewpath-posix-e2e-test`(명세의 `clewpath-posix-e2e:local` 대신) — 이 PC 의 규칙(일회용 컨테이너·이미지 이름은 `-test`·`-verify`)에 맞췄다. 실행은 `docker run --rm --init --network none --name clewpath-posix-e2e-test clewpath-posix-e2e-test`, 볼륨·운영 네트워크 없음. 이미지는 다음 실행을 위해 남긴다.
- 컨텍스트(`--out`): `Dockerfile` + `base/`(uv tarball·`warm_cache.sh`·데우기용 최소 트리 = A 의 pyproject·uv.lock·LICENSE·`session_manager/__init__.py`) + `ctx/`(매번 바뀌는 zip·매니페스트·get.sh·드라이버·가짜 CP·`index.json`). 데우기 층을 ctx 보다 앞에 두었다 — 시험 키가 실행마다 새로 생겨 zip 이 바뀌어도 PyPI 데우기를 다시 하지 않는다(명세는 ctx 를 COPY 한 뒤 데움). `--out`은 저장소 밖이어야 하고, 이 도구가 만든 표(`.clewpath-e2e-build`)가 없는 비어 있지 않은 폴더는 거부한다(남의 폴더를 지우지 않게). 키 가드는 이름(`*.pem`·`*.key`·키 폴더)과 내용(개인키 머리글)을 zip 안까지 본다.
- 기본 zip: pwsh 가 있으면 진짜 `package-for-user.ps1 -OutDir`(TEMP 를 임시 폴더로 — 사용자 `%TEMP%`에 스테이지를 만들지 않게). 파이썬 판은 ps1 의 `$include`·`$excludePatterns`를 그대로 읽어 같은 목록·같은 `-like '*패턴*'` 규칙을 쓰고, 시험이 pwsh 결과와 파일·폴더 항목 목록까지 대조한다.
- zipfile 은 항목 속성이 0 이면 쓸 때 `0o600 << 16`을 넣는다(`ZipFile._open_to_write`, 실측) — unzip 은 FAT 항목의 그 값을 유닉스 권한으로 받아 0600 으로 푼다. 변형 zip 이 배포 zip(Compress-Archive, 속성 0)과 같게 풀리도록 속성 0 을 그대로 쓰게 했다(시험이 다시 읽어 확인). `tests/test_get_sh.py`의 `_zip_bytes`도 같은 성질인데 거기 단정에는 영향이 없다.
- 변형: 표의 A·B·C·D·L·P·Z·T·M 에 더해 E(90.0.4 — S9 의 `bash --posix` 러너용, CP 로 내지 않고 드라이버가 `updater.stage`·`osdep.update_runner_argv`로 apply 와 같은 자리를 만든다), L-noinstall(linux 클레임은 있으나 install.sh 없음 — 받아서 푼 뒤 거부되는 길). Z 는 세 zip(`../`·절대 경로·심볼릭 링크), T 는 세 매니페스트(sha256·size 큼·size 작음 — curl 63, 같은 시험 키로 다시 서명). 모든 변형의 README 첫 줄 표식, B~E 는 런처 템플릿 3행 뒤 주석(`# e2e-build: X`) — C 는 뜨지 못하므로 롤백 뒤 런처가 B 그대로인지도 본다.
- 순서: S9 를 S8 앞에 돌린다(설치본이 있어야 한다). S5 에 S23 확인을 더했다 — maintenance.flag 가 있으면 수동 적용이 400 으로 거부되고 Host·러너가 그대로다. S8 은 watch 를 띄운 채 제거하고, watch·Host·브로커를 첫 길(watch.lock.d·런처 stop·ptyd.json)로 멈췄는지(남은 것 쓸기는 빈손인지)도 본다 — 둘째 방어선이 첫 길의 고장을 가리지 않게.
- S3 의 '옛 pid normal 표식': 새 Host 가 기동하며 소비한다(`check_previous_exit`). 그래서 '표식이 소비됐고 사고 기록이 없음'으로 본다. 표식 자체는 S4⑤에서 stop 직후 `shutdown.json`으로 확인한다.
- S4: ①에 '멈춘 뒤 동시에 start 셋 → Host 하나'를 더했다. ④는 시체 잠금 두 갈래(pid 죽음·pid 없이 2분 넘음). ⑤의 TERM 을 받지 못하는 Host 는 진짜 Host 를 SIGSTOP 해서 만든다(15초 뒤 KILL, forced-stop 표식 → 다음 기동이 사고로 보지 않음). 브로커는 Host 의 `/api/owner/ptyd/restart`(→ `ptyclient.ensure`)로 띄운다 — 명령줄이 `venv python -m session_manager.ptyd`인지 본다. ⑪은 산 pid·`CLEWPATH_UPDATER=1`·`by=runner`인데 러너가 아닌 pid(재사용)·죽은 pid 네 갈래.
- 실측: ⑧의 막는 소켓도 SO_REUSEADDR 가 필요했다 — 방금 내린 Host 의 연결이 5100 에 TIME_WAIT 로 남는다(urllib 은 `Connection: close`라 응답마다 서버 쪽이 먼저 닫는다). 곧 ⑨의 상황은 사람이 로컬 웹을 열기만 해도 생긴다. ⑨는 그 TIME_WAIT 에서 평범한 bind 가 `EADDRINUSE`인 것을 확인한 뒤 곧바로 start 해 5100 에 뜨는지 본다.
- 설치 루트에 `clewpath_host.egg-info`가 생긴다(uv sync 의 setuptools 편집 설치, 실측) — 2.3 에 더했다. PAYLOAD 밖이라 러너가 건드리지 않고 sync 가 버전마다 다시 쓴다.
- 결과: 드라이버는 표준출력 마지막 줄 `E2E-RESULT {json}`(단계별 성공·실패·근거·시간), 실패하면 launcher·host·apply·recover·가짜 CP 로그 꼬리를 앞에 찍는다. pytest 는 `CLEWPATH_E2E_OUT`(없으면 tmp)에 `e2e-run.log`·`e2e-result.json`·`docker-build.log`·`fixtures.log`를 남기고, 통과했고 작업 트리가 깨끗하면(release.ps1 과 같은 기준) `ops/logs/posix-e2e-<ver>.json` = `{version, head, ok}`를 쓴다(4.5 게이트). env `CLEWPATH_E2E_UV_ARCHIVE`(고정 해시와 같은 uv tarball — 없으면 GitHub 공식 릴리스에서 받아 `.sha256`·고정값과 대조), `CLEWPATH_GET_SH`.
- 시험: `tests/test_posix_e2e.py` — 하네스 시험(모든 OS: ps1 과 같은 패키지 목록·zip 모양·pwsh 결과 대조, 변형 내용, 시험 키 서명과 운영 키 거부, 컨텍스트·키 가드, uv 고정 해시, 가짜 CP 라우트·요청 로그, 드라이버의 컨테이너 밖 거부, 릴리스 기록 조건)과 컨테이너 왕복 1건(`-m posix_e2e`, 186 단정·약 4분). `test_posix_scripts`의 PENDING 이 비었고, `tests/posix_e2e`의 셸(`warm_cache.sh`)도 POSIX 규칙으로 검사한다. 배포 zip 사본(또는 get.sh 사본)에 버그를 하나씩 넣어(12종 — port_free 평범한 bind·커넥터가 설정 포트로 프록시·훅 curl.exe·런처 env 정리 없음·런처 umask 077·강제 종료 표식 없음·러너 uv sync 건너뜀·롤백이 브로커까지·apply 의 유지보수 표식 무시·제거가 브로커를 첫 길로 멈추지 않음/두 길 다·get.sh 의 `..` 허용) 컨테이너 왕복을 돌려 모두 겨냥한 단정에서 잡히는 것을 확인했다 — '첫 길로 멈추지 않음'은 처음에 둘째 방어선(sweep)이 가려 놓쳤고, 그래서 S8 의 '제 길로 멈춤' 단정을 더했다.

## 7. CP 쪽 변경 (서비스 모니터 배포 의뢰 대상)

**이번 단계에서는 CP를 바꾸지 않아도 된다.** 시험장은 가짜 CP를 쓰고, 릴리스는 `platforms=["win"]`이라 운영 get.sh가 있어도 POSIX 설치는 거부된다. 공개는 맥 단계와 함께 하는 것을 권장한다(Q12).

준비할 변경:

1. **`007/control_plane/get.sh`** (새 파일, 3.1절)
   - 라우트가 없으면 서빙되지 않으므로 이번 단계에서 007 작업 트리에 미리 두어도 운영 영향이 없다.
   - 같은 폴더의 get.ps1은 요청마다 읽혀 즉시 반영되므로 함께 고치지 않는다.
2. **`007/control_plane/api.py`**
   - `@app.get("/get.sh")`를 추가한다. utf-8-sig로 읽고(BOM 제거) `\r`을 제거한 뒤 `text/plain; charset=utf-8`로 응답한다. 읽기 실패는 503 `bootstrap_unavailable`(get_bootstrap :510-526과 같은 모양).
   - `/cp/get.sh`는 nginx를 바꾸지 않아도 catch-all(`default.conf:310-319`)이 rewrite로 이 라우트에 보낸다. 이 경로는 CP 재시작만으로 열린다.
3. **`007/.gitattributes`**(새 파일): `*.sh text eol=lf`. 007은 `core.autocrlf=true`이고 `.gitattributes`가 없다(실측). 체크아웃할 때 CRLF가 되면 bind-mount로 그대로 서빙된다. 엔드포인트의 `\r` 제거와 함께 이중으로 막는다.
4. **007 테스트**
   - `test_get_sh_served`: `/get.sh`·`/cp/get.sh`가 200, charset=utf-8, BOM·CR 없음, `#!/bin/sh`로 시작, `install.sh` 언급.
   - `test_get_sh_parses`: dash·`bash --posix`가 있으면 `-n`.
   - `test_get_sh_decode`: 마지막 줄 `main "$@"`를 뺀 사본을 source 하고, 한글 notes가 든 pyjwt 토큰으로 디코드·추출 함수를 시험한다. 형식 위반과 http url 거부도 본다. 제품 코드에 시험용 훅을 넣지 않는다.
   - 공백 메우기: `test_updates_roundtrip`. 버린 키로 서명해 publish(sha·size가 틀리면 400) → manifest는 최신만, withdrawn 제외 → download 바이트 일치·404. 지금 0건이다.
5. **nginx** (`D:\docker\infra\nginx\default.conf`, 서비스 모니터)
   - 기존 `location ~ ^/(get|install\.ps1)$`(:251-258)는 고정 URI `/get`으로 넘기므로 그대로 둔다. 별도 블록을 더한다.
     ```nginx
     # 원라인 설치(맥·리눅스):  curl -fsSL https://clewpath.pyongso.com/get.sh | sh
     location = /get.sh {
         set $cp_getsh session-cp;
         proxy_pass http://$cp_getsh:5200/get.sh;
         proxy_set_header Host $host;
         proxy_set_header X-Real-IP $remote_addr;
         proxy_set_header X-Forwarded-Proto $scheme;
         proxy_read_timeout 15;
     }
     ```
   - `nginx -t` 후 reload.
6. **랜딩·README**(맥 단계): OS별 한 줄 설치. `landing.html`에 맥·리눅스 줄을 넣는데, 리눅스는 "시험용"으로 표시하거나 넣지 않는다.

**의뢰서 요지** (`/deploy-request` 스킬 형식으로 보낸다)
- 변경: 007 api.py `/get.sh` 라우트, get.sh, .gitattributes, nginx `location = /get.sh`.
- 작업:
  - `docker restart util-session-cp`. 소스가 bind-mount이므로 restart만으로 반영된다. recreate·rebuild는 금지다(DB 유실).
  - `nginx -t` 후 reload.
- 확인:
  - `curl -fsS https://clewpath.pyongso.com/get.sh | head -n 1`이 `#!/bin/sh`
  - `curl -sI …/get.sh`가 `text/plain; charset=utf-8`
  - `curl -fsS …/get.sh | od -c | head -n 2`에 `\r`이나 BOM(`357 273 277`)이 없음
  - `/cp/get.sh`도 같은 결과
  - 기존 `/get`·`/install.ps1` 회귀
- 되돌리기: location 블록을 지우고 reload. 라우트는 남아도 무해하다.

## 8. 위험·열린 질문

### 8.1 위험

| # | 위험 | 대응 |
|---|---|---|
| R1 | 공통 스크립트가 맥(bash 3.2의 sh, BSD 도구, APFS 대소문자 무시, 공백 경로)에서 처음 돈다. 이번엔 정적 검사와 `bash --posix` 근사뿐이다 | 다음 단계 맥 실기에서 6.3 시나리오를 맥에서 반복. 그 전에는 `platforms`에 mac을 넣지 않는다 |
| R2 | TIME_WAIT 포트 밀림. 리눅스는 확실하고(uvicorn keep-alive 5초), 맥은 추정이다 | 4.1 `port_free`, 6.3 S4⑨로 확인 |
| R3 | 런처가 venv 파이썬 판정에 기댄다. venv가 깨지면 판정을 못 한다(종료코드 2, `--reinstall` 안내) | uv 관리 런타임을 ClewPath 아래에 두어 위험을 줄인다 |
| R4 | uv 0.12.1 POSIX 해시가 저장소에 없다 | 공식 릴리스 `.sha256`과 직접 계산한 값이 같을 때만 기록한다. 추측 금지 |
| R5 | `uv python install`이 `~/.local/bin`에 실행 파일을 둘 수 있다(0.8 이후 동작) | `--no-bin` 또는 `UV_PYTHON_BIN_DIR`. 0.12.1에서 확인 |
| R6 | CP가 최신 한 건만 준다. platforms로 어떤 OS를 빼면 그 OS는 그동안 설치·업데이트를 못 받는다 | 릴리스 규칙(모든 릴리스에 지원 OS를 유지), 장기적으로 CP `?platform=`(007 변경) |
| R7 | 첫 설치의 신뢰는 TLS뿐이다(서명 미검증) | Windows와 같은 모델. 머리말에 명시 |
| R8 | [linux] 시스템 파이썬 업그레이드가 venv를 깨뜨릴 수 있다 | 리눅스는 시험용. 정식 지원 때 관리 런타임 고정을 검토 |
| R9 | 007 bind-mount라 get.ps1 수정이 즉시 운영에 반영된다 | 이번 단계 get.ps1 무접촉. 007 수정은 새 파일만 |
| R10 | 러너와 재기동 Host가 옛 Host의 env(apply_env가 채운 `SM_*`)를 물려받는다. 업데이트 직후에는 toml 변경이 가려진다. Windows와 같은 기존 성질이다 | Q15에서 별건으로 |
| R11 | curl이 없는 리눅스(slim)에서는 훅이 실패한다(비차단) | 시험 이미지에 curl, 안내 |
| R12 | 브로커의 늦은 import가 교체 순간 새 코드를 볼 수 있다 | rename으로 교체 시간을 줄임(Windows는 더 김) |
| R13 | 작업 폴더(zip, stage, backup)가 계속 쌓인다 | Q16에서 별건 |
| R14 | 이번 릴리스는 Windows 사용자에게도 간다. OS 무관 수정(local_base, apply 거부, 프록시 무시, port_free 이동)이 Windows 동작을 바꾼다 | Windows 전체 회귀, 0.28.1에서 업데이트하는 왕복을 이 PC에서 확인(release.ps1 `-Apply`) |
| R15 | (Windows 기존 결함, 별건)<br>① PS5.1 러너의 `last_apply.json` BOM 때문에 롤백 반복 차단이 꺼질 수 있다(추정)<br>② Copy-Payload가 오류를 삼키고, 백업 실패 exit가 잠금·결과를 남기지 않는다<br>③ Start-Process 리다이렉트가 host.log를 덮어써 사고 꼬리가 사라진다(실측)<br>④ get.ps1:88-90이 브로커까지 강제 종료한다<br>⑤ install.ps1:425-429가 `CLAUDE_CONFIG_DIR`을 빠뜨린다<br>⑥ `launcher-ensured-*`가 버전마다 쌓인다(이 PC 90여 개)<br>⑦ 템플릿·ensure_task에 BOM이 없고 PS5.1 검사 대상도 아니다 | 이번 범위 밖. TODOS 별건으로 등록을 권장 |

### 8.2 열린 질문 (사장님 결정 필요, 괄호 안은 권장)

| # | 질문 | 권장 |
|---|---|---|
| Q1 | 리눅스 설치 위치 | `${XDG_DATA_HOME:-~/.local/share}/clewpath` |
| Q2 | 파이썬 선택 | [mac] 관리 런타임만 / [linux] 시스템 우선 |
| Q3 | uv 관리 런타임 위치 | `$CLEWPATH_HOME/python`(사용자의 `uv python uninstall`과 분리) |
| Q4 | platforms 기본값과 클레임이 없을 때 | 이번 릴리스는 `["win"]`, 없으면 win 전용 |
| Q5 | `CLAUDE_CODE_*` 전부를 지울지 | 브로커(`ptyd.clean_env`)와 같은 규칙으로 통일. 다만 `CLAUDE_CODE_USE_BEDROCK` 같은 사용자 설정 변수까지 빠지는 문제는 브로커에도 이미 있으므로 범위 축소를 별도로 검토 |
| Q6 | 제거할 때 settings.json 훅을 지울지 | 안내만. `--remove-hooks`는 승인 후 별건 (불가침 원칙 1) |
| Q7 | 훅 자동 등록이 CLAUDE.md 승인 예외 목록에 없다. POSIX로 넓어지는 것을 승인하는지 | 확인 필요(기존 Windows 동작과 같음) |
| Q8 | 의존성 동기화가 두 번 다 실패하면 | 즉시 롤백(Windows는 헬스에 맡김) |
| Q9 | 교체 방식 | POSIX만 rename과 저널. Windows를 맞출지는 별건 |
| Q10 | 스테이지에 없는 기존 항목 | 남김(Windows와 같음) |
| Q11 | watch를 사용자 안내에 넣을지 | 임시용으로 안내하되 다음 단계 자동 시작으로 대체 |
| Q12 | CP `/get.sh` 공개 시점 | 맥 단계와 함께. 리눅스 시험용 `/cp/get.sh`를 먼저 열 필요는 없다 |
| Q13 | 서빙 경로 | `/get.sh`(nginx 별도 location)와 `/cp/get.sh` 둘 다 |
| Q14 | OS 무관 수정을 이번 릴리스에 넣을지 | 넣는다(작고 명백한 결함, 회귀 테스트 동반) |
| Q15 | 러너에 넘기는 env에서 Host가 채운 `SM_*`를 뺄지 | 별건(양 OS) |
| Q16 | 작업 폴더 정리(최근 2개 보관) | 별건(파이썬 공통) |
| Q17 | POSIX e2e를 릴리스 게이트로 둘지 | platforms에 mac·linux를 넣는 릴리스부터 필수 |
| Q18 | install.sh 대화형 입력 | 받지 않음. uninstall 확인만 `/dev/tty` |
| Q19 | `--client-token` argv | 받되 경고 후 env로 옮김. 안내문은 env만 |
| Q20 | install.ps1 데이터 폴더 규칙(`CLAUDE_CONFIG_DIR` 누락) 수정 | 별건(Windows) |
| Q21 | 런처의 강제 stop 표식(`forced-stop`)을 사고로 볼지 | 정상으로 본다(사람이 명령함) |
## 9. 2회차 검토 반영 (2026-10-21)

다섯 스크립트와 hostctl, 1회차 수리 커밋을 검토자 6명이 다시 봤다. 스크립트별 5명이 보고, 1명은 스크립트 사이 연동과 맥(bash 3.2·BSD) 호환을 봤다.
지적은 36건, 겹친 것을 빼면 약 30건이다. 모두 고치기 전에 실패하는 회귀 시험을 먼저 썼다(`tests/test_uninstall_sh.py`·`tests/test_update_watch.py` 는 새 파일).
아래는 앞 절들과 달라진 약속이다. 앞 절의 본문과 다르면 이 절이 우선한다.

### 9.1 다섯 스크립트 공통 약속
- **물리 경로:** `phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }`(다섯 스크립트가 같은 본문). 맥 APFS 는 대소문자를 무시하고, bash 내장 `pwd -P` 는 친 글자를 그대로 둔다. 그래서 대소문자만 다르게 부르면 명령줄 판정이 빗나갔다. 외부 `/bin/pwd` 는 getcwd 로 디스크의 대소문자를 준다(맥 실기 확인 거리).
- **홈 보호:** 설치 루트가 `/`·홈(글자와 물리 경로 모두)·홈의 조상·클로드 폴더나 데이터 폴더와 같거나 그 조상이면 거부한다(get·install·uninstall).
- **우리 설치 항목(최상위 이름):** PAYLOAD 9개, `.venv clewpath_host.egg-info start-connector.sh .clewpath-install`, 일시 이름 `.clewpath-swap.* .clewpath-old.* .clewpath-trash.* .clewpath-install.*`, `.DS_Store`. uninstall 은 이 이름만 지우고 `rmdir` 한다. install 은 기록 없는 첫 설치에서 이 밖의 항목이 있으면 거부한다.
- **맥 비ASCII 경로:** 맥 ps 는 비ASCII 바이트를 이스케이프해 Host 를 못 알아본다. 그래서 get.sh·install.sh 가 맥에서 출력 가능한 ASCII 밖의 설치 경로를 거부한다.
- **bash 3.2:** `$( )` 안의 case 패턴은 `(` 로 연다. `$( )` 안에는 주석을 두지 않는다. 정적 시험 `test_case_patterns_inside_command_substitution_open_with_paren` 이 지킨다.
- **잠금:** 잠금 파일 둘째 줄에 `boot=<id>`(리눅스 boot_id, 맥 `kern.bootsessionuuid` → `kern.boottime`)를 적는다. 내 pid(`$$`)나 다른 부팅의 잠금은 시체로 본다. `watch.lock.d/root` 에는 그 watch 의 설치 루트를 적는다.
- **대기 마감:** 반초 수로 센다(`nap`). `date +%s` 는 기록 시각에만 쓴다(시계가 뒤로 보정되면 수 시간을 기다렸다).

### 9.2 get.sh
- zip 도구는 Info-ZIP unzip 이어야 한다. `unzip -Z` 출력에 ZipInfo 가 있는지로 확인한다. 없거나 busybox 이면 [linux] 만 python3 대역으로 가고, 둘 다 없으면 받기 전에 설치를 안내한다.
- 설치 위치 거부(`dir_guard`)는 받기 전에 글자로 한 번, 물리 경로로 바꾼 뒤 stage 를 만들기 전에 한 번 더 본다.
- install.sh 가 서막 전에 거절하는 두 가지도 받기 전에 같은 규칙으로 거절한다: 클로드 폴더 env 의 모양, `CLEWPATH_AGENT_TOKEN` 의 제어문자.

### 9.3 install.sh·hostctl
- `GET_TMP` 를 인자 해석 직후에 정해, 서막의 어떤 거절도 get.sh 의 stage 를 치운다.
- 받은 파일을 옮기는 mv 는 `( trap '' INT TERM HUP; exec mv … )` 로 돈다. 터미널 신호는 프로세스 그룹 전체로 가서 mv 자식을 죽였다. 6.1 의 `trap ''` 금지 규칙의 유일한 예외다.
- `.venv` 는 설치 기록이나 `.venv/.clewpath-venv` 표지가 있을 때만 재사용한다(남의 venv 에 정확 동기화를 걸지 않게).
- `CLEWPATH_HOME` 은 절대 경로이고 `/`·홈이 아니어야 한다. bin/uv 는 기본 위치이거나 기록의 `uv=` 일 때만 쓰고 바꾼다. `--system-uv` 는 고정 버전과 같은 부 버전 이상만 받는다.
- musl 판정은 glibc(getconf·ldd)를 먼저 본다. 잠금 밖 폴백 전에 sync.log 꼬리를 보여 주고 데이터 폴더에 `install-sync-<시각>.log` 로 남긴다(S11).
- 기존 런처의 conf_file 을 모든 hostctl 호출에 `CLEWPATH_CONFIG` 로 넘긴다. 재설치에서 데이터 폴더가 달라지면 갈아타지 않고 멈춘다(갈아타기는 첫 설치에만). 비교는 정규화·물리 경로로 한다.
- provision 은 Host 와 같은 규칙(env > 설정 파일)으로 CP 를 정한다. 설정 파일을 유지할 때 준 URL·토큰이 파일과 다르면 경고한다(값은 찍지 않음). wait-ready 는 진단의 claude_home 을 대조해 다른 사용자의 Host 를 내 것으로 받지 않는다.
- 제거가 끊겨 남은 `maintenance.flag`(by=uninstall)는 재설치가 지우고 기동한다. 다른 by 는 그대로 둔다.

### 9.4 start-connector(런처)·liveness
- probe 상태는 `ok|busy|foreign|dead|none` 이다. pid 는 runtime.json, 없거나 죽었으면 `launcher.spawn`(띄운 pid, `{"pid","at"}`)에서 읽는다. foreground 도 exec 전에 제 pid 를 적는다. 그래서 runtime.json 을 쓰기 전(느린 첫 기동 15초 넘김)에도 두 번째 Host 를 띄우지 않고, stop 도 그 pid 를 멈춘다.
- `foreign`(health 는 답하는데 이 설치본 Host 가 아님)이면 start·foreground 는 이유를 남기고 종료코드 1 로 끝나고, status 는 stopped 다.
- S21 순서: 잠금 → 다시 probe → maintenance·업데이트 표식·교체 잔재 다시 보기 → 데이터 폴더 일치 → 회전 → 기동. stop 은 표식을 쓴 뒤 `launcher.lock` 이 풀릴 때까지(최대 25초) 기다렸다가 판정한다.
- 데이터 폴더: 기동 직전에 Host 와 같은 규칙(config.py·appconfig.apply_env)으로 다시 계산해 DATA_DIR 과 다르면 거부한다(같은 폴더를 링크로 가리키는 것은 통과). env 는 빼지 않는다 — claude 자식이 `CLAUDE_CONFIG_DIR` 로 제 설정을 찾는다.
- `liveness.log_tail` 은 구분줄 앞이 비었으면(기동 직전 5MB 회전) `host.log.1` 꼬리를 읽는다. 구분줄이 없는 Windows 로그는 예전과 같다.

### 9.5 update_runner.sh·updater
- 헬스 통과 조건: runtime.json 의 pid 가 WaitPid 도, 재기동 직전 runtime.json 의 pid 도 아니고 살아 있다 · 응답은 runtime.json 의 port 에서 받는다 · claude_home 이 데이터 폴더의 부모와 같다 · 셸이 그 pid 의 명령줄을 이 설치본 Host 로 확인한다. `-Port` 는 로그에만 쓴다. 롤백 헬스도 같다(S33).
- 끝난 저널은 `.clewpath-trash.<pid>` 로 rename 한 뒤 지운다. 못 지우면 경고만 남긴다. 매 실행 남은 휴지통을 다시 지워 본다. 커밋됨·빈 저널을 못 치워도 복구는 0 이다(못 지운 저널이 런처를 영구히 막던 것).
- 재기동 직전이나 헬스가 '응답 없음'으로 끝날 때 `maintenance.flag` 가 있으면 롤백하지 않고 커밋한다. last_apply 에 `"ok":true,"held":true`, 종료코드 0 이다. 사용자가 멈춘 것을 실패로 보지 않는다.
- uv 하위 셸에서 `VIRTUAL_ENV`·`CONDA_PREFIX`·`UV_PYTHON` 을 뺀다. 상대 경로 `CLEWPATH_HOME` 은 uv 후보에서 뺀다. pip 폴백으로 커밋하면 [WARN] 과 last_apply `"deps_fallback":"pip"` 를 남긴다.
- `updater.apply` 는 맥·리눅스에서 살아 있는 `update.inprogress`(러너의 marker_live 와 같은 규칙)를 보면 거부한다(S24). Windows 는 이 표식을 쓰지 않아 동작이 같다.
- (Windows 포함) `server._update_watch` 의 auto_apply 도 `recently_failed` 를 본다. 재기동 확인만 보고 있어서, 롤백 직후 주기 루프 첫 바퀴가 같은 버전을 다시 적용해 2~3분마다 끝없이 돌았다.

### 9.6 uninstall.sh
- 판정은 `.clewpath-install` 첫 줄(`# clewpath-install v1`)만으로도 받는다. 판정·재실행에 필요한 넷(`session_manager`·`pyproject.toml`·`uninstall.sh`·`.clewpath-install`)은 맨 마지막에 지운다.
- 표식을 만든 직후 업데이트·설치 진행 중을 다시 본다. 지우기 전에 끊기면 표식을 거둔다(전에 있던 표식은 원래 내용으로 되돌림). 지우는 중에 끊기면 표식을 남기고 '다시 실행하면 마저 지운다'고 안내한다.
- watch 는 `watch.lock.d/root` 가 이 설치 루트일 때만 멈춘다. 옛 런처(root 파일 없음)는 명령줄에 런처 전체 경로가 있을 때만 멈춘다.
- 마지막 확인은 멈추기 전에 적어 둔 Host·브로커 pid 도 본다. `--purge-uv` 는 uv 가 늘 두는 `.gitignore` 를 허용하고, 남긴 python 폴더를 남은 것으로 센다. bin/uv 는 설치 기록의 `uv=` 가 그 자리일 때만 지운다(`--system-uv`·남이 둔 uv 는 남긴다).

### 9.7 이번에 고치지 않은 것 (TODOS 「멀티 OS」)
셸 쪽 update.inprogress 원자적 잡기, `CLEWPATH_CONFIG` 의 브로커·claude 자식 유출(ptyd 해시), foreign 판정을 ClewPath 일 때로 좁히기, 런처 stop 대기의 벽시계 마감, held 커밋 뒤 첫 기동 실패 안내.
