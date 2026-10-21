# 맥 측 작업 지시 — ClewPath Host 맥 실기(3b) (2026-10-21)

> 이 문서는 Windows 측(주 개발)이 맥 측 Claude 에게 전달하는 작업 지시서다. 앱 빌드 지시서 `docs/mac-build-tasks.md` 와 같은 방식이다.
> 위에서부터 순서대로 실행한다. 각 단계의 결과는 **M6 결과 보고** 규칙대로 남긴다.
> **고치지 말고 보고만 한다.** 실패가 나와도 코드를 고치지 않는다. 무엇이 어떻게 실패했는지만 남긴다. 수리는 Windows 측이 한다.

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
cd /applications && pwd -P && /bin/pwd -P; cd ~          # 앞은 친 글자 그대로, 뒤(/bin/pwd)는 디스크 글자 'Applications' 여야 한다
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
uv run --python 3.11 --extra dev python -m pytest -q -p no:cacheprovider -rs > /tmp/clewpath-pytest-mac.log 2>&1; echo "종료코드 $?"
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

1. **설치 직후의 Host 는 launchd 밖에서 뜬 것이다**(설치가 띄움).
   - Host 를 `kill -9 <pid>` 로 죽인다.
   - **5분 안에** launchd 의 5분 확인이 다시 띄워야 한다. 이번에는 launchd 작업 자신이 Host 다(`launchctl print` 의 pid = Host pid).
2. 다시 `kill -9 <pid>` 로 죽인다. 이번에는 **30초 안팎에** 다시 떠야 한다(KeepAlive).
3. `sh start-connector.sh stop` 을 한다.
   - 정상 종료라 launchd 가 곧바로 되살리지 않아야 한다. 30초 뒤에도 stopped 여야 한다.
   - 그다음 **5분 안에** 다시 running 이 된다(5분 확인 — Windows 예약 작업과 같은 동작).
4. `sh start-connector.sh stop --hold` 를 하고 6분 기다려도 stopped 인지 본다. 그다음 `sh start-connector.sh resume` → running.
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
5. '재개'(이전 세션 이어가기)를 눌러 본다. 맥에서는 새 터미널 창 재개가 아직 없으니 **명령어를 알려 주는 것이 정상**이다.

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
