#!/bin/sh
# clewpath-launcher v2 (posix) — 자동 생성됨(install.sh / Host). 업데이트 뒤 Host 가 템플릿으로 다시 만듭니다. 직접 고치지 마세요.
# 설정은 이 파일이 아니라 {{CONF_FILE}} 을 고치세요.
#
# 멱등 런처 — Windows start-connector.template.ps1 의 맥·리눅스판(docs/designs/posix-install.md 3.3).
#   sh start-connector.sh [start]        떠 있으면 아무것도 안 한다. 죽어 있을 때만(health 없음 + pid 없음) 새로 띄운다.
#                                        [mac] 이 설치본의 launchd 작업이 올라 있으면 launchd 에 맡긴다(kickstart — Host 가
#                                        그 작업의 프로세스가 된다). 업데이트 러너의 재기동은 직접
#   sh start-connector.sh stop [--hold]  진행 중인 start 를 기다린 뒤 SIGTERM(정상 종료 표식·브로커 보존), 15초 뒤에도
#                                        남으면 SIGKILL.
#                                        --hold 면 maintenance.flag 도 만든다(다시 켜기: resume)
#   sh start-connector.sh resume         maintenance.flag 를 지우고 start
#   sh start-connector.sh status         state= pid= port= url= broker_pid= broker_alive= watch_pid= maintenance=
#                                        (종료코드 0 정상 / 1 아님 / 3 유지보수)
#   sh start-connector.sh watch [초]     상주 감시 — 자동 시작이 없는 곳(리눅스)의 임시용(기본 60초, 최소 10초). 하나만 돈다
#   sh start-connector.sh paths          root= data_dir= conf_file= default_port= python=
#   sh start-connector.sh foreground     (감독자용 — [mac] launchd 가 부른다) 죽어 있으면 이 프로세스가 곧 Host 가 된다.
#                                        띄우지 않을 때는 늘 0(0 이 아닌 종료는 Host 자신이 죽었을 때만)
# 지키는 것:
#  - health 실패만으로 기존 프로세스를 죽이거나 겹쳐 띄우지 않는다(기동 중일 수 있음)
#  - maintenance.flag 면 아무것도 띄우지 않는다. 업데이트·설치 중(update.inprogress)이면 러너 자신의 재기동만 통과
#  - 프로세스는 명령줄(설치 경로·모듈)로 알아본다 — venv 파이썬은 심볼릭 링크라 실행 파일로 고르지 않는다.
#    pid 재사용·좀비는 죽은 것으로 본다. Host 의 pid 는 runtime.json, 그 pid 가 아니면 런처가 띄운 pid(launcher.spawn)
#  - health 에 답하는 것이 이 설치본의 Host 가 아니면(남의 서버) 띄우지 않고 실패로 알린다
#  - 이 env 로 뜬 Host 의 데이터 폴더가 DATA_DIR 과 다르면(CLAUDE_CONFIG_DIR 등) 띄우지 않는다
#  - Host 는 사용자의 umask·HOME·TMPDIR·PATH·LANG 을 그대로 물려받는다(그 안의 claude 가 다시 물려받는다).
#    env 에서는 claude 표식만 뺀다(브로커 ptyd.clean_env 와 같은 규칙)
#  - 판정은 venv 파이썬(표준 라이브러리·psutil)으로 — session_manager 를 import 하지 않아 롤백 직후 버전이 어긋나도 돈다
# 로그: {{DATA_DIR}}/host.log(.err.log, 이어 쓰기), 런처 기록은 같은 폴더의 launcher.log

DATA_DIR={{DATA_DIR}}
CONF_FILE={{CONF_FILE}}
DEFAULT_PORT={{PORT}}
# 물리 경로 — 맥 APFS(대소문자 무시)에서 셸 내장 pwd -P 는 cd 에 준 글자를 그대로 둔다. /bin/pwd 는 디스크의 글자(다섯 스크립트 공통)
phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }
ROOT=$(CDPATH='' cd "$(dirname "$0")" && phys_pwd) || exit 1
PY="$ROOT/.venv/bin/python"
SELF="$ROOT/${0##*/}"
NL='
'
OS=linux                                   # [mac] 은 start 를 launchd 에 맡길 때만 쓴다(launchd_ours·launchd_start)
[ "$(uname -s 2>/dev/null)" = Darwin ] && OS=mac

# 상태 판정 — 출력 "<ok|busy|foreign|other|dead|none> <pid> <port>". pid 는 runtime.json 의 것, 그 pid 가 이 설치본의 Host 로
# 살아 있지 않으면 런처가 띄운 pid(launcher.spawn — runtime.json 을 아직 안 쓴 느린 기동, 겹쳐 뜬 진 쪽이 덮어쓴 runtime.json).
# 이 설치본의 Host: 있고·좀비 아니고·내 uid·명령줄 인자 하나는 ROOT/.venv/bin/…, 다른 하나는 Host 모듈. 그 Host 가 runtime.json
# 의 port 로 health(프록시 무시)에 답하면 ok, 아니면 busy(기동 중·응답 불능). health 는 답하는데 그 Host 가 없으면 foreign
# (다른 ClewPath 가 포트를 차지) 또는 other(ClewPath 가 아닌 프로그램 — 아래). pid 재사용은 명령줄로 거른다 — 기동 시각(started_at)과 견주지 않는다: 리눅스 psutil 의
# create_time 은 지금의 부팅 시각으로 계산해 시계가 앞으로 뛰면(WSL2·VM 재개·NTP) 같이 밀려, 산 Host 를 시체로 본다.
# psutil 이 없으면 /proc 를, /proc 도 없으면(맥) ps 를 읽는다 — 셸의 pid_alive·is_our_host 와 같은 규칙(ps 는 인자를 공백으로
# 잇는다). ps 를 못 부를 때만 보수적으로 busy(맥 실기 3b: ps 없이 '산 pid = busy'로 보아 재사용된 pid 앞에서 영영 안 띄웠다).
# 이 파이썬 본문은 python -c 인자라 ASCII 만 쓴다(작은따옴표도 없다 — 셸 문자열 안).
#
# foreign 은 그 서버가 ClewPath(다른 설치본·개발 서버·다른 사용자)일 때만 — 진단(/api/owner/diagnostics, 루프백은 인증 없음)에
# claude_home 이 있으면(Host 의 _clewpath_already_there 와 같은 기준) 또는 진단이 응답하지 않으면(보수적). HTTP 로 답했는데
# claude_home 이 없으면 ClewPath 가 아니다 — other: 런처는 띄우고 Host 가 옆 포트를 찾는다(port_fallback). Host 도 같은 포트의
# ClewPath 앞에서는 옆 포트로 가지 않고 끝나므로(중복 기동 방지) 두 판정이 같다.
PROBE_PY='
import json, os, sys, urllib.error, urllib.request
data, port, root = sys.argv[1], int(sys.argv[2]), sys.argv[3]
pid = spid = 0
try:
    with open(os.path.join(data, "runtime.json"), encoding="utf-8-sig") as f:
        rt = json.load(f)
    if type(rt.get("port")) is int and 0 < rt["port"] < 65536:
        port = rt["port"]
    if type(rt.get("pid")) is int and rt["pid"] > 0:
        pid = rt["pid"]
except Exception:
    pass
try:
    with open(os.path.join(data, "launcher.spawn"), encoding="utf-8") as f:
        sp = json.load(f).get("pid")
    if type(sp) is int and sp > 0:
        spid = sp
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
    return (any(a.startswith(root + "/.venv/bin/") for a in args)
            and any(a == "session_manager.server" or a.endswith("/clewpath-host") for a in args))


def by_ps(pid):
    import subprocess
    try:
        r = subprocess.run(["ps", "-ww", "-o", "stat=", "-o", "command=", "-p", str(pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        return "busy"
    line = r.stdout.decode("utf-8", "replace").strip()
    if not line:
        return "dead"
    st, _, cmd = line.partition(" ")
    cmd = " " + cmd.strip() + " "
    if "Z" in st or (" " + root + "/.venv/bin/") not in cmd:
        return "dead"
    return "busy" if (" session_manager.server " in cmd or "/clewpath-host " in cmd) else "dead"


def by_proc(pid):
    try:
        os.kill(pid, 0)
    except Exception:
        return "dead"
    if not os.path.isdir("/proc/self"):
        return by_ps(pid)
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
        return "busy" if is_host(p.cmdline()) else "dead"
    except psutil.AccessDenied:
        return "busy"
    except Exception:
        return "dead"


def clewpath_there():
    try:
        with opener.open("http://127.0.0.1:%d/api/owner/diagnostics" % port, timeout=3) as r:
            body = json.loads(r.read(1 << 20))
    except urllib.error.HTTPError:
        return False
    except ValueError:
        return False
    except Exception:
        return True
    return isinstance(body, dict) and "claude_home" in body


state = state_of(pid)
if state != "busy" and spid and spid != pid and state_of(spid) == "busy":
    state, pid = "busy", spid
if healthy:
    state = "ok" if state == "busy" else "foreign" if clewpath_there() else "other"
print(state, pid, port)
'

# 기동 — env 에서 claude 표식·러너 표식을 빼고 toml 위치를 고정해, 새 세션(setsid)으로 띄운다. 로그는 이어 쓰기.
# runtime.json 에 새 pid 가 적히거나(up) 끝날 때(exited:rc)까지 최대 15초 기다린다 — 그동안 잠금을 쥐고 있어야
# 다른 런처가 '아직 runtime.json 없음'을 죽음으로 읽고 겹쳐 띄우지 않는다. 출력 "<pid> <up|starting|exited:rc>".
# 띄운 pid 는 곧바로 launcher.spawn 에 적는다(tmp 뒤 교체) — 15초를 넘긴 느린 기동도 판정·stop 이 알아본다.
# HUP·INT·QUIT 은 기본 처리로 되돌려 띄운다 — nohup(HUP)·스크립트의 &(INT·QUIT)가 둔 '무시'는 exec 로 Host → 브로커 →
# pty 안의 claude 까지 물려가, 브로커가 죽어도 pty 자식이 SIGHUP 으로 끝나지 않는다(새 세션이라 터미널 신호는 오지 않는다).
# exec 모드(foreground)는 이 프로세스를 Host 로 바꾼다(pid 그대로 — launchd·systemd 가 지켜볼 수 있게). 이때는 부른 쪽의
# 신호 처리를 그대로 둔다 — launchd·systemd 는 기본값이고, 사람이 nohup 으로 붙잡아 둔 터미널 밖 기동을 풀지 않게.
# 표준 입출력은 start 와 같게 바꿔 단다(stdin /dev/null, stdout host.log, stderr host.err.log — 이어 쓰기): launchd 의
# launchd.log 로 새지 않고, 사고 꼬리(liveness.log_tail)·사람의 로그 확인이 start 로 뜬 Host 와 같다. foreground 가 쥔
# launcher.lock 은 제 pid 를 launcher.spawn 에 적은 뒤 놓는다(그 뒤 다른 런처의 판정은 이 pid 를 Host 로 본다). 다섯째 인자(런처
# 경로)는 그때까지 명령줄에 런처 이름이 남게 하는 것 — 잠금의 주인 확인(lock_live)이 그 이름을 본다.
SPAWN_PY='
import json, os, signal, subprocess, sys, time
root, data, conf, mode = sys.argv[1:5]
SCRUB = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLEWPATH_UPDATER"}
env = {k: v for k, v in os.environ.items() if k not in SCRUB and not k.upper().startswith("CLAUDE_CODE_")}
env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", CLEWPATH_CONFIG=conf)
py = os.path.join(root, ".venv", "bin", "python")
argv = [py, "-m", "session_manager.server"]


def note_spawn(pid):
    tmp = os.path.join(data, "launcher.spawn.%d" % os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "at": int(time.time())}, f)
        os.replace(tmp, os.path.join(data, "launcher.spawn"))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def unlock(lock):
    try:
        with open(os.path.join(lock, "pid"), encoding="utf-8") as f:
            mine = f.readline().strip() == str(os.getpid())
        if mine:
            os.unlink(os.path.join(lock, "pid"))
            os.rmdir(lock)
    except Exception:
        pass


if mode == "exec":
    os.chdir(root)
    for fd, name, flags in ((0, os.devnull, os.O_RDONLY),
                            (1, os.path.join(data, "host.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND),
                            (2, os.path.join(data, "host.err.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND)):
        f = os.open(name, flags, 0o666)
        if f == fd:
            os.set_inheritable(fd, True)
        else:
            os.dup2(f, fd)
            os.close(f)
    note_spawn(os.getpid())
    unlock(os.path.join(data, "launcher.lock"))
    os.execve(py, argv, env)
for s in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
    signal.signal(s, signal.SIG_DFL)
with open(os.path.join(data, "host.log"), "ab") as out, open(os.path.join(data, "host.err.log"), "ab") as err:
    p = subprocess.Popen(argv, cwd=root, env=env, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=err)
note_spawn(p.pid)
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

# [mac] 자동 시작 등록 파일(plistlib)이 부르는 런처 — 한 줄에 하나(uninstall.sh 의 PLIST_PY 와 같은 규칙). 못 읽으면 3
LAUNCHD_LABEL=com.clewpath.host
LAUNCHD_PY='
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

# stop 이 강제 종료(SIGKILL)했을 때만 — 죽인 Host 의 pid 로 정상 종료 표식(다음 기동이 사고로 보지 않게). 실패는 무시.
MARK_PY='
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from session_manager import config, liveness
config.data_dir = lambda: Path(sys.argv[2])
liveness.mark_shutdown("forced-stop", pid=int(sys.argv[3]))
'

# 이 env 로 뜬 Host 가 쓸 데이터 폴더 — server.main 과 같은 규칙(appconfig.apply_env 뒤 config.data_dir):
# SESSION_MANAGER_CLAUDE_HOME > CLAUDE_CONFIG_DIR(env, 없으면 설정 파일 [claude] config_dir) > ~/.claude, 그 아래
# session_manager. 상대 경로는 Host 의 cwd(설치 루트) 기준. 출력 "same" 또는 그 폴더(DATA_DIR 과 다를 때)
DATADIR_PY='
import os, sys, tomllib
from pathlib import Path
data, conf, root = sys.argv[1:4]
home = os.environ.get("SESSION_MANAGER_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
if not home:
    try:
        with open(conf, encoding="utf-8-sig") as f:
            v = tomllib.loads(f.read()).get("claude", {}).get("config_dir")
        if v not in (None, ""):
            home = str(v)
    except Exception:
        pass
dd = Path(root) / (Path(home) if home else Path.home() / ".claude") / "session_manager"
try:
    same = os.path.normpath(dd) == os.path.normpath(data) or os.path.samefile(dd, data)
except OSError:
    same = False
print("same" if same else dd)
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

need_venv() {   # [$1=끝낼 종료코드(기본 2 — foreground 는 0)]
  [ -x "$PY" ] && return 0
  llog "fail: venv 없음($PY)"
  say "venv 가 없습니다($PY) — sh \"$ROOT/install.sh\" --reinstall"
  exit "${1:-2}"
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

boot_id() {   # 이번 부팅의 식별자 — 리눅스 boot_id, 맥 부팅 세션 UUID(없으면 부팅 시각 초). 못 얻으면 빈 출력
  if [ -r /proc/sys/kernel/random/boot_id ]; then
    { tr -cd 'A-Za-z0-9-' </proc/sys/kernel/random/boot_id; } 2>/dev/null
  else
    { sysctl -n kern.bootsessionuuid || sysctl -n kern.boottime | sed -n 's/^{ *sec *= *\([0-9][0-9]*\).*/\1/p'; } 2>/dev/null \
      | tr -cd 'A-Za-z0-9-'
  fi
}

# 잠금 = 폴더(mkdir 은 원자적) + 안의 pid(첫 줄)·부팅 식별자(boot=, 둘째 줄). 시체: pid 가 죽음·다른 프로그램(명령줄에 $2
# 없음)·내 pid(시체 잠금의 pid 를 내가 다시 받음 — 나는 아직 안 잡았다)·다른 부팅에서 잡음(전원 차단 뒤 재부팅하면 그 pid 는
# 이제 남의 것 — 로그인 때 뜨는 watch 가 받기도 한다), 또는 pid 없이 2분 넘게 묵음. 식별자가 없으면(옛 잠금·못 얻음) pid 로만
lock_live() {
  [ -d "$1" ] || return 1
  _lp=$(sed -n '1s/^\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$1/pid" 2>/dev/null)
  if [ -z "$_lp" ]; then
    [ -n "$(find "$1" -maxdepth 0 -mmin +2 2>/dev/null)" ] && return 1
    return 0                                            # pid 를 막 쓰려는 참
  fi
  [ "$_lp" = "$$" ] && return 1
  _lb=$(sed -n 's/^boot=\([A-Za-z0-9-]\{1,64\}\)[[:space:]]*$/\1/p' "$1/pid" 2>/dev/null | head -n 1)
  if [ -n "$_lb" ]; then
    _cb=$(boot_id)
    [ -n "$_cb" ] && [ "$_lb" != "$_cb" ] && return 1
  fi
  pid_alive "$_lp" && cmd_has "$_lp" "$2"
}

lock_drop() {
  case "$1" in "$DATA_DIR"/*.lock|"$DATA_DIR"/*.lock.d) rm -rf "$1" ;; esac
}

# 시체 잠금을 치우고 새로 잡는다(mkdir 까지). 치우는 권리(옆 폴더 $1.reclaim, mkdir)를 쥔 하나만 한다 — 둘이 같은 시체를
# 보고 따로 지우면 늦게 지운 쪽이 먼저 잡은 쪽의 새 잠금을 지워 둘 다 잡았다고 믿는다(감독자·사람·러너가 겹치면 Host 둘).
# 권리를 쥔 채 다시 보고(그새 남이 새로 잡았으면 산 잠금) 지우고 잡은 뒤 권리를 놓는다. 권리 폴더가 2분 넘게 남았으면
# 치우다 죽은 것이다(권리는 수 ms 만 쥔다). 0 = 잡음
lock_reclaim() {
  if ! mkdir "$1.reclaim" 2>/dev/null; then
    [ -n "$(find "$1.reclaim" -maxdepth 0 -mmin +2 2>/dev/null)" ] || return 1
    rmdir "$1.reclaim" 2>/dev/null
    mkdir "$1.reclaim" 2>/dev/null || return 1
  fi
  _lr=1
  if ! lock_live "$1" "$2"; then
    lock_drop "$1"
    mkdir "$1" 2>/dev/null && _lr=0
  fi
  rmdir "$1.reclaim" 2>/dev/null
  return "$_lr"
}

lock_acquire() {   # $1=잠금 폴더 $2=쥔 프로세스 명령줄의 글자. 시체면 치우고 한 번만 다시 잡는다
  if ! mkdir "$1" 2>/dev/null; then
    lock_live "$1" "$2" && return 1
    lock_reclaim "$1" "$2" || return 1
  fi
  _la=$(boot_id)
  [ -n "$_la" ] && _la="boot=$_la$NL"
  if ! { printf '%s\n%s' "$$" "$_la" >"$1/pid"; } 2>/dev/null; then
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
  _uf="$DATA_DIR/update.inprogress"   # (at 은 앞자리 0 금지 — $(( )) 가 8진수로 읽다 셸째 끝난다. install.sh·uninstall.sh 와 같은 규칙)
  [ -f "$_uf" ] || return 1
  _uc=$(cat "$_uf" 2>/dev/null)
  _up=$(sed -n 's/^pid=\([0-9]\{1,10\}\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
  _ua=$(sed -n 's/^at=\([1-9][0-9]\{0,11\}\)[[:space:]]*$/\1/p' "$_uf" 2>/dev/null | head -n 1)
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
  # 시체 치우기는 표식 잠금(설치·러너가 '확인 → 쓰기'를 묶는 .update.inprogress.lock) 안에서 내용이 그대로일 때만 — 그 사이
  # 설치·러너가 이 시체를 보고 새로 쓴 표식을 지우지 않게. 잠금을 남이 쥐고 있으면(누군가 쓰는 중) 지우지 않는다
  if [ "${1:-}" = clean ] && mkdir "$DATA_DIR/.update.inprogress.lock" 2>/dev/null; then
    if [ "$(cat "$_uf" 2>/dev/null)" = "$_uc" ]; then
      rm -f "$_uf"
      llog "stale update marker 제거(pid ${_up:-?}, by=${_ub:-?})"
    fi
    rmdir "$DATA_DIR/.update.inprogress.lock" 2>/dev/null
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
  _po=$("$PY" -I -c "$PROBE_PY" "$DATA_DIR" "$DEFAULT_PORT" "$ROOT" 2>/dev/null) || return 1
  case "$_po" in "ok "*|"busy "*|"foreign "*|"other "*|"dead "*|"none "*) ;; *) return 1 ;; esac
  P_STATE=${_po%% *}
  _po=${_po#* }
  P_PID=${_po%% *}
  P_PORT=${_po#* }
  case "$P_PID" in ''|*[!0-9]*) return 1 ;; esac
  case "$P_PORT" in ''|*[!0-9]*) return 1 ;; esac
  return 0
}

probe_or_die() {   # [$1=끝낼 종료코드(기본 2 — foreground 는 0)]
  probe_now && return 0
  llog "fail: 상태 판정 실패(venv 파이썬 — $PY)"
  say "venv 파이썬으로 상태를 판정하지 못했습니다($PY) — sh \"$ROOT/install.sh\" --reinstall"
  exit "${1:-2}"
}

port_taken() {   # P_STATE=foreign(다른 ClewPath) — 띄우지 않고 실패로(조용히 '떠 있음'으로 보면 watch 도 영영 안 되살린다). Host 도
  # 같은 포트의 ClewPath 앞에서는 옆 포트로 가지 않고 끝난다(중복 기동 방지). [$1=종료코드(기본 1)]
  llog "fail: 포트 $P_PORT 를 다른 프로그램이 쓰고 있음(health 는 답하지만 이 설치본의 Host 가 아님 — 다른 ClewPath)"
  say "포트 $P_PORT 를 다른 프로그램(다른 ClewPath)이 쓰고 있어 띄우지 않습니다 — 그 프로그램을 끄거나 $CONF_FILE 의 [server] port 를 바꾸세요"
  exit "${1:-1}"
}

spawn_why() {   # 기동 사유 → S_WHY (start·foreground 같은 문구). P_STATE=other 면 Host 가 옆 포트를 찾는다는 것도
  if [ "$P_PID" = 0 ]; then S_WHY="runtime.json 없음"; else S_WHY="이전 pid $P_PID 없음(health 없음)"; fi
  if [ "$P_STATE" = other ]; then
    S_WHY="$S_WHY, 포트 $P_PORT 는 다른 프로그램(ClewPath 아님)이 씀 — Host 가 옆 포트를 찾는다(port_fallback)"
  fi
  return 0
}

datadir_same() {   # 0 = 이 env 로 뜬 Host 도 DATA_DIR 을 쓴다. 판정을 못 하면(파이썬 실패) 막지 않는다
  _dd=$("$PY" -I -c "$DATADIR_PY" "$DATA_DIR" "$CONF_FILE" "$ROOT" 2>/dev/null) || return 0
  case "$_dd" in same|'') return 0 ;; esac
  llog "fail: 데이터 폴더 불일치 — 이 env 로는 Host 가 $_dd 를 씀(런처는 $DATA_DIR)"
  say "이 셸의 SESSION_MANAGER_CLAUDE_HOME·CLAUDE_CONFIG_DIR(또는 설정 파일의 [claude] config_dir)로는 Host 의 데이터 폴더가 $_dd 가 되어 런처($DATA_DIR)와 갈립니다 — 띄우지 않습니다. 설치 때와 같은 값으로(또는 그 변수를 빼고) 다시 실행하세요"
  return 1
}

# 대기 마감은 벽시계(date +%s)가 아니라 쉰 반초 수(NAP_T)로 센다 — 마감 시각으로 재면 NTP 가 시계를 뒤로 돌린 만큼(수 시간) 더
# 기다린다(러너의 nap 과 같다). 소수 sleep 이 없으면 1초(= 2)
nap() {
  if sleep 0.5 2>/dev/null; then NAP_T=$((NAP_T + 1)); else sleep 1; NAP_T=$((NAP_T + 2)); fi
}

start_wait() {   # 진행 중인 start(launcher.lock)가 끝나기를 기다린다(상한 25초 — 기동 대기 15초 + 판정) — 그 start 가 띄우는 Host 까지 보려고
  NAP_T=0
  while lock_live "$DATA_DIR/launcher.lock" "${0##*/}" && [ "$NAP_T" -lt 50 ]; do
    nap
  done
}

rotate() {   # 5MB 넘으면 한 세대 밀어 둔다(기동 직전에만 — 이어 쓰는 Host 가 없을 때)
  [ -f "$1" ] || return 0
  _sz=$(wc -c <"$1" 2>/dev/null | tr -d ' \t')
  case "$_sz" in ''|*[!0-9]*) return 0 ;; esac
  [ "$_sz" -gt 5242880 ] && mv -f "$1" "$1.1" 2>/dev/null
  return 0
}

launch_logs() {   # 띄우기 직전(start·foreground 같은 규칙) — 로그 회전, 그리고 host.log 에 런처 구분줄(liveness.LAUNCHER_MARK:
  # 새 Host 가 읽는 죽은 Host 의 꼬리는 마지막 구분줄 앞까지 — 회전이 구분줄 바로 앞이면 host.log.1 의 꼬리)
  rotate "$DATA_DIR/host.log"
  rotate "$DATA_DIR/host.err.log"
  rotate "$DATA_DIR/launcher.log"
  { printf '=== launcher start %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$DATA_DIR/host.log"; } 2>/dev/null
}

# 러너가 업데이트 중 stop --hold 를 보면 헬스 없이 커밋하고(held) 표지 updates/held-commit(version= from= backup= at=)를 남긴다.
# 그 새 버전은 자동 롤백이 없으니 그 뒤 첫 기동이 실패하면 그렇다고 알리고 백업 위치를 보여 준다. 뜨면(ok·up) 표지를 지운다
held_ok() {
  [ -f "$DATA_DIR/updates/held-commit" ] || return 0
  rm -f "$DATA_DIR/updates/held-commit" 2>/dev/null && llog "held 커밋 뒤 기동 확인 — 표지 제거(updates/held-commit)"
  return 0
}

held_fail() {
  _hf="$DATA_DIR/updates/held-commit"
  [ -f "$_hf" ] || return 0
  _hv=$(sed -n 's/^version=\([0-9A-Za-z.+-]\{1,64\}\)[[:space:]]*$/\1/p' "$_hf" 2>/dev/null | head -n 1)
  _ho=$(sed -n 's/^from=\([0-9A-Za-z.+?-]\{1,64\}\)[[:space:]]*$/\1/p' "$_hf" 2>/dev/null | head -n 1)
  _hb=$(sed -n 's/^backup=\(\/.*\)$/\1/p' "$_hf" 2>/dev/null | head -n 1)
  llog "held 커밋 뒤 첫 기동 실패(버전 ${_hv:-?}) — 자동 롤백 없음, 백업 ${_hb:-?}"
  say "이 버전(${_hv:-?})은 업데이트 중 멈춤(stop --hold) 때문에 헬스체크 없이 커밋돼 자동 롤백이 없습니다"
  say "  이전 버전(${_ho:-?})의 백업: ${_hb:-$DATA_DIR/updates/backup-*}"
  say "  의존성을 다시 맞춰 보려면: sh \"$ROOT/install.sh\" --reinstall  (기동 로그: $DATA_DIR/host.err.log)"
  return 0
}

launchd_ours() {   # 0 = [mac] 이 설치본의 launchd 작업이 올라 있다 — 등록 파일이 이 런처를 부르고(같은 이름의 다른 설치본 작업은
  # 깨우지 않는다) launchctl 이 그 작업을 안다. 리눅스·등록 없음·못 읽음이면 1
  [ "$OS" = mac ] || return 1
  _jp="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
  [ -f "$_jp" ] || return 1
  _ja=$("$PY" -I -c "$LAUNCHD_PY" "$_jp" </dev/null 2>/dev/null) || return 1
  case "$NL$_ja$NL" in *"$NL$SELF$NL"*) ;; *) return 1 ;; esac
  launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" </dev/null >/dev/null 2>&1
}

# [mac] launchd 에 맡겨 띄운다(kickstart) — 그 작업의 foreground 가 곧 Host 가 되어(exec) 죽으면 30초 KeepAlive 가 받고 로그아웃
# 때 같이 끝난다(맥 실기 3b: 설치·resume·start 가 직접 띄운 Host 는 PPID 1·launchd 밖이라 죽으면 5분 확인이 받았고 로그아웃에도
# 살아남았다). 잠금을 잡기 전에 부른다 — foreground 가 launcher.lock 을 잡는다. 반초 낮잠 40번(약 20초)까지 판정이 ok·busy 가
# 되기를 본다(벽시계 마감이 아니라 횟수). 0 = 떴다. 아니면 사유를 남기고 1 — 부른 쪽이 지금처럼 직접 띄운다(늦게 뜬 작업과는 잠금
# 안 재판정이 겹쳐 띄우기를 막는다)
launchd_start() {
  [ "$OS" = mac ] || return 1
  if [ "$P_PID" = 0 ]; then _kw="runtime.json 없음"; else _kw="이전 pid $P_PID 없음(health 없음)"; fi
  if ! _ke=$(launchctl kickstart "gui/$(id -u)/$LAUNCHD_LABEL" </dev/null 2>&1); then
    llog "start: launchd kickstart 실패($(printf '%s' "$_ke" | tr '\n' ' ' | cut -c 1-160)) → 직접 기동"
    return 1
  fi
  _kn=0
  while [ "$_kn" -lt 40 ]; do
    sleep 0.5 2>/dev/null || sleep 1
    _kn=$((_kn + 1))
    probe_now || continue
    case "$P_STATE" in
      ok|busy) [ "$P_STATE" = ok ] && held_ok
               llog "start: $_kw → launchd 에 맡김(kickstart) pid $P_PID"
               note "기동했습니다(launchd 작업) — pid $P_PID (로그: $DATA_DIR/host.log)"
               return 0 ;;
      foreign) port_taken ;;
    esac                                                  # other — launchd 의 foreground 가 띄워 옆 포트로 뜨기를 기다린다
  done
  llog "start: launchd 에 맡겼지만 약 20초 안에 뜨지 않음(kickstart) → 직접 기동"
  return 1
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
    ok) held_ok; note "이미 실행 중입니다 — pid $P_PID, http://127.0.0.1:$P_PORT/"; exit 0 ;;
    busy) llog "skip: pid $P_PID 살아 있음(health 없음 — 기동 중이거나 응답 불능)"
          note "pid $P_PID 가 살아 있지만 응답이 없습니다(기동 중이거나 응답 불능) — 겹쳐 띄우지 않습니다"
          exit 0 ;;
    foreign) port_taken ;;
  esac
  # [mac] 이 설치본의 launchd 작업에 맡긴다. 업데이트 러너의 재기동(CLEWPATH_UPDATER=1)은 직접 — 러너가 update.inprogress 를
  # 쥐고 있어 launchd 의 foreground 는 띄우지 않는다(그 Host 는 다음에 죽거나 로그아웃할 때까지 launchd 밖에 있다)
  if [ "${CLEWPATH_UPDATER:-}" != 1 ] && launchd_ours && launchd_start; then
    exit 0
  fi
  if ! lock_acquire "$DATA_DIR/launcher.lock" "${0##*/}"; then
    llog "skip: 다른 런처가 기동 중(launcher.lock)"
    exit 0
  fi
  trap 'start_unlock' EXIT
  trap 'exit 1' INT TERM HUP
  probe_or_die
  case "$P_STATE" in ok|busy) exit 0 ;; foreign) port_taken ;; esac   # 잠금 안에서 한 번 더 — 그 사이 다른 런처가 띄웠을 수 있다
  # 표식도 잠금 안에서 다시 본다 — 첫 판정(health 최대 3초) 사이에 stop --hold·러너·설치가 쓴 것. stop 은 표식을 쓴 뒤
  # 잠금이 풀리기를 기다리므로, 표식을 못 보고 지나간 start 가 띄운 Host 는 stop 이 본다
  if [ -e "$DATA_DIR/maintenance.flag" ]; then
    llog "skip: maintenance.flag(잠금 안 재확인)"
    exit 0
  fi
  if [ "${CLEWPATH_UPDATER:-}" != 1 ]; then
    if update_live; then llog "skip: 업데이트 진행 중(잠금 안 재확인)"; exit 0; fi
    if swap_left; then llog "skip: 교체 잔재(잠금 안 재확인 — 다음 판정에서 복구)"; exit 0; fi
  fi
  datadir_same || exit 1
  launch_logs
  spawn_why
  llog "start: $S_WHY → 기동"
  if ! S_OUT=$("$PY" -I -c "$SPAWN_PY" "$ROOT" "$DATA_DIR" "$CONF_FILE" spawn 2>>"$DATA_DIR/launcher.log"); then
    llog "fail: spawn 실패"
    say "Host 를 띄우지 못했습니다 — $DATA_DIR/launcher.log 를 보세요"
    held_fail
    exit 1
  fi
  S_PID=${S_OUT%% *}
  S_ST=${S_OUT#* }
  case "$S_PID" in ''|*[!0-9]*) llog "fail: spawn 결과를 읽지 못함($S_OUT)"; exit 1 ;; esac
  case "$S_ST" in
    up) llog "spawned pid $S_PID"
        held_ok
        note "기동했습니다 — pid $S_PID (로그: $DATA_DIR/host.log)" ;;
    exited:*) llog "fail: 기동 직후 종료(pid $S_PID, rc=${S_ST#exited:})"
              say "Host 가 기동 직후 끝났습니다(rc=${S_ST#exited:}) — $DATA_DIR/host.err.log 를 보세요"
              held_fail
              [ "$P_STATE" = other ] && say "  (포트 $P_PORT 를 다른 프로그램이 쓰고 있습니다 — 옆 포트도 다 찼거나 $CONF_FILE 의 [server] port_fallback 이 0 일 수 있습니다)"
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
  start_wait                                              # 진행 중인 start 가 띄우는 Host 까지 멈춘다
  K_PID=$(json_pid "$DATA_DIR/runtime.json")
  if ! is_our_host "$K_PID"; then
    K_PID=$(json_pid "$DATA_DIR/launcher.spawn")         # runtime.json 을 아직 안 쓴(느린 기동)·진 쪽이 덮어쓴 경우
    if ! is_our_host "$K_PID"; then
      printf 'not running\n'
      exit 0
    fi
  fi
  kill -TERM "$K_PID" 2>/dev/null
  llog "stop: SIGTERM pid $K_PID"
  NAP_T=0                                                 # Host 의 _shutdown_soon 과 같은 15초(반초 수)
  while is_our_host "$K_PID" && [ "$NAP_T" -lt 30 ]; do
    nap
  done
  if ! is_our_host "$K_PID"; then
    printf 'stopped (pid %s)\n' "$K_PID"
    exit 0
  fi
  kill -KILL "$K_PID" 2>/dev/null
  llog "stop: forced — pid $K_PID 가 15초 안에 끝나지 않아 SIGKILL"
  [ -x "$PY" ] && "$PY" -I -c "$MARK_PY" "$ROOT" "$DATA_DIR" "$K_PID" >/dev/null 2>&1
  NAP_T=0
  while is_our_host "$K_PID" && [ "$NAP_T" -lt 6 ]; do
    nap
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
  # 이 watch 의 설치 루트(물리 경로 한 줄) — 제거(uninstall)가 자기 설치본의 watch 만 멈추게. tmp 뒤 mv
  { printf '%s\n' "$ROOT" >"$DATA_DIR/watch.lock.d/root.tmp" \
      && mv -f "$DATA_DIR/watch.lock.d/root.tmp" "$DATA_DIR/watch.lock.d/root"; } 2>/dev/null \
    || llog "watch: 잠금에 설치 루트를 적지 못함"
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

fg_check() {   # foreground 의 판정 — 떠 있으면(ok·busy) 0 으로 끝내고, 남의 서버·판정 실패도 알린 뒤 0
  probe_or_die 0
  case "$P_STATE" in
    ok) exit 0 ;;                                         # 늘 있는 일(5분마다) — 기록하지 않는다(start 와 같다)
    busy) llog "foreground: pid $P_PID 살아 있음(health 없음 — 기동 중이거나 응답 불능) — 띄우지 않음"; exit 0 ;;
    foreign) port_taken 0 ;;
  esac
}

# launchd(KeepAlive·5분 StartInterval)·systemd 가 부른다 — 죽어 있을 때만 이 프로세스가 곧 Host 가 된다(exec, pid 그대로).
# 띄우지 않을 때는 늘 0 이다: 0 이 아닌 종료는 감독자에게 'Host 가 죽었다 — 곧 다시'라는 뜻이라(launchd 는 30초마다), 곧 풀리지
# 않는 사유(venv 없음·데이터 폴더 불일치·남의 서버·잔재 복구 막힘)로 쉼 없이 돌지 않게 한다 — 다시 보는 것은 5분 주기다. 사유는
# launcher.log(와 표준오류 = launchd.log)에 남긴다. 순서·잠금·로그는 start 와 같다(S21) — 잠금은 SPAWN_PY 가 제 pid 를 적은 뒤 놓는다
cmd_foreground() {
  need_venv 0
  data_dir_ready
  [ -e "$DATA_DIR/maintenance.flag" ] && exit 0           # 보류 — 5분마다 오므로 기록하지 않는다(start 와 같다)
  if update_live clean && [ "${CLEWPATH_UPDATER:-}" != 1 ]; then
    llog "foreground: 업데이트 진행 중 — 띄우지 않음"
    exit 0
  fi
  recover_swap_leftovers || exit 0                        # 보류(10)·막힘(1) — 기록·안내는 그 안에서
  fg_check
  if ! lock_acquire "$DATA_DIR/launcher.lock" "${0##*/}"; then
    llog "foreground: 다른 런처가 기동 중(launcher.lock) — 띄우지 않음"
    exit 0
  fi
  trap 'start_unlock' EXIT
  trap 'exit 1' INT TERM HUP
  fg_check                                                # 잠금 안에서 한 번 더 — 그 사이 다른 런처가 띄웠을 수 있다
  [ -e "$DATA_DIR/maintenance.flag" ] && exit 0
  if [ "${CLEWPATH_UPDATER:-}" != 1 ]; then
    if update_live; then llog "foreground: 업데이트 진행 중(잠금 안 재확인) — 띄우지 않음"; exit 0; fi
    if swap_left; then llog "foreground: 교체 잔재(잠금 안 재확인 — 다음 판정에서 복구)"; exit 0; fi
  fi
  datadir_same || exit 0
  launch_logs
  spawn_why
  llog "foreground: $S_WHY → 기동(exec, pid $$)"
  exec "$PY" -I -c "$SPAWN_PY" "$ROOT" "$DATA_DIR" "$CONF_FILE" exec "$SELF"
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
