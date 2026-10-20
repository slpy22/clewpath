#!/bin/sh
# clewpath-launcher v2 (posix) — 자동 생성됨(install.sh / Host). 업데이트 뒤 Host 가 템플릿으로 다시 만듭니다. 직접 고치지 마세요.
# 설정은 이 파일이 아니라 {{CONF_FILE}} 을 고치세요.
#
# 멱등 런처 — Windows start-connector.template.ps1 의 맥·리눅스판(docs/designs/posix-install.md 3.3).
#   sh start-connector.sh [start]        떠 있으면 아무것도 안 한다. 죽어 있을 때만(health 없음 + pid 없음) 새로 띄운다
#   sh start-connector.sh stop [--hold]  SIGTERM(정상 종료 표식·브로커 보존), 15초 뒤에도 남으면 SIGKILL.
#                                        --hold 면 maintenance.flag 도 만든다(다시 켜기: resume)
#   sh start-connector.sh resume         maintenance.flag 를 지우고 start
#   sh start-connector.sh status         state= pid= port= url= broker_pid= broker_alive= watch_pid= maintenance=
#                                        (종료코드 0 정상 / 1 아님 / 3 유지보수)
#   sh start-connector.sh watch [초]     상주 감시 — 자동 시작이 생기기 전의 임시용(기본 60초, 최소 10초). 하나만 돈다
#   sh start-connector.sh paths          root= data_dir= conf_file= default_port= python=
#   sh start-connector.sh foreground     (다음 단계 launchd·systemd 용) 이 프로세스가 곧 Host 가 된다
# 지키는 것:
#  - health 실패만으로 기존 프로세스를 죽이거나 겹쳐 띄우지 않는다(기동 중일 수 있음)
#  - maintenance.flag 면 아무것도 띄우지 않는다. 업데이트·설치 중(update.inprogress)이면 러너 자신의 재기동만 통과
#  - 프로세스는 명령줄(설치 경로·모듈)로 알아본다 — venv 파이썬은 심볼릭 링크라 실행 파일로 고르지 않는다.
#    pid 재사용·좀비는 죽은 것으로 본다
#  - Host 는 사용자의 umask·HOME·TMPDIR·PATH·LANG 을 그대로 물려받는다(그 안의 claude 가 다시 물려받는다).
#    env 에서는 claude 표식만 뺀다(브로커 ptyd.clean_env 와 같은 규칙)
#  - 판정은 venv 파이썬(표준 라이브러리·psutil)으로 — session_manager 를 import 하지 않아 롤백 직후 버전이 어긋나도 돈다
# 로그: {{DATA_DIR}}/host.log(.err.log, 이어 쓰기), 런처 기록은 같은 폴더의 launcher.log

DATA_DIR={{DATA_DIR}}
CONF_FILE={{CONF_FILE}}
DEFAULT_PORT={{PORT}}
ROOT=$(CDPATH='' cd "$(dirname "$0")" && pwd -P) || exit 1
PY="$ROOT/.venv/bin/python"
SELF="$ROOT/${0##*/}"
NL='
'

# 상태 판정 — 출력 "<ok|busy|dead|none> <pid> <port>". runtime.json 의 port 로 health(프록시 무시) → 안 되면 pid 를
# 본다: 있고·좀비 아니고·내 uid·runtime.json 기록보다 먼저 생겼고·명령줄 인자가 Host 모듈이면 busy(기동 중·응답 불능).
PROBE_PY='
import json, os, sys, urllib.request
data, port = sys.argv[1], int(sys.argv[2])
pid, started = 0, 0
try:
    with open(os.path.join(data, "runtime.json"), encoding="utf-8-sig") as f:
        rt = json.load(f)
    if type(rt.get("port")) is int and 0 < rt["port"] < 65536:
        port = rt["port"]
    if type(rt.get("pid")) is int and rt["pid"] > 0:
        pid = rt["pid"]
    if type(rt.get("started_at")) in (int, float):
        started = rt["started_at"]
except Exception:
    pass
healthy = False
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:%d/api/health" % port, timeout=3) as r:
        body = json.loads(r.read(65536))
    healthy = isinstance(body, dict) and body.get("status") == "ok"
except Exception:
    pass


def is_host(args):
    return any(a == "session_manager.server" or a.endswith("/clewpath-host") for a in args)


def by_proc(pid):
    try:
        os.kill(pid, 0)
    except Exception:
        return "dead"
    if not os.path.isdir("/proc/self"):
        return "busy"
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            st = f.read().rsplit(b")", 1)[1].split()[0]
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = f.read().decode("utf-8", "replace").split("\0")
    except Exception:
        return "dead"
    return "busy" if st not in (b"Z", b"X") and is_host(args) else "dead"


def state_of(pid):
    if not pid:
        return "none"
    try:
        import psutil
    except Exception:
        return by_proc(pid)
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE or p.uids().real != os.getuid():
            return "dead"
        if started and p.create_time() > started + 5:
            return "dead"
        return "busy" if is_host(p.cmdline()) else "dead"
    except psutil.AccessDenied:
        return "busy"
    except Exception:
        return "dead"


print("ok" if healthy else state_of(pid), pid, port)
'

# 기동 — env 에서 claude 표식·러너 표식을 빼고 toml 위치를 고정해, 새 세션(setsid)으로 띄운다. 로그는 이어 쓰기.
# runtime.json 에 새 pid 가 적히거나(up) 끝날 때(exited:rc)까지 최대 15초 기다린다 — 그동안 잠금을 쥐고 있어야
# 다른 런처가 '아직 runtime.json 없음'을 죽음으로 읽고 겹쳐 띄우지 않는다. 출력 "<pid> <up|starting|exited:rc>".
# exec 모드(foreground)는 이 프로세스를 Host 로 바꾼다(pid 그대로 — launchd·systemd 가 지켜볼 수 있게).
SPAWN_PY='
import json, os, subprocess, sys, time
root, data, conf, mode = sys.argv[1:5]
SCRUB = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLEWPATH_UPDATER"}
env = {k: v for k, v in os.environ.items() if k not in SCRUB and not k.upper().startswith("CLAUDE_CODE_")}
env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", CLEWPATH_CONFIG=conf)
py = os.path.join(root, ".venv", "bin", "python")
argv = [py, "-m", "session_manager.server"]
if mode == "exec":
    os.chdir(root)
    os.execve(py, argv, env)
with open(os.path.join(data, "host.log"), "ab") as out, open(os.path.join(data, "host.err.log"), "ab") as err:
    p = subprocess.Popen(argv, cwd=root, env=env, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=err)
state, end = "starting", time.monotonic() + 15
while time.monotonic() < end:
    rc = p.poll()
    if rc is not None:
        state = "exited:%d" % rc
        break
    try:
        with open(os.path.join(data, "runtime.json"), encoding="utf-8-sig") as f:
            if json.load(f).get("pid") == p.pid:
                state = "up"
                break
    except Exception:
        pass
    time.sleep(0.2)
print(p.pid, state)
'

# stop 이 강제 종료(SIGKILL)했을 때만 — 죽인 Host 의 pid 로 정상 종료 표식(다음 기동이 사고로 보지 않게). 실패는 무시.
MARK_PY='
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from session_manager import config, liveness
config.data_dir = lambda: Path(sys.argv[2])
liveness.mark_shutdown("forced-stop", pid=int(sys.argv[3]))
'

say() { printf '%s\n' "$*" >&2; }
note() { [ -t 2 ] && printf '%s\n' "$*" >&2; return 0; }   # 사람이 터미널에서 불렀을 때만(watch·러너 로그는 조용히)
llog() { { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$DATA_DIR/launcher.log"; } 2>/dev/null; return 0; }
usage() { printf '%s\n' "쓰는 법: sh \"$SELF\" [start | stop [--hold] | resume | status | watch [초] | paths | foreground]"; }
usage_error() { usage >&2; exit 2; }

data_dir_ready() {   # 폴더 권한만 좁힌다 — umask 는 사용자 것 그대로(Host 안의 claude 가 물려받는다)
  [ -d "$DATA_DIR" ] || mkdir -p "$DATA_DIR" || { say "데이터 폴더를 만들 수 없습니다: $DATA_DIR"; exit 1; }
  chmod 700 "$DATA_DIR" 2>/dev/null
  return 0
}

need_venv() {
  [ -x "$PY" ] && return 0
  llog "fail: venv 없음($PY)"
  say "venv 가 없습니다($PY) — sh \"$ROOT/install.sh\" --reinstall"
  exit 2
}

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

cmd_has() {   # $1=pid $2=글자 — 명령줄에 있나. 못 읽으면 있다고 본다(보수적)
  _cc=$(cmdline_of "$1")
  [ -z "$_cc" ] && return 0
  case "$_cc" in *"$2"*) return 0 ;; esac
  return 1
}

is_our_host() {   # $1=pid — 이 설치본의 Host 인가: 인자 하나는 "$ROOT/.venv/bin/…", 다른 하나는 Host 모듈(또는 진입점)
  pid_alive "$1" || return 1
  if [ -d /proc/self ]; then
    _hc="$NL$(cmdline_of "$1")$NL"
    case "$_hc" in *"$NL$ROOT/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_hc" in *"${NL}session_manager.server$NL"*|*"/clewpath-host$NL"*) return 0 ;; esac
  else
    _hc=" $(cmdline_of "$1") "
    case "$_hc" in *" $ROOT/.venv/bin/"*) ;; *) return 1 ;; esac
    case "$_hc" in *" session_manager.server "*|*"/clewpath-host "*) return 0 ;; esac
  fi
  return 1
}

json_pid() {   # $1=파일 → "pid": N (runtime.json·ptyd.json — 파이썬 없이 읽는다: stop 은 venv 가 깨져도 돌아야 한다)
  sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -n 1
}

# 잠금 = 폴더(mkdir 은 원자적) + 안의 pid. 시체: pid 가 죽음·다른 프로그램(명령줄에 $2 없음), 또는 pid 없이 2분 넘게 묵음
lock_live() {
  [ -d "$1" ] || return 1
  _lp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$1/pid" 2>/dev/null)
  if [ -z "$_lp" ]; then
    [ -n "$(find "$1" -maxdepth 0 -mmin +2 2>/dev/null)" ] && return 1
    return 0                                            # pid 를 막 쓰려는 참
  fi
  pid_alive "$_lp" && cmd_has "$_lp" "$2"
}

lock_drop() {
  case "$1" in "$DATA_DIR"/*.lock|"$DATA_DIR"/*.lock.d) rm -rf "$1" ;; esac
}

lock_acquire() {   # $1=잠금 폴더 $2=쥔 프로세스 명령줄의 글자. 시체면 지우고 한 번만 다시 잡는다
  if ! mkdir "$1" 2>/dev/null; then
    lock_live "$1" "$2" && return 1
    lock_drop "$1"
    mkdir "$1" 2>/dev/null || return 1
  fi
  if ! { printf '%s\n' "$$" >"$1/pid"; } 2>/dev/null; then
    rmdir "$1" 2>/dev/null                             # pid 없는 잠금을 남기면 남들이 2분을 기다린다
    return 1
  fi
  return 0
}

lock_release() {   # 내가 쥔 것만 푼다
  [ "$(sed -n '1p' "$1/pid" 2>/dev/null)" = "$$" ] && lock_drop "$1"
  return 0
}

# trap 문자열에는 경로를 넣지 않는다(경로에 따옴표가 있어도 trap 이 그대로 돌게)
start_unlock() { lock_release "$DATA_DIR/launcher.lock"; }
watch_unlock() { lock_release "$DATA_DIR/watch.lock.d"; }

update_live() {   # 0 = 업데이트·설치 진행 중(update.inprogress 의 pid 가 그 프로그램으로 살아 있고 30분 안). $1=clean 이면 시체를 지운다
  _uf="$DATA_DIR/update.inprogress"
  [ -f "$_uf" ] || return 1
  _up=$(sed -n 's/^pid=\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  _ua=$(sed -n 's/^at=\([0-9]\{1,12\}\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  _ub=$(sed -n 's/^by=\([a-z]*\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  if [ -z "$_up" ] || [ -z "$_ua" ]; then
    [ -z "$(find "$_uf" -mmin +1 2>/dev/null)" ] && return 0      # 막 쓰는 중일 수 있다 — 1분은 기다린다
  elif pid_alive "$_up" && [ "$(( $(date +%s) - _ua ))" -lt 1800 ]; then
    case "$_ub" in
      runner) cmd_has "$_up" update_runner && return 0 ;;
      install) cmd_has "$_up" install.sh && return 0 ;;
      *) return 0 ;;
    esac
  fi
  if [ "${1:-}" = clean ]; then
    rm -f "$_uf"
    llog "stale update marker 제거(pid ${_up:-?}, by=${_ub:-?})"
  fi
  return 1
}

swap_left() {   # 중단된 업데이트의 교체 잔재(러너 저널)
  for _sd in "$ROOT"/.clewpath-swap.*; do
    [ -d "$_sd" ] && return 0
  done
  return 1
}

runner_live() { lock_live "$DATA_DIR/updates/runner.lock.d" update_runner; }

recover_swap_leftovers() {   # 0 계속 / 10 보류(러너가 교체 중) / 1 막힘
  swap_left || return 0
  if runner_live; then
    [ "${CLEWPATH_UPDATER:-}" = 1 ] && return 0                       # 러너 자신의 재기동
    llog "skip: 러너가 교체 중(.clewpath-swap.*)"
    return 10
  fi
  _rr="$DATA_DIR/updates/update_runner.sh"
  if [ ! -f "$_rr" ]; then
    llog "blocked: 중단된 업데이트 잔재($ROOT/.clewpath-swap.*) — 러너 사본 없음"
    say "중단된 업데이트 잔재가 있어 띄우지 않습니다 — sh \"$ROOT/install.sh\" 로 복구하세요"
    return 1
  fi
  llog "recover: 중단된 업데이트 잔재 → 러너 사본 -RecoverOnly"
  /bin/sh "$_rr" -RecoverOnly -InstallRoot "$ROOT" -LogFile "$DATA_DIR/updates/recover.log" \
    </dev/null >>"$DATA_DIR/updates/recover.log" 2>&1
  _rc=$?
  if [ "$_rc" = 3 ]; then
    llog "skip: 복구 보류(rc=3 — 다른 러너가 돌고 있거나 이 설치본의 Host 가 떠 있음)"
    return 10
  fi
  if [ "$_rc" != 0 ] || swap_left; then
    llog "blocked: 잔재 복구 실패(rc=$_rc)"
    say "중단된 업데이트를 복구하지 못했습니다(rc=$_rc, $DATA_DIR/updates/recover.log) — sh \"$ROOT/install.sh\" 로 복구하세요"
    return 1
  fi
  llog "recover: 완료"
  return 0
}

probe_now() {   # P_STATE P_PID P_PORT 를 채운다. 판정 못 하면(venv 파이썬 고장) 1
  P_STATE=none
  P_PID=0
  P_PORT=$DEFAULT_PORT
  _po=$("$PY" -I -c "$PROBE_PY" "$DATA_DIR" "$DEFAULT_PORT" 2>/dev/null) || return 1
  case "$_po" in "ok "*|"busy "*|"dead "*|"none "*) ;; *) return 1 ;; esac
  P_STATE=${_po%% *}
  _po=${_po#* }
  P_PID=${_po%% *}
  P_PORT=${_po#* }
  case "$P_PID" in ''|*[!0-9]*) return 1 ;; esac
  case "$P_PORT" in ''|*[!0-9]*) return 1 ;; esac
  return 0
}

probe_or_die() {
  probe_now && return 0
  llog "fail: 상태 판정 실패(venv 파이썬 — $PY)"
  say "venv 파이썬으로 상태를 판정하지 못했습니다($PY) — sh \"$ROOT/install.sh\" --reinstall"
  exit 2
}

rotate() {   # 5MB 넘으면 한 세대 밀어 둔다(기동 직전에만 — 이어 쓰는 Host 가 없을 때)
  [ -f "$1" ] || return 0
  _sz=$(wc -c <"$1" 2>/dev/null | tr -d ' \t')
  case "$_sz" in ''|*[!0-9]*) return 0 ;; esac
  [ "$_sz" -gt 5242880 ] && mv -f "$1" "$1.1" 2>/dev/null
  return 0
}

cmd_start() {
  need_venv
  data_dir_ready
  if [ -e "$DATA_DIR/maintenance.flag" ]; then
    note "maintenance.flag 가 있어 띄우지 않습니다 — 다시 켜기: sh \"$SELF\" resume"
    exit 0
  fi
  if update_live clean && [ "${CLEWPATH_UPDATER:-}" != 1 ]; then
    llog "skip: 업데이트 진행 중"
    note "업데이트·설치가 진행 중이라 띄우지 않습니다(끝나면 러너가 다시 띄웁니다)"
    exit 0
  fi
  recover_swap_leftovers
  case $? in 0) ;; 10) exit 0 ;; *) exit 1 ;; esac
  probe_or_die
  case "$P_STATE" in
    ok) note "이미 실행 중입니다 — pid $P_PID, http://127.0.0.1:$P_PORT/"; exit 0 ;;
    busy) llog "skip: pid $P_PID 살아 있음(health 없음 — 기동 중이거나 응답 불능)"
          note "pid $P_PID 가 살아 있지만 응답이 없습니다(기동 중이거나 응답 불능) — 겹쳐 띄우지 않습니다"
          exit 0 ;;
  esac
  if ! lock_acquire "$DATA_DIR/launcher.lock" "${0##*/}"; then
    llog "skip: 다른 런처가 기동 중(launcher.lock)"
    exit 0
  fi
  trap 'start_unlock' EXIT
  trap 'exit 1' INT TERM HUP
  probe_or_die
  case "$P_STATE" in ok|busy) exit 0 ;; esac            # 잠금 안에서 한 번 더 — 그 사이 다른 런처가 띄웠을 수 있다
  rotate "$DATA_DIR/host.log"
  rotate "$DATA_DIR/host.err.log"
  rotate "$DATA_DIR/launcher.log"
  if [ "$P_PID" = 0 ]; then S_WHY="runtime.json 없음"; else S_WHY="이전 pid $P_PID 없음(health 없음)"; fi
  llog "start: $S_WHY → 기동"
  { printf '=== launcher start %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$DATA_DIR/host.log"; } 2>/dev/null
  if ! S_OUT=$("$PY" -I -c "$SPAWN_PY" "$ROOT" "$DATA_DIR" "$CONF_FILE" spawn 2>>"$DATA_DIR/launcher.log"); then
    llog "fail: spawn 실패"
    say "Host 를 띄우지 못했습니다 — $DATA_DIR/launcher.log 를 보세요"
    exit 1
  fi
  S_PID=${S_OUT%% *}
  S_ST=${S_OUT#* }
  case "$S_PID" in ''|*[!0-9]*) llog "fail: spawn 결과를 읽지 못함($S_OUT)"; exit 1 ;; esac
  case "$S_ST" in
    up) llog "spawned pid $S_PID"
        note "기동했습니다 — pid $S_PID (로그: $DATA_DIR/host.log)" ;;
    exited:*) llog "fail: 기동 직후 종료(pid $S_PID, rc=${S_ST#exited:})"
              say "Host 가 기동 직후 끝났습니다(rc=${S_ST#exited:}) — $DATA_DIR/host.err.log 를 보세요"
              exit 1 ;;
    *) llog "spawned pid $S_PID (runtime.json 아직 없음 — 기동 중)"
       note "기동 중입니다 — pid $S_PID (로그: $DATA_DIR/host.log)" ;;
  esac
  exit 0
}

cmd_stop() {
  case "${1:-}" in '') K_HOLD=0 ;; --hold) K_HOLD=1 ;; *) usage_error ;; esac
  if [ "$K_HOLD" = 1 ]; then
    data_dir_ready
    if ! { printf 'by=stop\nat=%s\n' "$(date +%s)" >"$DATA_DIR/maintenance.flag"; } 2>/dev/null; then
      say "maintenance.flag 를 만들 수 없습니다: $DATA_DIR"
      exit 1
    fi
    llog "stop --hold: maintenance.flag"
    note "maintenance.flag 를 만들었습니다 — 다시 켜기: sh \"$SELF\" resume"
  fi
  K_PID=$(json_pid "$DATA_DIR/runtime.json")
  if ! is_our_host "$K_PID"; then
    printf 'not running\n'
    exit 0
  fi
  kill -TERM "$K_PID" 2>/dev/null
  llog "stop: SIGTERM pid $K_PID"
  K_END=$(( $(date +%s) + 15 ))                           # Host 의 _shutdown_soon 과 같은 15초
  while is_our_host "$K_PID" && [ "$(date +%s)" -lt "$K_END" ]; do
    sleep 0.5 2>/dev/null || sleep 1
  done
  if ! is_our_host "$K_PID"; then
    printf 'stopped (pid %s)\n' "$K_PID"
    exit 0
  fi
  kill -KILL "$K_PID" 2>/dev/null
  llog "stop: forced — pid $K_PID 가 15초 안에 끝나지 않아 SIGKILL"
  [ -x "$PY" ] && "$PY" -I -c "$MARK_PY" "$ROOT" "$DATA_DIR" "$K_PID" >/dev/null 2>&1
  K_END=$(( $(date +%s) + 3 ))
  while is_our_host "$K_PID" && [ "$(date +%s)" -lt "$K_END" ]; do
    sleep 0.5 2>/dev/null || sleep 1
  done
  printf 'stopped (pid %s, forced)\n' "$K_PID"
  exit 0
}

cmd_resume() {
  if [ -e "$DATA_DIR/maintenance.flag" ]; then
    rm -f "$DATA_DIR/maintenance.flag" || { say "maintenance.flag 를 지울 수 없습니다"; exit 1; }
    llog "resume: maintenance.flag 제거"
  fi
  cmd_start
}

cmd_status() {
  T_VENV=ok
  if [ ! -x "$PY" ]; then
    T_VENV=missing
  elif ! probe_now; then
    T_VENV=broken
  fi
  if [ "$T_VENV" != ok ]; then
    P_STATE=none
    P_PID=0
    P_PORT=$DEFAULT_PORT
  fi
  T_MAINT=0
  [ -e "$DATA_DIR/maintenance.flag" ] && T_MAINT=1
  if [ "$P_STATE" = ok ]; then T_STATE=running; T_RC=0
  elif [ "$T_MAINT" = 1 ]; then T_STATE=maintenance; T_RC=3
  elif update_live || { swap_left && runner_live; }; then T_STATE=updating; T_RC=1
  elif [ "$P_STATE" = busy ]; then T_STATE=starting; T_RC=1
  else T_STATE=stopped; T_RC=1
  fi
  T_PID=
  case "$P_STATE" in ok|busy) [ "$P_PID" = 0 ] || T_PID=$P_PID ;; esac
  T_BP=$(json_pid "$DATA_DIR/ptyd.json")
  T_BA=0
  if [ -n "$T_BP" ] && pid_alive "$T_BP"; then
    case "$(cmdline_of "$T_BP")" in *session_manager.ptyd*) T_BA=1 ;; esac
  fi
  T_WP=
  if lock_live "$DATA_DIR/watch.lock.d" "${0##*/}"; then
    T_WP=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$DATA_DIR/watch.lock.d/pid" 2>/dev/null)
  fi
  printf 'state=%s\npid=%s\nport=%s\nurl=http://127.0.0.1:%s/\nbroker_pid=%s\nbroker_alive=%s\nwatch_pid=%s\nmaintenance=%s\n' \
    "$T_STATE" "$T_PID" "$P_PORT" "$P_PORT" "$T_BP" "$T_BA" "$T_WP" "$T_MAINT"
  [ "$T_VENV" = ok ] || printf 'venv=%s\n' "$T_VENV"
  exit "$T_RC"
}

watch_tick() {
  [ -e "$DATA_DIR/maintenance.flag" ] && return 0
  if [ -x "$PY" ] && probe_now && { [ "$P_STATE" = ok ] || [ "$P_STATE" = busy ]; }; then
    W_DEAD=0
    W_CUR=$W_IV
    return 0
  fi
  W_DEAD=$((W_DEAD + 1))
  /bin/sh "$SELF" start </dev/null >/dev/null 2>&1         # 새 프로세스로 — 잠금 pid 가 진짜가 되고, 다시 렌더된 런처를 쓴다
  if [ "$W_DEAD" -ge 3 ] && [ "$W_CUR" -lt "$W_MAX" ]; then
    W_CUR=$((W_CUR * 2))
    [ "$W_CUR" -gt "$W_MAX" ] && W_CUR=$W_MAX
    llog "watch: backoff — 연속 ${W_DEAD}틱 dead, 이제 ${W_CUR}초마다"
  fi
  return 0
}

watch_end() {
  [ -n "$W_SLEEP" ] && kill "$W_SLEEP" 2>/dev/null
  llog "watch: 종료(pid $$)"
  exit 0
}

cmd_watch() {
  W_IV=${1:-60}
  case "$W_IV" in ''|*[!0-9]*) usage_error ;; esac
  while :; do
    case "$W_IV" in 0?*) W_IV=${W_IV#0} ;; *) break ;; esac    # 앞자리 0 은 8진수로 읽힌다
  done
  [ "${#W_IV}" -gt 6 ] && usage_error
  [ "$W_IV" -lt 10 ] && W_IV=10
  W_MAX=300
  [ "$W_IV" -gt "$W_MAX" ] && W_MAX=$W_IV
  data_dir_ready
  if ! lock_acquire "$DATA_DIR/watch.lock.d" "${0##*/}"; then
    note "watch 가 이미 돌고 있습니다(pid $(sed -n '1p' "$DATA_DIR/watch.lock.d/pid" 2>/dev/null))"
    exit 0
  fi
  W_SLEEP=
  trap 'watch_unlock' EXIT
  trap 'watch_end' INT TERM HUP
  llog "watch: 시작(pid $$, ${W_IV}초마다)"
  W_DEAD=0
  W_CUR=$W_IV
  while :; do
    watch_tick
    sleep "$W_CUR" &
    W_SLEEP=$!
    wait "$W_SLEEP"                                      # 신호가 오면 sleep 을 기다리지 않고 바로 trap
    W_SLEEP=
  done
}

cmd_paths() {
  printf 'root=%s\ndata_dir=%s\nconf_file=%s\ndefault_port=%s\npython=%s\n' \
    "$ROOT" "$DATA_DIR" "$CONF_FILE" "$DEFAULT_PORT" "$PY"
  exit 0
}

cmd_foreground() {
  need_venv
  data_dir_ready
  if [ -e "$DATA_DIR/maintenance.flag" ]; then
    llog "foreground: maintenance.flag — 띄우지 않음"
    exit 0
  fi
  if update_live clean && [ "${CLEWPATH_UPDATER:-}" != 1 ]; then
    llog "foreground: 업데이트 진행 중 — 띄우지 않음"
    exit 0
  fi
  recover_swap_leftovers
  case $? in 0) ;; 10) exit 0 ;; *) exit 1 ;; esac
  probe_or_die
  case "$P_STATE" in
    ok|busy) llog "foreground: 이미 떠 있음(pid $P_PID) — 띄우지 않음"; exit 0 ;;
  esac
  llog "foreground: exec"
  exec "$PY" -I -c "$SPAWN_PY" "$ROOT" "$DATA_DIR" "$CONF_FILE" exec
}

[ $# -gt 0 ] || set -- start
C_CMD=$1
shift
case "$C_CMD" in
  start)          [ $# -eq 0 ] || usage_error; cmd_start ;;
  stop)           [ $# -le 1 ] || usage_error; cmd_stop "$@" ;;
  resume)         [ $# -eq 0 ] || usage_error; cmd_resume ;;
  status)         [ $# -eq 0 ] || usage_error; cmd_status ;;
  watch)          [ $# -le 1 ] || usage_error; cmd_watch "$@" ;;
  paths)          [ $# -eq 0 ] || usage_error; cmd_paths ;;
  foreground)     [ $# -eq 0 ] || usage_error; cmd_foreground ;;
  -h|--help|help) usage; exit 0 ;;
  *)              usage_error ;;
esac
