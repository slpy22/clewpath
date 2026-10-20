#!/bin/sh
# ClewPath Host — 제거 스크립트(맥·리눅스). README 의 수동 제거 절차를 스크립트로 — docs/designs/posix-install.md 3.5
#
#   sh uninstall.sh                  확인을 묻고(터미널) 멈춤 → 설치 폴더 삭제. 데이터(기기·자격증명)와 uv 는 남긴다
#   sh uninstall.sh --yes            묻지 않고
#   sh uninstall.sh --purge-data     데이터 폴더(~/.claude/session_manager — 기기 페어링·자격증명·설정)도 지운다
#   sh uninstall.sh --purge-uv       ClewPath 가 받은 uv·파이썬 런타임($CLEWPATH_HOME/bin·python)도 지운다
#   sh uninstall.sh --purge          둘 다
#
# 순서: 확인 → maintenance.flag(감시가 다시 띄우지 않게) → 감시(watch)·Host·터미널 관리 프로세스(브로커) 정지
#   → 설치 폴더 삭제 → (선택) uv·데이터 삭제 → 남은 것 보고
#
# 지키는 것:
#  - 클로드의 파일은 건드리지 않는다: ~/.claude/projects 는 어떤 경우에도 그대로, settings.json 은 읽기만 한다 —
#    ClewPath 가 등록한 훅이 남아 있으면 개수와 지우는 방법만 알려 준다(사장님 결정 2026-10-20)
#  - 지우는 자리는 정해 둔 것뿐(설치 루트·그 기록의 uv·파이썬 폴더·이름이 session_manager 인 데이터 폴더). 개발 체크아웃
#    (.git·ops)·/·홈은 거부한다. 업데이트·설치가 진행 중이면 멈춘다(러너가 끝에 Host 를 다시 띄운다)
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

safe_target() {   # $1=지울 자리 — 절대 경로이고 / 도 홈도 아니다(rm -rf 앞의 마지막 가드)
  path_ok "$1" && [ "$1" != / ] && [ "$1" != "$HOME" ] && [ "$1" != "$HOME/" ]
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
  else
    _pc=$(ps -A -ww -o pid= -o command= 2>/dev/null | while IFS= read -r _pl; do
            _pl=${_pl#"${_pl%%[! ]*}"}
            case "$_pl" in *"$1"*) printf '%s\n' "${_pl%% *}" ;; esac
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

stop_watch() {   # 5. 감시(watch) — 잠금의 pid 가 런처의 watch 일 때만 TERM
  _wl="$DATA_DIR/watch.lock.d"
  [ -d "$_wl" ] || return 0
  _wp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_wl/pid" 2>/dev/null)
  pid_alive "$_wp" || return 0
  _wc=$(cmdline_of "$_wp")
  case "$_wc" in *start-connector*) ;; *) return 0 ;; esac
  case "$_wc" in *watch*) ;; *) return 0 ;; esac
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

purge_uv() {   # 10. ClewPath 가 받은 uv·파이썬 런타임 — 우리가 둔 것만(CLEWPATH_HOME 이 남과 같이 쓰는 폴더여도 안전하게)
  _ch=$CLEWPATH_HOME
  safe_target "$_ch" || { warn "CLEWPATH_HOME 이 이상해 uv·파이썬을 지우지 않습니다: $_ch"; return 0; }
  for _f in "$_ch/bin/uv" "$_ch/bin/uvx" "$_ch"/bin/.uv.tmp.*; do
    exists "$_f" && rm -f "$_f"
  done
  [ -d "$_ch/bin" ] && rmdir "$_ch/bin" 2>/dev/null
  if exists "$_ch/bin/uv"; then warn "uv 를 지우지 못했습니다: $_ch/bin/uv"; else ok "uv 삭제: $_ch/bin"; fi
  _py="$_ch/python"
  if [ -d "$_py" ]; then
    _pok=1                                       # uv 관리 런타임 폴더의 모양(cpython-… 와 uv 의 잠금·임시)만 있을 때 통째로
    for _pe in "$_py"/* "$_py"/.[!.]* "$_py"/..?*; do
      exists "$_pe" || continue
      case "${_pe##*/}" in cpython-*|pypy-*|graalpy-*|.lock|.temp|.cache|.tmp*|bin) ;; *) _pok=0 ;; esac
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
  say "    - 설치 폴더: $ROOT"
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

on_exit() {   # 임시 사본(clewpath-uninstall.XXXXXX)만 치운다
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

  # 1. 확인 — 설치 루트(물리 경로)가 ClewPath 설치본이어야 한다
  if [ -n "$ROOT_ARG" ]; then
    ROOT=$(CDPATH='' cd "$ROOT_ARG" 2>/dev/null && pwd -P) || die "설치 폴더가 없습니다: $ROOT_ARG"
    _sd=$(CDPATH='' cd "$(dirname "$0")" 2>/dev/null && pwd -P) || _sd=
    case "${_sd##*/}" in clewpath-uninstall.*) SELF_TMP=$_sd ;; esac
  else
    ROOT=$(CDPATH='' cd "$(dirname "$0")" && pwd -P) || die "설치 폴더를 찾지 못했습니다: $0"
  fi
  trap 'on_exit' EXIT
  trap 'exit 1' INT TERM HUP
  safe_target "$ROOT" || die "설치 폴더가 / 나 홈이거나 이상한 경로입니다: $ROOT"
  if [ -e "$ROOT/.git" ] || [ -d "$ROOT/ops" ]; then
    die "개발 체크아웃(.git·ops)입니다 — 소스 트리는 지우지 않습니다: $ROOT"
  fi
  if [ ! -d "$ROOT/session_manager" ] || ! grep -q '^name[[:space:]]*=[[:space:]]*"clewpath-host"' "$ROOT/pyproject.toml" 2>/dev/null; then
    die "ClewPath 설치 폴더가 아닙니다(session_manager·pyproject.toml 이 안 보임): $ROOT"
  fi
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
  _rh=$(rec_get home)
  path_ok "$_rh" && CLEWPATH_HOME=$_rh

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

  # 4. 감시가 다시 띄우지 못하게(업데이트 적용도 이 표식을 보면 거부한다)
  FLAG_MADE=0
  if [ -d "$DATA_DIR" ]; then
    if { printf 'by=uninstall\nat=%s\n' "$(date +%s)" >"$DATA_DIR/maintenance.flag"; } 2>/dev/null; then
      FLAG_MADE=1
    else
      warn "maintenance.flag 를 만들지 못했습니다 — 감시(watch)가 다시 띄울 수 있습니다"
    fi
    stop_watch                                   # 5.
    stop_host                                    # 6.
    stop_broker                                  # 7.
  fi
  sweep
  # 8. 자동 시작 해제 자리 — 다음 단계에서 [mac] launchctl bootout + plist 삭제, [linux] systemctl --user disable --now

  # 9. 설치 폴더
  if safe_target "$ROOT" && [ -d "$ROOT/session_manager" ]; then
    rm -rf "$ROOT" 2>/dev/null
    if exists "$ROOT"; then warn "설치 폴더를 다 지우지 못했습니다: $ROOT"; else ok "설치 폴더 삭제: $ROOT"; fi
  fi
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
  elif [ "$FLAG_MADE" = 1 ]; then               # 남겨 두면 다시 설치했을 때 첫 기동이 막힌다
    rm -f "$DATA_DIR/maintenance.flag"
  fi
  # 12. ~/.claude/projects 는 어떤 경우에도 건드리지 않는다
  hooks_notice                                  # 13.

  # 14. 남은 것
  U_LEFT=0
  _lp=$(proc_pids "$ROOT/")
  if [ -n "$_lp" ]; then
    U_LEFT=1
    warn "설치 폴더를 쓰는 프로세스가 남아 있습니다:"
    for _p in $_lp; do say "         pid $_p: $(cmdline_of "$_p" | tr '\n' ' ' | cut -c 1-120)"; done
  fi
  exists "$ROOT" && U_LEFT=1
  [ "$PURGE_DATA" = 1 ] && exists "$DATA_DIR" && U_LEFT=1
  [ "$PURGE_UV" = 1 ] && exists "$CLEWPATH_HOME/bin/uv" && U_LEFT=1
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
