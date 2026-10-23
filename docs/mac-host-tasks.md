# 맥 측 작업 지시 — ClewPath Host 맥 실기(3b) (1회차 2026-10-21, 2회차 2026-10-22, 3회차~)

> 이 문서는 Windows 측(주 개발)이 맥 측 Claude 에게 전달하는 작업 지시서다. 앱 빌드 지시서 `docs/mac-build-tasks.md` 와 같은 방식이다.
> **3회차부터는 맥 측이 직접 고치고 시험한다.** 규칙은 `docs/mac-dev-loop.md` — 브랜치 `mac/<주제>`, main 에 올리지 않음,
> 올리기 전 맥 전체 시험 실패 0, 공통 코드를 바꿨으면 보고서에 적는다. (1·2회차는 「고치지 말고 보고만」이었다.)
> **지금 할 일은 바로 아래 「4회차」뿐이다.** 3회차·2회차 R0~R6·1회차 M0~M6 은 참고·다시 할 때의 절차다.
> 아래 「지킬 것」은 그대로다.

## 4회차(3b-4) — 지금 할 일 (2026-10-23~)

`docs/mac-dev-loop.md` 를 먼저 읽는다. 3회차 결과(`docs/mac-results/3b3-*.md`, 통과 6·실패 0)를 받아 Windows 측이 한 것:

| 3회차 | Windows 측이 한 것 |
|---|---|
| 이슈 B — `clewpath.toml` 사라짐 | 원인은 **시험**: 런처가 넣는 `CLEWPATH_CONFIG`(진짜 설정 파일)가 ClewPath 웹 터미널로 새어, 그 안에서 돈 `test_service_s2` 가 진짜 파일을 쓰고 지웠다. `tests/conftest.py` 가 시험마다 `CLEWPATH_CONFIG` 를 지우게 했다(a17e500). 제품 쪽 누출은 사장님 결정으로 그대로 둔다 |
| (Windows 노트북 사고) 런처 보장이 실패해도 표식을 써서 다시 안 함 | 맥·리눅스도 같은 결함이 있었다 — `osdep/_posix.ensure_launcher` 가 실패를 문자열로 돌려줬다. 이제 예외로 알리고 표식을 쓰지 않는다(01b3987, 리눅스 시험 통과) |
| 새 기능 | 0.32.0 PC 사이 원격 묻기(pclink) — **맥 경로는 맥 정식 출시 뒤**(아래 T4 는 조사만) |

### T1 확인 (터미널 앱에서)

1. `git checkout main && git pull && git log --oneline -1`
2. 맥 전체 시험(`docs/mac-dev-loop.md` 4-3) — 실패 0 이어야 한다. pclink 시험 중 `test_owner_pid_of_real_socket` 이 맥에서 skip 이면 정상이다(아래 T4 가 그 이유를 조사한다).

### T2 ClewPath 웹 터미널 안에서 전체 시험 한 번 (이슈 B 재발 방지 확인)

3회차 원인을 그대로 재현하는 자리다 — **이번엔 지워지지 않아야 한다.**
1. ClewPath 웹 터미널(폰이든 PC 브라우저든)에서 이 저장소 폴더로 가서 `echo "$CLEWPATH_CONFIG"` 를 기록한다(진짜 설정 파일 경로가 보이면 누출이 그대로라는 뜻 — 정상, 결정대로).
2. 데이터 폴더 목록을 남기고(`ls -la ~/.claude/session_manager > /tmp/sm-before.txt`, 내용은 열지 않는다) 그 웹 터미널에서 전체 시험을 돌린 뒤 다시 `ls -la … > /tmp/sm-after.txt; diff …`.
3. 기대: 실패 0, 「시험이 진짜 데이터 폴더의 파일을 지웠습니다」 없음, `clewpath.toml` 그대로(크기·시각 같음).
   ⚠ 이 단계는 시험만 돈다. **제거·재설치는 웹 터미널에서 하지 않는다**(자기 자신이 끝난다, 규칙 5).

### T3 런처 보장 표식 (읽기만)

1. 지금 설치본에서 `ls -la ~/.claude/session_manager/launcher-ensured-* 2>/dev/null` 과
   `grep -n "\[liveness\] 런처 보장" ~/.claude/session_manager/host.log | tail -5` 를 기록한다.
2. 표식이 있으면 그 내용(한 줄, 비밀 없음)이 `launcher: ok (posix v2)` 또는 `launcher: regenerated (posix v2)` 인지 본다.
   `regenerate failed` 가 적힌 표식이 있으면 옛 결함으로 남은 것이다 — 지우지 말고 보고한다(새 버전은 표식 이름이 달라 다시 돈다).

### T4 (조사만, 고치지 않음) pclink 맥 경로 — 보낸 세션 확인이 맥에서 되는가

pclink 는 이 PC 의 Claude 세션이 로컬 API 를 부를 때 **연결한 프로세스**로 보낸 세션을 확인한다(`session_manager/pclink/sender.py`).
리눅스·Windows 는 `psutil.net_connections` 로 되지만 맥은 관리자 권한이 필요하다고 알려져 있다. 맥 정식 출시 뒤 구현할 때 쓸 사실만 모은다:
1. `python3 -c "import psutil; print(len(psutil.net_connections(kind='tcp')))"` — 결과 또는 오류 이름(AccessDenied 등).
2. 내 프로세스의 연결만 보는 길: `python3 -c "import psutil,os; print(psutil.Process(os.getpid()).connections(kind='tcp'))"` 가 되는지.
3. `lsof -nP -iTCP -sTCP:ESTABLISHED -a -u "$USER" | head -5` 가 관리자 암호 없이 되는지(창이 뜨면 바로 취소하고 그렇다고 적는다).
4. `sudo` 는 쓰지 않는다.

### T5 보고

브랜치 `mac/3b4` 에 `docs/mac-results/3b4-YYYYMMDD.md`(규칙 7). T1~T3 에서 고친 게 있으면 같은 브랜치에 커밋(공통 코드 변경 여부 표시).
사장님께 한 줄: "맥 4회차를 mac/3b4 브랜치에 올렸습니다 — 시험 통과 N·실패 M, 웹 터미널 안 시험 이상 없음/있음, pclink 맥 조사: <한 줄>"

## 3회차(3b-3) — 참고(끝남, 결과 `docs/mac-results/3b3-*.md`)

`docs/mac-dev-loop.md` 를 먼저 읽는다. 2회차 결과(`docs/mac-results/3b2-20261022.md`)로 Windows 측이 고친 것은 main 에 있다.

| 2회차 | Windows 측이 고친 것 |
|---|---|
| 이슈 A — 옛 화면 `/legacy` 가 API 를 `/legacy/api/…` 로 불러 404, 재개 창이 틀림 | 주소 계산이 끝의 `/legacy` 를 뗀다(`terminal.html` 과 같은 방식). 응답이 실패면 재개 창을 열지 않고 알린다. node 시험 `tests/pwa/legacy-ui.test.mjs` |
| 관찰 2 — `test_mac_start_leaves_launchd_alone[linux]` | 그 경우의 가짜 uname 이 Linux 를 낸다 |
| 이슈 B — 데이터 폴더의 `clewpath.toml` 이 사라짐 | **원인 못 찾음 → T2.** 리눅스 컨테이너의 진짜 홈에 표식 파일을 두고 전체 시험(1,446건)을 돌렸는데 하나도 지워지지 않았다. 제품 코드에도 그 파일만 지우는 곳은 없다(제거는 `--purge-data` 때 폴더째뿐). 대신 시험마다 진짜 데이터 폴더의 `clewpath.toml`·`cp_credentials.json`·`devices.json` 이 그대로인지 보는 가드를 `tests/conftest.py` 에 넣었다 |
| 관찰 1·7 — 웹 터미널 안의 Claude 가 제거 때 끝남, `sfltool` 암호 창 | 규칙에 적었다(터미널 앱에서 돌림, `sfltool dumpbtm` 쓰지 않음) |

### T1 확인

1. `git checkout main && git pull && git log --oneline -1`
2. 맥 전체 시험(`docs/mac-dev-loop.md` 4-3) — 실패 0 이어야 한다. 시험 전후로 데이터 폴더 목록을 남긴다(T2 에 쓴다):
   ```bash
   ls -la ~/.claude/session_manager > /tmp/sm-before.txt      # 이름·크기·시각만(내용은 열지 않는다)
   # … 전체 시험 …
   ls -la ~/.claude/session_manager > /tmp/sm-after.txt; diff /tmp/sm-before.txt /tmp/sm-after.txt
   ```
3. 새 코드로 다시 설치한다(2회차 R3 의 zip → 제거 → 설치). 그다음 사장님과 `http://127.0.0.1:5100/legacy` → 세션 하나 → 재개:
   창의 명령이 `cd … && claude --resume …`(경로에 공백이 있으면 `cd '…'`)이고 '새 cmd 창에서 실행' 단추가 없어야 한다.
   `tail -100 ~/.claude/session_manager/host.log | grep -c '/legacy/api/'` 가 0 이어야 한다.

### T2 이슈 B 원인 — 맥에서 끝까지

2회차 기록: 10:40 재설치는 "기존 설정 파일 유지", 12:44 설치는 "설정 파일 생성". 그 사이에 사장님의 세션 정리(휴지통 11:48),
R2 자동 시험(12:20–12:32), 끊긴 1차 제거(12:33)가 있었다. 모두 **읽기만** 해서 좁힌다:

1. T1-2 전체 시험에서 「시험이 진짜 데이터 폴더의 파일을 지웠습니다」가 나오면 그 시험이 범인이다 → 고친다(규칙 3·4).
   가드가 안 울리고 `diff` 에도 변화가 없으면 시험은 아니다.
2. 기록에서 그 파일이 언제 없어졌는지 좁힌다: Host 는 뜰 때 설정 파일 값을 env 로 펼친 것이 있으면 `[config] <경로> 적용: …` 을 찍는다
   (`server.main`). 먼저 지금 기동에서 이 줄이 찍히는지 본다 — 찍히면 `launcher.log`(시각 있음)의 기동 줄과 `host.log`·`launchd.log` 의
   `[config]` 줄을 맞춰 10:40 이후 어느 기동부터 그 줄이 없는지 본다. 지금도 안 찍히면 이 방법은 쓸 수 없다고 적고 넘어간다.
3. 1·2회차 맥 측 Claude 자신의 세션 기록(`~/.claude/projects/-Volumes-Works-Projects-clewpath/*.jsonl`)에서 `clewpath.toml`·`session_manager` 를
   건드린 명령(`rm`·`mv`·`unzip`·`>` 등)을 찾는다. 읽기만 한다.
4. ClewPath 화면에서 세션을 지우는 재현이 필요하면 지울 세션을 사장님께 정해 달라고 한다. 제거를 중간에 끊는 재현은 사장님께 먼저 묻는다.
5. 재현되지 않으면 해 본 것과 결과만 적고 멈춘다. 짐작으로 고치지 않는다(가드가 다음에 잡는다).

### T3 보고

브랜치 `mac/3b3` 에 `docs/mac-results/3b3-YYYYMMDD.md`(규칙 7). T2 에서 코드를 고쳤으면 같은 브랜치에 커밋한다.
사장님께 한 줄: "맥 3회차를 mac/3b3 브랜치에 올렸습니다 — 확인 통과 N·실패 M, 이슈 B: <한 줄>, 공통 코드 변경 있음/없음"

## 2회차(3b-2) — 참고(끝남, 결과 `docs/mac-results/3b2-20261022.md`)

1회차 결과(`docs/mac-results/3b-20261022.md`)로 고친 것을 이 맥에서 확인한다. R0~R6 을 순서대로 한다.

| 1회차 | 고친 것 | 확인 |
|---|---|---|
| 이슈 1 — 시험이 진짜 파이썬의 실행 권한을 뺐다 | 시험 정리가 링크를 따라가지 않는다 | R2 |
| 이슈 2 — 시험이 진짜 claude 를 띄웠다 | 격리 모드(시험)에서는 '모델 목록 갱신'이 claude 를 띄우지 않는다. 시험 가드, 앱을 내리면 정기 작업도 끝 | R2 |
| M2 실패 94건 | 시험의 한글 임시 경로(맥은 영문)와 리눅스 전제를 정리했다. 런처 판정(psutil 없을 때 ps)은 제품도 고쳤다 | R2 |
| 관찰 3 — launchd 밖에서 뜬 Host | 맥에서 설치·`start`·`resume` 이 launchd 에 맡긴다(`launchctl kickstart`). Host 가 늘 launchd 작업 자신이다 | R3 |
| 관찰 2 — 폰 토큰 경고 | 컨트롤플레인이 있으면 경고하지 않는다 | R3 |
| 관찰 1 — 재개 | 재개 창의 명령이 맥 셸 문법(`cd '…'`)이다. 맥에서는 '새 cmd 창에서 실행' 단추가 없다 | R4 |
| 이슈 3 — 로그인 항목 "sh" | launchd 가 데이터 폴더의 `ClewPath` 스크립트를 부른다 → 로그인 항목·알림에 'ClewPath' 로 보여야 한다 | R3 |
| 이슈 3 — 권한 창 "python3.11" | **이번에는 바꾸지 않았다**(사장님 결정: 서명된 ClewPath.app 은 맥 정식 출시 전에 따로 기획) | — |

1회차가 남긴 '/model' 세션 3개(`~/.claude/projects/-Volumes-Works-Projects-clewpath/`)는 사장님이 ClewPath 화면에서 직접 지우신다 — 맥 측 Claude 는 손대지 않는다.

### R0 준비 (아무것도 바꾸지 않음)

```bash
ls -lO ~/.pyenv/versions/3.11.9/bin/python3.11      # -rwxr-xr-x 이고 uchg 가 없어야(1회차 뒤 되돌린 상태)
touch /tmp/clewpath-r2-start                         # R2 뒤 '새로 생긴 세션' 비교 기준
grep -c '"/model"' ~/.claude/history.jsonl           # 숫자만 적는다(읽기만)
```

### R1 코드 받기와 문법 확인

```bash
git checkout main && git pull && git log --oneline -1
for f in ops/get.sh install.sh uninstall.sh session_manager/start-connector.template.sh session_manager/update_runner.sh; do
  /bin/sh -n "$f" && echo "OK $f" || echo "FAIL $f"
done
```

### R2 자동 시험

```bash
uv run --python 3.11 --extra dev python -m pytest -q -p no:cacheprovider -rfEs > /tmp/clewpath-pytest-mac2.log 2>&1; echo "종료코드 $?"
tail -5 /tmp/clewpath-pytest-mac2.log
grep -E "^(FAILED|ERROR) " /tmp/clewpath-pytest-mac2.log | head -50     # -rfEs 라 이번에는 실패가 요약에 실린다
ls -lO ~/.pyenv/versions/3.11.9/bin/python3.11                           # 여전히 -rwxr-xr-x 여야(이슈 1)
find ~/.claude/projects -name '*.jsonl' -newer /tmp/clewpath-r2-start -size -3k -exec grep -l 'Kept model as' {} + 2>/dev/null   # 비어 있어야(이슈 2)
grep -c '"/model"' ~/.claude/history.jsonl                               # R0 과 같아야(이슈 2)
```
- 기대: 실패 0. 실패가 있으면 1회차처럼 목록과 traceback 끝 15줄을 보고한다.
- 걸린 시간을 적는다(1회차 533초).

### R3 다시 설치와 launchd

1회차 M5 와 같은 방법(제거 → 새로 설치)으로 새 코드를 설치한다. 페어링은 데이터 폴더에 남아 이어진다.

```bash
uv run --python 3.11 --no-project python -c "import sys; sys.path.insert(0, 'tests/posix_e2e'); import build_fixtures as b; open(sys.argv[1], 'wb').write(b.python_package())" /tmp/clewpath-host.zip
APP="$HOME/Library/Application Support/ClewPath/app"
cd "$APP" && sh uninstall.sh                         # 확인 질문에 y (1회차처럼 /dev/tty 로 묻는다)
mkdir -p "$APP" && cd "$APP" && unzip -q /tmp/clewpath-host.zip && sh install.sh 2>&1 | tee /tmp/clewpath-install2.log
grep -c "폰 접속 토큰" /tmp/clewpath-install2.log      # 0 이어야(관찰 2)
plutil -p ~/Library/LaunchAgents/com.clewpath.host.plist | grep -A3 ProgramArguments   # 첫 항목이 ~/.claude/session_manager/ClewPath
ls -l ~/.claude/session_manager/ClewPath                                               # -rwx------
```
- `sfltool dumpbtm` 은 쓰지 않는다 — 관리자 암호 창이 뜬다(2회차 관찰 7). 로그인 항목 이름은 사장님께 화면으로 확인을 부탁한다.
- 설치 때 '백그라운드 항목 추가됨' 알림이 뜨면 그 문구를 그대로 적는다("ClewPath" 여야). 시스템 설정 → 일반 → 로그인 항목에 보이는 이름도 적는다(사장님께 부탁).
- 1회차 "sh" 항목이 함께 남아 보이면 그대로 적는다(지우지 않는다 — macOS 가 정리하는지 본다).

각 단계에서 `sh start-connector.sh status` 의 pid 와 `launchctl print "gui/$(id -u)/com.clewpath.host" | grep -E "^\s*(state|pid|runs|last exit code) ="` 를 적는다.
**이번 기대는 늘 'status 의 pid = launchctl 의 pid'** 다(Host 가 launchd 작업 자신).

1. **설치 직후:** status pid = launchctl pid 여야 한다(1회차에는 설치가 띄운 Host 라 launchd 밖이었다).
2. `kill -9 <pid>` → **30초 안팎에** 새 pid(KeepAlive). 1회차의 첫 kill 은 281초였다.
3. `sh start-connector.sh stop` → 30초 뒤에도 stopped. 그다음 `sh start-connector.sh start` → 곧 running, pid 가 같아야 한다.
4. `sh start-connector.sh stop --hold` → `state=maintenance`(안 뜸). 그다음 `sh start-connector.sh resume` → running, pid 가 같아야 한다.
5. `grep -i kickstart ~/.claude/session_manager/launcher.log | tail -5` — 3·4 의 줄이 **"launchd 에 맡김(kickstart) pid N"** 이어야 한다.
   "kickstart 실패 → 직접 기동"이나 "약 20초 안에 뜨지 않음 → 직접 기동"이면 그 줄과 `launchctl print` 를 그대로 보고한다
   (stop 직후 30초 안에 start 하면 launchd 의 ThrottleInterval 30초에 걸릴 수 있다 — 그래서 3 은 30초 기다린 뒤 start 한다).
6. (사장님이 되실 때) 로그아웃 → 로그인. 로그아웃 때 Host 가 끝나고(launchd 작업이므로), 로그인 1분 안에 새 pid 로 뜨는지.

### R4 재개 창 (사장님과 함께)

```bash
SID=$(ls -t ~/.claude/projects/*/*.jsonl | head -1 | xargs basename | sed 's/\.jsonl$//')
curl -s "http://127.0.0.1:5100/api/sessions/$SID/resume-command"; echo     # "shell":"sh", "can_launch":false, full_command 이 "cd " 로 시작(경로에 공백·특수문자가 있을 때만 작은따옴표 — shlex 식)
```
- 브라우저 `http://127.0.0.1:5100/legacy`(옛 화면 — 재개 창은 여기에 있다) → 세션 하나 → 재개: 창의 명령이 `cd '…' && claude --resume …` 꼴인지(`cd /d` 가 아님), '새 cmd 창에서 실행' 단추가 없는지 본다. '🌐 브라우저에서 재개'는 1회차처럼 열려야 한다.
- 명령을 복사해 터미널 앱에 붙여 넣으면 그 세션이 이어지는지(확인 뒤 claude 는 `/exit`).

### R5 터미널 유지 (짧게, 사장님과 함께)

웹 터미널 하나를 연 채로 `kill -9 <Host pid>` → 새 Host 가 뜬 뒤 새로고침하면 같은 터미널이 이어지는지(1회차 M4-3 과 같다).

### R6 결과 보고

1. 브랜치 `mac-3b2` 를 main 에서 만들고 `docs/mac-results/3b2-YYYYMMDD.md` 하나에 적는다. 1회차와 같은 형식(요약 표 → 이슈 → 관찰 → 출력)이다.
2. 커밋 `docs: 맥 실기 결과(3b 2회차) YYYY-MM-DD` → `git push -u origin mac-3b2`. **main 에 직접 올리지 않는다.**
3. 사장님께 한 줄로 알린다: "맥 2회차 결과를 mac-3b2 브랜치에 올렸습니다 — 통과 N, 실패 M"

---

# 1회차 전체 절차 (참고)

> 처음부터 다시 할 때의 순서다. 1회차 결과로 기대값을 고쳐 두었다.

## 이 작업이 하는 일

ClewPath Host 는 PC 에 설치하는 프로그램이고, 지금까지 Windows 에서만 썼다.
맥·리눅스용 설치·업데이트·제거 스크립트와 맥 자동 시작(launchd)은 다 만들었다.
다만 리눅스 컨테이너로만 시험했고, **맥에서는 한 번도 실행해 보지 않았다.**

이번 작업은 이 맥에서 처음으로 세 가지를 해 보고 결과를 보고하는 것이다.
1. 자동 시험
2. 실제 설치와 자동 시작
3. 실제 claude 사용

여기를 통과해야 출시 대상(platforms)에 mac 을 넣는다.

## 지킬 것

- **비밀을 올리지 않는다.** 저장소(github.com/slpy22/clewpath)는 공개다.
  - 결과 보고에 넣지 않는 것: 토큰, 자격증명, `clewpath.toml`, `cp_credentials.json`, `host.log` 원문, `~/.claude` 아래 파일.
  - 필요한 줄만 옮긴다. 32자 이상 긴 영숫자 값은 `<가림>` 으로 바꾼다.
- **`~/.claude` 는 건드리지 않는다.** 예외는 하나다. Host 가 첫 기동 때 `~/.claude/settings.json` 에 상태 훅을 병합 등록한다(사장님이 승인한 예외이고, 백업을 남긴다).
- 설치는 이 맥의 사용자 영역에만 한다. sudo 를 쓰지 않고, root 로 실행하지 않는다.
- 설치하면 이 맥이 ClewPath 운영 컨트롤플레인(clewpath.pyongso.com)에 **새 PC 로 등록**된다. 사장님이 맥을 실제로 쓰기 위한 정상 절차다.
- **샌드박스:** Claude Code 의 명령 샌드박스가 `~/Library/LaunchAgents` 쓰기나 `launchctl` 을 막을 수 있다.
  - 그러면 그 명령은 사장님께 **터미널 앱에서 직접** 붙여 넣어 달라고 부탁하고, 출력을 받아 적는다.
  - 샌드박스를 임의로 끄지 않는다.
- 로그아웃·재부팅처럼 사람이 해야 하는 단계는 사장님께 부탁한다.
- **맥 측 Claude 는 터미널 앱에서 돌린다.** ClewPath 웹 터미널 안에서 돌리면 제거·브로커 교체 단계에서 자기 자신이 끝난다(2회차 관찰 1).

## M0 정보 수집 (아무것도 바꾸지 않음)

```bash
sw_vers; uname -m; /bin/sh --version | head -1; bash --version | head -1
/bin/sh -c 'cd /applications && pwd -P && /bin/pwd -P'   # 앞(sh 내장)은 친 글자 그대로, 뒤(/bin/pwd)는 디스크 글자 'Applications' 여야 한다
                                                         # (Claude Code 의 zsh 에서 치면 zsh 내장 pwd -P 가 이미 디스크 글자를 준다 — 1회차)
sysctl -n kern.bootsessionuuid; sysctl -n kern.boottime; sysctl -n hw.optional.arm64
/usr/bin/unzip -Z 2>&1 | head -2                           # ZipInfo 라는 말이 나와야 한다
command -v claude; claude --version; echo "$PATH"; echo "LANG=$LANG"; umask
command -v python3 && python3 --version; command -v uv && uv --version; command -v brew
ls -d "$HOME/Library/Application Support/ClewPath" 2>&1; ls ~/Library/LaunchAgents 2>&1 | grep -i clewpath
```

## M1 코드 받기와 문법 확인

앱 빌드에 쓰는 같은 저장소 사본에서 main 을 받는다.

```bash
git checkout main && git pull
for f in ops/get.sh install.sh uninstall.sh session_manager/start-connector.template.sh session_manager/update_runner.sh; do
  /bin/sh -n "$f" && echo "OK $f" || echo "FAIL $f"         # /bin/sh = bash 3.2 의 sh 모드 — 다섯 다 OK 여야 한다
done
```

## M2 자동 시험(맥에서 처음)

uv 가 없으면 `brew install uv` 로 설치한다. 이미 있으면 그대로 쓴다.

```bash
uv run --python 3.11 --extra dev python -m pytest -q -p no:cacheprovider -rfEs > /tmp/clewpath-pytest-mac.log 2>&1; echo "종료코드 $?"
tail -5 /tmp/clewpath-pytest-mac.log
grep -E "^(FAILED|ERROR) " /tmp/clewpath-pytest-mac.log | head -50
```
- 실패가 있어도 다음 단계로 간다. 실패 목록과, 실패마다 traceback 의 마지막 15줄을 보고에 넣는다.
- 시험은 진짜 운영 서버에 등록하지 않고(가드가 있다) `~/.claude` 도 건드리지 않는다.

## M3 실제 설치와 자동 시작(launchd)

**3-1. 설치 zip 만들기** — 출시 전 코드라 저장소에서 직접 만든다. 목록은 Windows 의 `package-for-user.ps1` 과 같다.

```bash
uv run --python 3.11 --no-project python -c "import sys; sys.path.insert(0, 'tests/posix_e2e'); import build_fixtures as b; open(sys.argv[1], 'wb').write(b.python_package())" /tmp/clewpath-host.zip
unzip -l /tmp/clewpath-host.zip | tail -1
```

**3-2. 설치** — 기본 위치의 빈 새 폴더에 풀고 설치한다.

```bash
APP="$HOME/Library/Application Support/ClewPath/app"
mkdir -p "$APP" && cd "$APP" && unzip -q /tmp/clewpath-host.zip
sh install.sh --dry-run          # 점검만 — 출력 전체를 보고
sh install.sh                    # 실제 설치 — 출력 전체를 보고(토큰은 찍히지 않는다)
```
- macOS 13 이상이면 '백그라운드 항목이 추가됨' 알림이 뜰 수 있다. 알림 문구와, 시스템 설정 → 일반 → 로그인 항목에 보이는 이름을 적는다.
- `launchctl bootstrap` 이 실패했다는 경고가 나오면 그 문구를 그대로 적는다. 그다음 안내대로 터미널 앱에서 다시 해 본다.

**3-3. 확인**

```bash
cd "$APP"
sh start-connector.sh status                                   # state=running 이어야
grep '^autostart=' .clewpath-install                           # launchd 여야(launchd-pending 이면 등록 실패)
launchctl print "gui/$(id -u)/com.clewpath.host" | head -50    # state·pid·program·environment·umask
plutil -p ~/Library/LaunchAgents/com.clewpath.host.plist       # 키·PATH·LANG·Umask
curl -s http://127.0.0.1:5100/api/health; echo
```

**3-4. 자기 회복** — 각 단계에서 `sh start-connector.sh status` 의 pid 와 `launchctl print … | grep -E "state|pid"` 를 적는다.

1. **설치 직후의 Host 도 launchd 작업 자신이다**(설치가 launchd 에 맡겨 띄운다 — 2회차부터. 1회차에는 launchd 밖이었다).
   - `launchctl print` 의 pid = status 의 pid 인지 본다.
   - Host 를 `kill -9 <pid>` 로 죽인다. **30초 안팎에** 다시 떠야 한다(KeepAlive).
2. 다시 `kill -9 <pid>` 로 죽인다. 역시 30초 안팎(1회차 15초).
3. `sh start-connector.sh stop` 을 한다.
   - 정상 종료라 launchd 가 곧바로 되살리지 않아야 한다. 30초 뒤에도 stopped 여야 한다.
   - 그다음 **약 5분 뒤**(5분 확인 + 기동 몇 초 — 1회차 301초) 다시 running 이 된다. Windows 예약 작업과 같은 동작이다.
4. `sh start-connector.sh stop --hold` 를 하고 6분 기다려도 안 뜨는지 본다(status 는 `state=maintenance`). 그다음 `sh start-connector.sh resume` → running.
5. **환경:** `ps eww -p <Host pid> | tr ' ' '\n' | grep -E '^(PATH|LANG)='`
   - PATH·LANG 이 plist 와 같아야 한다.
   - `ps -o pid,command -p <pid>` 로 명령줄도 적는다.
6. **잠자기:** 맥을 잠자기했다가 깨운 뒤 status·pid 를 본다(그대로 running 이어야).
7. **로그인:** 로그아웃 뒤 다시 로그인한다(또는 재부팅). 로그인 1분 안에 running 이어야 한다. 사장님께 부탁한다.

## M4 실제 사용(사장님과 함께)

1. 이 맥의 브라우저에서 `http://127.0.0.1:5100` 을 연다. 세션 목록이 보이는지 본다.
2. 목록에서 세션 하나를 열고, 터미널에 `claude` 가 뜨는지 본다. 한글을 몇 글자 쳐서 응답하는지, 한글이 깨지지 않는지 본다.
3. **터미널이 Host 재기동에도 살아남는지**(브로커가 터미널을 쥐고 있다 — launchd 의 AbandonProcessGroup):
   - 그 터미널을 연 채로 Host 를 `kill -9` 한다.
   - Host 가 다시 뜬 뒤 화면을 새로고침하면 같은 터미널이 이어지는지 본다.
   - `sh start-connector.sh stop` 뒤 다시 켰을 때도 같다.
4. 사장님 폰에서 ClewPath 를 연다 → 🖧 → ＋ 새 PC 추가 → 이 맥 화면의 QR 로 페어링한다. 폰에서 이 맥의 세션 목록·터미널이 보이는지 본다.
5. '재개'(이전 세션 이어가기)를 눌러 본다.
   - 재개 창의 명령은 맥 셸 문법(`cd '…' && claude --resume …`)이어야 한다. 맥에는 새 터미널 창 재개가 아직 없어 '새 cmd 창에서 실행' 단추가 없다.
   - '🌐 브라우저에서 재개'는 웹 터미널로 이어진다(1회차에 사장님이 본 '재개가 되는 것'이 이 길이다).

## M5 제거와 다시 설치

```bash
cd "$APP" && sh uninstall.sh            # 확인 질문에 y — 데이터(페어링 등)는 남는다
ls ~/Library/LaunchAgents | grep -i clewpath; launchctl print "gui/$(id -u)/com.clewpath.host" 2>&1 | head -2   # 둘 다 없어야
ps -A -o pid,command | grep -i "session_manager" | grep -v grep                                                  # 남은 Host 없어야(브로커는 확인 뒤 멈춤)
ls "$HOME/Library/Application Support/ClewPath" 2>&1
```
- 제거 출력 전체를 보고한다.
- 그다음 M3-1·M3-2 를 다시 해서 설치를 되돌려 놓는다(사장님이 계속 쓰실 수 있게). 페어링은 데이터 폴더에 남아 있으니, 폰에서 다시 페어링하지 않아도 이어지는지 확인한다.

## M6 결과 보고

1. 브랜치 `mac-3b` 를 main 에서 만들고, `docs/mac-results/3b-YYYYMMDD.md` 파일 하나에 결과를 적는다.
   - 단계마다 통과·실패를 적는다. 실패면 명령, 출력(필요한 줄만, 비밀 가림), 짐작 원인을 함께 적는다.
   - M0 의 출력 전체(비밀 없음).
   - M2 의 요약 줄과 실패 목록·traceback 끝 15줄.
   - 3-4 의 단계별 pid 와 걸린 시간.
   - 이상하게 오래 걸린 것.
2. 커밋 메시지 `docs: 맥 실기 결과(3b) YYYY-MM-DD` 로 커밋하고 `git push -u origin mac-3b` 한다. **main 에 직접 올리지 않는다.**
3. 사장님께 한 줄로 알린다: "맥 실기 결과를 mac-3b 브랜치에 올렸습니다 — 통과 N, 실패 M"
