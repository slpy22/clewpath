# 맥 측 작업 지시 — ClewPath Host 맥 실기(3b) (1회차 2026-10-21, 2회차 2026-10-22)

> 이 문서는 Windows 측(주 개발)이 맥 측 Claude 에게 전달하는 작업 지시서다. 앱 빌드 지시서 `docs/mac-build-tasks.md` 와 같은 방식이다.
> **고치지 말고 보고만 한다.** 실패가 나와도 코드를 고치지 않는다. 무엇이 어떻게 실패했는지만 남긴다. 수리는 Windows 측이 한다.
> **지금 할 일은 바로 아래 「2회차」뿐이다.** 그 아래 M0~M6 은 1회차 전체 절차(참고·다음에 처음부터 다시 할 때)다.
> 아래 「지킬 것」은 2회차에도 그대로다.

## 2회차(3b-2) — 지금 할 일

1회차 결과(`docs/mac-results/3b-20261022.md`)로 고친 것을 이 맥에서 확인한다. R0~R6 을 순서대로 한다.

| 1회차 | 고친 것 | 확인 |
|---|---|---|
| 이슈 1 — 시험이 진짜 파이썬의 실행 권한을 뺐다 | 시험 정리가 링크를 따라가지 않는다 | R2 |
| 이슈 2 — 시험이 진짜 claude 를 띄웠다 | 격리 모드(시험)에서는 '모델 목록 갱신'이 claude 를 띄우지 않는다. 시험 가드, 앱을 내리면 정기 작업도 끝 | R2 |
| M2 실패 94건 | 시험의 한글 임시 경로(맥은 영문)와 리눅스 전제를 정리했다. 런처 판정(psutil 없을 때 ps)은 제품도 고쳤다 | R2 |
| 관찰 3 — launchd 밖에서 뜬 Host | 맥에서 설치·`start`·`resume` 이 launchd 에 맡긴다(`launchctl kickstart`). Host 가 늘 launchd 작업 자신이다 | R3 |
| 관찰 2 — 폰 토큰 경고 | 컨트롤플레인이 있으면 경고하지 않는다 | R3 |
| 관찰 1 — 재개 | 재개 창의 명령이 맥 셸 문법(`cd '…'`)이다. 맥에서는 '새 cmd 창에서 실행' 단추가 없다 | R4 |
| 이슈 3 — "sh"·"python3.11" 이름과 권한 창 | **이번에는 바꾸지 않았다**(사장님 결정 대기) | — |

1회차가 남긴 '/model' 세션 3개(`~/.claude/projects/-Volumes-Works-Projects-clewpath/`)는 그대로 둔다(사장님이 정한다).

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
```

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
curl -s "http://127.0.0.1:5100/api/sessions/$SID/resume-command"; echo     # "shell":"sh", "can_launch":false, full_command 이 "cd '" 로 시작
```
- 브라우저 `http://127.0.0.1:5100` → 세션 하나 → 재개: 창의 명령이 `cd '…' && claude --resume …` 꼴인지(`cd /d` 가 아님), '새 cmd 창에서 실행' 단추가 없는지 본다. '🌐 브라우저에서 재개'는 1회차처럼 열려야 한다.
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
