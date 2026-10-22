#!/bin/sh
# ClewPath Host — 제거 스크립트(맥·리눅스). README 의 수동 제거 절차를 스크립트로 — docs/designs/posix-install.md 3.5
#
#   sh uninstall.sh                  확인을 묻고(터미널) 멈춤 → 설치 폴더의 ClewPath 항목 삭제. 데이터(기기·자격증명)와 uv 는 남긴다
#   sh uninstall.sh --yes            묻지 않고
#   sh uninstall.sh --purge-data     데이터 폴더(~/.claude/session_manager — 기기 페어링·자격증명·설정)도 지운다
#   sh uninstall.sh --purge-uv       ClewPath 가 받은 uv·파이썬 런타임($CLEWPATH_HOME/bin·python)도 지운다
#   sh uninstall.sh --purge          둘 다
#
# 순서: 확인 → maintenance.flag(감시·launchd 가 다시 띄우지 않게) → 감시(watch)·Host·터미널 관리 프로세스(브로커) 정지
#   → [mac] 자동 시작(launchd) 내리기·등록 파일 삭제(이 설치본을 부를 때만) → 설치 폴더의 우리 항목 삭제(비면 폴더도)
#   → (선택) uv·데이터 삭제 → 남은 것 보고
#
# 지키는 것:
#  - 클로드의 파일은 건드리지 않는다: ~/.claude/projects 는 어떤 경우에도 그대로, settings.json 은 읽기만 한다 —
#    ClewPath 가 등록한 훅이 남아 있으면 개수와 지우는 방법만 알려 준다(사장님 결정 2026-10-20)
#  - 지우는 자리는 정해 둔 것뿐(설치 폴더 안의 우리 이름 — our_name, 그 기록의 uv·파이썬 폴더, 이름이 session_manager 인
#    데이터 폴더, [mac] 이 설치본의 런처를 부르는 launchd 등록 파일). 설치 폴더에 남의 파일이 있으면 그것과 폴더는
#    남긴다(zip 을 ~/Downloads 에 풀고 설치했어도). 다른 설치본의 launchd 등록은 남긴다.
#    개발 체크아웃(.git·ops)·/·홈·클로드 폴더(와 그 위 — 물리 경로로도)는 거부한다. 업데이트·설치가 진행 중이면
#    멈춘다(러너가 끝에 Host 를 다시 띄운다)
#  - 지우다 끊기면(Ctrl+C·창 닫힘) maintenance.flag 를 남기고, 같은 명령으로 다시 실행해 마저 지운다 — 판정·다시 실행에
#    쓰는 것(session_manager·pyproject.toml·uninstall.sh·.clewpath-install)을 맨 끝에 지운다
#  - 이 파일이 설치 폴더 안에 있으므로 임시 사본으로 다시 실행한 뒤 지운다(지우는 동안 스크립트를 읽지 않게)
#  - 대화형 입력은 이 확인 하나뿐이고 /dev/tty 로 묻는다(curl | sh 처럼 표준 입력이 파이프여도)
# 종료코드: 0 다 지움 / 1 남은 것이 있음·중단·취소 / 2 인자 오류·확인할 터미널 없음

set -u

case "${HOME:-}" in
  /*) ;;
  *) printf '  [FAIL] %s\n' "HOME 이 절대 경로가 아닙니다: '${HOME:-}'"; exit 1 ;;
esac

NL='
'
# [mac] 자동 시작(launchd) 이름 — hostctl.LAUNCHD_LABEL·install.sh 와 같아야 한다(test_posix_scripts.test_launchd_names_agree).
# 등록 파일은 "$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
LAUNCHD_LABEL=com.clewpath.host

# 등록 파일(plist)이 부르는 런처들(…/start-connector.sh, 한 줄에 하나) — plistlib 이 XML 이스케이프(&amp; 등)·형식을 푼다.
# 설치본 venv 의 파이썬으로(-I, 표준 라이브러리만). 읽을 수 없는 파일이면 종료코드 3
PLIST_PY='
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as f:
        d = plistlib.load(f)
except Exception:
    sys.exit(3)
args = d.get("ProgramArguments") if isinstance(d, dict) else None
for a in args if isinstance(args, list) else []:
    if isinstance(a, str) and a.endswith("/start-connector.sh") and "\n" not in a:
        print(a)
'

# >>> clewpath-home (get.sh·install.sh·uninstall.sh·update_runner.sh 공통 — 바꾸면 넷 다)
case "$(uname -s)" in
  Darwin) CLEWPATH_HOME="${CLEWPATH_HOME:-$HOME/Library/Application Support/ClewPath}" ;;
  *)      _xdg="${XDG_DATA_HOME:-}"; case "$_xdg" in /*) ;; *) _xdg="$HOME/.local/share" ;; esac
          CLEWPATH_HOME="${CLEWPATH_HOME:-$_xdg/clewpath}" ;;
esac
# <<< clewpath-home

# ---------------------------------------------------------------- 출력·검사

say() { printf '%s\n' "$*"; }
ok() { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; }
die() { fail "$*"; exit 1; }
usage() { say "쓰는 법: sh uninstall.sh [--yes] [--purge-data] [--purge-uv] [--purge]"; }
usage_die() { fail "$*"; usage; exit 2; }

exists() { [ -e "$1" ] || [ -L "$1" ]; }

path_ok() {   # 절대 경로 — 제어문자·. ·.. 조각이 없다
  case "$1" in /*) ;; *) return 1 ;; esac
  case "$1" in *[[:cntrl:]]*) return 1 ;; esac
  case "$1/" in */./*|*/../*) return 1 ;; esac
  return 0
}

phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }
# ↑ 지금 폴더의 물리 경로 — 다섯 스크립트가 같은 본문. 맥 APFS 는 대소문자를 무시하고 bash 3.2 의 내장(-P)은 친 글자를
#   그대로 두므로 /bin/pwd(getcwd — 디스크의 글자)를 먼저 쓴다

under() {   # $1=자리 $2=경로 — $2 가 $1 자체이거나 그 아래인가(글자로, 끝의 / 하나는 없는 것으로)
  case "${2%/}/" in "${1%/}/"*) return 0 ;; esac
  return 1
}

safe_target() {   # $1=지울 자리 — 절대 경로이고 / 도, 홈이나 그 위도 아니다(글자로, 그리고 홈의 물리 경로로 — 끝의 /·
  path_ok "$1" || return 1   # 심볼릭 링크 홈·맥의 대소문자). rm -rf 앞의 마지막 가드
  case "$1" in /|"$HOME"|"$HOME/") return 1 ;; esac
  under "$1" "$HOME" && return 1
  under "$1" "${HOME_P:-$HOME}" && return 1
  return 0
}

root_guard() {   # 설치 폴더가 클로드 폴더·데이터 폴더(글자·물리 경로)이거나 그 위면 거부 — 우리 이름 session_manager 가
  _gc=$(dirname "$DATA_DIR")   # 곧 데이터 폴더가 된다(클로드 폴더에 zip 을 풀었을 때)
  for _g in "$DATA_DIR" "$_gc" "$(CDPATH='' cd "$DATA_DIR" 2>/dev/null && phys_pwd)" \
            "$(CDPATH='' cd "$_gc" 2>/dev/null && phys_pwd)"; do
    [ -n "$_g" ] && under "$ROOT" "$_g" && die "설치 폴더가 클로드 폴더·데이터 폴더이거나 그 위입니다 — 지우지 않습니다: $ROOT"
  done
  return 0
}

our_name() {   # $1=설치 폴더 바로 아래 이름 — ClewPath 가 놓거나 만드는 것(PAYLOAD 9개·설치가 만드는 것·일시 이름, 맥
  case "$1" in   # Finder 의 .DS_Store). install.sh 가 '남의 폴더'를 가리는 목록과 같다 — 바꾸면 둘 다
    session_manager|pwa|pyproject.toml|uv.lock|install.ps1|install.sh|uninstall.sh|README.md|LICENSE) return 0 ;;
    .venv|clewpath_host.egg-info|start-connector.sh|.clewpath-install) return 0 ;;
    .clewpath-swap.*|.clewpath-old.*|.clewpath-trash.*|.clewpath-install.*|.DS_Store) return 0 ;;
  esac
  return 1
}

ours_left() {   # 0 = 설치 폴더에 우리 이름이 아직 있다
  for _e in "$ROOT"/* "$ROOT"/.[!.]* "$ROOT"/..?*; do
    exists "$_e" && our_name "${_e##*/}" && return 0
  done
  return 1
}

foreign_names() {   # 설치 폴더의 우리 것이 아닌 이름들 — 쉼표로, 앞의 10개(나머지는 개수만)
  _fl='' _fn=0
  for _e in "$ROOT"/* "$ROOT"/.[!.]* "$ROOT"/..?*; do
    exists "$_e" || continue
    our_name "${_e##*/}" && continue
    _fn=$((_fn + 1))
    [ "$_fn" -le 10 ] && _fl="$_fl${_fl:+, }${_e##*/}"
  done
  [ "$_fn" -gt 10 ] && _fl="$_fl 외 $((_fn - 10))개"
  printf '%s' "$_fl"
}

is_install_root() {   # 1. ClewPath 설치 폴더인가 — 설치 기록의 첫 줄 표식(지우다 끊겨 session_manager 가 먼저 없어졌어도),
  case "$(sed -n '1p' "$ROOT/.clewpath-install" 2>/dev/null)" in   # 또는 session_manager + 이름이 clewpath-host 인 pyproject
    "# clewpath-install v1"*) return 0 ;;
  esac
  [ -d "$ROOT/session_manager" ] && grep -q '^name[[:space:]]*=[[:space:]]*"clewpath-host"' "$ROOT/pyproject.toml" 2>/dev/null
}

rec_get() {   # 설치 기록(.clewpath-install)의 key= 값 — sed 로만 읽는다(소스로 실행하지 않는다)
  [ -f "$ROOT/.clewpath-install" ] || return 0
  sed -n "s/^$1=//p" "$ROOT/.clewpath-install" 2>/dev/null | head -n 1
}

# ---------------------------------------------------------------- 프로세스(명령줄로 알아본다 — 런처·러너와 같은 규칙)

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

cmd_has() {   # $1=pid $2=글자 — 명령줄에 있나. 못 읽으면 있다고 본다(진행 중 표식을 함부로 시체로 보지 않게)
  _cc=$(cmdline_of "$1")
  [ -z "$_cc" ] && return 0
  case "$_cc" in *"$2"*) return 0 ;; esac
  return 1
}

is_ours() {   # $1=pid $2=모듈(session_manager.server|session_manager.ptyd) — 이 설치본의 venv 파이썬으로 그 모듈을 돌리나
  pid_alive "$1" || return 1
  if [ -d /proc/self ]; then
    _oc="$NL$(cmdline_of "$1")$NL"
    case "$_oc" in *"$NL$ROOT/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_oc" in *"$NL$2$NL"*) return 0 ;; esac
    [ "$2" = session_manager.server ] && case "$_oc" in *"/clewpath-host$NL"*) return 0 ;; esac
  else
    _oc=" $(cmdline_of "$1") "
    case "$_oc" in *" $ROOT/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_oc" in *" $2 "*) return 0 ;; esac
    [ "$2" = session_manager.server ] && case "$_oc" in *"/clewpath-host "*) return 0 ;; esac
  fi
  return 1
}

json_pid() {   # $1=파일 → "pid": N (파이썬 없이 — venv 가 깨져도 제거는 돌아야 한다)
  sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n 1
}

wait_gone() {   # $1=pid $2=초 — 그 안에 사라지면 0
  WG_END=$(( $(date +%s) + $2 ))
  while pid_alive "$1"; do
    [ "$(date +%s)" -ge "$WG_END" ] && return 1
    sleep 0.5 2>/dev/null || sleep 1
  done
  return 0
}

term_then_kill() {   # $1=pid $2=초 $3=이름 — SIGTERM, 그 안에 안 끝나면 SIGKILL
  kill -TERM "$1" 2>/dev/null
  if wait_gone "$1" "$2"; then
    ok "$3 를 멈췄습니다(pid $1)"
    return 0
  fi
  kill -KILL "$1" 2>/dev/null
  if wait_gone "$1" 5; then
    warn "$3 가 ${2}초 안에 끝나지 않아 강제 종료했습니다(pid $1)"
    return 0
  fi
  warn "$3 를 멈추지 못했습니다(pid $1)"
  return 1
}

proc_pids() {   # 명령줄에 $1 이 든 프로세스(나·부모·내 하위 셸 제외) — 한 줄에 pid 하나. 후보를 다 모은 뒤 하나씩
  if [ -d /proc/self ]; then                    # 다시 본다: 찾는 grep 자신도 그 글자를 인자로 가져 후보에 든다(끝나면 사라진다)
    _pc=$(grep -l -F -e "$1" /proc/[0-9]*/cmdline 2>/dev/null)
  else                                          # 맥 — $( ) 안의 case 패턴은 ( 로 연다: bash 3.2 는 짝 없는 ) 에서 끝낸다
    _pc=$(ps -A -ww -o pid= -o command= 2>/dev/null | while IFS= read -r _pl; do
            _pl=${_pl#"${_pl%%[! ]*}"}
            case "$_pl" in (*"$1"*) printf '%s\n' "${_pl%% *}" ;; esac
          done)
  fi
  _pme=$(cmdline_of "$$")                       # 하위 셸 안에서도 $$ 는 본 셸 — 하위 셸(같은 명령줄)은 명령줄로 거른다
  for _pp in $_pc; do
    _pp=${_pp#/proc/}
    _pp=${_pp%/cmdline}
    case "$_pp" in ''|*[!0-9]*|"$$"|"$PPID") continue ;; esac
    pid_alive "$_pp" || continue
    _pcl=$(cmdline_of "$_pp")
    [ -n "$_pme" ] && [ "$_pcl" = "$_pme" ] && continue
    case "$_pcl" in *"$1"*) printf '%s\n' "$_pp" ;; esac
  done
}

# ---------------------------------------------------------------- 진행 중 확인(설치·업데이트)

marker_busy() {   # 0 = 살아 있는 update.inprogress(러너·설치가 그 프로그램으로 살아 있고 30분 안)
  _mf="$DATA_DIR/update.inprogress"
  [ -f "$_mf" ] || return 1
  MK_PID=$(sed -n 's/^pid=\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_mf" 2>/dev/null | head -n 1)
  _ma=$(sed -n 's/^at=\([1-9][0-9]\{0,11\}\)[[:space:]]*$/\1/p' "$_mf" 2>/dev/null | head -n 1)
  MK_BY=$(sed -n 's/^by=\([a-z]*\)[[:space:]]*$/\1/p' "$_mf" 2>/dev/null | head -n 1)
  if [ -z "$MK_PID" ] || [ -z "$_ma" ]; then
    [ -z "$(find "$_mf" -mmin +1 2>/dev/null)" ] && return 0
    return 1
  fi
  pid_alive "$MK_PID" || return 1
  [ "$(( $(date +%s) - _ma ))" -lt 1800 ] || return 1
  case "$MK_BY" in
    runner) cmd_has "$MK_PID" update_runner ;;
    install) cmd_has "$MK_PID" install.sh ;;
    *) return 0 ;;
  esac
}

runner_live() {   # 업데이트 러너가 잠금(updates/runner.lock.d)을 쥐고 살아 있나
  _rl="$DATA_DIR/updates/runner.lock.d"
  [ -d "$_rl" ] || return 1
  _lp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_rl/pid" 2>/dev/null)
  if [ -z "$_lp" ]; then
    [ -n "$(find "$_rl" -maxdepth 0 -mmin +2 2>/dev/null)" ] && return 1
    return 0
  fi
  pid_alive "$_lp" && cmd_has "$_lp" update_runner
}

# ---------------------------------------------------------------- 단계

stop_watch() {   # 5. 감시(watch) — 잠금의 pid 가 런처의 watch 이고 이 설치본의 것일 때만 TERM(데이터 폴더를 같이 쓰는
  _wl="$DATA_DIR/watch.lock.d"   # 다른 설치본의 watch 는 둔다)
  [ -d "$_wl" ] || return 0
  _wp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_wl/pid" 2>/dev/null)
  pid_alive "$_wp" || return 0
  _wc=$(cmdline_of "$_wp")
  case "$_wc" in *start-connector*) ;; *) return 0 ;; esac
  case "$_wc" in *watch*) ;; *) return 0 ;; esac
  if [ -f "$_wl/root" ]; then                    # 런처가 적어 둔 그 watch 의 설치 폴더(물리 경로)
    _wr=$(sed -n '1p' "$_wl/root" 2>/dev/null)
    if [ "$_wr" != "$ROOT" ]; then
      ok "감시(watch)는 다른 설치본의 것이라 두었습니다(pid $_wp): $_wr"
      return 0
    fi
  else                                           # 옛 런처(root 없음) — 명령줄에 이 설치본의 런처 경로가 있을 때만
    case "$_wc" in
      *"$ROOT/start-connector.sh"*) ;;
      *) warn "감시(watch)가 이 설치본의 것인지 알 수 없어 두었습니다(pid $_wp) — 이 설치본의 것이면 직접 멈추세요: kill $_wp"
         return 0 ;;
    esac
  fi
  term_then_kill "$_wp" 5 "감시(watch)"
}

stop_host() {   # 6. Host — 런처 stop(SIGTERM → 15초 뒤 SIGKILL). 런처가 없으면 runtime.json 의 pid 가 이 설치본일 때만
  if [ -f "$ROOT/start-connector.sh" ]; then
    _so=$(/bin/sh "$ROOT/start-connector.sh" stop </dev/null 2>&1)
    _sl=$(printf '%s\n' "$_so" | sed -n '/^stopped (pid /p; /^not running$/p' | tail -n 1)
    _sp=$(printf '%s' "$_sl" | tr -dc '0-9')
    case "$_sl" in
      "stopped "*forced*) warn "Host(pid $_sp)가 15초 안에 끝나지 않아 강제로 멈췄습니다" ;;
      "stopped "*) ok "Host 를 멈췄습니다(pid $_sp)" ;;
      "not running") ok "Host 는 떠 있지 않습니다" ;;
      *) warn "Host 를 멈추지 못했을 수 있습니다: $_so" ;;
    esac
    return 0
  fi
  _hp=$(json_pid "$DATA_DIR/runtime.json")
  is_ours "$_hp" session_manager.server && term_then_kill "$_hp" 15 "Host"
  return 0
}

stop_broker() {   # 7. 터미널 관리 프로세스(브로커) — 이 설치본의 venv 로 돈 session_manager.ptyd 일 때만. 남은 소켓도 치운다
  _bj="$DATA_DIR/ptyd.json"
  [ -f "$_bj" ] || return 0
  _bp=$(json_pid "$_bj")
  if is_ours "$_bp" session_manager.ptyd; then
    term_then_kill "$_bp" 5 "터미널 관리 프로세스(브로커)"
  fi
  _ba=$(sed -n 's/.*"address"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_bj" 2>/dev/null | head -n 1)
  if [ -S "$_ba" ] && ! pid_alive "$_bp"; then
    case "${_ba##*/}" in ptyd.sock|clewpath-ptyd-*.sock) rm -f "$_ba" ;; esac
  fi
  return 0
}

sweep() {   # 남은 Host·브로커(같은 설치본의 venv) — ptyd.json·runtime.json 에 없던 것까지
  for _sp in $(proc_pids "$ROOT/.venv/bin/"); do
    if is_ours "$_sp" session_manager.server; then term_then_kill "$_sp" 10 "남은 Host"
    elif is_ours "$_sp" session_manager.ptyd; then term_then_kill "$_sp" 5 "남은 브로커"
    fi
  done
}

left_pid() {   # 14. $1=pid(정지 전 runtime.json·ptyd.json 의 것) $2=이름 $3=server|ptyd — 아직 살아 있으면 알린다.
  pid_alive "$1" || return 0   # 명령줄이 그 모듈이고 그 venv 가 이제 없으면(이 설치본을 다른 글자로 가리킨 것 — 맥 대소문자)
  _lc=$(cmdline_of "$1")       # 남은 것. 명령줄을 못 읽거나 venv 가 아직 있는 다른 설치본이면 경고만
  if [ -z "$_lc" ]; then
    warn "$2(pid $1)가 아직 살아 있습니다 — 명령줄을 읽지 못해 이 설치본의 것인지 확인하지 못했습니다"
    return 0
  fi
  case "$3:$_lc" in
    server:*session_manager.server*|server:*/clewpath-host*|ptyd:*session_manager.ptyd*) ;;
    *) return 0 ;;                               # pid 를 다른 프로그램이 다시 쓴다
  esac
  _lv=$(printf '%s\n' "$_lc" | sed -n 's|/\.venv/bin/.*||p' | head -n 1)
  _lo=$(printf '%s' "$_lc" | tr '\n' ' ' | cut -c 1-160)
  if [ -n "$_lv" ] && [ -d "$_lv/.venv" ]; then
    warn "$2(pid $1)는 다른 설치본의 것으로 보여 두었습니다: $_lo"
    return 0
  fi
  U_LEFT=1
  warn "$2(pid $1)가 아직 살아 있습니다 — 이 설치본의 것인데 멈추지 못했습니다: $_lo"
}

purge_uv() {   # 10. ClewPath 가 받은 uv·파이썬 런타임 — 우리가 둔 것만(CLEWPATH_HOME 이 남과 같이 쓰는 폴더여도 안전하게)
  _ch=$CLEWPATH_HOME
  safe_target "$_ch" || { warn "CLEWPATH_HOME 이 이상해 uv·파이썬을 지우지 않습니다: $_ch"; UV_SKIP=1; return 0; }
  if [ "$REC_UV" = "$_ch/bin/uv" ]; then         # 설치 기록이 이 자리 uv 를 썼다고 할 때만(--system-uv·남이 둔 uv 는 남긴다)
    UV_OURS=1
    for _f in "$_ch/bin/uv" "$_ch/bin/uvx" "$_ch"/bin/.uv.tmp.*; do
      exists "$_f" && rm -f "$_f"
    done
    [ -d "$_ch/bin" ] && rmdir "$_ch/bin" 2>/dev/null
    if exists "$_ch/bin/uv"; then warn "uv 를 지우지 못했습니다: $_ch/bin/uv"; else ok "uv 삭제: $_ch/bin"; fi
  elif exists "$_ch/bin/uv"; then
    warn "설치 기록의 uv(${REC_UV:-없음})가 이 자리가 아니어서 남깁니다: $_ch/bin/uv"
  fi
  _py="$_ch/python"
  if [ -d "$_py" ]; then
    _pok=1                                       # uv 관리 런타임 폴더의 모양(cpython-… 와 부 버전 링크, uv 가 늘 두는
    for _pe in "$_py"/* "$_py"/.[!.]* "$_py"/..?*; do   # .gitignore('*')·잠금·임시)만 있을 때 통째로
      exists "$_pe" || continue
      case "${_pe##*/}" in cpython-*|pypy-*|graalpy-*|.gitignore|.lock|.temp|.cache|.tmp*|bin) ;; *) _pok=0 ;; esac
    done
    if [ "$_pok" != 1 ]; then
      warn "다른 파일이 섞여 있어 파이썬 폴더는 남깁니다: $_py"
    elif rm -rf "$_py" 2>/dev/null && ! exists "$_py"; then
      ok "파이썬 런타임 삭제: $_py"
    else
      warn "파이썬 런타임을 다 지우지 못했습니다: $_py"
    fi
  fi
  for _g in "$_ch"/.clewpath-get.*; do         # 끊긴 get.sh 의 임시 폴더
    [ -d "$_g" ] && rm -rf "$_g"
  done
  rmdir "$_ch" 2>/dev/null && ok "ClewPath 폴더도 비어 지웠습니다: $_ch"
  say "         uv 캐시(~/.cache/uv, 맥 ~/Library/Caches/uv)는 다른 프로그램도 쓸 수 있어 남깁니다 — 필요하면 직접 지우세요."
}

launchd_ours() {   # 0 = [mac] 자동 시작 등록 파일이 있고 이 설치본의 런처를 부른다. LD_ARGS = 그 파일이 부르는 런처(알 수 있으면)
  LD_ARGS=
  exists "$LD_PLIST" || return 1
  _lw="$ROOT/start-connector.sh"
  if [ -x "$ROOT/.venv/bin/python" ] \
     && LD_ARGS=$("$ROOT/.venv/bin/python" -I -c "$PLIST_PY" "$LD_PLIST" </dev/null 2>/dev/null); then
    case "$NL$LD_ARGS$NL" in *"$NL$_lw$NL"*) return 0 ;; esac
    return 1
  fi
  # venv 가 없거나(지우다 끊긴 뒤 다시 실행) 파이썬이 못 읽으면 글자로 — plistlib 이 쓴 <string>…</string> 한 줄과 견준다.
  # 경로의 & < > 는 XML 이스케이프로 바꿔서(plistlib 이 쓰는 그대로)
  LD_ARGS=
  _le=$(printf '%s' "$_lw" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
  grep -q -F -e "<string>$_le</string>" "$LD_PLIST" 2>/dev/null
}

launchd_program_remove() {   # 등록 파일이 부르던 데이터 폴더의 프로그램(ClewPath) — 둘째 줄에 우리 표식(hostctl.LAUNCHD_PROGRAM_MARK)이
  # 있을 때만 지운다(옛 등록 /bin/sh -c 에는 없다 — 그때는 할 일이 없다)
  _lpg="$DATA_DIR/ClewPath"
  { [ -f "$_lpg" ] && [ ! -L "$_lpg" ]; } || return 0
  sed -n 2p "$_lpg" 2>/dev/null | grep -q -F 'clewpath-launchd v1' || return 0
  rm -f "$_lpg" 2>/dev/null
  exists "$_lpg" && warn "자동 시작 프로그램을 지우지 못했습니다: $_lpg"
  return 0
}

autostart_remove() {   # 8. [mac] 자동 시작(launchd) — 등록 파일이 이 설치본의 런처를 부를 때만 내리고(bootout) 지운다. 다른
  # 설치본의 것은 둔다. maintenance.flag(4)를 만든 뒤라 내리는 사이 launchd 가 부르는 foreground 는 아무것도 띄우지 않는다.
  # [linux] 자동 시작이 없다(systemd --user 는 결정 전)
  [ "$OS" = mac ] || return 0
  exists "$LD_PLIST" || return 0
  if ! launchd_ours; then
    warn "자동 시작(launchd) 등록은 이 설치본의 것이 아니라(또는 읽을 수 없어) 두었습니다: $LD_PLIST${LD_ARGS:+ ($(printf '%s' "$LD_ARGS" | tr '\n' ' '))}"
    return 0
  fi
  _ld="gui/$(id -u)/$LAUNCHD_LABEL"
  launchctl bootout "$_ld" </dev/null >/dev/null 2>&1        # 올라 있지 않으면 실패 — 상관없다
  rm -f "$LD_PLIST" 2>/dev/null
  if exists "$LD_PLIST"; then
    warn "자동 시작 등록 파일을 지우지 못했습니다: $LD_PLIST"
    return 0
  fi
  launchd_program_remove
  if launchctl print "$_ld" </dev/null >/dev/null 2>&1; then  # 내리지 못했다(SSH 셸 — 로그인한 화면 세션에 닿지 못함 등)
    LD_LOADED=1
    warn "자동 시작 등록 파일은 지웠지만 launchd 에는 아직 올라 있습니다 — 로그아웃하면 사라집니다"
    say "         지금 내리려면 로그인한 화면의 터미널에서: launchctl bootout $_ld"
    return 0
  fi
  ok "자동 시작(launchd) 해제: $LD_PLIST"
}

hooks_notice() {   # 13. 훅은 지우지 않는다(claude 파일 무접촉) — 남은 개수와 지우는 법만
  _cs="$(dirname "$DATA_DIR")/settings.json"   # 데이터 폴더의 부모 = Host 가 훅을 적은 클로드 폴더
  [ -f "$_cs" ] || return 0
  _hn=$(grep -c -F '/api/hooks/event' "$_cs" 2>/dev/null) || _hn=0
  case "$_hn" in ''|*[!0-9]*) _hn=0 ;; esac
  if [ "$_hn" = 0 ]; then
    ok "Claude 설정에 남은 ClewPath 훅 없음: $_cs"
    return 0
  fi
  warn "Claude 설정에 ClewPath 훅이 ${_hn}개 남아 있습니다(지우지 않았습니다): $_cs"
  say "         hooks 의 SessionStart·UserPromptSubmit·Notification·Stop·SessionEnd 에서 명령에"
  say "         '/api/hooks/event' 가 든 항목을 지우세요. 그대로 두면 claude 가 이벤트마다 닿지 않는 주소로"
  say "         curl 을 한 번 부릅니다(최대 3초, 세션 진행에는 영향 없음)."
  if [ -f "$_cs.bak-clewpath" ]; then
    say "         훅을 처음 등록하기 전의 원본: $_cs.bak-clewpath"
    say "         (참고용 — 그 뒤에 바뀐 설정이 있을 수 있으니 통째로 되돌리지는 마세요)"
  fi
}

confirm() {   # 3. /dev/tty 로 묻는다 — 표준 입력이 파이프여도 사람에게 묻는다. 터미널이 없으면 2
  if ! ( true </dev/tty ) 2>/dev/null; then
    fail "확인할 터미널이 없습니다 — 지울 것을 확인했다면 --yes 를 붙여 다시 실행하세요"
    exit 2
  fi
  say ""
  say "  다음을 지웁니다:"
  say "    - 설치 폴더의 ClewPath 항목: $ROOT"
  say "      (ClewPath 가 놓는 이름만 — session_manager·pwa·.venv·start-connector.sh 등. 다른 파일은 지우지 않습니다)"
  _cf=$(foreign_names)
  [ -n "$_cf" ] && say "      ClewPath 것이 아닌 항목이 있어 그것과 폴더는 남깁니다: $_cf"
  [ "$OS" = mac ] && launchd_ours && say "    - 자동 시작(launchd) 등록: $LD_PLIST"
  [ "$PURGE_UV" = 1 ] && say "    - uv·파이썬 런타임: $CLEWPATH_HOME/bin, $CLEWPATH_HOME/python"
  [ "$PURGE_DATA" = 1 ] && say "    - 데이터 폴더: $DATA_DIR"
  say "  열린 ClewPath 터미널(그 안의 claude)이 모두 끝납니다."
  [ "$PURGE_DATA" = 1 ] && say "  기기 페어링·자격증명·설정이 모두 지워져 폰을 다시 페어링해야 합니다."
  say "  ~/.claude 의 세션 기록(projects)은 지우지 않습니다."
  printf '  계속할까요? [y/N] ' >/dev/tty 2>/dev/null
  U_ANS=
  IFS= read -r U_ANS 2>/dev/null </dev/tty || U_ANS=
  case "$U_ANS" in
    y|Y|yes|YES|Yes) return 0 ;;
  esac
  say "  취소했습니다 — 아무것도 바꾸지 않았습니다."
  exit 1
}

remove_root() {   # 9. 설치 폴더 — 우리 이름만 지운다. 판정·다시 실행에 쓰는 넷은 맨 끝에(중간에 끊겨도 같은 명령으로 마저
  for _e in "$ROOT"/* "$ROOT"/.[!.]* "$ROOT"/..?*; do   # 지우게). 비면 폴더도, 남의 것이 있으면 그것과 폴더는 남긴다
    exists "$_e" || continue
    case "${_e##*/}" in session_manager|pyproject.toml|uninstall.sh|.clewpath-install) continue ;; esac
    our_name "${_e##*/}" && rm -rf "$_e" 2>/dev/null
  done
  for _n in session_manager pyproject.toml uninstall.sh .clewpath-install; do
    exists "$ROOT/$_n" && rm -rf "${ROOT:?}/$_n" 2>/dev/null
  done
  rmdir "$ROOT" 2>/dev/null
  if ! exists "$ROOT"; then
    ok "설치 폴더 삭제: $ROOT"
  elif ! ours_left; then
    warn "설치 폴더에 ClewPath 것이 아닌 항목이 있어 그것과 폴더는 남겼습니다: $ROOT"
    say "         남긴 것: $(foreign_names)"
  fi
  return 0
}

after_root() {   # 9 의 끝(또는 9 에서 끊김) — 우리 것이 남았으면 표식을 남기고(반쪽 설치본으로 Host 가 뜨지 않게) 다시
  if ours_left; then   # 실행을 안내한다. 다 지웠으면 표식을 거둔다 — 남겨 두면 다시 설치했을 때 첫 기동이 막힌다. $1=알림
    warn "$1"
    [ "$FLAG_MADE" = 1 ] && say "         maintenance.flag 는 남겨 둡니다 — 반쯤 지운 설치본으로 Host 가 뜨지 않게"
    if exists "$ROOT/uninstall.sh"; then
      say "         같은 명령을 다시 실행하면 마저 지웁니다: sh \"$ROOT/uninstall.sh\" --yes$U_OPTS"
    else
      say "         다시 설치(get.sh)한 뒤 제거하거나, 남은 것과 $DATA_DIR/maintenance.flag 를 직접 지우세요"
    fi
  elif [ "$FLAG_MADE" = 1 ]; then
    rm -f "$DATA_DIR/maintenance.flag"
  fi
  U_PHASE=finished
}

flag_undo() {   # 4 의 표식을 거둔다 — 아무것도 지우기 전에 멈췄을 때. 먼저 있던 것(stop --hold 등)이면 그 내용으로 되돌린다
  if [ "$FLAG_PRE" = 1 ]; then
    { printf '%s\n' "$FLAG_OLD" >"$DATA_DIR/maintenance.flag"; } 2>/dev/null
  else
    rm -f "$DATA_DIR/maintenance.flag"
  fi
}

on_exit() {   # 끝까지 못 간 제거의 표식 정리(U_PHASE: stopping = 표식 뒤 정지 중, removing = 설치 폴더를 지우는 중), 그리고 임시 사본
  case "${U_PHASE:-}" in
    stopping)
      [ "$FLAG_MADE" = 1 ] && flag_undo
      [ "$U_SIG" = 1 ] && warn "제거를 멈췄습니다 — 설치 폴더는 지우지 않았습니다(Host 를 멈췄으면 다시 띄우세요: sh \"$ROOT/start-connector.sh\" start)" ;;
    removing)
      after_root "제거가 중간에 끊겼습니다 — 설치 폴더에 ClewPath 항목이 남았습니다: $ROOT" ;;
  esac
  if [ -n "$SELF_TMP" ]; then
    case "${SELF_TMP##*/}" in clewpath-uninstall.*) rm -rf "$SELF_TMP" ;; esac
  fi
  return 0
}

# ---------------------------------------------------------------- 흐름

main() {
  ASSUME_YES=0
  PURGE_DATA=0
  PURGE_UV=0
  ROOT_ARG=''
  SELF_TMP=''
  U_PHASE=''
  U_SIG=0
  FLAG_MADE=0
  UV_SKIP=0
  UV_OURS=0
  REC_UV=''
  LD_ARGS=''
  LD_LOADED=0
  case "$(uname -s)" in Darwin) OS=mac ;; *) OS=linux ;; esac
  LD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes|-y) ASSUME_YES=1 ;;
      --purge-data) PURGE_DATA=1 ;;
      --purge-uv) PURGE_UV=1 ;;
      --purge) PURGE_DATA=1; PURGE_UV=1 ;;
      --root) [ $# -ge 2 ] || usage_die "--root 의 값이 없습니다"; ROOT_ARG=$2; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage_die "모르는 인자: $1" ;;
    esac
    shift
  done

  U_OPTS=''                                     # 다시 실행 안내에 붙일 선택
  [ "$PURGE_DATA" = 1 ] && U_OPTS="$U_OPTS --purge-data"
  [ "$PURGE_UV" = 1 ] && U_OPTS="$U_OPTS --purge-uv"

  # 1. 확인 — 설치 루트(물리 경로 — 맥은 디스크의 대소문자)가 ClewPath 설치본이어야 한다
  if [ -n "$ROOT_ARG" ]; then
    ROOT=$(CDPATH='' cd "$ROOT_ARG" 2>/dev/null && phys_pwd) || die "설치 폴더가 없습니다: $ROOT_ARG"
    _sd=$(CDPATH='' cd "$(dirname "$0")" 2>/dev/null && phys_pwd) || _sd=
    case "${_sd##*/}" in clewpath-uninstall.*) SELF_TMP=$_sd ;; esac
  else
    ROOT=$(CDPATH='' cd "$(dirname "$0")" && phys_pwd) || die "설치 폴더를 찾지 못했습니다: $0"
  fi
  trap 'on_exit' EXIT
  trap 'U_SIG=1; exit 1' INT TERM HUP
  HOME_P=$(CDPATH='' cd "$HOME" 2>/dev/null && phys_pwd) || HOME_P=$HOME
  safe_target "$ROOT" || die "설치 폴더가 / 나 홈(또는 그 위)이거나 이상한 경로입니다 — 지우지 않습니다: $ROOT"
  if [ -e "$ROOT/.git" ] || [ -d "$ROOT/ops" ]; then
    die "개발 체크아웃(.git·ops)입니다 — 소스 트리는 지우지 않습니다: $ROOT"
  fi
  is_install_root || die "ClewPath 설치 폴더가 아닙니다(설치 기록·session_manager·pyproject.toml 이 안 보임): $ROOT"
  if [ -z "$ROOT_ARG" ]; then                    # 이 파일은 지울 폴더 안에 있다 — 임시 사본으로 다시 실행한다
    command -v mktemp >/dev/null 2>&1 || die "mktemp 가 없습니다"
    _t=$(mktemp -d "${TMPDIR:-/tmp}/clewpath-uninstall.XXXXXX") || die "임시 폴더를 만들 수 없습니다(TMPDIR)"
    if ! cp "$ROOT/uninstall.sh" "$_t/uninstall.sh" 2>/dev/null; then
      rm -rf "$_t"
      die "임시 사본을 만들 수 없습니다: $_t"
    fi
    set -- --root "$ROOT"
    [ "$ASSUME_YES" = 1 ] && set -- "$@" --yes
    [ "$PURGE_DATA" = 1 ] && set -- "$@" --purge-data
    [ "$PURGE_UV" = 1 ] && set -- "$@" --purge-uv
    trap - EXIT INT TERM HUP
    exec /bin/sh "$_t/uninstall.sh" "$@"
  fi

  # 2. 정보 — 데이터 폴더는 런처의 말 → 설치 기록 → 경로 규칙, ClewPath 폴더는 설치 기록 → 경로 규칙
  DATA_DIR=
  [ -f "$ROOT/start-connector.sh" ] && DATA_DIR=$(/bin/sh "$ROOT/start-connector.sh" paths </dev/null 2>/dev/null | sed -n 's/^data_dir=//p' | head -n 1)
  path_ok "$DATA_DIR" || DATA_DIR=$(rec_get data_dir)
  path_ok "$DATA_DIR" || DATA_DIR="${SESSION_MANAGER_CLAUDE_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}/session_manager"
  path_ok "$DATA_DIR" || die "데이터 폴더를 정하지 못했습니다: $DATA_DIR"
  root_guard
  _rh=$(rec_get home)
  path_ok "$_rh" && CLEWPATH_HOME=$_rh
  REC_UV=$(rec_get uv)                           # 설치 폴더를 지운 뒤(10.)에는 기록을 못 읽는다 — 지금 읽어 둔다

  say ""
  say "=== ClewPath Host 제거 ==="
  say "  설치 폴더 : $ROOT"
  say "  데이터    : $DATA_DIR ($( [ "$PURGE_DATA" = 1 ] && printf '지움' || printf '남김' ))"
  say "  uv·파이썬 : $CLEWPATH_HOME ($( [ "$PURGE_UV" = 1 ] && printf '지움' || printf '남김' ))"

  if runner_live || marker_busy; then           # 러너는 끝에 Host 를 다시 띄우고, 설치는 파일을 놓는 중이다
    die "업데이트·설치가 진행 중입니다(pid ${MK_PID:-?}) — 끝난 뒤 다시 실행하세요"
  fi
  [ "$ASSUME_YES" = 1 ] || confirm              # 3.
  cd / || die "/ 로 옮겨 가지 못했습니다"         # 지울 폴더 안에 머물지 않게

  # 4. 감시가 다시 띄우지 못하게(업데이트 적용도 이 표식을 보면 거부한다). 여기부터 끊기면 on_exit 가 표식을 정리한다
  FLAG_PRE=0
  FLAG_OLD=''
  HP0=''
  BP0=''
  U_PHASE=stopping
  if [ -d "$DATA_DIR" ]; then
    if [ -f "$DATA_DIR/maintenance.flag" ]; then  # 먼저 있던 표식(stop --hold 등) — 멈추면 되돌린다
      FLAG_PRE=1
      FLAG_OLD=$(cat "$DATA_DIR/maintenance.flag" 2>/dev/null)
    fi
    FLAG_MADE=1
    if ! { printf 'by=uninstall\nat=%s\n' "$(date +%s)" >"$DATA_DIR/maintenance.flag"; } 2>/dev/null; then
      FLAG_MADE=0
      warn "maintenance.flag 를 만들지 못했습니다 — 감시(watch)가 다시 띄울 수 있습니다"
    fi
    if runner_live || marker_busy; then         # 묻는 사이 업데이트·설치가 시작됐다 — 표식을 거두고(on_exit) 멈춘다
      die "업데이트·설치가 진행 중입니다(pid ${MK_PID:-?}) — 끝난 뒤 다시 실행하세요"
    fi
    HP0=$(json_pid "$DATA_DIR/runtime.json")     # 14 에서 아직 살아 있나 본다
    BP0=$(json_pid "$DATA_DIR/ptyd.json")
    stop_watch                                   # 5.
    stop_host                                    # 6.
    stop_broker                                  # 7.
  fi
  sweep
  # 8~9. 여기부터 끊기면 표식을 남긴다(반쯤 지운 설치본으로 Host 가 뜨지 않게 — 다시 실행하면 마저 지운다)
  U_PHASE=removing
  autostart_remove                              # 8. [mac] launchd 내리기·등록 파일 삭제([linux] 자동 시작 없음)
  safe_target "$ROOT" && remove_root            # 9. 설치 폴더
  after_root "설치 폴더의 ClewPath 항목을 다 지우지 못했습니다: $ROOT"
  [ "$PURGE_UV" = 1 ] && purge_uv               # 10.
  if [ "$PURGE_DATA" = 1 ]; then                # 11. 이름이 session_manager 이고 클로드 폴더 자체가 아닐 때만
    case "$DATA_DIR" in
      */session_manager)
        if safe_target "$DATA_DIR" && [ "$DATA_DIR" != "$HOME/.claude" ]; then
          rm -rf "$DATA_DIR" 2>/dev/null
          if exists "$DATA_DIR"; then warn "데이터 폴더를 다 지우지 못했습니다: $DATA_DIR"; else ok "데이터 폴더 삭제: $DATA_DIR"; fi
        fi ;;
      *) warn "데이터 폴더 이름이 session_manager 가 아니어서 지우지 않습니다: $DATA_DIR" ;;
    esac
  fi                                            # (표식은 9 의 끝에서 거뒀다)
  # 12. ~/.claude/projects 는 어떤 경우에도 건드리지 않는다
  hooks_notice                                  # 13.

  # 14. 남은 것 — 설치 폴더의 우리 항목을 쓰는 프로세스(남긴 남의 파일을 연 프로그램은 빼고), 정지 전 runtime.json·
  #     ptyd.json 이 가리키던 Host·브로커, 우리 항목, (purge 때) 데이터·uv·파이썬
  U_LEFT=0
  _lf=''
  for _p in $(proc_pids "$ROOT/"); do
    _qc=$(cmdline_of "$_p")
    _qn=${_qc#*"$ROOT/"}
    _qn=${_qn%%"$NL"*}
    _qn=${_qn%%/*}
    our_name "${_qn%% *}" && _lf="$_lf $_p"
  done
  if [ -n "$_lf" ]; then
    U_LEFT=1
    warn "설치 폴더를 쓰는 프로세스가 남아 있습니다:"
    for _p in $_lf; do say "         pid $_p: $(cmdline_of "$_p" | tr '\n' ' ' | cut -c 1-120)"; done
  fi
  left_pid "$HP0" "Host" server
  left_pid "$BP0" "터미널 관리 프로세스(브로커)" ptyd
  ours_left && U_LEFT=1
  if [ "$OS" = mac ]; then                      # 이 설치본의 자동 시작 등록이 남았거나 launchd 에 아직 올라 있으면
    if launchd_ours; then warn "자동 시작 등록이 남아 있습니다: $LD_PLIST"; U_LEFT=1; fi
    [ "$LD_LOADED" = 1 ] && U_LEFT=1
  fi
  [ "$PURGE_DATA" = 1 ] && exists "$DATA_DIR" && U_LEFT=1
  if [ "$PURGE_UV" = 1 ]; then
    { [ "$UV_SKIP" = 1 ] || { [ "$UV_OURS" = 1 ] && exists "$CLEWPATH_HOME/bin/uv"; } || exists "$CLEWPATH_HOME/python"; } && U_LEFT=1
  fi
  say ""
  if [ "$U_LEFT" = 0 ]; then
    say "=== 제거 완료 ==="
    [ "$PURGE_DATA" = 1 ] || say "  데이터 폴더(기기·자격증명·설정)는 남겼습니다: $DATA_DIR — 지우려면 다시 --purge-data"
    exit 0
  fi
  say "=== 제거가 끝나지 않았습니다 — 위의 [WARN] 을 확인하세요 ==="
  exit 1
}

main "$@"
