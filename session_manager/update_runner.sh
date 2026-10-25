#!/bin/sh
# ClewPath Host — 업데이트 적용기(맥·리눅스). update_runner.ps1 의 짝 — docs/designs/posix-install.md 3.4
#
# 왜 별도 프로세스인가: 돌고 있는 Host 는 자기 코드를 갈아끼울 수 없다. 그래서 이 스크립트는 설치 폴더 **밖**(데이터
# 폴더 updates/)에 복사돼 /bin/sh 로 돌고(실행 비트에 기대지 않는다), Host 가 내려가기를 기다렸다가 파일을 바꾼다.
#
# 호출 — osdep._posix.update_runner_argv(플래그 이름은 ps1 과 같다):
#   /bin/sh update_runner.sh -InstallRoot R -StageDir S -BackupDir W/backup-<지금> -WaitPid <Host pid> -Port P
#                            -Version X.Y.Z -LogFile W/apply-X.Y.Z.log [-RestartScript R/start-connector.sh]
#                            [-WaitSeconds 60] [-HealthSeconds 45]
#   /bin/sh update_runner.sh -RecoverOnly -InstallRoot R -LogFile W/recover.log   (런처·다음 러너: 중단된 교체 되돌리기)
#
# 순서: 인자 확인 → 잠금(updates/runner.lock.d)·표식(update.inprogress) → 본체 종료 대기(안 죽으면 TERM, 10초 뒤 KILL)
#   → 지난 교체 잔재 복구 → 백업(.tmp 에 다 만든 뒤 rename) → 교체(설치 루트 안 .clewpath-swap.<pid>/ 로 rename, 저널)
#   → uv sync(안 되면 uv pip install -e, 둘 다 실패면 곧바로 롤백) → 재기동(런처, CLEWPATH_UPDATER=1)
#   → 헬스(방금 띄운 Host·같은 claude_home·기대 버전) → 커밋 | 보류(maintenance.flag — 롤백하지 않고 커밋)
#   | 롤백(실패본 보관 → 실패한 Host 정지 → 저널 되돌림 → uv sync → 옛 버전 재기동·헬스)
#
# 지키는 것:
#  - 스스로 죽지 않는다(set -e·set -u 없음). 교체는 rename·저널이라 어디서 죽어도 다음 런처·러너가 -RecoverOnly 로
#    되돌린다. 되돌림도 rename 이라 되돌리다 죽어도 다시 돌리면 같은 결과다. 헬스를 통과하면 committed 표식을 남긴다
#  - 설치 폴더의 항목은 지우지 않고 옮기기만 한다. rm -rf 는 정해 둔 자리(백업·교체 저널·휴지통·러너 잠금)에만.
#    끝난 교체 저널은 휴지통(.clewpath-trash.<pid>)으로 옮긴 뒤 지운다 — 못 지워도 경고만(런처를 막지 않는다)
#  - 대기 마감은 반초 수로 센다(벽시계 아님 — NTP 가 시계를 뒤로 돌려도 더 기다리지 않는다)
#  - 터미널 관리 프로세스(브로커, session_manager.ptyd)는 건드리지 않는다. 프로세스는 명령줄(설치 경로·모듈)로 알아본다
#  - 재기동한 Host 는 사용자의 umask·env 를 그대로 물려받는다. HUP·INT 는 무시('')가 아니라 빈 처리(:) — 무시는 exec
#    로 자식(uv·런처·재기동 Host·그 Host 가 띄우는 브로커와 claude)까지 물려가, 브로커가 죽어도 claude 가 안 끝난다
#  - TERM 은 표시만 한다: 교체 전이면 중단(옛 버전으로 계속), 교체가 시작됐으면 끝까지
#  - 로그 문구는 ps1 과 같다(release.ps1 -Apply 가 꼬리를 보여 준다). set -x·env 덤프 금지(Host 의 SM_* 를 물려받는다)
# 종료코드: 0 성공(보류 포함) / 1 실패(롤백·중단·복구 실패) / 2 인자·사전 조건 오류(설치 폴더 무변경) / 3 다른 러너·설치가 진행 중

# 교체 대상. updater.py 의 PAYLOAD 와 같은 순서·같은 이름(test_updater.test_payload_lists_agree)
PAYLOAD="session_manager pwa pyproject.toml uv.lock install.ps1 install.sh uninstall.sh README.md LICENSE"
NL='
'

# >>> clewpath-home (get.sh·install.sh·uninstall.sh·update_runner.sh 공통 — 바꾸면 넷 다)
case "$(uname -s)" in
  Darwin) CLEWPATH_HOME="${CLEWPATH_HOME:-$HOME/Library/Application Support/ClewPath}" ;;
  *)      _xdg="${XDG_DATA_HOME:-}"; case "$_xdg" in /*) ;; *) _xdg="$HOME/.local/share" ;; esac
          CLEWPATH_HOME="${CLEWPATH_HOME:-$_xdg/clewpath}" ;;
esac
# <<< clewpath-home

# 헬스 확인(파이썬이 있을 때) — 표준 라이브러리만(-I -S), 설치 폴더 코드를 import 하지 않는다. 프록시는 쓰지 않는다.
# '방금 띄운 그 Host' 의 대답만 본다: runtime.json 의 pid 가 제외 목록(옛 Host·재기동 직전 runtime.json 의 pid)에 없고 살아
# 있을 때 그 runtime.json 의 port 로 묻는다(Host 는 포트를 열기 전에 runtime.json 을 쓴다). 진단의 claude_home 이 이 데이터
# 폴더의 부모와 같고 version 이 없거나 기대와 같으면 통과. version 은 Host 가 디스크(dist-info)에서 그때그때 읽으므로 같은
# 설치본의 다른(옛 코드) Host 도 uv sync 뒤엔 새 버전을 말한다 — 그래서 대답한 프로세스를 묶는다. 그 pid 의 명령줄은 셸이
# 본다(is_our_host). 인자: health RUNTIME HOME EXPECT SECONDS EXCL(쉼표). 시간은 monotonic.
# 출력 "ok <port> <ver> <pid>" | "mismatch <port> <ver>" | "none" (종료코드 0 / 2 / 1)
HELPER_PY='
import json, os, sys, time, urllib.request


def bound(runtime, excl):
    try:
        with open(runtime, encoding="utf-8-sig") as f:
            rt = json.load(f)
        pid, port = rt.get("pid"), rt.get("port")
    except Exception:
        return None
    if type(pid) is not int or type(port) is not int or pid <= 0 or pid in excl or not 0 < port < 65536:
        return None
    try:
        os.kill(pid, 0)
    except Exception:
        return None
    return pid, port


def clean(v):
    return "".join(c for c in str(v) if c.isascii() and (c.isalnum() or c in ".+-"))[:40] or "?"


def health(runtime, home, expect, seconds, excl):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.monotonic() + seconds
    last = "none"
    while True:
        b = bound(runtime, excl)
        if b:
            try:
                with opener.open("http://127.0.0.1:%d/api/owner/diagnostics" % b[1], timeout=3) as r:
                    body = json.loads(r.read(1 << 20))
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("claude_home") == home:
                v = body.get("version")
                if not expect or not v or v == expect:
                    return "ok %d %s %d" % (b[1], clean(v) if v else "-", b[0])
                last = "mismatch %d %s" % (b[1], clean(v))
        if time.monotonic() >= end:
            return last
        time.sleep(0.7)


if sys.argv[1] == "health":
    excl = {int(x) for x in sys.argv[6].split(",") if x.isdigit()}
    res = health(sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]), excl)
    print(res)
    sys.exit(0 if res.startswith("ok ") else 2 if res.startswith("mismatch ") else 1)
sys.exit(64)
'

# ---------------------------------------------------------------- 기록·검사

log() {   # ps1 의 Log 와 같은 모양 "[HH:MM:SS] …" — 로그 자리를 모르면(인자 오류) 표준오류로
  if [ "$LOG_OK" = 1 ]; then
    { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >>"$LogFile"; } 2>/dev/null
  else
    printf '%s\n' "$*" >&2
  fi
  return 0
}

exists() { [ -e "$1" ] || [ -L "$1" ]; }

num_ok() {   # 0 이상의 정수(9자리까지). 앞자리 0 은 거부 — $(( )) 가 8진수로 읽는다
  case "$1" in ''|*[!0-9]*|0?*) return 1 ;; esac
  [ "${#1}" -le 9 ]
}

path_ok() {   # 절대 경로 — 제어문자·. ·.. 조각이 없다
  case "$1" in /*) ;; *) return 1 ;; esac
  case "$1" in *[[:cntrl:]]*) return 1 ;; esac
  case "$1/" in */./*|*/../*) return 1 ;; esac
  return 0
}

trim_slash() {   # 끝의 / 를 걷어 낸 값 → T_OUT("/" 는 그대로)
  T_OUT=$1
  while :; do
    case "$T_OUT" in /|'') return 0 ;; */) T_OUT=${T_OUT%/} ;; *) return 0 ;; esac
  done
}

payload_name() {
  for PN_X in $PAYLOAD; do
    [ "$1" = "$PN_X" ] && return 0
  done
  return 1
}

pyproject_version() {   # $1 = pyproject.toml → [project] 의 version(우리 파일 모양: version = "X")
  sed -n '/^\[project\][[:space:]]*$/,/^\[/s/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1
}

rec_get() {   # 설치 기록(.clewpath-install)의 key= 값 — sed 로만 읽는다(소스로 실행하지 않는다)
  [ -f "$InstallRoot/.clewpath-install" ] || return 0
  sed -n "s/^$1=//p" "$InstallRoot/.clewpath-install" 2>/dev/null | head -n 1
}

json_esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

json_pid() {   # $1=파일 → "pid": N (파이썬 없이)
  sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n 1
}

runtime_port() {
  sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$DATA/runtime.json" 2>/dev/null | head -n 1
}

beat() {   # apply.lock 하트비트 — 파이썬 apply 의 5분 만료가 러너가 도는 동안 지나지 않게(없으면 만들지 않는다)
  [ "$RECOVER_ONLY" = 1 ] || touch -c "$WORK/apply.lock" 2>/dev/null
  return 0
}

# ---------------------------------------------------------------- 프로세스(명령줄로 알아본다)

pid_alive() {   # $1=pid — 좀비(끝났는데 부모가 아직 안 거둠)는 죽은 것
  case "${1:-}" in ''|0|*[!0-9]*) return 1 ;; esac
  kill -0 "$1" 2>/dev/null || return 1
  if [ -r "/proc/$1/stat" ]; then
    _ps=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    _ps=${_ps##*") "}
    case "$_ps" in Z*|X*|x*) return 1 ;; esac
  elif [ ! -d /proc/self ]; then
    case "$(ps -o stat= -p "$1" 2>/dev/null)" in *Z*) return 1 ;; esac
  fi
  return 0
}

cmdline_of() {   # $1=pid → 명령줄. 리눅스는 인자마다 한 줄(/proc), 맥은 ps 가 공백으로 잇는다. 못 읽으면 빈 출력
  if [ -d /proc/self ]; then
    { tr '\000' '\n' <"/proc/$1/cmdline"; } 2>/dev/null
  else
    ps -ww -o command= -p "$1" 2>/dev/null
  fi
}

cmd_has() {   # $1=pid $2=글자 — 명령줄에 있나. 못 읽으면 있다고 본다(잠금·표식을 함부로 시체로 보지 않게)
  _cc=$(cmdline_of "$1")
  [ -z "$_cc" ] && return 0
  case "$_cc" in *"$2"*) return 0 ;; esac
  return 1
}

is_our_host() {   # $1=pid — 이 설치본의 Host: 인자 하나는 "$InstallRoot/.venv/bin/…", 다른 하나는 Host 모듈(또는 진입점).
  pid_alive "$1" || return 1   # 브로커(session_manager.ptyd)는 아니다. 명령줄을 못 읽으면 아니라고 본다(죽이지 않게)
  if [ -d /proc/self ]; then
    _hc="$NL$(cmdline_of "$1")$NL"
    case "$_hc" in *"${NL}session_manager.ptyd$NL"*) return 1 ;; esac
    case "$_hc" in *"$NL$InstallRoot/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_hc" in *"${NL}session_manager.server$NL"*|*"/clewpath-host$NL"*) return 0 ;; esac
  else
    _hc=" $(cmdline_of "$1") "
    case "$_hc" in *" session_manager.ptyd "*) return 1 ;; esac
    case "$_hc" in *" $InstallRoot/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_hc" in *" session_manager.server "*|*"/clewpath-host "*) return 0 ;; esac
  fi
  return 1
}

host_pids() {   # 이 설치본의 Host 로 보이는 프로세스(나·부모 제외) — 한 줄에 pid 하나
  if [ -d /proc/self ]; then
    # 미리 거르기 한 번(명령줄에 설치본 venv 경로) — 정확한 판정은 is_our_host 가 인자 단위로
    grep -l -F -e "$InstallRoot/.venv/bin/" /proc/[0-9]*/cmdline 2>/dev/null | while IFS= read -r _hf; do
      _hp=${_hf#/proc/}
      _hp=${_hp%/cmdline}
      case "$_hp" in "$$"|"$PPID") continue ;; esac
      is_our_host "$_hp" && printf '%s\n' "$_hp"
    done
  else
    ps -A -ww -o pid= -o command= 2>/dev/null | while IFS= read -r _hl; do
      _hl=${_hl#"${_hl%%[! ]*}"}
      _hp=${_hl%% *}
      case "$_hp" in ''|*[!0-9]*|"$$"|"$PPID") continue ;; esac
      case " ${_hl#* } " in *" $InstallRoot/.venv/bin/"*) is_our_host "$_hp" && printf '%s\n' "$_hp" ;; esac
    done
  fi
}

# 대기 마감은 벽시계(date +%s)가 아니라 쉰 반초 수(NAP_T)로 센다 — 마감 시각으로 재면 NTP 가 시계를 뒤로 돌린 만큼(수 시간)
# 더 기다리고, 그동안 apply.lock 하트비트가 이어져 수동 적용도 거부된다. 소수 sleep 이 없으면 1초(= 2)
nap() {
  if sleep 0.5 2>/dev/null; then NAP_T=$((NAP_T + 1)); else sleep 1; NAP_T=$((NAP_T + 2)); fi
}

wait_gone() {   # $1=pid $2=초 — 그 안에 사라지면 0
  NAP_T=0
  while pid_alive "$1"; do
    [ "$NAP_T" -ge $(( $2 * 2 )) ] && return 1
    nap
  done
  return 0
}

# ---------------------------------------------------------------- 잠금·표식

lock_live() {   # $1=잠금 폴더 $2=쥔 프로세스 명령줄의 글자 — 시체: pid 가 죽음·다른 프로그램, 또는 pid 없이 2분 넘게 묵음
  [ -d "$1" ] || return 1
  _lp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$1/pid" 2>/dev/null)
  if [ -z "$_lp" ]; then
    [ -n "$(find "$1" -maxdepth 0 -mmin +2 2>/dev/null)" ] && return 1
    return 0
  fi
  [ "$_lp" = "$$" ] && return 1
  pid_alive "$_lp" && cmd_has "$_lp" "$2"
}

marker_live() {   # 0 = 다른 프로그램(설치·다른 러너)이 쥔 살아 있는 update.inprogress(pid 가 그 프로그램으로 살아 있고 30분 안)
  _uf="$DATA/update.inprogress"   # (at 은 앞자리 0 금지 — $(( )) 가 8진수로 읽다 셸째 끝난다. install.sh·uninstall.sh 와 같은 규칙)
  [ -f "$_uf" ] || return 1
  MK_PID=$(sed -n 's/^pid=\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  _ua=$(sed -n 's/^at=\([1-9][0-9]\{0,11\}\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  MK_BY=$(sed -n 's/^by=\([a-z]*\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  [ "$MK_PID" = "$$" ] && return 1
  if [ -z "$MK_PID" ] || [ -z "$_ua" ]; then
    [ -z "$(find "$_uf" -mmin +1 2>/dev/null)" ] && return 0      # 막 쓰는 중일 수 있다 — 1분은 산 것으로
    return 1
  fi
  pid_alive "$MK_PID" || return 1
  [ "$(( $(date +%s) - _ua ))" -lt 1800 ] || return 1
  case "$MK_BY" in
    runner) cmd_has "$MK_PID" update_runner ;;
    install) cmd_has "$MK_PID" install.sh ;;
    *) return 0 ;;
  esac
}

mark_take() {   # update.inprogress 를 내 것으로 — 임시 파일에 쓰고 mv(런처가 반쯤 쓴 표식을 읽지 않게)
  _mt="$DATA/.update.inprogress.$$.tmp"
  if { printf 'pid=%s\nat=%s\nby=runner\n' "$$" "$(date +%s)" >"$_mt"; } 2>/dev/null \
     && mv -f "$_mt" "$DATA/update.inprogress" 2>/dev/null; then
    HAVE_MARK=1
    return 0
  fi
  rm -f "$_mt" 2>/dev/null
  return 1
}

# 시체 잠금을 치우고 새로 잡는다(mkdir 까지) — 런처와 같은 규칙. 치우는 권리(옆 폴더 $1.reclaim)를 쥔 하나만 다시 보고
# 지우고 잡는다: 둘이 같은 시체를 따로 지우면 늦은 쪽이 먼저 잡은 쪽의 새 잠금을 지워 두 러너가 같은 교체 저널을 동시에
# 되돌린다(시체는 러너가 교체 도중 죽었을 때 — 곧 복구가 필요할 때 — 남는다). 2분 넘게 남은 권리 폴더는 치우다 죽은 것
lock_reclaim() {
  if ! mkdir "$1.reclaim" 2>/dev/null; then
    [ -n "$(find "$1.reclaim" -maxdepth 0 -mmin +2 2>/dev/null)" ] || return 1
    rmdir "$1.reclaim" 2>/dev/null
    mkdir "$1.reclaim" 2>/dev/null || return 1
  fi
  _lr=1
  if ! lock_live "$1" "$2"; then
    log "버려진 러너 잠금을 치웁니다"
    rm_guarded "$1" && mkdir "$1" 2>/dev/null && _lr=0
  fi
  rmdir "$1.reclaim" 2>/dev/null
  return "$_lr"
}

runner_lock_take() {   # $1=러너 잠금 폴더 → 0 잡음(pid 까지) / 1 살아 있는 러너가 쥠 / 2 남이 막 잡음·치우는 중 / 3 pid 를 못 씀
  if ! mkdir "$1" 2>/dev/null; then
    lock_live "$1" update_runner && return 1
    lock_reclaim "$1" update_runner || return 2
  fi
  if ! { printf '%s\n' "$$" >"$1/pid"; } 2>/dev/null; then
    rmdir "$1" 2>/dev/null                                 # pid 없는 잠금을 남기면 남들이 2분을 기다린다
    return 3
  fi
  return 0
}

take_lock_and_mark() {   # 1. 러너는 한 번에 하나(살아 있는 러너·설치가 있으면 종료코드 3 — apply.lock 은 그쪽 것이라 둔다)
  RL="$WORK/runner.lock.d"
  runner_lock_take "$RL"
  case $? in
    0) ;;
    1) log "다른 러너가 실행 중입니다(pid $(sed -n '1p' "$RL/pid" 2>/dev/null)) — 이번 실행은 건너뜁니다"
       KEEP_APPLY_LOCK=1
       finish false 3 ;;
    2) log "다른 러너가 방금 잠금을 잡았습니다 — 이번 실행은 건너뜁니다"
       KEEP_APPLY_LOCK=1
       finish false 3 ;;
    *) fail_args "러너 잠금을 쓸 수 없습니다: $RL" ;;
  esac
  HAVE_LOCK=1
  if marker_live; then
    log "업데이트·설치가 진행 중입니다(update.inprogress: pid ${MK_PID:-?}, by=${MK_BY:-?}) — 이번 실행은 건너뜁니다"
    KEEP_APPLY_LOCK=1
    finish false 3
  fi
  mark_take || fail_args "update.inprogress 를 쓸 수 없습니다: $DATA"
}

release_all() {   # 모든 종료 길에서 — apply.lock(적용일 때)·표식(내 것)·헬퍼·러너 잠금(내 것)
  if [ "$RECOVER_ONLY" != 1 ] && [ "$LOG_OK" = 1 ] && [ "$KEEP_APPLY_LOCK" != 1 ]; then
    rm -f "$WORK/apply.lock" 2>/dev/null
  fi
  if [ "$HAVE_MARK" = 1 ]; then
    [ "$(sed -n 's/^pid=//p' "$DATA/update.inprogress" 2>/dev/null | head -n 1)" = "$$" ] && rm -f "$DATA/update.inprogress"
    HAVE_MARK=0
  fi
  if [ -n "$HELPER" ]; then
    rm -f "$HELPER" 2>/dev/null
    HELPER=
  fi
  if [ "$HAVE_LOCK" = 1 ]; then
    [ "$(sed -n '1p' "$WORK/runner.lock.d/pid" 2>/dev/null)" = "$$" ] && rm_guarded "$WORK/runner.lock.d"
    HAVE_LOCK=0
  fi
  return 0
}

finish() {   # $1=true|false $2=종료코드 — 정리 후 끝(10. 정리)
  release_all
  log "=== 종료 (성공: $1) ==="
  exit "$2"
}

# ---------------------------------------------------------------- 파일

rm_guarded() {   # rm -rf 는 여기로만 — 정해 둔 자리(백업·실패본·교체 저널·휴지통·러너 잠금)가 아니면 거부한다
  G_T=$1
  G_OK=0
  if [ "$LOG_OK" = 1 ] && path_ok "$G_T" && [ "$G_T" != / ] && [ "$G_T" != "$HOME" ] && [ "$G_T" != "$HOME/" ]; then
    case "$G_T" in "$WORK"/runner.lock.d) G_OK=1 ;; esac
    if [ "$BACKUP_OK" = 1 ]; then
      case "$G_T" in "$BackupDir"|"$BackupDir".tmp.*|"$BackupDir"-failed-*) G_OK=1 ;; esac
    fi
    if [ "$ROOT_OK" = 1 ]; then
      case "$G_T" in "$InstallRoot"/.clewpath-swap.*|"$InstallRoot"/.clewpath-trash.*) G_OK=1 ;; esac
    fi
  fi
  if [ "$G_OK" != 1 ]; then
    log "[FAIL] 지우기 거부(정해 둔 자리가 아님): $G_T"
    return 1
  fi
  exists "$G_T" || return 0
  rm -rf "$G_T" 2>>"$LogFile"
  if exists "$G_T"; then
    log "  [WARN] 다 지우지 못했습니다: $G_T"
    return 1
  fi
  return 0
}

mv_new() {   # rename — 대상이 이미 있으면 거부(있는 폴더로 mv 하면 그 안으로 들어간다. POSIX 엔 mv -T 가 없다)
  if exists "$2"; then
    log "  대상이 이미 있습니다: $2"
    return 1
  fi
  mv "$1" "$2" 2>>"$LogFile"
}

write_result() {   # 9. $1=true|false [$2=버전] [$3=덧붙일 키 — ,"k":v 고정 문자열만(held·deps_fallback)] — last_apply.json(BOM
  _rv=${2:-$Version}   # 없는 UTF-8, 임시 파일 → mv). updater.recently_failed 가 읽는다(version·ok 만 — 덧붙인 키는 모른다)
  _rt="$WORK/.last_apply.$$.tmp"
  if { printf '{"version":"%s","ok":%s,"finished_at":%s,"log":"%s"%s}\n' "$(json_esc "$_rv")" "$1" "$(date +%s)" \
         "$(json_esc "$LogFile")" "${3:-}" >"$_rt"; } 2>/dev/null && mv -f "$_rt" "$WORK/last_apply.json" 2>/dev/null; then
    return 0
  fi
  rm -f "$_rt" 2>/dev/null
  log "  [WARN] last_apply.json 을 쓰지 못했습니다"
  return 0
}

# ---------------------------------------------------------------- 교체(rename + 저널)

journal() { { printf '%s %s\n' "$1" "$2" >>"$SWAP/journal"; } 2>>"$LogFile"; }

# 런처(start-connector.sh)는 PAYLOAD 밖이지만 새 버전 Host 가 뜨자마자(헬스 판정 전) 자기 템플릿으로 다시 렌더한다
# (liveness.ensure_launcher). 그래서 교체 전 런처를 저널(SWAP/launcher)·백업에 함께 두고 되돌림 때 제자리로 — 안 그러면
# 롤백한 옛 버전이 실패한 릴리스의 런처로 재기동되고(launcher-ensured-<옛 버전> 표식이 있어 다시 렌더하지 않는다)
# 그 뒤 모든 기동도 그렇다. 템플릿이 문제인 릴리스면 옛 버전이 영영 못 뜬다.
launcher_restore() {   # $1=옛 런처 사본 → 제자리로(임시 파일 → rename). 0 됨(사본 없음 포함) / 1 못 함(경고만)
  exists "$1" || return 0
  _lt="$InstallRoot/.start-connector.sh.runner.$$"
  if cp -p "$1" "$_lt" 2>>"$LogFile" && mv -f "$_lt" "$InstallRoot/start-connector.sh" 2>>"$LogFile"; then
    return 0
  fi
  rm -f "$_lt" 2>/dev/null
  log "  [WARN] 런처를 되돌리지 못했습니다: $InstallRoot/start-connector.sh"
  return 1
}

launcher_forget() {   # $1=되돌린 버전 — 그 버전의 런처 보장 표식을 지운다(다시 받으면 그때 새 템플릿으로 렌더하게)
  [ -n "$1" ] && rm -f "$DATA/launcher-ensured-$1" 2>/dev/null
  return 0
}

swap_prepare() {   # 4a. $1(스테이지·백업)의 PAYLOAD 항목을 SWAP/new 로 복사 — 설치 폴더는 아직 그대로. $2=새 버전 $3=옛 버전
  SWAP="$InstallRoot/.clewpath-swap.$$"            # 설치 루트 안 — mv 가 곧 rename(2)
  if exists "$SWAP"; then
    log "[FAIL] 교체 저널 자리가 이미 있습니다: $SWAP"
    SWAP=
    return 1
  fi
  if ! mkdir "$SWAP" 2>>"$LogFile"; then
    log "[FAIL] 교체 저널을 만들 수 없습니다: $SWAP"
    SWAP=
    return 1
  fi
  if ! mkdir "$SWAP/new" "$SWAP/old" 2>>"$LogFile" \
     || ! { printf 'format=1\nrunner_pid=%s\nversion=%s\nfrom=%s\nstarted=%s\n' "$$" "$2" "$3" "$(date +%s)" \
              >"$SWAP/info"; } 2>>"$LogFile" \
     || { exists "$InstallRoot/start-connector.sh" \
          && ! cp -p "$InstallRoot/start-connector.sh" "$SWAP/launcher" 2>>"$LogFile"; }; then
    log "[FAIL] 교체 저널을 만들 수 없습니다: $SWAP"
    swap_drop "$SWAP"
    SWAP=
    return 1
  fi
  for SP_N in $PAYLOAD; do
    exists "$1/$SP_N" || continue
    if ! cp -pR "$1/$SP_N" "$SWAP/new/$SP_N" 2>>"$LogFile"; then
      log "[FAIL] 새 항목 복사 실패: $SP_N"
      swap_drop "$SWAP"
      SWAP=
      return 1
    fi
  done
  return 0
}

swap_moves() {   # 4b. 항목마다 옛 것을 SWAP/old 로, 새 것을 제자리로 — rename 직전마다 저널 한 줄.
  for SM_N in $PAYLOAD; do   # 0 됨 / 1 실패(되돌림까지 끝 — 설치 폴더는 이전 그대로) / 2 되돌림도 실패(SWAP 을 남긴다)
    exists "$SWAP/new/$SM_N" || continue
    if exists "$InstallRoot/$SM_N"; then
      if ! journal old "$SM_N" || ! mv_new "$InstallRoot/$SM_N" "$SWAP/old/$SM_N"; then
        swap_moves_failed "$SM_N"
        return $?
      fi
    fi
    if ! journal new "$SM_N" || ! mv_new "$SWAP/new/$SM_N" "$InstallRoot/$SM_N"; then
      swap_moves_failed "$SM_N"
      return $?
    fi
  done
  return 0
}

swap_moves_failed() {
  log "[FAIL] 교체 중 오류: $1"
  if swap_undo "$SWAP" && swap_drop "$SWAP"; then
    log "옮긴 항목을 되돌렸습니다(설치 폴더는 이전 그대로)"
    SWAP=
    return 1
  fi
  log "[FAIL] 되돌림도 실패했습니다 — $SWAP 를 남깁니다(sh \"$InstallRoot/install.sh\" 로 복구하세요)"
  return 2
}

swap_undo() {   # $1=교체 저널 폴더 — 저널을 거꾸로 읽어 rename 을 되돌린다. 지우지 않고 반대로 옮기기만 하므로
  U_SD=$1       # 되돌리다 죽어도 다시 돌리면 같은 결과(멱등). 0 됨(저널 없음 포함) / 1 실패(손대지 않은 채 멈춤)
  [ -s "$U_SD/journal" ] || return 0
  _uf=$(sed -n 's/^format=//p' "$U_SD/info" 2>/dev/null | head -n 1)
  if [ -n "$_uf" ] && [ "$_uf" != 1 ]; then
    log "[FAIL] 모르는 저널 형식(format=$_uf): $U_SD"
    return 1
  fi
  while IFS= read -r U_L || [ -n "$U_L" ]; do      # 먼저 줄을 모두 본다(줄바꿈 없이 끊긴 끝줄까지) — 모르는 줄이 하나라도
    case "$U_L" in "old "*|"new "*) payload_name "${U_L#* }" && continue ;; esac   # 있으면 아무것도 건드리지 않는다
    log "[FAIL] 저널에 모르는 줄: $U_L"
    return 1
  done <"$U_SD/journal"
  _ur="$U_SD/journal.rev"
  if ! awk '{ l[NR] = $0 } END { for (i = NR; i > 0; i--) print l[i] }' "$U_SD/journal" >"$_ur" 2>>"$LogFile"; then
    log "[FAIL] 저널을 읽지 못했습니다: $U_SD/journal"
    return 1
  fi
  while IFS= read -r U_L; do
    U_N=${U_L#* }
    case "$U_L" in
      "new "*)   # 새 항목이 제자리로 옮겨졌으면 SWAP/new 로 되돌린다(아직이면·이미 되돌렸으면 SWAP/new 에 있다)
        if ! exists "$U_SD/new/$U_N" && exists "$InstallRoot/$U_N"; then
          mv_new "$InstallRoot/$U_N" "$U_SD/new/$U_N" || { log "[FAIL] 되돌리지 못했습니다: $U_N"; return 1; }
        fi ;;
      "old "*)   # 옛 항목이 SWAP/old 에 있으면 제자리로(없으면 아직 안 옮겼거나 이미 되돌렸다)
        if exists "$U_SD/old/$U_N"; then
          mv_new "$U_SD/old/$U_N" "$InstallRoot/$U_N" || { log "[FAIL] 되돌리지 못했습니다: $U_N"; return 1; }
        fi ;;
    esac
  done <"$_ur"
  launcher_restore "$U_SD/launcher"                # 새 버전 Host 가 다시 렌더했을 런처도(사본은 복사라 다시 돌려도 같다)
  return 0
}

trash_name() {   # 비어 있는 휴지통 이름 → TRASH: 설치 루트 안 .clewpath-trash.<pid>(있으면 .<n> 을 붙인다). 설치 폴더 안의
  TRASH="$InstallRoot/.clewpath-trash.$$"   # 일시 이름은 .clewpath-swap.* .clewpath-old.* .clewpath-trash.* 셋(install·uninstall 도 안다)
  _tn=0
  while exists "$TRASH"; do
    _tn=$((_tn + 1))
    [ "$_tn" -le 99 ] || return 1
    TRASH="$InstallRoot/.clewpath-trash.$$.$_tn"
  done
  return 0
}

# 끝난 교체 저널 폴더(커밋됨·되돌림 끝·옮긴 것 없음 — 부르는 쪽이 보장)를 치운다. 저널 파일부터 지우고(지우다 죽어도 다음
# 복구가 되돌리지 않게), 같은 폴더 안 휴지통 이름으로 rename 해 저널 이름 공간(.clewpath-swap.*) 밖으로 뺀 뒤 지운다. 못 지운
# 것(root 소유 __pycache__ 등)이 남아도 런처·복구가 잔재로 보지 않는다 — 경고만, 다음 러너·복구가 다시 지워 본다(trash_sweep).
# 전엔 남은 .clewpath-swap.* 때문에 런처 start 가 '잔재 복구 실패'로 영구히 막혔다. 0 = 저널 이름에서 빠짐 / 1 = 그대로 남음
swap_drop() {
  [ -n "$1" ] || return 0
  exists "$1" || return 0
  rm -f "$1/journal" "$1/journal.rev" 2>/dev/null
  if trash_name && mv "$1" "$TRASH" 2>>"$LogFile"; then
    rm_guarded "$TRASH"
    return 0
  fi
  if exists "$1/journal"; then
    log "  [WARN] 저널을 지우지 못했습니다: $1/journal"
    return 1
  fi
  rm_guarded "$1"
}

trash_sweep() {   # 지난 러너·복구가 못 지운 휴지통(.clewpath-trash.*)을 다시 지워 본다 — 안 되면 경고만(rm_guarded)
  for _ts in "$InstallRoot"/.clewpath-trash.*; do
    exists "$_ts" && rm_guarded "$_ts"
  done
  return 0
}

commit_swap() {   # 헬스 통과 — 커밋 표식을 먼저(여기서 죽어도 다음 복구가 되돌리지 않게), 그다음 저널을 치운다(휴지통으로)
  # true(일반 명령) — ':'(특수 내장)는 리디렉션이 실패하면 dash·bash --posix 가 스크립트를 통째로 끝낸다
  { true >"$SWAP/committed"; } 2>/dev/null || log "  [WARN] 커밋 표식을 쓰지 못했습니다: $SWAP"
  swap_drop "$SWAP" || log "  [WARN] 교체 저널을 다 지우지 못했습니다: $SWAP"
  SWAP=
}

swap_dirs_left() {   # 설치 루트에 교체 저널(.clewpath-swap.*)이 있나
  for _sl in "$InstallRoot"/.clewpath-swap.*; do
    exists "$_sl" && return 0
  done
  return 1
}

recover_leftovers() {   # 지난 러너가 남긴 .clewpath-swap.* — 커밋된 것·옮긴 것 없는 것은 치우기만(되돌릴 것이 없으니 못 치워도
  RV_DONE=0             # 막지 않는다), 나머지는 되돌린 뒤 uv sync(dist-info)·last_apply(그 버전 ok=false). 먼저 지난 휴지통을 다시
  trash_sweep           # 지워 본다. 0 됨·없음 / 1 실패(잔재를 남긴다)
  for RV_SD in "$InstallRoot"/.clewpath-swap.*; do
    exists "$RV_SD" || continue
    if [ -L "$RV_SD" ] || [ ! -d "$RV_SD" ]; then
      log "[FAIL] 교체 저널 자리가 폴더가 아닙니다: $RV_SD"
      return 1
    fi
    if [ -e "$RV_SD/committed" ] || [ ! -s "$RV_SD/journal" ]; then
      log "지난 교체 저널을 치웁니다(커밋됨 또는 옮긴 것 없음): ${RV_SD##*/}"
      swap_drop "$RV_SD" || log "  [WARN] 지난 교체 저널을 치우지 못했습니다(되돌릴 것은 없음): ${RV_SD##*/}"
      RV_DONE=1
      continue
    fi
    log "중단된 업데이트를 되돌립니다: ${RV_SD##*/}"
    swap_undo "$RV_SD" || return 1
    sync_deps "복구" || log "  [WARN] 복구 뒤 의존성 동기화 실패 — install.sh·다음 업데이트가 다시 맞춥니다"
    _rvv=$(sed -n 's/^version=\([0-9A-Za-z.+-]*\)$/\1/p' "$RV_SD/info" 2>/dev/null | head -n 1)
    [ -n "$_rvv" ] && write_result false "$_rvv"
    launcher_forget "$_rvv"
    swap_drop "$RV_SD" || return 1
    log "중단된 업데이트 되돌림 완료(${_rvv:-버전 모름})"
    RV_DONE=1
  done
  return 0
}

# ---------------------------------------------------------------- 의존성·재기동·헬스

find_uv() {   # 설치 기록(uv=) → 공통 경로의 $CLEWPATH_HOME/bin/uv → PATH. launchd·분리 기동엔 XDG 변수가 없을 수 있어 기록이 먼저.
  UV=          # CLEWPATH_HOME 이 절대 경로가 아니면 뺀다 — 있는지는 지금 폴더에서 보고 실행은 아래 cd 뒤라 다른 파일이 된다
  _fu=$(rec_get uv)
  if path_ok "$_fu" && [ -f "$_fu" ] && [ -x "$_fu" ]; then
    UV=$_fu
  elif path_ok "$CLEWPATH_HOME" && [ -f "$CLEWPATH_HOME/bin/uv" ] && [ -x "$CLEWPATH_HOME/bin/uv" ]; then
    UV="$CLEWPATH_HOME/bin/uv"
  else
    UV=$(command -v uv 2>/dev/null) || UV=
    path_ok "$UV" || UV=                           # PATH 의 상대 경로 항목이면 아래 cd 뒤에 못 찾는다
  fi
  REC_PREF=$(rec_get python_pref)
  case "$REC_PREF" in only-system|only-managed|system|managed) ;; *) REC_PREF= ;; esac
  REC_PYDIR=$(rec_get python_install_dir)
  path_ok "$REC_PYDIR" || REC_PYDIR=
  return 0
}

# 5. $1=까닭. 파일을 바꾼 뒤엔 꼭 — 의존성뿐 아니라 설치 메타데이터(dist-info) 때문이다(롤백 뒤에도 새 버전을 말하면 다시는
# 업데이트를 받지 않는다). 0 됨(uv·venv 가 없어 건너뜀 포함) / 1 uv sync·pip 둘 다 실패. SYNC_VIA = none|uv|pip(잠금 밖 폴백).
# uv 에는 Host 를 띄운 셸의 가상 환경 표식(VIRTUAL_ENV·CONDA_PREFIX·UV_PYTHON)을 주지 않는다 — install.sh 의 uv_run 과 같다
# (UV_PYTHON=3.12 셸에서 띄운 Host 의 업데이트가 .venv 를 다른 파이썬으로 다시 만들었다)
sync_deps() {
  find_uv
  SYNC_VIA=none
  if [ -z "$UV" ] || [ ! -x "$VENV_PY" ]; then
    log "uv 나 venv 를 못 찾아 의존성 동기화를 건너뜁니다 ($1)"
    return 0
  fi
  beat
  SYNC_VIA=uv
  _so="$WORK/.runner-uv.$$.out"
  (
    unset VIRTUAL_ENV CONDA_PREFIX UV_PYTHON
    cd "$InstallRoot" || exit 1
    [ -n "$REC_PREF" ] && export UV_PYTHON_PREFERENCE="$REC_PREF"
    [ -n "$REC_PYDIR" ] && export UV_PYTHON_INSTALL_DIR="$REC_PYDIR"
    UV_PROJECT_ENVIRONMENT="$InstallRoot/.venv" UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-1}" exec "$UV" sync --frozen --no-dev
  ) </dev/null >"$_so" 2>&1
  _src=$?
  { LC_ALL=C sed 's/^/  uv: /' "$_so" >>"$LogFile"; } 2>/dev/null
  if [ "$_src" != 0 ]; then
    log "  uv sync 실패(종료코드 $_src) — pyproject 기준으로 다시: uv pip install -e"
    SYNC_VIA=pip
    (
      unset VIRTUAL_ENV CONDA_PREFIX UV_PYTHON
      cd "$InstallRoot" || exit 1
      [ -n "$REC_PREF" ] && export UV_PYTHON_PREFERENCE="$REC_PREF"
      [ -n "$REC_PYDIR" ] && export UV_PYTHON_INSTALL_DIR="$REC_PYDIR"
      UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-1}" exec "$UV" pip install --python "$VENV_PY" -e "$InstallRoot"
    ) </dev/null >"$_so" 2>&1
    _src=$?
    { LC_ALL=C sed 's/^/  pip: /' "$_so" >>"$LogFile"; } 2>/dev/null
  fi
  rm -f "$_so" 2>/dev/null
  beat
  if [ "$_src" != 0 ]; then
    log "  pip 도 실패했습니다(종료코드 $_src)"
    return 1
  fi
  log "의존성 동기화 완료 ($1)"
  return 0
}

restart_host() {   # $1=1 이면 러너 자신의 재기동(CLEWPATH_UPDATER=1 — 표식·교체 저널을 통과). 런처는 기동 확인까지
  if [ -z "$RestartScript" ] || [ ! -f "$RestartScript" ]; then   # 기다렸다 끝나므로 동기 호출. 0 = 런처 정상 종료
    log "재기동 스크립트가 없어 건너뜁니다: $RestartScript"
    return 1
  fi
  beat
  [ -e "$DATA/maintenance.flag" ] && log "  [WARN] maintenance.flag 가 있어 런처가 띄우지 않습니다(resume 으로 해제)"
  if [ "$1" = 1 ]; then
    CLEWPATH_UPDATER=1 /bin/sh "$RestartScript" start </dev/null >>"$LogFile" 2>&1
  else
    /bin/sh "$RestartScript" start </dev/null >>"$LogFile" 2>&1
  fi
  _rr=$?
  beat
  [ "$_rr" = 0 ] && return 0
  log "  런처가 실패했습니다(종료코드 $_rr)"
  return 1
}

helper_py() {   # 헬퍼를 돌릴 파이썬(그때그때 — 업데이트 중 venv 가 바뀔 수 있다): venv 가 되면 그것, 아니면 [linux] python3.
  HPY=          # [mac] 은 시스템 python3 를 부르지 않는다(Command Line Tools 설치 대화상자)
  [ -n "$HELPER" ] && [ -f "$HELPER" ] || return 1
  if [ -x "$VENV_PY" ] && "$VENV_PY" -I -S -c pass </dev/null >/dev/null 2>&1; then
    HPY=$VENV_PY
    return 0
  fi
  [ "$(uname -s)" = Darwin ] && return 1
  _hy=$(command -v python3 2>/dev/null) || return 1
  "$_hy" -I -S -c pass </dev/null >/dev/null 2>&1 || return 1
  HPY=$_hy
  return 0
}

host_bound() {   # $1=runtime.json 의 pid — '방금 띄운 그 Host' 인가: 옛 Host(WaitPid)도 재기동 직전 runtime.json 의 pid(H_PRE)도
  case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac   # 아니고, 이 설치본의 Host 명령줄로 살아 있다
  [ "$1" != "$WaitPid" ] && [ "$1" != "$H_PRE" ] && is_our_host "$1"
}

health_curl() {   # 파이썬이 없을 때 — curl 과 문자열 검사(프록시 무시). $1=기대 버전. 헬퍼와 같은 묶음(방금 띄운 Host 의
  _hj=$(json_esc "$H_HOME")   # runtime.json port 에만, claude_home 이 같을 때). 마감은 반초 수 — -m 3 을 다 쓴 시도는 3초로 센다
  _hl=$((HealthSeconds * 2))
  NAP_T=0
  while :; do
    _hk=$(json_pid "$DATA/runtime.json")
    _hp=$(runtime_port)
    if [ -n "$_hp" ] && host_bound "$_hk"; then
      _hb=$(curl --noproxy '*' -s -m 3 "http://127.0.0.1:$_hp/api/owner/diagnostics" 2>/dev/null)
      _hc=$?
      [ "$_hc" = 28 ] && NAP_T=$((NAP_T + 6))
      if [ "$_hc" = 0 ]; then
        case "$_hb" in
          *"\"claude_home\":\"$_hj\""*|*"\"claude_home\": \"$_hj\""*)
            _hv=$(printf '%s' "$_hb" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9A-Za-z.+-]*\)".*/\1/p' | head -n 1)
            if [ -z "$1" ] || [ -z "$_hv" ] || [ "$_hv" = "$1" ]; then
              H_RES="ok $_hp ${_hv:--} $_hk"
              return 0
            fi
            H_RES="mismatch $_hp $_hv" ;;
        esac
      fi
    fi
    [ "$NAP_T" -ge "$_hl" ] && return 0
    nap
  done
}

# 7. $1=기대 버전(비면 아무 버전) → H_RES. 0 통과 / 2 방금 띄운 Host 가 답했지만 버전이 다름 / 1 응답 없음.
# 통과는 '방금 띄운 그 Host' 의 대답만: runtime.json 의 pid 가 옛 Host·재기동 직전 pid(H_PRE — 부르는 쪽이 재기동 직전에
# 적는다)가 아니고 이 설치본의 Host 명령줄로 살아 있으며, 진단의 claude_home 이 이 데이터 폴더의 부모(H_HOME)와 같다.
# 전엔 claude_home 이 비었는지만 봤고 -Port 에 누가 답해도 통과라, 같은 설치본의 다른 Host(디스크의 dist-info 를 읽어 새
# 버전을 말한다)·같은 claude_home 의 개발 서버의 대답으로 새 코드가 한 번도 답하지 않았는데 커밋했다. 롤백 헬스도 같다
health_check() {
  H_RES=none
  if helper_py; then
    H_RES=$("$HPY" -I -S "$HELPER" health "$DATA/runtime.json" "$H_HOME" "$1" "$HealthSeconds" "$WaitPid,${H_PRE:-0}" \
              </dev/null 2>/dev/null)
  elif command -v curl >/dev/null 2>&1; then
    health_curl "$1"
  else
    log "  헬스체크 수단이 없습니다(파이썬·curl 없음)"
  fi
  case "$H_RES" in
    "ok "*) host_bound "${H_RES##* }" && return 0
            log "  runtime.json 의 pid ${H_RES##* } 는 방금 띄운 이 설치본의 Host 가 아닙니다 — 그 대답으로는 통과시키지 않습니다" ;;
    "mismatch "*) return 2 ;;
  esac
  H_RES=none
  return 1
}

stop_failed_hosts() {   # 8b. 실패한 Host — 이 설치본의 Host 명령줄만(브로커 session_manager.ptyd 는 빼낸다). TERM, 5초 뒤 KILL
  _sp=$(host_pids)
  _sr=$(json_pid "$DATA/runtime.json")
  if [ -n "$_sr" ] && is_our_host "$_sr"; then
    case "$NL$_sp$NL" in *"$NL$_sr$NL"*) ;; *) _sp="$_sp$NL$_sr" ;; esac
  fi
  _sp=$(printf '%s\n' "$_sp" | tr -s '\n' ' ')
  case "$_sp" in *[0-9]*) ;; *) return 0 ;; esac
  log "실패한 Host 를 멈춥니다(SIGTERM):$(printf ' %s' $_sp)"
  for _p in $_sp; do kill -TERM "$_p" 2>/dev/null; done
  NAP_T=0
  while :; do
    _sl=
    for _p in $_sp; do is_our_host "$_p" && _sl="$_sl $_p"; done
    [ -z "$_sl" ] && return 0
    [ "$NAP_T" -ge 10 ] && break
    nap
  done
  log "  5초 뒤에도 남아 SIGKILL:$_sl"
  for _p in $_sl; do kill -KILL "$_p" 2>/dev/null; done
  for _p in $_sl; do wait_gone "$_p" 5 || log "  [WARN] 강제 종료 뒤에도 남아 있습니다(pid $_p)"; done
  return 0
}

# ---------------------------------------------------------------- 흐름

fail_args() {   # 0. 인자·사전 조건 오류 — 설치 폴더는 그대로, 종료코드 2. 설치 루트가 멀쩡하면 옛 버전으로 계속
  log "[FAIL] 적용하지 않습니다: $1"
  [ "$RECOVER_ONLY" != 1 ] && [ "$LOG_OK" = 1 ] && [ "$VER_OK" = 1 ] && write_result false
  release_all
  [ "$RESUMABLE" = 1 ] && [ "$RECOVER_ONLY" != 1 ] && resume_old
  [ "$LOG_OK" = 1 ] && log "=== 종료 (성공: false) ==="
  exit 2
}

resume_old() {   # 옛 버전으로 계속 — 본체가 스스로 내려가는 중이면(수동 적용) 기다렸다가 다시 띄운다. 죽이지는 않는다:
  if [ "$HOST_GONE" != 1 ] && is_our_host "$WaitPid"; then   # 재기동 자동 적용이면 본체는 스스로 내려가지 않는다
    log "본체(PID $WaitPid)가 내려가기를 기다립니다(죽이지 않음)..."
    if ! wait_gone "$WaitPid" "$WaitSeconds"; then
      log "본체(PID $WaitPid)가 계속 떠 있어 그대로 둡니다(옛 버전)"
      return 0
    fi
  fi
  log "옛 버전으로 다시 띄웁니다"
  restart_host 0
  return 0
}

abort_before_swap() {   # 교체 전 중단 — 설치 폴더는 이전 그대로. 잠금·표식을 먼저 풀어 런처가 평소대로 판단하게(잔재 복구 포함)
  log "$1"
  if [ -n "$SWAP" ] && ! { swap_undo "$SWAP" && swap_drop "$SWAP"; }; then   # (지금 부르는 자리엔 옮긴 것이 없다 — 방어)
    log "[FAIL] 교체 저널을 정리하지 못했습니다 — $SWAP 를 남깁니다(sh \"$InstallRoot/install.sh\" 로 복구하세요)"
    write_result false
    finish false 1
  fi
  SWAP=
  write_result false
  release_all
  resume_old
  log "=== 종료 (성공: false) ==="
  exit 1
}

stop_point() {   # TERM 은 교체 전까지만 듣는다
  [ "$STOP_REQ" = 1 ] || return 0
  abort_before_swap "[FAIL] TERM 을 받아 업데이트를 중단합니다(교체 전) — 옛 버전으로 계속합니다"
}

wait_host() {   # 2. 본체 종료 대기 — 안 죽으면 이 설치본의 Host 인지(명령줄) 확인한 뒤에만 TERM, 10초 뒤 KILL
  log "본체(PID $WaitPid) 종료 대기..."
  NAP_T=0
  while pid_alive "$WaitPid" && [ "$NAP_T" -lt $(( WaitSeconds * 2 )) ]; do
    stop_point
    beat
    nap
  done
  stop_point
  if pid_alive "$WaitPid"; then
    if is_our_host "$WaitPid"; then
      log "시간 내에 안 죽어서 강제 종료합니다."
      kill -TERM "$WaitPid" 2>/dev/null
      if ! wait_gone "$WaitPid" 10; then
        log "  10초 뒤에도 남아 있어 SIGKILL 합니다"
        kill -KILL "$WaitPid" 2>/dev/null
        wait_gone "$WaitPid" 5 || log "  [WARN] 강제 종료 뒤에도 남아 있습니다(pid $WaitPid)"
      fi
    else
      log "PID $WaitPid 는 이 설치본의 Host 가 아닙니다(pid 재사용) — 끝난 것으로 보고 진행합니다"
    fi
  fi
  HOST_GONE=1
  beat
}

backup() {   # 3. 교체 대상만 백업 — .tmp 에 다 만든 뒤 rename(완성된 백업만 정식 이름). 실패면 설치 폴더는 그대로 두고 중단
  _bt="$BackupDir.tmp.$$"
  _bk=1
  rm_guarded "$_bt" || _bk=0
  if [ "$_bk" = 1 ] && ! mkdir "$_bt" 2>>"$LogFile"; then _bk=0; fi
  for _bn in $PAYLOAD start-connector.sh; do       # 런처도(PAYLOAD 밖 — 교체 저널 없이 되돌릴 때 쓴다)
    [ "$_bk" = 1 ] || break
    exists "$InstallRoot/$_bn" || continue
    if ! cp -pR "$InstallRoot/$_bn" "$_bt/$_bn" 2>>"$LogFile"; then
      log "  복사 실패: $_bn"
      _bk=0
    fi
  done
  if [ "$_bk" = 1 ] && { [ ! -f "$_bt/session_manager/__init__.py" ] || [ ! -f "$_bt/pyproject.toml" ]; }; then
    log "  백업에 session_manager/__init__.py·pyproject.toml 이 없습니다"
    _bk=0
  fi
  if [ "$_bk" = 1 ]; then
    rm_guarded "$BackupDir" && mv_new "$_bt" "$BackupDir" || _bk=0
  fi
  if [ "$_bk" != 1 ]; then
    rm_guarded "$_bt"
    abort_before_swap "[FAIL] 백업 실패 - 되돌릴 수 없으므로 여기서 멈춥니다"
  fi
  log "백업 완료: $BackupDir"
  beat
}

restore_from_backup() {   # 8c. 교체 저널이 없을 때(밖에서 지워졌을 때) — 백업을 원본으로 4a·4b 를 다시 한다
  log "교체 저널이 없어 백업으로 되돌립니다: $BackupDir"
  [ -f "$BackupDir/session_manager/__init__.py" ] || { log "[FAIL] 백업이 없습니다: $BackupDir"; return 1; }
  swap_prepare "$BackupDir" "$OLD_VER" "$Version" || return 1
  swap_moves || return 1
  commit_swap
  launcher_restore "$BackupDir/start-connector.sh"
  return 0
}

deps_note() {   # 커밋할 때 — 새 버전 의존성을 잠금 밖(uv pip install -e)으로 맞췄으면 경고하고 last_apply 에 새 키로(S11) → RES_X
  RES_X=
  [ "$NEW_DEPS" = pip ] || return 0
  log "  [WARN] 새 버전 의존성은 uv.lock 밖(uv pip install -e)으로 설치했습니다 — 고정 버전과 다를 수 있습니다(sh \"$InstallRoot/install.sh\" 로 다시 맞출 수 있음)"
  RES_X=',"deps_fallback":"pip"'
  return 0
}

# 사용자가 멈춤(maintenance.flag — stop --hold): 재기동 직전에, 또는 띄운 뒤 헬스가 응답 없이 끝났을 때 표식이 있으면 롤백하지
# 않는다. 롤백하면 멀쩡할 수 있는 새 버전을 버리고 last_apply ok=false 로 그 버전을 계속 건너뛴다(옛 버전 재기동도 표식에
# 막혀 Host 는 어차피 꺼져 있다). 교체·의존성 동기화는 끝났으니 커밋하고 '보류'로 끝낸다 — last_apply 는 ok=true·held=true
# (updater.recently_failed 는 ok=false 만 본다). 다시 켜면(resume) 새 버전이 뜬다. 헬스를 거치지 않았으니 새 버전이 못 뜨면
# 자동 롤백은 없다(백업은 $BackupDir 에 남는다 — install.sh 로 다시 설치). 버전이 어긋난 대답(2)·런처 실패는 그대로 롤백
hold_commit() {
  log "maintenance.flag 가 있어(사용자가 멈춤 — stop --hold) 롤백하지 않고 새 버전으로 둡니다 — 다시 켜기: resume"
  commit_swap
  deps_note
  write_result true "" "$RES_X,\"held\":true"
  log "업데이트 적용 - 보류(헬스체크 없이 커밋, $Version)"
  finish true 0
}

rollback() {   # 8. 실패한 새 버전을 되돌린다 — 돌아오지 않는다
  log "롤백합니다."
  write_result false                              # 먼저 — 다시 띄운 옛 Host 의 재기동 자동 적용이 같은 버전을 또 받지 않게
  _rf="$BackupDir-failed-$Version"                # a. 실패본 보관(원인을 봐야 하니까)
  if rm_guarded "$_rf" && mkdir "$_rf" 2>>"$LogFile"; then
    for _rn in $PAYLOAD; do
      exists "$InstallRoot/$_rn" || continue
      cp -pR "$InstallRoot/$_rn" "$_rf/$_rn" 2>>"$LogFile" || log "  [WARN] 실패본 보관 실패: $_rn"
    done
  else
    log "  [WARN] 실패본 보관 자리를 만들지 못했습니다: $_rf"
  fi
  stop_failed_hosts                               # b.
  if [ -n "$SWAP" ] && exists "$SWAP"; then        # c. 복원 → d. uv sync → 저널 정리(sync 뒤 — 사이에 죽으면 복구가 다시 맞춘다)
    if ! swap_undo "$SWAP"; then
      log "[FAIL] 롤백 실패: 교체를 되돌리지 못했습니다 — $SWAP 를 남깁니다(sh \"$InstallRoot/install.sh\" 로 복구하세요)"
      finish false 1
    fi
    log "이전 버전 복원 완료"
    sync_deps "롤백" || log "  [WARN] 롤백 의존성 동기화 실패"
    swap_drop "$SWAP" || log "  [WARN] 교체 저널을 다 지우지 못했습니다: $SWAP"
    SWAP=
  else
    if ! restore_from_backup; then
      log "[FAIL] 롤백 실패: 백업으로 되돌리지 못했습니다(sh \"$InstallRoot/install.sh\" 로 복구하세요)"
      finish false 1
    fi
    log "이전 버전 복원 완료"
    sync_deps "롤백" || log "  [WARN] 롤백 의존성 동기화 실패"
  fi
  launcher_forget "$Version"
  _rb=1                                           # e. 옛 버전 재기동·헬스 — 옛 버전을 기대(옛 코드가 새 버전을 말하는 어긋남도 잡는다)
  H_PRE=$(json_pid "$DATA/runtime.json")          #    (재기동 직전 runtime.json 의 pid — 실패한 새 Host — 의 대답은 치지 않는다)
  if restart_host 1; then
    health_check "$OLD_VER"
    _rb=$?
  fi
  case "$_rb" in
    0) log "롤백 후 정상 동작 확인" ;;
    2) log "[FAIL] 롤백 후에도 응답이 없습니다 - 수동 확인이 필요합니다. 버전 표기가 어긋납니다(응답 ${H_RES##* }, 기대 ${OLD_VER:-?}) — sh \"$InstallRoot/install.sh\" 로 다시 설치하세요" ;;
    *) log "[FAIL] 롤백 후에도 응답이 없습니다 - 수동 확인이 필요합니다." ;;
  esac
  finish false 1
}

parse_args() {
  A_ERR=
  while [ $# -gt 0 ]; do
    case "$1" in
      -RecoverOnly) RECOVER_ONLY=1; shift; continue ;;
      -InstallRoot|-StageDir|-BackupDir|-WaitPid|-Port|-Version|-LogFile|-RestartScript|-WaitSeconds|-HealthSeconds) ;;
      *) A_ERR="모르는 인자: $1"; return 0 ;;
    esac
    if [ $# -lt 2 ]; then
      A_ERR="$1 의 값이 없습니다"
      return 0
    fi
    case "$1" in
      -InstallRoot) InstallRoot=$2 ;;
      -StageDir) StageDir=$2 ;;
      -BackupDir) BackupDir=$2 ;;
      -WaitPid) WaitPid=$2 ;;
      -Port) Port=$2 ;;
      -Version) Version=$2 ;;
      -LogFile) LogFile=$2 ;;
      -RestartScript) RestartScript=$2 ;;
      -WaitSeconds) WaitSeconds=$2 ;;
      -HealthSeconds) HealthSeconds=$2 ;;
    esac
    shift 2
  done
  return 0
}

check_args() {   # 0. 인자 검증 — 빈 값·상대 경로·제어문자면 아무것도 지우거나 옮기지 않고 종료코드 2
  if path_ok "$LogFile"; then                       # 로그 자리부터(그래야 아래 실패도 기록이 남는다)
    WORK=$(dirname "$LogFile")
    DATA=$(dirname "$WORK")
    [ -d "$WORK" ] && LOG_OK=1
  fi
  [ -n "$A_ERR" ] && fail_args "$A_ERR"
  [ "$LOG_OK" = 1 ] || fail_args "-LogFile 은 있는 폴더 안의 절대 경로여야 합니다: '$LogFile'"
  if [ "$RECOVER_ONLY" = 1 ]; then
    log "=== 중단된 업데이트 복구(-RecoverOnly) ==="
  else
    case "$Version" in [0-9]*) ;; *) fail_args "-Version 이 버전 모양이 아닙니다" ;; esac
    case "$Version" in *[!0-9A-Za-z.+-]*) fail_args "-Version 에 쓸 수 없는 글자가 있습니다" ;; esac
    [ "${#Version}" -le 64 ] || fail_args "-Version 이 너무 깁니다"
    VER_OK=1
    log "=== 업데이트 적용 시작 -> $Version ==="
    log "  설치폴더 : $InstallRoot"
    log "  스테이지 : $StageDir"
  fi
  trim_slash "$InstallRoot"
  InstallRoot=$T_OUT
  path_ok "$InstallRoot" || fail_args "-InstallRoot 는 절대 경로여야 합니다: '$InstallRoot'"
  if [ "$InstallRoot" = / ] || [ "$InstallRoot" = "$HOME" ]; then fail_args "-InstallRoot 가 / 나 홈입니다: $InstallRoot"; fi
  [ -d "$InstallRoot" ] || fail_args "설치 폴더가 없습니다: $InstallRoot"
  VENV_PY="$InstallRoot/.venv/bin/python"
  if [ "$RECOVER_ONLY" != 1 ]; then
    if [ -n "$RestartScript" ] && ! path_ok "$RestartScript"; then fail_args "-RestartScript 는 절대 경로여야 합니다: '$RestartScript'"; fi
    num_ok "$WaitPid" && [ "$WaitPid" != 0 ] || fail_args "-WaitPid 가 잘못됐습니다: '$WaitPid'"
    num_ok "$WaitSeconds" || fail_args "-WaitSeconds 가 잘못됐습니다: '$WaitSeconds'"
    [ -d "$InstallRoot/session_manager" ] || fail_args "설치 폴더에 session_manager 가 없습니다: $InstallRoot"
    RESUMABLE=1                                     # 여기부터는 설치 루트가 멀쩡하다 — 실패해도 옛 버전으로 계속
    trim_slash "$StageDir"
    StageDir=$T_OUT
    path_ok "$StageDir" && [ "$StageDir" != / ] || fail_args "-StageDir 는 절대 경로여야 합니다: '$StageDir'"
    trim_slash "$BackupDir"
    BackupDir=$T_OUT
    path_ok "$BackupDir" || fail_args "-BackupDir 는 절대 경로여야 합니다: '$BackupDir'"
    [ "$(dirname "$BackupDir")" = "$WORK" ] || fail_args "-BackupDir 는 로그 폴더($WORK) 바로 아래여야 합니다: $BackupDir"
    case "${BackupDir##*/}" in .*|'') fail_args "-BackupDir 이름이 잘못됐습니다: $BackupDir" ;; esac
    BACKUP_OK=1
    num_ok "$Port" && [ "$Port" -ge 1 ] && [ "$Port" -le 65535 ] || fail_args "-Port 가 잘못됐습니다: '$Port'"
    num_ok "$HealthSeconds" && [ "$HealthSeconds" -ge 1 ] || fail_args "-HealthSeconds 가 잘못됐습니다: '$HealthSeconds'"
  fi
  if [ -e "$InstallRoot/.git" ] || [ -d "$InstallRoot/ops" ]; then
    fail_args "개발 체크아웃(.git·ops)입니다 — 소스 트리를 덮어쓰지 않습니다: $InstallRoot"
  fi
  if [ "$RECOVER_ONLY" = 1 ]; then
    if [ ! -d "$InstallRoot/session_manager" ]; then   # 교체 도중 죽었으면 session_manager 가 SWAP/old 에 있을 수 있다
      _ck=0
      for _cs in "$InstallRoot"/.clewpath-swap.*; do [ -f "$_cs/info" ] && _ck=1; done
      [ "$_ck" = 1 ] || fail_args "설치 폴더에 session_manager 도 교체 저널도 없습니다: $InstallRoot"
    fi
    ROOT_OK=1
    return 0
  fi
  [ -d "$StageDir/session_manager" ] && [ -f "$StageDir/pyproject.toml" ] \
    || fail_args "스테이지에 session_manager·pyproject.toml 이 없습니다: $StageDir"
  _cv=$(pyproject_version "$StageDir/pyproject.toml")
  [ "$_cv" = "$Version" ] || fail_args "스테이지 버전($_cv)이 -Version($Version)과 다릅니다"
  ROOT_OK=1
  return 0
}

main() {
  unset CLEWPATH_UPDATER                            # 러너 자신의 재기동에서만 붙인다
  trap ':' HUP INT
  trap 'STOP_REQ=1' TERM
  STOP_REQ=0
  RECOVER_ONLY=0
  InstallRoot=''
  StageDir=''
  BackupDir=''
  WaitPid=''
  Port=5100
  Version=''
  LogFile=''
  RestartScript=''
  WaitSeconds=60
  HealthSeconds=45
  LOG_OK=0
  VER_OK=0
  ROOT_OK=0
  BACKUP_OK=0
  RESUMABLE=0
  HAVE_LOCK=0
  HAVE_MARK=0
  KEEP_APPLY_LOCK=0
  HOST_GONE=0
  WORK=''
  DATA=''
  HELPER=''
  SWAP=''
  TRASH=''
  OLD_VER=''
  NAP_T=0
  H_PRE=''
  H_RES=none
  SYNC_VIA=none
  NEW_DEPS=none
  RES_X=''
  parse_args "$@"
  check_args
  H_HOME=$(dirname "$DATA")                         # Host 의 claude_home(데이터 폴더 = claude_home/session_manager) — 헬스가 대조

  take_lock_and_mark                                # 1.

  if [ "$RECOVER_ONLY" = 1 ]; then
    _lh=$(swap_dirs_left && host_pids | tr '\n' ' ')
    case "$_lh" in
      *[0-9]*) log "이 설치본의 Host 가 떠 있어(pid ${_lh% }) 지금은 되돌리지 않습니다 — 그 Host 가 이 파일로 돌고 있다. 내려간 뒤(재기동·다음 업데이트) 되돌립니다"
               finish false 3 ;;
    esac
    if recover_leftovers; then
      [ "$RV_DONE" = 1 ] || log "교체 잔재가 없습니다"
      finish true 0
    fi
    log "[FAIL] 교체 잔재를 되돌리지 못했습니다 — sh \"$InstallRoot/install.sh\" 로 복구하세요"
    finish false 1
  fi

  HELPER="$WORK/.runner-helper.$$.py"
  { printf '%s\n' "$HELPER_PY" >"$HELPER"; } 2>/dev/null || { rm -f "$HELPER" 2>/dev/null; HELPER=; }
  beat
  wait_host                                         # 2.
  if ! recover_leftovers; then                      # 지난 실행의 잔재 — Host 가 내려간 뒤에, 백업보다 먼저
    log "[FAIL] 지난 업데이트의 교체 잔재를 되돌리지 못해 멈춥니다 — sh \"$InstallRoot/install.sh\" 로 복구하세요"
    write_result false
    finish false 1
  fi
  stop_point
  backup                                            # 3.
  OLD_VER=$(pyproject_version "$BackupDir/pyproject.toml")
  stop_point
  swap_prepare "$StageDir" "$Version" "$OLD_VER" \
    || abort_before_swap "[FAIL] 교체 준비 실패(새 항목 복사) — 설치 폴더는 그대로입니다. 옛 버전으로 계속합니다"
  stop_point                                        # (안에서 SWAP 을 치운다)
  swap_moves                                        # 4. 여기부터는 TERM 이 와도 끝까지
  case $? in
    0) log "파일 교체 완료" ;;
    1) abort_before_swap "옛 버전으로 계속합니다" ;;
    *) write_result false; finish false 1 ;;
  esac
  beat
  if ! sync_deps "새 버전 $Version"; then           # 5. 두 번 다 실패면 곧바로 롤백(헬스에 맡기지 않는다)
    log "[FAIL] 의존성 동기화 실패(새 버전 $Version)"
    rollback
  fi
  NEW_DEPS=$SYNC_VIA
  [ -e "$DATA/maintenance.flag" ] && hold_commit     # 6. 재기동 → 7. 헬스. 그새 사용자가 멈췄으면(stop --hold) 보류(돌아오지 않는다)
  M_HC=1
  M_UP=0
  H_PRE=$(json_pid "$DATA/runtime.json")            # 재기동 직전 runtime.json 의 pid — 그 pid 의 대답은 통과로 치지 않는다
  if restart_host 1; then
    M_UP=1
    health_check "$Version"
    M_HC=$?
  else
    log "재기동이 안 돼 헬스체크를 건너뜁니다"
  fi
  if [ "$M_HC" = 0 ]; then
    commit_swap
    deps_note
    log "헬스체크 통과 - 업데이트 성공 ($Version)"
    write_result true "" "$RES_X"
    [ "$STOP_REQ" = 1 ] && log "(교체가 시작된 뒤 TERM 을 받아 끝까지 진행했습니다)"
    finish true 0
  fi
  if [ "$M_UP" = 1 ] && [ "$M_HC" = 1 ] && [ -e "$DATA/maintenance.flag" ]; then
    hold_commit                                     # 헬스 대기 중에 멈췄다(응답 없음) — 롤백하지 않는다
  fi
  log "헬스체크 실패 (port=$Port, runtime.json port=$(runtime_port))"
  [ "$M_HC" = 2 ] && log "  응답한 버전이 다릅니다: ${H_RES##* } (기대 $Version)"
  rollback                                          # 8.
}

main "$@"
