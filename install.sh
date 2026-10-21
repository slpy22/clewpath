#!/bin/sh
# ClewPath Host — 설치 스크립트(맥·리눅스). install.ps1 의 짝 — docs/designs/posix-install.md 3.2
#
#   sh install.sh [옵션]                        이 파일이 있는 폴더(설치 루트)에 설치·재설치
#   CLEWPATH_CLIENT_TOKEN=… sh install.sh       토큰은 env 로(인자는 같은 PC 의 다른 사용자에게 ps 로 보인다)
#   (get.sh) sh <임시>/stage/install.sh --from-stage <임시>/stage --dir <설치 루트> …   받은 파일을 옮긴 뒤 이어 간다
#
# 하는 일: [1/6] uv(고정 버전·SHA-256) → [2/6] 파이썬([linux] 시스템 우선, [mac] uv 관리 런타임만) → [3/6] 클로드 폴더
#   → [4/6] 전용 .venv·의존성(uv.lock 고정) → [5/6] 설정·권한·런처·설치 기록·자격증명 → [6/6] (재)기동·확인
#
# 옵션: --cp-url URL --relay-url URL --app-url URL --port N --python 3.11 --uv-version X.Y.Z
#       --system-uv --reinstall --no-autostart --dry-run
#       --no-provision --allow-root --allow-http     (시험·오프라인용)
#       --client-token T --agent-token T             (받기는 하지만 경고하고 env 로 옮긴다)
# env:  CLEWPATH_CLIENT_TOKEN·CLEWPATH_AGENT_TOKEN 토큰 / CLEWPATH_UV_ARCHIVE 로컬 uv tar.gz(고정 해시표에 있어야 한다)
#       CLEWPATH_HOME·XDG_DATA_HOME 설치 위치 / SESSION_MANAGER_CLAUDE_HOME·CLAUDE_CONFIG_DIR 클로드 폴더 / UV_SYSTEM_CERTS
#
# 지키는 것:
#  - 대화형 입력을 받지 않는다(curl | sh 의 표준 입력은 이미 다 읽힌 파이프다). root 로는 설치하지 않는다(sudo 없음)
#  - 설치하는 동안 update.inprogress(by=install)를 쥔다 — 감독자(watch)·업데이트 러너가 그동안 Host 를 띄우거나 바꾸지 않게
#  - 설정·자격증명은 덮어쓰지 않는다. 비밀은 권한(데이터 폴더 0700·파일 0600)으로 지키고 umask 는 바꾸지 않는다 —
#    Host 와 그 안의 claude 는 사용자의 umask·env 를 그대로 물려받는다(uv 설정·토큰 env 는 Host 로 넘기지 않는다)
#  - 경로·설정 규칙은 파이썬(session_manager.hostctl)이 원본 — 셸은 venv 가 생기기 전에만 같은 식으로 추정한다
#  - 터미널 관리 프로세스(브로커)는 건드리지 않는다. 프로세스는 명령줄(설치 경로·모듈)로 알아본다
#  - set -x·env 덤프 금지, 토큰은 출력하지 않는다
# 종료코드: 0 설치됨(자격증명 발급·기동 확인 실패는 경고만) / 1 실패 / 2 인자 오류

set -u

case "${HOME:-}" in
  /*) ;;
  *) printf '  [FAIL] %s\n' "HOME 이 절대 경로가 아닙니다: '${HOME:-}'"; exit 1 ;;
esac

# uv 고정 버전 — install.ps1 의 $UvDefault 와 같아야 한다(test_posix_scripts.test_uv_pins_match_windows_installer)
UV_DEFAULT="0.12.1"
NL='
'
CLEWPATH_HOME_GIVEN=${CLEWPATH_HOME:-}           # 사용자가 준 값인가 — 아래 공통 블록이 기본값을 채운다(uv_ours 가 본다)

# >>> clewpath-home (get.sh·install.sh·uninstall.sh·update_runner.sh 공통 — 바꾸면 넷 다)
case "$(uname -s)" in
  Darwin) CLEWPATH_HOME="${CLEWPATH_HOME:-$HOME/Library/Application Support/ClewPath}" ;;
  *)      _xdg="${XDG_DATA_HOME:-}"; case "$_xdg" in /*) ;; *) _xdg="$HOME/.local/share" ;; esac
          CLEWPATH_HOME="${CLEWPATH_HOME:-$_xdg/clewpath}" ;;
esac
# <<< clewpath-home

phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }

# CLEWPATH_HOME 은 절대 경로이고 / 나 홈 그 자체가 아니어야 한다 — 그 아래 bin/uv·python 을 두고 제거(--purge-uv)가 지운다.
# 상대 경로면 지금 폴더에 uv 를 깔려다 엉뚱하게 실패했고, 홈이면 사용자의 ~/bin/uv 를 덮어썼다
case "$CLEWPATH_HOME" in
  *[[:cntrl:]]*) printf '  [FAIL] %s\n' "CLEWPATH_HOME 에 줄바꿈·제어문자가 있습니다"; exit 1 ;;
  /*) ;;
  *) printf '  [FAIL] %s\n' "CLEWPATH_HOME 은 절대 경로여야 합니다: '$CLEWPATH_HOME'"; exit 1 ;;
esac
_chp=$(CDPATH='' cd "$CLEWPATH_HOME" 2>/dev/null && phys_pwd) || _chp=$CLEWPATH_HOME
_hmp=$(CDPATH='' cd "$HOME" 2>/dev/null && phys_pwd) || _hmp=$HOME
if [ "$_chp" = / ] || [ "${_chp%/}" = "${_hmp%/}" ]; then
  printf '  [FAIL] %s\n' "CLEWPATH_HOME 이 / 나 홈 폴더입니다: '$CLEWPATH_HOME' — ClewPath 전용 폴더를 주세요(비워 두면 기본 위치)"
  exit 1
fi

# uv 배포본 해시 — 소스에 박아 두는 이유는 install.ps1 과 같다(내려받는 서버가 뚫려도 바뀐 파일을 안다).
# 값은 공식 릴리스의 .sha256 과 직접 내려받아 계산한 값이 같을 때만 적는다(2026-10-20, 0.12.1 네 대상 확인).
# 버전을 올릴 때: https://github.com/astral-sh/uv/releases/download/<버전>/uv-<대상>.tar.gz.sha256
uv_pinned_sha256() {   # $1=버전 $2=대상 → 64자(모르면 빈 출력)
  case "$1|$2" in
    "0.12.1|x86_64-unknown-linux-gnu")  echo "90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb" ;;
    "0.12.1|aarch64-unknown-linux-gnu") echo "769d373e146692c639b5fbaae33b331c297a32e03d30448772051902df52bbf4" ;;
    "0.12.1|x86_64-apple-darwin")       echo "69d9f9a00337f25a50dcb13882052da08b8469bac11091c98c5694c3c6721467" ;;
    "0.12.1|aarch64-apple-darwin")      echo "77d2906988e8074fd43f2f329ec452ebbf9b0c257ba1c66451c71de70a6baf42" ;;
    *) echo "" ;;
  esac
}

# ---------------------------------------------------------------- 출력·검사

say() { printf '%s\n' "$*"; }
ok() { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; }
die() { fail "$*"; exit 1; }
usage_die() { fail "$*"; say "         도움말: sh install.sh --help"; exit 2; }

usage() {
  say "쓰는 법: sh install.sh [--cp-url URL] [--relay-url URL] [--app-url URL] [--port N] [--python 3.11]"
  say "                       [--uv-version X.Y.Z] [--system-uv] [--reinstall] [--no-autostart] [--dry-run]"
  say "                       [--no-provision] [--allow-root] [--allow-http]"
  say "  토큰: CLEWPATH_CLIENT_TOKEN=… sh install.sh   (인자 --client-token 은 ps 에 보여 권하지 않습니다)"
}

shq() {   # 안내문에 그대로 붙여 쓸 수 있게 작은따옴표로 감싼다
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

exists() { [ -e "$1" ] || [ -L "$1" ]; }

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

check_url() {   # $1=값 $2=안전한 스킴(https|wss) $3=인자 이름 — 시험용 http·ws 는 --allow-http 일 때만
  case "$1" in *[[:cntrl:]]*|*[[:space:]]*|*'"'*|*"'"*|*'\'*) usage_die "$3 에 쓸 수 없는 글자가 있습니다" ;; esac
  case "$1" in "$2"://?*) return 0 ;; esac
  if [ "$ALLOW_HTTP" = 1 ]; then
    case "$2" in https) _cu=http ;; *) _cu=ws ;; esac
    case "$1" in "$_cu"://?*) HTTP_USED=1; return 0 ;; esac
  fi
  usage_die "$3 는 $2:// 주소여야 합니다(시험용 http 는 --allow-http): $1"
}

pyproject_version() {   # $1 = pyproject.toml → [project] 의 version
  sed -n '/^\[project\][[:space:]]*$/,/^\[/s/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1
}

is_clewpath_tree() {   # $1=폴더 — ClewPath 설치본인가(설치 기록, 또는 session_manager + 이름이 clewpath-host 인 pyproject)
  [ -f "$1/.clewpath-install" ] && return 0
  [ -d "$1/session_manager" ] && grep -q '^name[[:space:]]*=[[:space:]]*"clewpath-host"' "$1/pyproject.toml" 2>/dev/null
}

kv() {   # $1=키 $2=파일 — hostctl·런처의 key=value 출력에서 값 하나
  sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n 1
}

run_quiet() {   # $1=로그 파일, 나머지=명령 — 출력은 로그로(실패할 때만 꼬리를 보여 준다). 종료코드는 그 명령의 것
  RQ_LOG=$1
  shift
  "$@" </dev/null >"$RQ_LOG" 2>&1
}

show_tail() {   # $1=로그 $2=제목 [$3=줄 수(기본 25)]
  [ -s "$1" ] || return 0
  say "  -- $2 --"
  tail -n "${3:-25}" "$1" | sed 's/^/    /'
}

phys_of() {   # $1=경로 → PO_OUT: 폴더가 있으면 물리 경로, 없으면 겹친 / 와 끝 / 를 걷어 낸 글자(같은 폴더를 달리 쓴 것을 같게 본다)
  PO_OUT=$(CDPATH='' cd "$1" 2>/dev/null && phys_pwd) && [ -n "$PO_OUT" ] && return 0
  PO_OUT=$(printf '%s\n' "$1" | sed 's#//*#/#g; s#\(.\)/$#\1#')
  return 0
}

covers() {   # $1 이 $2 와 같거나 그 조상인가(둘 다 phys_of 로 맞춘 절대 경로)
  [ "$1" = / ] && return 0
  case "$2/" in "$1"/*) return 0 ;; esac
  return 1
}

sha256_of() {   # $1=파일 → 소문자 64자(도구가 없거나 실패하면 빈 출력). 표준 입력으로 넘긴다 — 파일 이름이 출력에 안 섞이게
  if command -v sha256sum >/dev/null 2>&1; then
    _sh=$(sha256sum <"$1" 2>/dev/null) || _sh=
  elif command -v shasum >/dev/null 2>&1; then
    _sh=$(shasum -a 256 <"$1" 2>/dev/null) || _sh=
  elif command -v openssl >/dev/null 2>&1; then
    _sh=$(openssl dgst -sha256 -r <"$1" 2>/dev/null) || _sh=
  else
    _sh=
  fi
  _sh=$(printf '%s' "${_sh%% *}" | tr 'A-F' 'a-f')
  case "$_sh" in *[!0-9a-f]*) _sh= ;; esac
  [ "${#_sh}" = 64 ] && printf '%s' "$_sh"
  return 0
}

fetch() {   # $1=URL $2=파일 $3=초 — https 만(리다이렉트로 http 로 내려가지 않게)
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --max-time "$3" -o "$2" "$1"
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

cmd_has() {   # $1=pid $2=글자 — 명령줄에 있나. 못 읽으면 있다고 본다(표식·잠금을 함부로 시체로 보지 않게)
  _cc=$(cmdline_of "$1")
  [ -z "$_cc" ] && return 0
  case "$_cc" in *"$2"*) return 0 ;; esac
  return 1
}

is_host_of() {   # $1=설치 루트 $2=pid — 그 설치본의 Host: 인자 하나는 "$1/.venv/bin/…", 다른 하나는 Host 모듈(또는 진입점)
  pid_alive "$2" || return 1
  if [ -d /proc/self ]; then
    _hc="$NL$(cmdline_of "$2")$NL"
    case "$_hc" in *"$NL$1/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_hc" in *"${NL}session_manager.server$NL"*|*"/clewpath-host$NL"*) return 0 ;; esac
  else
    _hc=" $(cmdline_of "$2") "
    case "$_hc" in *" $1/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_hc" in *" session_manager.server "*|*"/clewpath-host "*) return 0 ;; esac
  fi
  return 1
}

json_pid() {   # $1=파일 → "pid": N (파이썬 없이)
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

stop_host() {   # $1=설치 루트 — 그 설치본의 Host 를 정상 종료(SIGTERM — 정상 종료 표식·bye. 브로커는 그대로)
  if [ -f "$1/start-connector.sh" ]; then
    # 설치가 표식을 쥐고 있다 — 러너와 같은 통과 표시(stop 은 표식과 상관없이 돈다)
    _so=$(CLEWPATH_UPDATER=1 /bin/sh "$1/start-connector.sh" stop </dev/null 2>&1)
    _sl=$(printf '%s\n' "$_so" | sed -n '/^stopped (pid /p; /^not running$/p' | tail -n 1)
    _sp=$(printf '%s' "$_sl" | tr -dc '0-9')
    case "$_sl" in
      "stopped "*forced*) warn "Host(pid $_sp)가 15초 안에 끝나지 않아 강제로 멈췄습니다" ;;
      "stopped "*) ok "실행 중이던 Host 를 멈췄습니다(pid $_sp)" ;;
      "not running") ;;
      *) warn "Host 를 멈추지 못했을 수 있습니다: $_so" ;;
    esac
    return 0
  fi
  _sp=$(json_pid "$DATA_DIR/runtime.json")         # 런처가 없는 설치본(깨진 설치) — runtime.json 의 pid 가 그 설치본일 때만
  is_host_of "$1" "$_sp" || return 0
  kill -TERM "$_sp" 2>/dev/null
  if wait_gone "$_sp" 15; then
    ok "실행 중이던 Host 를 멈췄습니다(pid $_sp)"
  else
    kill -KILL "$_sp" 2>/dev/null
    wait_gone "$_sp" 5
    warn "Host(pid $_sp)가 15초 안에 끝나지 않아 강제 종료했습니다"
  fi
  return 0
}

# ---------------------------------------------------------------- 표식(update.inprogress)·잠금 — 런처·러너와 같은 모양

marker_info() {   # $1=표식 파일 → MK_PID MK_AT MK_BY (at 은 앞자리 0 금지 — $(( )) 가 8진수로 읽는다)
  MK_PID=$(sed -n 's/^pid=\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$1" 2>/dev/null | head -n 1)
  MK_AT=$(sed -n 's/^at=\([1-9][0-9]\{0,11\}\)[[:space:]]*$/\1/p' "$1" 2>/dev/null | head -n 1)
  MK_BY=$(sed -n 's/^by=\([a-z]*\)[[:space:]]*$/\1/p' "$1" 2>/dev/null | head -n 1)
}

marker_busy() {   # $1=데이터 폴더 — 0 = 다른 프로그램(러너·다른 설치)이 쥔 살아 있는 표식(pid 가 그 프로그램으로 살아 있고 30분 안)
  _mf="$1/update.inprogress"
  [ -f "$_mf" ] || return 1
  marker_info "$_mf"
  [ "$MK_PID" = "$$" ] && return 1
  if [ -z "$MK_PID" ] || [ -z "$MK_AT" ]; then
    [ -z "$(find "$_mf" -mmin +1 2>/dev/null)" ] && return 0      # 막 쓰는 중일 수 있다 — 1분은 산 것으로
    return 1
  fi
  pid_alive "$MK_PID" || return 1
  [ "$(( $(date +%s) - MK_AT ))" -lt 1800 ] || return 1
  case "$MK_BY" in
    runner) cmd_has "$MK_PID" update_runner ;;
    install) cmd_has "$MK_PID" install.sh ;;
    *) return 0 ;;
  esac
}

runner_live() {   # $1=데이터 폴더 — 업데이트 러너가 잠금(updates/runner.lock.d)을 쥐고 살아 있나
  _rl="$1/updates/runner.lock.d"
  [ -d "$_rl" ] || return 1
  _lp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_rl/pid" 2>/dev/null)
  if [ -z "$_lp" ]; then
    [ -n "$(find "$_rl" -maxdepth 0 -mmin +2 2>/dev/null)" ] && return 1
    return 0
  fi
  pid_alive "$_lp" && cmd_has "$_lp" update_runner
}

mark_take() {   # $1=데이터 폴더 — update.inprogress 를 내 것으로(임시 파일에 쓰고 mv — 반쯤 쓴 표식을 남이 읽지 않게)
  _mt="$1/.update.inprogress.$$.tmp"
  if ( umask 077; printf 'pid=%s\nat=%s\nby=install\n' "$$" "$(date +%s)" >"$_mt" ) 2>/dev/null \
     && mv -f "$_mt" "$1/update.inprogress" 2>/dev/null; then
    MARK_DIR=$1
    return 0
  fi
  rm -f "$_mt" 2>/dev/null
  return 1
}

mark_release() {   # 내 것일 때만 지운다
  [ -n "$MARK_DIR" ] || return 0
  marker_info "$MARK_DIR/update.inprogress"
  [ "$MK_PID" = "$$" ] && rm -f "$MARK_DIR/update.inprogress"
  MARK_DIR=
  return 0
}

take_marker() {   # $1=데이터 폴더 — 업데이트·다른 설치가 돌고 있으면 멈춘다
  if [ ! -d "$1" ]; then                         # 처음 설치 — 클로드 폴더(projects)가 없으면 [3/6] 에서 멈출 설치다.
    [ -d "$CLAUDE_HOME/projects" ] && [ -d "$(dirname "$1")" ] || return 0   # 지킬 것이 없으니 아무것도 만들지 않는다
  fi
  if runner_live "$1"; then
    die "업데이트가 진행 중입니다(러너, $1/updates/runner.lock.d) — 끝난 뒤 다시 실행하세요"
  fi
  if marker_busy "$1"; then
    die "업데이트·설치가 진행 중입니다(pid ${MK_PID:-?}, by=${MK_BY:-?}) — 끝난 뒤 다시 실행하세요"
  fi
  if [ ! -d "$1" ]; then
    mkdir -p "$1" 2>/dev/null || die "데이터 폴더를 만들 수 없습니다: $1"
  fi
  chmod 700 "$1" 2>/dev/null                     # 폴더 권한만 좁힌다(umask 는 그대로)
  mark_take "$1" || die "update.inprogress 를 쓸 수 없습니다: $1"
}

on_exit() {
  mark_release
  [ -n "$I_TMP" ] && rm -rf "$I_TMP"
  # get.sh 가 넘긴 임시 stage(설치 루트 옆 .clewpath-get.*) — 서막이 중간에 멈춰도 남기지 않는다(같은 명령이 다시 받는다)
  if [ -n "$GET_TMP" ]; then chmod -R u+rwX "$GET_TMP" 2>/dev/null; rm -rf "$GET_TMP"; fi
  return 0
}

# ---------------------------------------------------------------- 경로 추정(venv 가 생기기 전 — 그 뒤엔 hostctl paths)

claude_home_guess() { printf '%s' "${SESSION_MANAGER_CLAUDE_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"; }

launcher_paths() {   # $1=설치 루트 → LP_DD·LP_CF: 그 루트의 기존 런처가 쓰는 데이터 폴더·설정 파일(런처가 없거나 말이 이상하면 빈 값)
  LP_DD=
  LP_CF=
  [ -f "$1/start-connector.sh" ] || return 0
  _lpo=$(/bin/sh "$1/start-connector.sh" paths </dev/null 2>/dev/null)
  LP_DD=$(printf '%s\n' "$_lpo" | sed -n 's/^data_dir=//p' | head -n 1)
  LP_CF=$(printf '%s\n' "$_lpo" | sed -n 's/^conf_file=//p' | head -n 1)
  path_ok "$LP_DD" && path_ok "$LP_CF" || { LP_DD=; LP_CF=; }
  return 0
}

data_dir_guess() {   # $1=설치 루트 → DATA_DIR: 그 루트의 런처가 있으면 그 말(paths)을, 없으면 셸 규칙(config.data_dir 과 같은 식)
  launcher_paths "$1"
  DATA_DIR=${LP_DD:-$(claude_home_guess)/session_manager}
}

root_guard() {   # $1=설치 루트(물리 경로) — / ·홈(이나 그 위)·클로드 폴더·데이터 폴더(나 그 위)는 설치 루트가 아니다. 거기서 돌면
  # 남의 .venv 를 정확 동기화(uv sync)로 바꾸고, 제거가 홈·클로드 폴더째 지울 수 있다
  [ "$1" = / ] && die "설치 폴더가 / 입니다 — ClewPath 전용 빈 폴더에 zip 을 풀어 다시 실행하세요"
  phys_of "$HOME"
  covers "$1" "$PO_OUT" && die "설치 폴더가 홈 폴더이거나 그 위입니다: $1 — ClewPath 전용 빈 폴더에 zip 을 풀어 다시 실행하세요"
  phys_of "$CLAUDE_HOME"
  covers "$1" "$PO_OUT" && die "설치 폴더가 클로드 폴더($CLAUDE_HOME)이거나 그 위입니다: $1 — ClewPath 전용 빈 폴더에 zip 을 풀어 다시 실행하세요"
  if [ -n "$DATA_DIR" ]; then
    phys_of "$DATA_DIR"
    covers "$1" "$PO_OUT" && die "설치 폴더가 데이터 폴더($DATA_DIR)이거나 그 위입니다: $1 — ClewPath 전용 빈 폴더에 zip 을 풀어 다시 실행하세요"
  fi
  return 0
}

mac_ascii_guard() {   # $1=설치 루트 — [mac] ps 는 명령줄의 ASCII 밖 바이트를 바꿔(\xxx) 보여 준다. 그러면 런처·러너·설치가 명령줄로
  # Host 를 알아보지 못한다(멈추지 못하고 겹쳐 띄운다). 리눅스는 /proc 에서 바이트 그대로 읽으므로 그대로 둔다
  [ "$OS" = mac ] || return 0
  [ -z "$(printf '%s' "$1" | LC_ALL=C tr -d ' -~')" ] && return 0
  fail "맥에서는 설치 폴더 경로에 한글 등 ASCII 밖의 글자를 쓸 수 없습니다: $1"
  say "         (맥의 ps 가 그 글자를 바꿔 보여 줘 ClewPath 가 자기 Host 를 알아보지 못합니다)"
  say "         영문 경로의 빈 새 폴더에 zip 을 풀어 다시 실행하세요"
  exit 1
}

our_entry() {   # $1=설치 루트의 최상위 이름 — 우리 설치 항목인가(PAYLOAD 9개·설치가 만드는 것·일시 이름 — uninstall.sh 와 같은
  # 목록). 맥 Finder 의 .DS_Store 는 셈하지 않는다
  case "$1" in
    session_manager|pwa|pyproject.toml|uv.lock|install.ps1|install.sh|uninstall.sh|README.md|LICENSE) return 0 ;;
    .venv|clewpath_host.egg-info|start-connector.sh|.clewpath-install) return 0 ;;
    .clewpath-swap.*|.clewpath-old.*|.clewpath-trash.*|.clewpath-install.*|.DS_Store) return 0 ;;   # 일시 이름(기록 임시 파일 포함)
  esac
  return 1
}

foreign_entries() {   # $1=폴더 → FE_N(우리 것이 아닌 최상위 항목 수)·FE_LIST(앞 5개 이름)
  FE_N=0
  FE_LIST=
  for _fe in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    exists "$_fe" || continue
    our_entry "${_fe##*/}" && continue
    FE_N=$((FE_N + 1))
    [ "$FE_N" -le 5 ] && FE_LIST="$FE_LIST${FE_LIST:+, }${_fe##*/}"
  done
  [ "$FE_N" -gt 5 ] && FE_LIST="$FE_LIST 외 $((FE_N - 5))개"
  return 0
}

refuse_foreign() {   # $1=설치 폴더 — 설치 기록이 없는(첫 설치) 폴더에 남의 항목이 있으면 멈춘다. 남의 파일이 섞인 폴더(~/Downloads 등)에
  # 설치하면 제거가 그것까지 지울 수 있었다
  [ -f "$1/.clewpath-install" ] && return 0
  foreign_entries "$1"
  [ "$FE_N" = 0 ] && return 0
  fail "설치 폴더에 ClewPath 것이 아닌 항목이 있습니다: $FE_LIST"
  say "         빈 새 폴더에 zip 을 풀어 다시 실행하세요(남의 파일과 섞인 폴더에는 설치하지 않습니다): $1"
  exit 1
}

check_root_items() {   # 바로 실행한 설치 루트 — 첫 설치는 우리 항목만 있는 폴더에서만, 재설치(기록 있음)는 모르는 항목을 알리기만
  [ "$DEV_TREE" = 1 ] && return 0              # 개발 체크아웃은 위에서 따로 경고했다(제거는 개발 체크아웃을 거부한다)
  refuse_foreign "$ROOT"
  foreign_entries "$ROOT"
  [ "$FE_N" = 0 ] || warn "설치 폴더에 ClewPath 가 모르는 항목이 있습니다(그대로 둡니다): $FE_LIST"
  return 0
}

check_venv_owner() {   # 설치 기록이 없는 폴더의 .venv 는 남의 가상 환경일 수 있다 — 재사용해 uv sync(정확 동기화)를 걸면 그 안의 패키지를
  # 지운다. 우리가 만든 것(설치 기록, 또는 .venv 안의 표지 — 첫 설치가 의존성에서 멈춘 뒤 다시 칠 때)만 잇는다
  exists "$ROOT/.venv" || return 0
  if [ -f "$ROOT/.clewpath-install" ] || [ -f "$ROOT/.venv/$VENV_MARK" ]; then return 0; fi
  [ "$REINSTALL" = 1 ] && return 0              # 지우고 다시 만들라는 뜻 — [4/6] 이 가드를 거쳐 지운다
  fail "설치 기록이 없는 폴더에 .venv 가 있습니다(ClewPath 가 만든 것이 아닐 수 있어 쓰지 않습니다): $ROOT/.venv"
  say "         다른 곳에 쓰던 가상 환경이면 옮긴 뒤, ClewPath 용이면 --reinstall 을 붙여 다시 실행하세요(지우고 새로 만듭니다):"
  say "         sh $(shq "$ROOT/install.sh") --reinstall"
  exit 1
}

swap_left() {   # $1=설치 루트 — 중단된 업데이트의 교체 잔재(러너 저널)
  for _sd in "$1"/.clewpath-swap.*; do
    [ -d "$_sd" ] && return 0
  done
  return 1
}

recover_swaps() {   # 설치 루트의 교체 잔재를 러너(-RecoverOnly)로 되돌린다 — 표식을 잡기 전에(러너가 자기 표식을 쓴다)
  if runner_live "$DATA_DIR"; then
    die "업데이트가 진행 중입니다(러너) — 끝난 뒤 다시 실행하세요"
  fi
  warn "중단된 업데이트 잔재가 있습니다($ROOT/.clewpath-swap.*) — 이전 상태로 되돌립니다"
  stop_host "$ROOT"                            # 잔재 트리로 돌고 있을 수 있다(러너는 떠 있는 Host 를 보면 되돌리지 않는다)
  _rs=
  for _rc in "$DATA_DIR/updates/update_runner.sh" "$ROOT/session_manager/update_runner.sh" \
             "$ROOT"/.clewpath-swap.*/old/session_manager/update_runner.sh \
             "$ROOT"/.clewpath-swap.*/new/session_manager/update_runner.sh; do
    [ -f "$_rc" ] && { _rs=$_rc; break; }
  done
  if [ -z "$_rs" ]; then
    fail "되돌릴 도구(update_runner.sh)를 찾지 못했습니다."
    say "         처음부터 다시 설치하세요(받은 파일이 잔재를 덮습니다): curl -fsSL https://clewpath.pyongso.com/get.sh | sh"
    exit 1
  fi
  mkdir -p "$DATA_DIR/updates" 2>/dev/null && cp "$_rs" "$I_TMP/update_runner.sh" 2>/dev/null \
    || die "복구 준비 실패: $DATA_DIR/updates"
  marker_info "$DATA_DIR/update.inprogress"     # 내 표식(서막이 잡고 exec 로 이어 온 것)이면 내려놓는다 — 러너가 '설치 중'으로
  [ "$MK_PID" = "$$" ] && rm -f "$DATA_DIR/update.inprogress"   # 보고 비켜서면(3) 아무도 못 되돌린다. 곧 다시 잡는다
  _rlog="$DATA_DIR/updates/recover.log"
  /bin/sh "$I_TMP/update_runner.sh" -RecoverOnly -InstallRoot "$ROOT" -LogFile "$_rlog" </dev/null >>"$_rlog" 2>&1
  _rr=$?
  if [ "$_rr" = 3 ]; then                      # 다른 러너(감독자가 부른 복구)가 하는 중 — 끝나기를 기다린다
    _re=$(( $(date +%s) + 60 ))
    while swap_left "$ROOT" && [ "$(date +%s)" -lt "$_re" ]; do sleep 1; done
  fi
  if swap_left "$ROOT"; then
    fail "중단된 업데이트를 되돌리지 못했습니다(종료코드 $_rr, 기록: $_rlog)"
    say "         처음부터 다시 설치하세요(받은 파일이 잔재를 덮습니다): curl -fsSL https://clewpath.pyongso.com/get.sh | sh"
    exit 1
  fi
  ok "중단된 업데이트를 되돌렸습니다(기록: $_rlog)"
}

# ---------------------------------------------------------------- I1. get.sh 가 푼 stage → 설치 루트

check_stage() {   # $1=stage — 최상위 이름을 다시 본다(get.sh 가 거른 뒤에도, 아무것도 바꾸기 전에 — S8)
  for _ps in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    exists "$_ps" || continue
    _pn=${_ps##*/}
    case "$_pn" in
      ''|.*|start-connector.sh|*[[:cntrl:]]*|*'\'*) die "stage 에 둘 수 없는 이름이 있습니다: $_pn" ;;
    esac
    [ -L "$_ps" ] && die "stage 에 심볼릭 링크가 있습니다: $_pn"
  done
  return 0
}

mv_held() {   # mv 를 INT·TERM·HUP 을 무시한 채로 — 터미널의 Ctrl+C·HUP 는 프로세스 그룹 전체(mv 자식 포함)로 가서, 셸만 미뤄서는
  # mv 가 죽어 새것·옛것이 섞인다. 무시('')는 exec 로 mv 에 넘어가고, mv 는 곧 끝나며 다른 것을 띄우지 않는다
  ( trap '' INT TERM HUP; exec mv "$@" )
}

place_stage() {   # $1=stage $2=설치 루트 — 최상위 항목마다 옛 것을 옆(설치 루트 안 .clewpath-old.<pid>)으로 rename 한 뒤 새 것을
  # 제자리로 rename(같은 파일 시스템). 항목이 비는 틈은 rename 두 번 사이뿐이다(전엔 rm -rf 동안 — 큰 폴더면 몇 초). 옮기는
  # 동안 INT·TERM·HUP 은 미뤘다가 다 옮긴 뒤 끝낸다(셸은 미루고, mv 자식은 무시 — mv_held) — 도중에 끝나면 설치 폴더에
  # session_manager 가 빠진 채(또는 새것·옛것이 섞인 채) 남고, 아직 안 옮긴 새 항목은 on_exit 가 임시 폴더째 지운다.
  # zip 에 없는 기존 항목은 남긴다.
  # 지난번에 전원이 꺼져 남은 옛 사본(.clewpath-old.*)은 먼저 치운다 — 받은 파일이 그 항목을 모두 새로 덮는다
  for _po in "$2"/.clewpath-old.*; do
    exists "$_po" || continue
    rm -rf "$_po" || die "지난 설치가 남긴 옛 사본을 지우지 못했습니다: $_po — 지운 뒤 같은 명령을 다시 실행하세요"
  done
  PS_OLD="$2/.clewpath-old.$$"
  mkdir "$PS_OLD" || die "설치 폴더에 쓸 수 없습니다: $2 — 같은 명령을 다시 실행하세요"
  PS_SIG=
  PS_FAIL=
  trap 'PS_SIG=1' INT TERM HUP
  for _ps in "$1"/*; do
    exists "$_ps" || continue
    _pn=${_ps##*/}
    if exists "$2/$_pn" && ! mv_held "$2/$_pn" "$PS_OLD/$_pn"; then
      PS_FAIL="기존 항목을 옮기지 못했습니다: $2/$_pn"
      break
    fi
    if ! mv_held "$_ps" "$2/$_pn"; then
      exists "$PS_OLD/$_pn" && mv_held "$PS_OLD/$_pn" "$2/$_pn"     # 옛 것을 제자리로
      PS_FAIL="옮기지 못했습니다: $_pn"
      break
    fi
  done
  trap 'exit 1' INT TERM HUP
  [ -n "$PS_FAIL" ] && die "$PS_FAIL — 같은 명령을 다시 실행하세요"    # 옛 사본은 둔다(되돌리지 못한 것이 있을 수 있다)
  rm -rf "$PS_OLD" 2>/dev/null || warn "옛 사본을 다 지우지 못했습니다(다음 설치가 치웁니다): $PS_OLD"
  [ -n "$PS_SIG" ] && die "중단 신호를 받아 멈춥니다 — 받은 파일은 다 옮겼습니다. 같은 명령을 다시 실행하면 이어서 설치합니다"
  return 0
}

stage_temp() {   # --from-stage → GET_TMP: get.sh 의 임시 폴더(설치 루트 옆 .clewpath-get.*)를 인자 해석 바로 뒤에 알아 둔다 —
  # 서막의 어떤 거절(인자·root·경로 검사…)에도 on_exit 가 치운다(남기면 같은 명령을 다시 칠 때마다 쌓인다). 그 모양일 때만:
  # stage 가 이 install.sh 의 폴더이고, 그 부모 이름이 .clewpath-get.* 이고, 그 옆이 --dir 의 부모(물리 경로)
  GET_TMP=
  _gs=$(CDPATH='' cd "$FROM_STAGE" 2>/dev/null && phys_pwd) || return 0
  [ "$_gs" = "$ROOT" ] || return 0
  trim_slash "$DIR"
  path_ok "$T_OUT" || return 0
  _gp=$(CDPATH='' cd "$(dirname "$T_OUT")" 2>/dev/null && phys_pwd) || return 0
  _gt=$(dirname "$_gs")
  case "${_gt##*/}" in .clewpath-get.?*) ;; *) return 0 ;; esac
  [ "$(dirname "$_gt")" = "$_gp" ] && GET_TMP=$_gt
  return 0
}

prelude() {   # --from-stage: 옮긴 뒤 설치 루트의 install.sh 로 이어 간다(exec — pid 가 그대로라 표식도 그대로 유효)
  [ "$DRY_RUN" = 1 ] && usage_die "--dry-run 은 --from-stage 와 함께 쓸 수 없습니다"
  P_ST=$(CDPATH='' cd "$FROM_STAGE" 2>/dev/null && phys_pwd) || die "stage 폴더가 없습니다: $FROM_STAGE"
  [ "$P_ST" = "$ROOT" ] || die "--from-stage 는 이 install.sh 가 있는 폴더여야 합니다: $FROM_STAGE"
  [ -f "$P_ST/session_manager/__init__.py" ] && [ -f "$P_ST/install.sh" ] \
    || die "stage 에 session_manager·install.sh 가 없습니다: $P_ST"
  check_stage "$P_ST"
  trim_slash "$DIR"
  DIR=$T_OUT
  path_ok "$DIR" || usage_die "--dir 는 절대 경로여야 합니다: $DIR"
  if [ "$DIR" = / ] || [ "$DIR" = "$HOME" ]; then die "--dir 가 / 나 홈입니다: $DIR"; fi
  mac_ascii_guard "$DIR"
  case "$DIR/" in "$P_ST"/*) die "--dir 가 stage 안입니다: $DIR" ;; esac
  if exists "$DIR" && [ ! -d "$DIR" ]; then die "--dir 가 폴더가 아닙니다: $DIR"; fi
  if [ -e "$DIR/.git" ] || [ -d "$DIR/ops" ]; then die "개발 체크아웃(.git·ops) 위에는 설치하지 않습니다: $DIR"; fi
  if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && ! is_clewpath_tree "$DIR"; then
    die "설치 폴더가 비어 있지 않고 ClewPath 설치본도 아닙니다(덮어쓰지 않습니다): $DIR"
  fi
  [ -d "$DIR" ] && refuse_foreign "$DIR"         # 설치본처럼 보여도 기록이 없고 남의 파일이 섞였으면 — 옮기기 전에
  mkdir -p "$DIR" || die "설치 폴더를 만들 수 없습니다: $DIR"
  DIR=$(CDPATH='' cd "$DIR" && phys_pwd) || die "설치 폴더를 찾지 못했습니다: $DIR"   # 이어 갈 install.sh 의 ROOT 와 같은 물리 경로
  mac_ascii_guard "$DIR"
  root_guard "$DIR"                             # 홈·클로드 폴더(나 그 위) — 그 폴더의 런처를 부르기 전에
  data_dir_guess "$DIR"
  root_guard "$DIR"                             # 이제 데이터 폴더까지
  take_marker "$DATA_DIR"
  stop_host "$DIR"
  place_stage "$P_ST" "$DIR"
  for _sw in "$DIR"/.clewpath-swap.*; do       # 중단된 업데이트 잔재 — 받은 파일이 모든 교체 대상을 새로 덮었다
    [ -d "$_sw" ] || continue
    rm -rf "$_sw" 2>/dev/null
    exists "$_sw" && die "중단된 업데이트 잔재를 지우지 못했습니다: $_sw — 지운 뒤 같은 명령을 다시 실행하세요"
    ok "중단된 업데이트 잔재를 치웠습니다(받은 파일로 덮음): ${_sw##*/}"
  done
  if [ -n "$GET_TMP" ]; then rm -rf "$GET_TMP"; GET_TMP=''; fi    # 다 옮겼다 — 빈 stage 와 임시 폴더를 치운다
  ok "설치 폴더에 옮김: $DIR"
  set -- --cp-url "$CP_URL" --relay-url "$RELAY_URL" --app-url "$APP_URL" --port "$PORT" --python "$PYVER" \
         --uv-version "$UV_VERSION"
  [ "$SYSTEM_UV" = 1 ] && set -- "$@" --system-uv
  [ "$REINSTALL" = 1 ] && set -- "$@" --reinstall
  [ "$NO_AUTOSTART" = 1 ] && set -- "$@" --no-autostart
  [ "$NO_PROVISION" = 1 ] && set -- "$@" --no-provision
  [ "$ALLOW_ROOT" = 1 ] && set -- "$@" --allow-root
  [ "$ALLOW_HTTP" = 1 ] && set -- "$@" --allow-http
  if [ -n "$CLIENT_TOKEN" ]; then export CLEWPATH_CLIENT_TOKEN="$CLIENT_TOKEN"; fi   # 토큰은 env 로(argv 에 싣지 않는다)
  if [ -n "$AGENT_TOKEN" ]; then export CLEWPATH_AGENT_TOKEN="$AGENT_TOKEN"; fi
  rm -rf "$I_TMP"
  trap - EXIT INT TERM HUP
  exec /bin/sh "$DIR/install.sh" "$@" </dev/null
}

# ---------------------------------------------------------------- [1/6] uv

uv_version_of() {   # $1=uv 실행 파일 → "0.12.1"(못 읽으면 빈 출력)
  _uv=$("$1" --version </dev/null 2>/dev/null) || return 0
  _uv=${_uv#uv }
  printf '%s' "${_uv%% *}"
}

uv_min_ok() {   # $1=버전 — 고정 버전(UV_VERSION)과 같은 부 버전 이상인가(0.12.1 고정이면 0.12.0 이상)
  _vm=${1%%.*}
  _vn=${1#*.}
  _vn=${_vn%%.*}
  _wm=${UV_VERSION%%.*}
  _wn=${UV_VERSION#*.}
  _wn=${_wn%%.*}
  case "$_vm" in ''|*[!0-9]*) return 1 ;; esac
  case "$_vn" in ''|*[!0-9]*) return 1 ;; esac
  case "$_wm$_wn" in ''|*[!0-9]*) return 0 ;; esac
  [ "$_vm" -gt "$_wm" ] && return 0
  [ "$_vm" -eq "$_wm" ] && [ "$_vn" -ge "$_wn" ]
}

uv_ours() {   # $CLEWPATH_HOME/bin 의 uv 를 쓰거나 바꿔도 되나 — ClewPath 전용 기본 위치(CLEWPATH_HOME 을 주지 않음)이거나, 설치 기록의
  # uv= 가 그것일 때만. 남과 같이 쓰는 폴더(예: CLEWPATH_HOME=~/.local)의 사용자 uv 를 덮어쓰거나 제 것으로 적지 않게(제거가 지운다)
  [ -z "$CLEWPATH_HOME_GIVEN" ] && return 0
  [ "$(kv uv "$ROOT/.clewpath-install")" = "$CLEWPATH_HOME/bin/uv" ]
}

is_musl() {   # glibc 가 보이면 musl 이 아니다 — glibc 배포판에도 musl 패키지가 /lib/ld-musl-* 를 둘 수 있다
  getconf GNU_LIBC_VERSION >/dev/null 2>&1 && return 1
  case "$(ldd --version 2>&1)" in
    *GLIBC*|*"GNU libc"*|*"GNU C Library"*) return 1 ;;
    *musl*) return 0 ;;
  esac
  for _m in /lib/ld-musl-*; do
    [ -e "$_m" ] && return 0
  done
  return 1
}

uv_target() {   # → UV_TARGET
  if [ "$OS" = mac ]; then
    # Rosetta 로 돈 셸에서도 진짜 CPU 를 본다(uname -m 은 x86_64 라고 할 수 있다)
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then UV_TARGET=aarch64-apple-darwin; else UV_TARGET=x86_64-apple-darwin; fi
    return 0
  fi
  if is_musl; then
    fail "musl(Alpine 등)은 지원 범위 밖입니다 — glibc 배포판에서 설치하세요"
    return 1
  fi
  case "$(uname -m)" in
    x86_64|amd64) UV_TARGET=x86_64-unknown-linux-gnu ;;
    aarch64|arm64) UV_TARGET=aarch64-unknown-linux-gnu ;;
    *) fail "지원하지 않는 CPU 입니다: $(uname -m)"; return 1 ;;
  esac
  return 0
}

install_uv() {   # 고정 버전 uv 를 받아(또는 CLEWPATH_UV_ARCHIVE) 해시를 대조하고 $CLEWPATH_HOME/bin 에 둔다 → UV
  uv_target || return 1
  _url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_TARGET.tar.gz"
  _arc="$I_TMP/uv.tar.gz"
  for _t in tar gzip mktemp; do
    if [ "$_t" = gzip ] && [ "$OS" = mac ]; then continue; fi       # 맥 tar(bsdtar)는 gzip 을 스스로 푼다
    command -v "$_t" >/dev/null 2>&1 || { fail "$_t 가 없습니다 — 먼저 설치하세요"; return 1; }
  done
  if [ -n "${CLEWPATH_UV_ARCHIVE:-}" ]; then
    [ -f "$CLEWPATH_UV_ARCHIVE" ] || { fail "CLEWPATH_UV_ARCHIVE 파일이 없습니다: $CLEWPATH_UV_ARCHIVE"; return 1; }
    cp "$CLEWPATH_UV_ARCHIVE" "$_arc" || { fail "CLEWPATH_UV_ARCHIVE 를 읽지 못했습니다"; return 1; }
    _from="file"
    say "         로컬 파일 사용: $CLEWPATH_UV_ARCHIVE (uv $UV_VERSION, $UV_TARGET)"
  else
    command -v curl >/dev/null 2>&1 || { fail "curl 이 없습니다 — 먼저 설치하세요(예: sudo apt install curl)"; return 1; }
    say "         내려받는 중: uv $UV_VERSION ($UV_TARGET)"
    fetch "$_url" "$_arc" 180 || { fail "uv 다운로드 실패: $_url"; return 1; }
    _from="remote"
  fi
  _act=$(sha256_of "$_arc")
  [ -n "$_act" ] || { fail "SHA-256 도구(sha256sum·shasum·openssl)가 없습니다"; return 1; }
  _exp=$(uv_pinned_sha256 "$UV_VERSION" "$UV_TARGET")
  if [ -n "$_exp" ]; then
    if [ "$_act" != "$_exp" ]; then
      fail "uv 파일 해시가 다릅니다 — 설치를 중단합니다."
      say "         기대: $_exp"
      say "         실제: $_act"
      fail "uv_checksum_mismatch"
      return 1
    fi
    ok "uv 해시 검증 통과 (SHA-256, 스크립트에 고정된 값)"
  elif [ "$_from" = file ]; then
    fail "이 스크립트에 uv $UV_VERSION ($UV_TARGET) 의 해시가 없어 로컬 파일을 믿을 수 없습니다."
    return 1
  else
    # 소스에 없는 버전(--uv-version). 같은 서버의 해시라 전송 오류만 잡고 서버 침해는 못 잡는다 → 그래서 경고
    warn "이 스크립트에 $UV_VERSION 의 해시가 없습니다 — 배포처 해시로만 대조합니다."
    fetch "$_url.sha256" "$I_TMP/uv.sha256" 60 || { fail "배포처 해시를 받지 못했습니다: $_url.sha256"; return 1; }
    _pub=$(awk '{ print tolower($1); exit }' "$I_TMP/uv.sha256")
    if [ "${#_pub}" != 64 ] || [ "$_act" != "$_pub" ]; then
      fail "uv 해시 불일치(배포처 기준)"
      return 1
    fi
    ok "uv 해시 대조 통과 (배포처 기준)"
  fi
  mkdir "$I_TMP/uvx" && tar -xzf "$_arc" -C "$I_TMP/uvx" </dev/null || { fail "uv 압축을 풀지 못했습니다"; return 1; }
  _ub=$(find "$I_TMP/uvx" -type f -name uv | head -n 1)       # 폴더 구조에 기대지 않고 이름으로 찾는다
  _ux=$(find "$I_TMP/uvx" -type f -name uvx | head -n 1)
  [ -n "$_ub" ] || { fail "압축 안에 uv 가 없습니다"; return 1; }
  mkdir -p "$CLEWPATH_HOME/bin" || { fail "폴더를 만들 수 없습니다: $CLEWPATH_HOME/bin"; return 1; }
  _ut="$CLEWPATH_HOME/bin/.uv.tmp.$$"
  if ! { cp "$_ub" "$_ut" && chmod 755 "$_ut" && mv -f "$_ut" "$CLEWPATH_HOME/bin/uv"; }; then
    rm -f "$_ut"
    fail "uv 를 둘 수 없습니다: $CLEWPATH_HOME/bin/uv"
    return 1
  fi
  if [ -n "$_ux" ] && { ! exists "$CLEWPATH_HOME/bin/uvx" || uv_ours; }; then
    { cp "$_ux" "$_ut" && chmod 755 "$_ut" && mv -f "$_ut" "$CLEWPATH_HOME/bin/uvx"; } || rm -f "$_ut"
  fi
  UV="$CLEWPATH_HOME/bin/uv"
  [ "$(uv_version_of "$UV")" = "$UV_VERSION" ] || { fail "받은 uv 가 실행되지 않습니다: $UV"; return 1; }
  return 0
}

step_uv() {
  say ""
  say "[1/6] uv 확보"
  if [ "$SYSTEM_UV" = 1 ]; then
    UV=$(command -v uv 2>/dev/null) || UV=
    [ -n "$UV" ] || die "--system-uv 를 줬지만 PATH 에 uv 가 없습니다."
    case "$UV" in
      /*) ;;
      *) UV="$(CDPATH='' cd "$(dirname "$UV")" && phys_pwd)/${UV##*/}" ;;   # PATH 의 상대 경로 — cd 뒤에도 찾게
    esac
    _sv=$(uv_version_of "$UV")                   # 해시는 못 보지만 너무 옛 uv(모르는 옵션·잠금 형식)는 막는다
    uv_min_ok "$_sv" || die "PATH 의 uv 가 너무 옛 버전입니다(uv ${_sv:-?}, $UV) — uv ${UV_VERSION%.*} 이상이 필요합니다(--system-uv 를 빼면 고정 버전을 받습니다)"
    warn "PATH 의 uv 사용: $UV (--system-uv — 버전·해시를 검증하지 않습니다)"
  elif exists "$CLEWPATH_HOME/bin/uv" && ! uv_ours; then
    fail "$CLEWPATH_HOME/bin/uv 는 ClewPath 가 둔 것이 아닙니다(CLEWPATH_HOME=$CLEWPATH_HOME) — 쓰거나 덮어쓰지 않습니다."
    say "         ClewPath 전용 폴더를 CLEWPATH_HOME 으로 주거나(비워 두면 기본 위치), 그 uv 를 쓰려면 --system-uv 를 붙이세요"
    exit 1
  elif [ -f "$CLEWPATH_HOME/bin/uv" ] && [ -x "$CLEWPATH_HOME/bin/uv" ] \
       && [ "$(uv_version_of "$CLEWPATH_HOME/bin/uv")" = "$UV_VERSION" ]; then
    UV="$CLEWPATH_HOME/bin/uv"
    ok "설치된 uv 재사용: uv $UV_VERSION ($UV)"
  elif [ "$DRY_RUN" = 1 ]; then
    warn "DryRun: uv 설치 건너뜀 (받을 위치: $CLEWPATH_HOME/bin/uv)"
  else
    if [ -f "$CLEWPATH_HOME/bin/uv" ]; then
      warn "설치된 uv 가 고정 버전($UV_VERSION)이 아니어서 다시 받습니다"
    fi
    install_uv || exit 1
    ok "uv 설치 완료: $UV"
  fi
}

# ---------------------------------------------------------------- [2/6] 파이썬

uv_run() {   # $1=작업 폴더, 나머지=uv 인자. uv 설정 env 는 uv 에만 준다(Host·claude 로 새지 않게). 가상 환경 표식은 뺀다
  UR_CWD=$1
  shift
  ( unset VIRTUAL_ENV CONDA_PREFIX UV_PYTHON
    export UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-1}" UV_PYTHON_INSTALL_DIR="$PY_INSTALL_DIR" \
           UV_PROJECT_ENVIRONMENT="$ROOT/.venv"
    if [ -n "$PY_PREF" ]; then export UV_PYTHON_PREFERENCE="$PY_PREF"; fi
    if [ -n "$PY_BIN_DIR" ]; then export UV_PYTHON_BIN_DIR="$PY_BIN_DIR"; fi
    cd "$UR_CWD" || exit 1
    exec "$UV" "$@" ) </dev/null
}

uv_find() {   # $1=선호(only-system|only-managed) $2=요청 → 경로. --system: 가상 환경(.venv·VIRTUAL_ENV)은 보지 않는다
  UF_SAVED=$PY_PREF
  PY_PREF=$1
  UF_OUT=$(uv_run "$I_TMP" python find --system "$2" 2>/dev/null | head -n 1)
  PY_PREF=$UF_SAVED
  path_ok "$UF_OUT" && printf '%s' "$UF_OUT"
  return 0
}

py_smoke() {   # 진짜로 쓸 수 있는 파이썬인가 — 버전만 보면 안 된다(ssl 이 막힌 빌드도 뜬다)
  "$1" -c 'import ssl, ctypes, sqlite3, sys; assert sys.version_info >= (3, 11)' </dev/null >/dev/null 2>&1
}

step_python() {
  say ""
  say "[2/6] 파이썬 $PYVER 확보"
  if [ "$DRY_RUN" = 1 ] || [ -z "$UV" ]; then
    warn "DryRun: 파이썬 확보 건너뜀"
    return 0
  fi
  if [ "$OS" = linux ]; then                   # 시스템 파이썬 먼저(맥은 건너뛴다 — /usr/bin/python3 는 CLT 설치 대화상자일 수 있다)
    _pc=$(uv_find only-system ">=$PYVER")
    if [ -n "$_pc" ] && py_smoke "$_pc"; then
      PYEXE=$_pc
      PY_PREF=only-system
      ok "시스템 파이썬 사용: $PYEXE"
      return 0
    fi
    [ -n "$_pc" ] && warn "시스템 파이썬이 있으나 쓸 수 없습니다(표준 모듈 확인 실패): $_pc"
  fi
  _pc=$(uv_find only-managed "$PYVER")          # 이미 받은 관리 런타임(재설치)
  if [ -z "$_pc" ] || ! py_smoke "$_pc"; then
    say "         uv 관리 런타임을 받습니다 (시스템에 설치되지 않음 — $PY_INSTALL_DIR)"
    # 0.8 이후 uv 는 ~/.local/bin 에 python 실행 파일을 둔다 — 사용자의 PATH 를 건드리지 않게 막는다
    if uv_run "$I_TMP" python install --help 2>/dev/null | grep -q -e '--no-bin'; then
      run_quiet "$I_TMP/python.log" uv_run "$I_TMP" python install "$PYVER" --no-bin
    else
      PY_BIN_DIR="$PY_INSTALL_DIR/bin"
      run_quiet "$I_TMP/python.log" uv_run "$I_TMP" python install "$PYVER"
    fi
    _pc=$(uv_find only-managed "$PYVER")
  fi
  if [ -n "$_pc" ] && py_smoke "$_pc"; then
    PYEXE=$_pc
    PY_PREF=only-managed
    ok "uv 관리 파이썬 사용: $PYEXE (시스템 설치 아님)"
    return 0
  fi
  fail "쓸 수 있는 파이썬을 준비하지 못했습니다."
  show_tail "$I_TMP/python.log" "uv 출력(마지막 25줄)"
  say ""
  say "  다음 중 하나를 하신 뒤 같은 명령을 다시 실행하세요:"
  if [ "$OS" = linux ]; then
    say "    A) 배포판의 파이썬 $PYVER 이상 설치 (예: sudo apt install python3.11 / sudo dnf install python3.11)"
  fi
  say "    B) 네트워크·프록시 확인 — uv 가 파이썬 런타임을 내려받지 못했을 수 있습니다"
  exit 1
}

# ---------------------------------------------------------------- [3/6] 클로드 폴더

step_claude() {
  say ""
  say "[3/6] 클로드 데이터 폴더 점검"
  if command -v claude >/dev/null 2>&1; then
    ok "claude CLI 발견"
  else
    warn "claude CLI 를 못 찾았습니다. 세션 재개 기능은 claude 가 있어야 동작합니다(설치 후 이용 가능)."
  fi
  _pj="$CLAUDE_HOME/projects"
  if [ -d "$_pj" ]; then
    _pn=0
    for _pd in "$_pj"/*/; do
      [ -d "$_pd" ] && _pn=$((_pn + 1))
    done
    if [ "$_pn" -gt 0 ]; then ok "클로드 세션 폴더 발견: $_pj (프로젝트 $_pn 개)"; else warn "폴더는 있으나 프로젝트가 0개입니다: $_pj"; fi
  else
    fail "클로드 데이터 폴더를 찾지 못했습니다: $_pj"
    say "  - 이 PC 에서 클로드 코드를 한 번 이상 사용했나요?"
    say "  - 폴더를 옮겨 쓴다면 CLAUDE_CONFIG_DIR 을 설정한 뒤 다시 실행하세요."
    say "    예:  CLAUDE_CONFIG_DIR=/data/claude sh install.sh"
    [ "$DRY_RUN" = 1 ] || exit 1
  fi
  # 공유 agent 토큰은 선택이다 — 커넥터는 컨트롤플레인이 서명한 단기 JWT 로 릴레이에 들어간다
  if [ -n "$AGENT_TOKEN" ]; then ok "공유 agent 토큰 사용(레거시 호환 모드)"; else ok "공유 agent 토큰 없음 - 컨트롤플레인 JWT 로 접속(권장)"; fi
}

# ---------------------------------------------------------------- [4/6] 의존성

remove_venv() {   # 가드를 거친 .venv 삭제 — 설치 루트가 확인된 ClewPath 폴더이고, 지우는 것은 그 안의 .venv 하나뿐
  path_ok "$ROOT" && [ "$ROOT" != / ] && [ "$ROOT" != "$HOME" ] && [ -d "$ROOT/session_manager" ] \
    || die "설치 루트를 확인하지 못해 .venv 를 지우지 않습니다: $ROOT"
  stop_host "$ROOT"                            # 돌고 있는 Host 의 파이썬을 지우지 않게
  rm -rf "$ROOT/.venv" || die ".venv 를 지우지 못했습니다: $ROOT/.venv"
}

step_deps() {
  say ""
  say "[4/6] 의존성 설치 (전용 .venv)"
  if [ "$DRY_RUN" = 1 ] || [ -z "$UV" ]; then
    warn "DryRun: venv 생성/설치 건너뜀"
    return 0
  fi
  if [ "$REINSTALL" = 1 ] && exists "$ROOT/.venv"; then
    remove_venv
    ok "기존 .venv 삭제 (--reinstall)"
  elif exists "$ROOT/.venv" && [ ! -x "$VPY" ]; then
    remove_venv
    warn "파이썬이 없는 .venv 를 지우고 다시 만듭니다"
  fi
  if [ ! -x "$VPY" ]; then
    # [2/6] 에서 확인한 인터프리터로만 만든다(버전 문자열로 넘기면 uv 가 다시 고르며 다른 런타임을 집을 수 있다)
    run_quiet "$I_TMP/venv.log" uv_run "$ROOT" venv --python "$PYEXE" "$ROOT/.venv"
    if [ ! -x "$VPY" ]; then
      fail "venv 생성 실패"
      show_tail "$I_TMP/venv.log" "uv 출력(마지막 25줄)"
      exit 1
    fi
    # 우리가 만든 .venv 라는 표지 — 설치 기록을 쓰기 전에 멈춰도(의존성 실패) 다시 실행하면 잇는다(check_venv_owner)
    { printf 'clewpath-install\n' >"$ROOT/.venv/$VENV_MARK"; } 2>/dev/null || warn ".venv 표지를 쓰지 못했습니다: $ROOT/.venv"
    ok "venv 생성"
  else
    ok "기존 venv 재사용"
  fi
  if ! py_smoke "$VPY"; then
    fail "만들어진 venv 의 파이썬이 표준 모듈(ssl 등)을 못 씁니다."
    say "  --reinstall 로 .venv 를 지우고 다시 실행하세요:"
    say "     sh $(shq "$ROOT/install.sh") --reinstall"
    exit 1
  fi
  # uv.lock 이 있으면 그대로(고정 버전) — 사람마다 다른 버전이 깔려 "제 PC 에선 되는데" 가 생기지 않게
  _deps=0
  if [ -f "$ROOT/uv.lock" ]; then
    if run_quiet "$I_TMP/sync.log" uv_run "$ROOT" sync --frozen --no-dev; then
      ok "의존성 설치 완료 (uv.lock 고정 버전)"
      _deps=1
    else
      # 잠금 밖 폴백(S11) — 성공해도 원인이 임시 폴더째 사라지지 않게 데이터 폴더(있으면 updates/)에 남긴다
      _sd=$DATA_DIR
      [ -d "$DATA_DIR/updates" ] && _sd="$DATA_DIR/updates"
      _sl="$_sd/install-sync-$(date +%Y%m%d-%H%M%S).log"
      if [ -d "$_sd" ] && cp "$I_TMP/sync.log" "$_sl" 2>/dev/null; then
        warn "잠금 파일로 설치 실패 — pyproject 기준으로 재시도합니다(원인 기록: $_sl)"
      else
        warn "잠금 파일로 설치 실패 — pyproject 기준으로 재시도합니다."
      fi
      show_tail "$I_TMP/sync.log" "uv sync 출력(마지막 10줄)" 10
    fi
  fi
  if [ "$_deps" != 1 ]; then
    if ! run_quiet "$I_TMP/pip.log" uv_run "$ROOT" pip install --python "$VPY" -e "$ROOT"; then
      fail "의존성 설치 실패"
      show_tail "$I_TMP/pip.log" "uv pip 출력(마지막 25줄)"
      say "  대개 원인:"
      say "   1) 이전 설치의 .venv 손상 → --reinstall 로 다시: sh $(shq "$ROOT/install.sh") --reinstall"
      say "   2) PyPI(pypi.org) 연결 차단(사내 프록시/방화벽) → 네트워크·프록시 확인."
      say "  원인을 해결한 뒤 같은 명령을 다시 실행하세요."
      exit 1
    fi
    ok "의존성 설치 완료"
  fi
}

# ---------------------------------------------------------------- hostctl(경로·설정·런처·자격증명·기동 확인)

hostctl() {   # $1=결과 파일, 나머지=hostctl 인자 — key=value 는 파일로, 사람에게 하는 말(표준오류)은 화면으로.
  # 설정 파일은 HC_CONF(사용자의 CLEWPATH_CONFIG → 기존 런처가 쓰는 것 → paths 가 정한 것)로 고정한다 — Host 가 볼 것과 같게
  HC_OUT=$1
  shift
  ( unset CLEWPATH_CLIENT_TOKEN CLEWPATH_AGENT_TOKEN
    if [ -n "$HC_CONF" ]; then export CLEWPATH_CONFIG="$HC_CONF"; fi
    cd "$ROOT" || exit 1
    exec "$VPY" -I -m session_manager.hostctl "$@" ) </dev/null 2>&1 >"$HC_OUT"
}

hostctl_tokens() {   # 토큰을 env 로만 넘긴다(write-config) — 인자는 ps 에 보인다
  HC_OUT=$1
  shift
  ( if [ -n "$CLIENT_TOKEN" ]; then export CLEWPATH_CLIENT_TOKEN="$CLIENT_TOKEN"; else unset CLEWPATH_CLIENT_TOKEN; fi
    if [ -n "$AGENT_TOKEN" ]; then export CLEWPATH_AGENT_TOKEN="$AGENT_TOKEN"; else unset CLEWPATH_AGENT_TOKEN; fi
    if [ -n "$HC_CONF" ]; then export CLEWPATH_CONFIG="$HC_CONF"; fi
    cd "$ROOT" || exit 1
    exec "$VPY" -I -m session_manager.hostctl "$@" ) </dev/null 2>&1 >"$HC_OUT"
}

step_paths() {   # I7 — 경로는 파이썬 규칙(Host 가 볼 데이터 폴더·설정 파일)
  [ "$DRY_RUN" = 1 ] && return 0
  hostctl "$I_TMP/paths.out" paths || die "경로를 확인하지 못했습니다(hostctl paths)"
  _dd=$(kv data_dir "$I_TMP/paths.out")
  _cf=$(kv conf_file "$I_TMP/paths.out")
  path_ok "$_dd" && path_ok "$_cf" || die "hostctl paths 결과가 이상합니다: data_dir=$_dd conf_file=$_cf"
  phys_of "$_dd"
  _dp=$PO_OUT
  phys_of "$DATA_DIR"
  if [ "$_dp" = "$PO_OUT" ]; then              # 같은 폴더(겹친 / ·끝 / 만 다를 수 있다) — 파이썬이 정리한 글자로 적는다. 표식은 그 자리 그대로
    DATA_DIR=$_dd
  elif [ -n "$LP_DD" ]; then                    # 재설치 — 기존 런처가 쓰던 데이터 폴더(페어링·자격증명)를 버리고 갈아타지 않는다
    fail "데이터 폴더가 기존 설치와 다릅니다 — 갈아타지 않고 멈춥니다(페어링·자격증명은 기존 폴더에 있습니다)"
    say "         기존 런처   : $DATA_DIR"
    say "         지금 설정   : $_dd"
    say "         (지금 셸의 CLAUDE_CONFIG_DIR·SESSION_MANAGER_CLAUDE_HOME·CLEWPATH_CONFIG, 또는 설정 파일 $_cf 의 [claude] config_dir)"
    say "         처음 설치할 때와 같은 env 로 다시 실행하세요. 데이터 폴더를 옮기려면 sh $(shq "$ROOT/uninstall.sh") 로 지운 뒤 새로 설치하세요"
    exit 1
  else                                          # 첫 설치 — 셸 추정은 설정 파일을 읽지 못한다
    warn "데이터 폴더가 셸 추정($DATA_DIR)과 다릅니다 — 설정 파일($_cf)의 [claude] config_dir 를 따릅니다: $_dd"
    mark_release
    DATA_DIR=$_dd
    take_marker "$DATA_DIR"
  fi
  CONF_FILE=$_cf
  HC_CONF=$_cf
  if [ -n "${SESSION_MANAGER_CLAUDE_HOME:-}" ]; then
    warn "SESSION_MANAGER_CLAUDE_HOME 은 시험 격리용 변수입니다 — 기동할 때도 같은 값이 필요합니다"
  fi
}

# ---------------------------------------------------------------- [5/6] 설정·권한·런처·기록·자격증명

write_record() {   # 설치 기록 — 러너(uv=·python_pref=·python_install_dir=)와 제거 스크립트가 sed 로 읽는다(소스로 실행하지 않는다)
  _rf="$ROOT/.clewpath-install"
  _rt="$ROOT/.clewpath-install.$$.tmp"
  if { printf '%s\n' "# clewpath-install v1 — install.sh 가 쓴다. 손대지 마세요"
       printf 'home=%s\n' "$CLEWPATH_HOME"
       printf 'uv=%s\n' "$UV"
       printf 'python=%s\n' "$PYEXE"
       printf 'python_pref=%s\n' "$PY_PREF"
       printf 'python_install_dir=%s\n' "$PY_INSTALL_DIR"
       printf 'data_dir=%s\n' "$DATA_DIR"
       printf 'conf_file=%s\n' "$CONF_FILE"
       printf 'installed_version=%s\n' "$(pyproject_version "$ROOT/pyproject.toml")"
       printf 'installed_at=%s\n' "$(date +%s)"
     } >"$_rt" 2>/dev/null && mv -f "$_rt" "$_rf" 2>/dev/null; then
    ok "설치 기록: $_rf"
    return 0
  fi
  rm -f "$_rt" 2>/dev/null
  die "설치 기록을 쓸 수 없습니다: $_rf"
}

step_config() {
  say ""
  say "[5/6] 설정 파일 생성 + 자격증명 발급"
  if [ "$DRY_RUN" = 1 ]; then
    if [ -f "$CONF_FILE" ]; then ok "기존 설정 파일 유지: $CONF_FILE"; else warn "DryRun: 설정 파일 생성 건너뜀 ($CONF_FILE)"; fi
    warn "DryRun: $ROOT/start-connector.sh 생성 건너뜀"
    if [ -f "$DATA_DIR/cp_credentials.json" ]; then
      ok "이미 발급된 자격증명이 있습니다(재사용)"
    else
      warn "DryRun: 발급 건너뜀"
      dry_healthz
    fi
    return 0
  fi
  hostctl "$I_TMP/secure.out" secure-data-dir --data-dir "$DATA_DIR" \
    || warn "데이터 폴더 권한을 좁히지 못했습니다(비밀이 든 폴더 — 직접 chmod 700 하세요): $DATA_DIR"
  set -- write-config --conf-file "$CONF_FILE" --port "$PORT" --cp-url "$CP_URL" --relay-url "$RELAY_URL" --app-url "$APP_URL"
  [ -n "${CLAUDE_CONFIG_DIR:-}" ] && set -- "$@" --claude-config-dir "$CLAUDE_CONFIG_DIR"
  hostctl_tokens "$I_TMP/config.out" "$@" || die "설정 파일을 만들지 못했습니다: $CONF_FILE"
  EFF_PORT=$(kv port "$I_TMP/config.out")
  case "$EFF_PORT" in ''|*[!0-9]*) EFF_PORT=$PORT ;; esac
  hostctl "$I_TMP/launcher.out" render-launcher --root "$ROOT" --data-dir "$DATA_DIR" --conf-file "$CONF_FILE" \
    --port "$EFF_PORT" || die "런처(start-connector.sh)를 만들지 못했습니다"
  write_record
  if [ "$NO_PROVISION" = 1 ]; then
    warn "자격증명 발급 건너뜀(--no-provision) — Host 가 뜬 뒤 스스로 발급받습니다"
  else
    hostctl "$I_TMP/provision.out" provision --cp-url "$CP_URL" || warn "자격증명 발급을 확인하지 못했습니다(fail-open)"
  fi
}

dry_healthz() {   # DryRun 일 때 컨트롤플레인 도달만 본다. 루프백(시험용 CP)만 프록시를 건너뛴다 — 원격 CP 는 사내 프록시가 필요할 수 있다
  command -v curl >/dev/null 2>&1 || { warn "curl 이 없어 컨트롤플레인 도달 확인을 건너뜁니다"; return 0; }
  _hz=$(curl --noproxy '127.0.0.1,localhost,::1' -sS -m 10 "$CP_URL/healthz" 2>&1) || _hz="ERR $_hz"
  case "$_hz" in
    *'"ok":true'*|*'"ok": true'*) ok "컨트롤플레인 도달 확인" ;;
    ERR*) warn "컨트롤플레인에 닿지 못했습니다: ${_hz#ERR }" ;;
    *) warn "예상치 못한 응답: $(printf '%s' "$_hz" | head -c 200)" ;;
  esac
}

# ---------------------------------------------------------------- [6/6] 기동

autostart_install() {   # 자리만 — 다음 단계에서 [mac] launchd, [linux] systemd --user 를 여기에(그때도 런처 렌더 성공 뒤에만)
  [ "$NO_AUTOSTART" = 1 ] && return 0
  warn "자동 시작은 아직 없습니다 — 로그인할 때마다 아래 '시작' 명령으로 켜거나 임시 감시(watch)를 쓰세요"
}

step_start() {
  say ""
  say "[6/6] 기동"
  autostart_install
  if [ "$DRY_RUN" = 1 ]; then
    warn "DryRun: 기동 건너뜀"
    return 0
  fi
  _lc="$ROOT/start-connector.sh"
  /bin/sh "$_lc" status </dev/null >"$I_TMP/status.out" 2>/dev/null
  _hp=$(kv pid "$I_TMP/status.out")
  # 제거가 중간에 끊겨 남은 표식(by=uninstall) — install.sh 를 다시 돌린 것은 다시 켜겠다는 뜻이다. 다른 표식(stop --hold 등)은 그대로
  if [ -f "$DATA_DIR/maintenance.flag" ] && [ "$(kv by "$DATA_DIR/maintenance.flag")" = uninstall ]; then
    if rm -f "$DATA_DIR/maintenance.flag"; then
      ok "끊긴 제거가 남긴 maintenance.flag(by=uninstall)를 지웠습니다 — 다시 켭니다"
    fi
  fi
  if [ -e "$DATA_DIR/maintenance.flag" ]; then
    warn "maintenance.flag 가 있어 기동하지 않습니다 — 다시 켜기: sh $(shq "$_lc") resume"
    [ -n "$_hp" ] && warn "떠 있는 Host(pid $_hp)는 그대로 둡니다 — 새 설치는 다음 재기동부터 반영됩니다"
    return 0
  fi
  [ -n "$_hp" ] && stop_host "$ROOT"            # 이미 떠 있으면 내린 뒤 새로(옛 코드·옛 의존성으로 계속 돌지 않게)
  mark_release                                  # 이제부터는 감독자가 띄워도 된다(새 파일이 다 놓였다)
  ( unset CLEWPATH_CLIENT_TOKEN CLEWPATH_AGENT_TOKEN CLEWPATH_UPDATER
    exec /bin/sh "$_lc" start ) </dev/null
  _sr=$?
  [ "$_sr" = 0 ] || warn "런처가 실패했습니다(종료코드 $_sr) — 기록: $DATA_DIR/launcher.log"
  hostctl "$I_TMP/ready.out" wait-ready --port "$EFF_PORT" --tries 20 --data-dir "$DATA_DIR"
  _rp=$(kv port "$I_TMP/ready.out")
  case "$_rp" in ''|*[!0-9]*) ;; *) EFF_PORT=$_rp ;; esac
}

guide() {
  _lc="$ROOT/start-connector.sh"
  say ""
  say "=== 설치 완료 ==="
  say ""
  say "  1) 이 PC 에서 열기 :  http://127.0.0.1:$EFF_PORT/"
  say "  2) 폰에서 쓰려면   :  로컬 웹 오른쪽 위 [📱] -> '새 기기 추가' -> QR 스캔"
  say "  3) 서비스 관리     :"
  say "       시작 : sh $(shq "$_lc")"
  say "       상태 : sh $(shq "$_lc") status"
  say "       멈춤 : sh $(shq "$_lc") stop --hold   (다시 켜기: sh $(shq "$_lc") resume)"
  say "       감시 : nohup sh $(shq "$_lc") watch >/dev/null 2>&1 &   (임시 — 자동 시작이 생기기 전까지)"
  say "       제거 : sh $(shq "$ROOT/uninstall.sh")"
  say ""
  say "  설정 파일: $CONF_FILE"
  say "             (포트·주소는 여기서 바꾸고 재시작하세요. 재설치해도 유지됩니다)"
  say "  자격증명 : $DATA_DIR/cp_credentials.json"
  say "  실제 주소: $DATA_DIR/runtime.json"
  if [ "$SYSTEM_UV" = 1 ]; then
    say "  uv       : ${UV:-?} (PATH 의 uv — ClewPath 가 설치한 것이 아닙니다)"
  else
    say "  uv       : $CLEWPATH_HOME/bin/uv  (제거할 때 --purge-uv 를 붙이면 함께 지웁니다)"
  fi
  say ""
}

# ---------------------------------------------------------------- 인자

parse_args() {
  while [ $# -gt 0 ]; do
    PA_K=$1
    PA_V=
    PA_EQ=0
    case "$PA_K" in --*=*) PA_V=${PA_K#*=}; PA_K=${PA_K%%=*}; PA_EQ=1 ;; esac
    case "$PA_K" in
      --cp-url|--relay-url|--app-url|--port|--python|--uv-version|--from-stage|--dir|--client-token|--agent-token)
        if [ "$PA_EQ" = 0 ]; then
          [ $# -ge 2 ] || usage_die "$PA_K 의 값이 없습니다"
          PA_V=$2
          shift
        fi ;;
      --system-uv|--reinstall|--no-autostart|--dry-run|--no-provision|--allow-root|--allow-http|-h|--help)
        [ "$PA_EQ" = 0 ] || usage_die "$PA_K 는 값을 받지 않습니다" ;;
      -*) usage_die "모르는 인자: $PA_K" ;;
      *) usage_die "모르는 인자(값은 보이지 않게 생략)" ;;   # 토큰을 잘못 붙였을 수 있다 — 화면에 찍지 않는다
    esac
    case "$PA_K" in
      --cp-url) CP_URL=$PA_V ;;
      --relay-url) RELAY_URL=$PA_V ;;
      --app-url) APP_URL=$PA_V ;;
      --port) PORT=$PA_V ;;
      --python) PYVER=$PA_V ;;
      --uv-version) UV_VERSION=$PA_V ;;
      --from-stage) FROM_STAGE=$PA_V ;;
      --dir) DIR=$PA_V ;;
      --client-token) CLIENT_TOKEN=$PA_V; TOKEN_ARGV=1 ;;
      --agent-token) AGENT_TOKEN=$PA_V; TOKEN_ARGV=1 ;;
      --system-uv) SYSTEM_UV=1 ;;
      --reinstall) REINSTALL=1 ;;
      --no-autostart) NO_AUTOSTART=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --no-provision) NO_PROVISION=1 ;;
      --allow-root) ALLOW_ROOT=1 ;;
      --allow-http) ALLOW_HTTP=1 ;;
      -h|--help) usage; exit 0 ;;
    esac
    shift
  done
}

tokens_to_env() {   # 토큰 인자를 env 로 옮기고 그 인자만 뺀 채 이 스크립트를 다시 실행한다(exec — pid 그대로, 돌아오지 않는다).
  # 그대로 두면 uv·파이썬·의존성을 까는 내내(수 분) /proc/<pid>/cmdline(같은 PC 누구나, ps)에 토큰이 남는다(get.sh·서막이
  # 하는 것과 같다). 스크립트를 파일로 다시 읽을 수 없으면(표준 입력으로 돌림) 그대로 잇는다
  [ -f "$0" ] || return 0
  TE_N=$#
  while [ "$TE_N" -gt 0 ]; do
    TE_A=$1
    shift
    TE_N=$((TE_N - 1))
    case "$TE_A" in
      --client-token|--agent-token) shift; TE_N=$((TE_N - 1)); continue ;;   # 값은 parse_args 가 있는 것을 확인했다
      --client-token=*|--agent-token=*) continue ;;
    esac
    set -- ${1+"$@"} "$TE_A"                      # 앞에서 빼고 뒤에 붙인다 — 다 돌면 남긴 인자만 원래 순서로
  done
  if [ -n "$CLIENT_TOKEN" ]; then export CLEWPATH_CLIENT_TOKEN="$CLIENT_TOKEN"; fi
  if [ -n "$AGENT_TOKEN" ]; then export CLEWPATH_AGENT_TOKEN="$AGENT_TOKEN"; fi
  exec /bin/sh "$0" ${1+"$@"}
}

check_args() {
  while :; do                                   # 끝의 / 는 반복해서 걷어 낸다(get.ps1 과 같은 정규화)
    case "$CP_URL" in */) CP_URL=${CP_URL%/} ;; *) break ;; esac
  done
  check_url "$CP_URL" https --cp-url
  check_url "$RELAY_URL" wss --relay-url
  check_url "$APP_URL" https --app-url
  case "$PORT" in ''|0*|*[!0-9]*) usage_die "--port 는 1-65535 입니다: $PORT" ;; esac
  if [ "${#PORT}" -gt 5 ] || [ "$PORT" -gt 65535 ]; then usage_die "--port 는 1-65535 입니다: $PORT"; fi
  case "$PYVER" in [0-9]*.[0-9]*) ;; *) usage_die "--python 은 3.11 같은 버전입니다: $PYVER" ;; esac
  case "$PYVER" in *[!0-9.]*|*..*|*.) usage_die "--python 은 3.11 같은 버전입니다: $PYVER" ;; esac
  case "$UV_VERSION" in *[!0-9.]*|.*|*.|*..*|'') usage_die "--uv-version 은 0.12.1 같은 버전입니다: $UV_VERSION" ;; esac
  case "$CLIENT_TOKEN$AGENT_TOKEN" in *[[:cntrl:]]*) usage_die "토큰에 줄바꿈·제어문자가 있습니다" ;; esac
  if [ -n "$DIR" ] && [ -z "$FROM_STAGE" ]; then usage_die "--dir 은 --from-stage 와 함께만 씁니다(get.sh 가 쓴다)"; fi
  if [ -n "$FROM_STAGE" ] && [ -z "$DIR" ]; then usage_die "--from-stage 에는 --dir 가 필요합니다"; fi
}

# ---------------------------------------------------------------- 흐름

main() {
  CP_URL="https://clewpath.pyongso.com/cp"
  RELAY_URL="wss://clewpath.pyongso.com/relay/ws"
  APP_URL="https://clewpath.pyongso.com/relay/app"
  PORT=5100
  PYVER=3.11
  UV_VERSION=$UV_DEFAULT
  SYSTEM_UV=0
  REINSTALL=0
  NO_AUTOSTART=0
  DRY_RUN=0
  NO_PROVISION=0
  ALLOW_ROOT=0
  ALLOW_HTTP=0
  HTTP_USED=0
  FROM_STAGE=''
  DIR=''
  CLIENT_TOKEN=${CLEWPATH_CLIENT_TOKEN:-}
  AGENT_TOKEN=${CLEWPATH_AGENT_TOKEN:-}
  TOKEN_ARGV=0
  I_TMP=''
  GET_TMP=''
  MARK_DIR=''
  UV=''
  PYEXE=''
  PY_PREF=''
  PY_BIN_DIR=''
  PY_INSTALL_DIR="$CLEWPATH_HOME/python"
  OS=''
  ROOT=''
  DATA_DIR=''
  CONF_FILE=''
  EFF_PORT=''
  LP_DD=''
  LP_CF=''
  HC_CONF=''
  DEV_TREE=0
  VENV_MARK=.clewpath-venv
  parse_args "$@"
  ROOT=$(CDPATH='' cd "$(dirname "$0")" && phys_pwd) || die "설치 폴더를 찾지 못했습니다: $0"
  if [ -n "$FROM_STAGE" ]; then                 # get.sh 의 임시 stage 를 먼저 알아 둔다 — 서막의 어떤 거절에도 on_exit 가 치운다
    stage_temp
    trap 'on_exit' EXIT
    trap 'exit 1' INT TERM HUP
  fi
  check_args
  if [ "$TOKEN_ARGV" = 1 ]; then                # 경고는 한 번(다시 실행한 쪽엔 토큰 인자가 없다)
    warn "--client-token·--agent-token 은 같은 PC 의 다른 사용자에게 보입니다(ps) — 환경변수로 넘기세요:"
    say "         CLEWPATH_CLIENT_TOKEN=… sh install.sh"
    tokens_to_env "$@"
  fi
  EFF_PORT=$PORT

  # I0. 사전 점검
  case "$(uname -s)" in
    Darwin) OS=mac ;;
    Linux) OS=linux ;;
    *) die "지원하지 않는 OS 입니다: $(uname -s)" ;;
  esac
  if [ "$(id -u)" = 0 ] && [ "$ALLOW_ROOT" != 1 ]; then
    die "root(관리자)로는 설치하지 않습니다 — 일반 사용자로 실행하세요(sudo 없이)"
  fi
  path_ok "$ROOT" || die "설치 폴더 경로에 쓸 수 없는 글자가 있습니다: $ROOT"
  [ -d "$ROOT/session_manager" ] \
    || die "이 스크립트는 ClewPath Host 폴더 안에서 실행해야 합니다(session_manager 폴더가 안 보임)."
  VPY="$ROOT/.venv/bin/python"
  CLAUDE_HOME=$(claude_home_guess)
  path_ok "$CLAUDE_HOME" || die "클로드 폴더가 절대 경로가 아닙니다(SESSION_MANAGER_CLAUDE_HOME·CLAUDE_CONFIG_DIR): $CLAUDE_HOME"
  command -v mktemp >/dev/null 2>&1 || die "mktemp 가 없습니다"
  I_TMP=$(mktemp -d "${TMPDIR:-/tmp}/clewpath-install.XXXXXX") || die "임시 폴더를 만들 수 없습니다(TMPDIR)"
  trap 'on_exit' EXIT
  trap 'exit 1' INT TERM HUP

  if [ -n "$FROM_STAGE" ]; then                 # get.sh 경로 — 옮긴 뒤 exec 로 이어 가므로 돌아오지 않는다
    say ""
    say "=== ClewPath Host 설치 준비 — 받은 파일을 설치 폴더로: $DIR ==="
    prelude
  fi
  mac_ascii_guard "$ROOT"

  say ""
  say "=== ClewPath Host 설치 ==="
  say "  대상 폴더 : $ROOT"
  say "  컨트롤플레인: $CP_URL"
  say "  릴레이      : $RELAY_URL"
  [ "$OS" = linux ] && warn "리눅스는 시험용입니다(공식 지원 아님)"
  [ "$HTTP_USED" = 1 ] && warn "--allow-http — 암호화되지 않은 주소를 씁니다(시험 전용)"
  [ "$ALLOW_ROOT" = 1 ] && [ "$(id -u)" = 0 ] && warn "--allow-root — root 로 설치합니다(시험 전용)"
  [ "$DRY_RUN" = 1 ] && warn "DryRun — 점검만 하고 아무것도 변경하지 않습니다."

  if [ -e "$ROOT/.git" ] || [ -d "$ROOT/ops" ]; then
    DEV_TREE=1
    warn "개발 체크아웃 — 자동 업데이트는 꺼진 채로 동작합니다: $ROOT"
  fi
  root_guard "$ROOT"                            # 홈·클로드 폴더(나 그 위)면 — 그 폴더의 런처(paths)를 부르기 전에 멈춘다
  # 경로 — 기존 런처가 있으면 그 말(데이터 폴더·설정 파일)을 따른다(재설치를 다른 env 의 셸에서 해도 같은 폴더로)
  data_dir_guess "$ROOT"
  if [ -n "$LP_DD" ] && [ -z "${SESSION_MANAGER_CLAUDE_HOME:-}${CLAUDE_CONFIG_DIR:-}" ]; then
    case "$LP_DD" in */session_manager) CLAUDE_HOME=${LP_DD%/session_manager} ;; esac
  fi
  HC_CONF=${CLEWPATH_CONFIG:-$LP_CF}
  CONF_FILE=${HC_CONF:-$DATA_DIR/clewpath.toml}
  # 설치 폴더 — 데이터 폴더(나 그 위)도 아니고, 첫 설치면 우리 항목만, .venv 는 우리가 만든 것만
  root_guard "$ROOT"
  check_root_items
  check_venv_owner
  # I2. 표식 — 업데이트·다른 설치와 겹치지 않게, 감독자가 그동안 띄우지 않게
  if [ "$DRY_RUN" = 1 ]; then
    swap_left "$ROOT" && warn "중단된 업데이트 잔재가 있습니다($ROOT/.clewpath-swap.*) — 실제 설치 때 되돌립니다"
    marker_busy "$DATA_DIR" && warn "업데이트·설치가 진행 중입니다(pid ${MK_PID:-?}) — 실제 설치는 거부됩니다"
  else
    if [ -d "$(dirname "$DATA_DIR")" ] && swap_left "$ROOT"; then recover_swaps; fi
    take_marker "$DATA_DIR"
  fi

  step_uv
  step_python
  step_claude
  step_deps
  step_paths
  step_config
  step_start
  guide
  exit 0
}

main "$@"
