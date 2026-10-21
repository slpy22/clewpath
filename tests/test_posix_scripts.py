"""맥·리눅스 공통 셸 스크립트의 정적 계약 — docs/designs/posix-install.md 6.1. 모든 OS(Windows 포함)에서 돈다.

스크립트 다섯(get.sh·install.sh·uninstall.sh·session_manager/start-connector.template.sh·session_manager/update_runner.sh)과
hostctl.py·tests/posix_e2e 는 명세 1.4 의 차례로 하나씩 생긴다. 아직 없는 것은 PENDING 에 있다 — 만들면 PENDING 에서
빼야 한다(test_pending_list_is_honest 가 '만들었는데 PENDING' 과 '없는데 PENDING 아님' 을 둘 다 잡는다). 빼는 순간 아래
계약이 그 파일에 켜진다. 검사 도구(_code_view·_violations·_inline_python·HOME_BLOCK)는 합성 예제로 지금도 시험한다.

스크립트가 지킬 약속(새 스크립트를 쓰는 사람이 볼 곳):
- 형식: LF, BOM 없음, 1행 `#!/bin/sh`. 런처 템플릿 2행에 `clewpath-launcher v2`. 실행 비트에 기대지 않는다.
- POSIX sh 만(_BASHISMS), 금지 명령(_FORBIDDEN — 맥에 없는 도구·GNU 전용 옵션·set -x·env 덤프·trap '' 신호 무시(곧 끝나는
  mv 를 exec 하는 하위 셸만 예외)·
  `:` 에 리디렉션 — 특수 내장이라 리디렉션이 실패하면 dash·bash --posix 가 스크립트를 통째로 끝낸다. `true >f` 를 쓴다).
  `umask` 는 하위 셸 `( umask 077; … )` 꼴만(S38: Host 를 띄우는 경로는 사용자의 umask 를 바꾸지 않는다).
- 인라인 파이썬은 맨 앞 칸의 `NAME_PY='` 한 줄로 열고 `'` 한 줄로 닫는 셸 변수에 둔다. 안에 작은따옴표 금지,
  그대로 컴파일돼야 한다. 런처의 PROBE_PY·SPAWN_PY 는 session_manager 를 import 하지 않는다(롤백 뒤 버전 어긋남).
- 런처 SPAWN_PY 의 env 정리: `SCRUB = {…}` 집합 = ptyd._SCRUB_EXACT ∪ {"CLEWPATH_UPDATER"}, 그리고 접두 "CLAUDE_CODE_".
- 공통 경로 블록(HOME_BLOCK)은 get.sh·install.sh·uninstall.sh·update_runner.sh 에 글자까지 같게(고른 들여쓰기만 허용).
- update_runner.sh: `PAYLOAD="…"`(test_updater.test_payload_lists_agree), 롤백 정지 대상에서 session_manager.ptyd 제외.
- install.sh: `UV_DEFAULT="x.y.z"`(install.ps1 $UvDefault 와 같게), `uv_pinned_sha256` 표에 그 버전의 대상 4개(64자 hex).
  `uv python find` 는 늘 `--system`(설치 루트의 .venv·VIRTUAL_ENV 가 답을 가로채지 않게), 관리 런타임은 `--no-bin`.
- install.sh·uninstall.sh: claude 의 settings.json 은 읽기만(쓰기·지우기·옮기기 없음), projects 는 지우지 않는다(S36·S37).
- hostctl.py: 토큰은 env 로만(argparse 에 *token* 인자 없음, S17). 맨 위 import 는 표준 라이브러리만.
- get.sh: 맨 위에는 주석·set -u·값 대입·함수 정의뿐이고 맨 끝 줄이 `main "$@"`(curl | sh 가 끊겨도 반쪽이 돌지 않게).
  curl 은 g_curl 한 곳에서만 부른다(--proto·--proto-redir·--tlsv1.2). 머리말에 신뢰 모델(서명은 여기서 보지 않는다)을 적는다.
- tests/posix_e2e: release-keys·keygen·pyongso.com 금지(S19·S44) — 거부 단정문 줄은 끝에 `e2e-guard` 표시
  (파이썬·셸은 `# e2e-guard`, 마크다운은 `<!-- e2e-guard -->`).
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path, PurePosixPath

import pytest

from session_manager import ptyd, updater
from session_manager.osdep import _posix

ROOT = Path(__file__).resolve().parents[1]

PATHS = {
    # get.sh 는 CP 공개 전까지 006 ops/ 에 둔다(명세 '결정'). 007 로 옮긴 뒤엔 env 로 가리킨다.
    "get.sh": Path(os.environ.get("CLEWPATH_GET_SH") or ROOT / "ops" / "get.sh"),
    "install.sh": ROOT / "install.sh",
    "uninstall.sh": ROOT / "uninstall.sh",
    "start-connector.template.sh": ROOT / "session_manager" / _posix.LAUNCHER_TEMPLATE,
    "update_runner.sh": ROOT / "session_manager" / _posix.UPDATE_RUNNER,
    "hostctl.py": ROOT / "session_manager" / "hostctl.py",
    "posix_e2e": ROOT / "tests" / "posix_e2e",
}
SCRIPTS = ("get.sh", "install.sh", "uninstall.sh", "start-connector.template.sh", "update_runner.sh")
HOME_SCRIPTS = ("get.sh", "install.sh", "uninstall.sh", "update_runner.sh")

# 아직 만들지 않은 것(명세 1.4 의 단계). 만들면 여기서 뺀다 — 8단계(tests/posix_e2e)까지 다 생겼다.
PENDING: set[str] = set()

# 공통 경로 블록 — 명세 3.0. 네 스크립트가 이것과 글자까지 같아야 한다(바꾸면 넷 다, 그리고 여기).
HOME_BLOCK = '''\
# >>> clewpath-home (get.sh·install.sh·uninstall.sh·update_runner.sh 공통 — 바꾸면 넷 다)
case "$(uname -s)" in
  Darwin) CLEWPATH_HOME="${CLEWPATH_HOME:-$HOME/Library/Application Support/ClewPath}" ;;
  *)      _xdg="${XDG_DATA_HOME:-}"; case "$_xdg" in /*) ;; *) _xdg="$HOME/.local/share" ;; esac
          CLEWPATH_HOME="${CLEWPATH_HOME:-$_xdg/clewpath}" ;;
esac
# <<< clewpath-home
'''

UV_TARGETS = {"x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-apple-darwin", "aarch64-apple-darwin"}


def _need(name: str) -> Path:
    if name in PENDING:
        pytest.skip(f"{name} 는 아직 없음(posix-install.md 1.4 — 만들면 PENDING 에서 빼세요)")
    return PATHS[name]


def _text(name: str) -> str:
    return _need(name).read_text(encoding="utf-8")


def _find_shells() -> dict[str, list[str]]:
    """문법 검사·실행에 쓸 수 있는 sh 들. Windows 는 Git 의 sh(bash sh 모드)·dash·bash 가 있으면 쓴다."""
    out = {}
    for sid, argv in (("sh", ["sh"]), ("dash", ["dash"]), ("bash-posix", ["bash", "--posix"])):
        exe = shutil.which(argv[0])
        if exe and "system32" not in exe.lower():         # Windows 의 WSL bash.exe 는 Windows 경로를 못 읽는다
            out[sid] = [exe, *argv[1:]]
    return out


_SHELLS = _find_shells()
with_shells = pytest.mark.parametrize("shell", list(_SHELLS.values()), ids=list(_SHELLS))


# ---------------------------------------------------------------- 검사 도구

def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_]+))")
_DQ_PARAM = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9#?$!@*-])")


class _View:
    """셸 소스에서 '코드'만 남긴다 — 주석·작은따옴표 안·큰따옴표 안의 글자·heredoc 본문을 공백으로(줄 수 그대로).
    큰따옴표 안의 $(…)·`…`·${…}·$이름 은 코드라 남긴다. bash 문법 검사가 주석·메시지·인라인 파이썬에 걸리지 않게."""

    def __init__(self, src: str):
        self.s, self.i, self.out, self.pending = src, 0, [], []

    def run(self) -> str:
        self.code(stop=None)
        return "".join(self.out)

    def emit(self, t: str) -> None:
        self.out.append(t)

    def code(self, stop: str | None) -> None:
        s, depth = self.s, 0
        while self.i < len(s):
            c = s[self.i]
            if c == "\\":
                self.emit(s[self.i:self.i + 2])
                self.i += 2
                continue
            if stop == "`" and c == "`":
                return
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0 and stop == ")":
                    return
                depth = max(0, depth - 1)                       # case 패턴의 짝 없는 ')'
            if c == "'":
                j = s.find("'", self.i + 1)
                j = len(s) if j < 0 else j
                self.emit("'" + _blank(s[self.i + 1:j]) + ("'" if j < len(s) else ""))
                self.i = j + 1
                continue
            if c == '"':
                self.emit('"')
                self.i += 1
                self.dq()
                continue
            if c == "`":
                self.sub("`")
                continue
            if c == "#" and (self.i == 0 or s[self.i - 1] in " \t\n;&|()<>"):
                j = s.find("\n", self.i)
                j = len(s) if j < 0 else j
                self.emit(_blank(s[self.i:j]))
                self.i = j
                continue
            if s.startswith("<<", self.i) and not s.startswith("<<<", self.i):
                m = _HEREDOC.match(s, self.i)
                if m:
                    word = next(g for g in m.groups()[1:] if g is not None)
                    self.pending.append((m.group(1) == "-", word))
                    self.emit(m.group(0))
                    self.i = m.end()
                    continue
            self.emit(c)
            self.i += 1
            if c == "\n":
                while self.pending:
                    self.heredoc(*self.pending.pop(0))

    def sub(self, close: str) -> None:
        """`…` 또는 $(…) — 안은 코드."""
        self.emit("`" if close == "`" else "$(")
        self.i += 1 if close == "`" else 2
        self.code(stop=close if close == "`" else ")")
        if self.i < len(self.s):
            self.emit(self.s[self.i])
            self.i += 1

    def dq(self) -> None:
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c == "\\":
                self.emit(_blank(s[self.i:self.i + 2]))
                self.i += 2
            elif c == '"':
                self.emit('"')
                self.i += 1
                return
            elif s.startswith("$(", self.i):
                self.sub(")")
            elif c == "`":
                self.sub("`")
            elif s.startswith("${", self.i):
                k, depth = self.i + 2, 1
                while k < len(s) and depth:
                    if s.startswith("${", k):
                        depth, k = depth + 1, k + 2
                        continue
                    depth -= s[k] == "}"
                    k += 1
                self.emit(s[self.i:k])
                self.i = k
            elif c == "$" and _DQ_PARAM.match(s, self.i):
                m = _DQ_PARAM.match(s, self.i)
                self.emit(m.group(0))
                self.i = m.end()
            else:
                self.emit(_blank(c))
                self.i += 1

    def heredoc(self, strip: bool, word: str) -> None:
        s = self.s
        while self.i < len(s):
            j = s.find("\n", self.i)
            j = len(s) if j < 0 else j
            line = s[self.i:j]
            self.emit(_blank(line))
            self.i = j
            if self.i < len(s):
                self.emit("\n")
                self.i += 1
            if (line.lstrip("\t") if strip else line) == word:
                return


def _code_view(src: str) -> str:
    return _View(src).run()


_C = r"(?<![\w$./{}-])"                    # 명령 자리(앞이 낱말·$·경로·옵션 글자가 아님)
_BASHISMS = [
    ("[[ ]]", r"\[\[(?![:=.])"),
    ("function 키워드", _C + r"function\s+[A-Za-z_]"),
    ("$'…' 인용", r"\$'"),
    ("<<< here-string", r"<<<"),
    ("source(POSIX 는 .)", _C + r"source\s"),
    ("pipefail", r"\bpipefail\b"),
    ("local", _C + r"local\s+[A-Za-z_-]"),
    ("echo -e/-n(printf 를 쓴다)", _C + r"echo\s+-[neE]+\b"),
    ("배열 대입", _C + r"[A-Za-z_][A-Za-z0-9_]*\+?=\("),
    ("배열 첨자", r"\$\{#?[A-Za-z_][A-Za-z0-9_]*\["),
    ("bash 내장", _C + r"(?:declare|typeset|readarray|mapfile|shopt|pushd|popd|let)(?![\w=-])"),
    ("[ … == … ]", r"\[\s[^\]\n]*\s==\s"),
    ("test … == …", _C + r"test\s[^\n;&|]*\s==\s"),
    ("&> 리다이렉트", r"&>"),
    ("|& 파이프", r"\|&"),
    ("${v:N} 부분 문자열", r"\$\{[A-Za-z_][A-Za-z0-9_]*:(?:[0-9]|\s+-)"),
    ("${v/…} 치환", r"\$\{[A-Za-z_][A-Za-z0-9_]*/"),
    ("${!v} 간접", r"\$\{!"),
    ("${v^^}/${v,,}", r"\$\{[A-Za-z_][A-Za-z0-9_]*[\^,]"),
    ("bash 전용 변수", r"\$\{?(?:RANDOM|BASH[A-Z_]*|EPOCHSECONDS|EPOCHREALTIME|PIPESTATUS|FUNCNAME|SECONDS|UID|EUID"
                    r"|HOSTNAME|OSTYPE)\b"),
    ("printf -v", _C + r"printf\s+-v\b"),
    ("trap ERR/DEBUG/RETURN", _C + r"trap\b[^\n]*\b(?:ERR|DEBUG|RETURN)\b"),
]
_FORBIDDEN = [
    ("set -x/xtrace(토큰·env 가 로그로)", _C + r"set\s+-[A-Za-z]*x|\bxtrace\b"),
    ("env 덤프", _C + r"(?:printenv|export\s+-p)\b|" + _C + r"(?:env|set)[ \t]*(?:$|[;&|])"),
    ("sed -i(GNU·BSD 가 다름)", _C + r"sed\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*i|--in-place"),
    ("readlink -f(맥 옛 버전에 없음)", _C + r"readlink\s+-[A-Za-z]*f"),
    ("stat -c/-f(GNU·BSD 가 다름)", _C + r"stat\s+-[A-Za-z]*[cf]"),
    ("date -d(GNU 전용)", _C + r"date\s+-[A-Za-z]*d"),
    ("cp -T/mv -T(GNU 전용)", _C + r"(?:cp|mv)\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*T"),
    ("flock/setsid/timeout(맥에 없음)", _C + r"(?:flock|setsid|timeout)(?![\w=-])"),
    # 무시('')한 신호는 exec 로 자식에게 물려간다 — 재기동한 Host·브로커·claude 가 HUP 를 못 받으면 브로커가 죽어도
    # claude 가 끝나지 않는다. 막기만 하려면 빈 처리(trap ':' HUP)를 쓴다. 예외는 곧 끝나는 mv 하나를 exec 하는 하위 셸
    # `( trap '' INT TERM HUP; exec mv … )` 뿐 — 터미널 신호는 프로세스 그룹 전체로 가서 옮기던 mv 가 죽으면 설치가 섞인다
    ("trap '' 신호 무시(자식에게 물려간다)", _C + r"trap\s+(?:''|\"\")\s(?![A-Z \t]+;[ \t]*exec[ \t]+mv[ \t])"),
    # ':' 는 특수 내장 — 리디렉션이 실패하면(쓸 수 없는 자리) dash·bash --posix 는 `|| …` 로 넘어가지 않고 스크립트를
    # 끝낸다(test_colon_redirect_failure_ends_the_script). 파일을 비우거나 만들 땐 일반 명령 `true >f` 를 쓴다
    (": 에 리디렉션(특수 내장 — 실패하면 셸이 끝난다)", _C + r":[ \t]*[0-9]*[<>]"),
]
_UMASK = re.compile(_C + r"umask\b")
_UMASK_OK = re.compile(r"umask\s+0?077\s*(?:;|&&)")


def _violations(src: str) -> list[str]:
    view, lines = _code_view(src), src.split("\n")
    hits = []

    def line_of(pos: int) -> int:
        return view.count("\n", 0, pos) + 1
    for label, rx in _BASHISMS + _FORBIDDEN:
        for m in re.finditer(rx, view, re.M):
            hits.append(f"{line_of(m.start())}: {label}: {lines[line_of(m.start()) - 1].strip()[:80]}")
    for m in _UMASK.finditer(view):
        before = view[view.rfind("\n", 0, m.start()) + 1:m.start()]
        if not (re.search(r"\(\s*$", before) and _UMASK_OK.match(view, m.start())):
            hits.append(f"{line_of(m.start())}: umask 는 ( umask 077; … ) 하위 셸에서만(S38)")
    return hits


_PY_OPEN = re.compile(r"^([A-Z][A-Z0-9_]*_PY)='[ \t]*$")


def _inline_python(src: str) -> list[dict]:
    """`NAME_PY='` … `'` 블록들 — {name, line, body, closed}."""
    lines, out, i = src.split("\n"), [], 0
    while i < len(lines):
        m = _PY_OPEN.match(lines[i])
        if m:
            j = i + 1
            while j < len(lines) and lines[j] != "'":
                j += 1
            out.append({"name": m.group(1), "line": i + 1, "body": "\n".join(lines[i + 1:j]), "closed": j < len(lines)})
            i = j
        i += 1
    return out


def _python_problems(src: str) -> list[str]:
    bad = []
    for b in _inline_python(src):
        if not b["closed"]:
            bad.append(f"{b['name']}({b['line']}): 닫는 ' 한 줄이 없다")
            continue
        if "'" in b["body"]:
            bad.append(f"{b['name']}({b['line']}): 작은따옴표가 있다 — 셸 문자열이 거기서 끝난다")
            continue
        try:
            compile(b["body"], f"{b['name']}", "exec")
        except SyntaxError as e:
            bad.append(f"{b['name']}({b['line']}): 파이썬 문법 오류 {e}")
    return bad


def _home_block(src: str) -> str | None:
    m = re.search(r"^[ \t]*# >>> clewpath-home.*?^[ \t]*# <<< clewpath-home[^\n]*\n", src, re.S | re.M)
    return textwrap.dedent(m.group(0)) if m else None


# ---------------------------------------------------------------- 검사 도구 자체(합성 예제 — 지금도 돈다)

_GOOD = r"""#!/bin/sh
# 주석은 검사하지 않는다: [[ local source function echo -e <<< pipefail timeout set -x umask 022 sed -i
set -u
PROBE_PY='
import json, sys, urllib.request
r = urllib.request.urlopen("http://127.0.0.1:1/api/health", timeout=3)
'
msg="timeout 이나 [[ 같은 글자가 메시지에 있어도 된다 $HOME ${x:-기본} $(date +%s)"
case "$p" in *[[:cntrl:]]*) printf '%s\n' "bad" ;; esac
x=$(printf '%s' "$msg" | tr -d '[:space:]')
n=$((1 + 2)); [ "$n" -eq 3 ] && [ "$a" = "b" ] && printf '%s %s %s\n' "${x:-d}" "${#x}" "${x%/*}"
( umask 077; printf 'pid=%s\n' "$$" > "$DATA_DIR/update.inprogress" )
{ true >"$SWAP/committed"; } 2>/dev/null || printf '%s\n' "쓰지 못함"
trap ':' HUP INT
trap 'STOP_REQ=1' TERM
( trap '' INT TERM HUP; exec mv "$a" "$b" )
cat <<'EOF'
local x; [[ $a == b ]]; source y; timeout 5 z
EOF
cmd >/dev/null 2>&1 &
set -- "$@" --port "$PORT"
timeout_s=5; printf '%s\n' "$timeout_s" "${timeout_s}"
main() { : ; }
main "$@"
"""

_BAD = [
    '[[ -n "$x" ]] && :', "function f { :; }", "x=$'a\\n'", 'cat <<< "$x"', "source ./lib.sh",
    "set -o pipefail", "f() { local y=1; }", 'echo -e "a\\tb"', 'echo -n "x"', "arr=(a b)", 'printf "%s" "${arr[0]}"',
    'declare -r X=1', '[ "$a" == "b" ]', "cmd &>/dev/null", 'printf "%s" "${x:0:3}"', 'printf "%s" "${x//a/b}"',
    'printf "%s" "${!name}"', 'printf "%s" "${x^^}"', "n=$RANDOM", 'printf "%s" "$BASH_SOURCE"', "printf -v x %s y",
    "trap 'cleanup' ERR", "set -x", "set -eux", "printenv", "env", "export -p", "sed -i 's/a/b/' f",
    "sed -n -i.bak 's/a/b/' f", "readlink -f .", "stat -c %Y f", "date -d yesterday", "cp -T a b", "flock f cmd",
    "setsid cmd", "x=$(timeout 5 cmd)", "umask 077", "( umask 022; : )", "cmd |& tee f",
    "trap '' HUP INT", 'trap "" TERM', "( trap '' HUP; exec \"$HOST\" )", ': >"$f"', '{ : >"$SWAP/committed"; } 2>/dev/null || log x', ": 2>/dev/null",
]


def test_scanner_passes_posix_code():
    assert _violations(_GOOD) == []
    assert _python_problems(_GOOD) == []


@pytest.mark.parametrize("snippet", _BAD)
def test_scanner_catches_bashisms_and_forbidden(snippet):
    assert _violations(f"#!/bin/sh\n{snippet}\n"), snippet


def test_code_view_keeps_lines_and_hides_text():
    src = "a='x\ny'\n# c\nb=\"q $(echo 'z') ${v:-w}\"\ncat <<EOF\nbody\nEOF\nend\n"
    view = _code_view(src)
    assert view.count("\n") == src.count("\n")
    assert "x" not in view.split("\n")[0] and "body" not in view and "end" in view
    assert "$(echo '" in view and "${v:-w}" in view and "q" not in view.split("\n")[3]


def test_inline_python_rules():
    ok = "#!/bin/sh\nA_PY='\nimport sys\nprint(sys.argv)\n'\n"
    assert _python_problems(ok) == [] and [b["name"] for b in _inline_python(ok)] == ["A_PY"]
    quote = "#!/bin/sh\nA_PY='\nprint('x')\n'\n"
    assert "작은따옴표" in _python_problems(quote)[0]
    broken = "#!/bin/sh\nA_PY='\nif True print(1)\n'\n"
    assert "문법" in _python_problems(broken)[0]
    unclosed = "#!/bin/sh\nA_PY='\nprint(1)\n"
    assert "닫는" in _python_problems(unclosed)[0]


@with_shells
@pytest.mark.parametrize("uname,env,want", [
    ("Linux", {}, "{H}/.local/share/clewpath"),
    ("Linux", {"XDG_DATA_HOME": "rel/x"}, "{H}/.local/share/clewpath"),    # 상대 경로 XDG 는 무시(명세)
    ("Linux", {"XDG_DATA_HOME": "/x/데이터"}, "/x/데이터/clewpath"),
    ("Linux", {"CLEWPATH_HOME": "/c 홈"}, "/c 홈"),
    ("Darwin", {}, "{H}/Library/Application Support/ClewPath"),
    ("Darwin", {"XDG_DATA_HOME": "/x"}, "{H}/Library/Application Support/ClewPath"),
    ("Darwin", {"CLEWPATH_HOME": "/c"}, "/c"),
])
def test_home_block_resolves_install_home(shell, uname, env, want):
    """공통 경로 블록의 뜻(Q1): 맥 ~/Library/Application Support/ClewPath, 리눅스 ${XDG_DATA_HOME:-~/.local/share}/clewpath,
    CLEWPATH_HOME 이 있으면 그것. uname 은 셸 함수로 바꿔 끼운다."""
    home = "/home/u x/홈"
    script = 'uname() { printf "%s\\n" "$FAKE_UNAME"; }\n' + HOME_BLOCK + 'printf "%s" "$CLEWPATH_HOME"\n'
    e = {k: v for k, v in os.environ.items() if k not in ("XDG_DATA_HOME", "CLEWPATH_HOME")}
    e.update(HOME=home, FAKE_UNAME=uname, **env)
    r = subprocess.run([*shell, "-c", script], env=e, capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert r.stdout.decode("utf-8") == want.replace("{H}", home)


def test_home_block_is_posix():
    assert _violations("#!/bin/sh\n" + HOME_BLOCK) == []
    assert _home_block("#!/bin/sh\nmain() {\n" + textwrap.indent(HOME_BLOCK, "  ") + "}\n") == HOME_BLOCK


# ---------------------------------------------------------------- 런처 렌더(지금도 합성 템플릿으로)

_SYNTH_TPL = ("#!/bin/sh\n# clewpath-launcher v2 (posix) — 시험용 합성 템플릿\n"
              "DATA_DIR={{DATA_DIR}}\nCONF_FILE={{CONF_FILE}}\nDEFAULT_PORT={{PORT}}\n"
              "printf '%s\\n' \"$DATA_DIR\" \"$CONF_FILE\" \"$DEFAULT_PORT\"\n")
AWKWARD = "/home/u x/My Apps/클루패스 it's & co/$HOME`id`;$(id) \"q\""


@with_shells
def test_rendered_launcher_values_survive_the_shell(shell, tmp_path):
    """공백·한글·작은따옴표·& ·$·백틱이 든 경로로 렌더해도 자리표시가 남지 않고, 셸이 읽은 값이 원래 경로와 같다."""
    data, conf = PurePosixPath(AWKWARD), PurePosixPath(AWKWARD + "/clewpath.toml")
    out = _posix.render_launcher(_SYNTH_TPL, data_dir=data, conf_file=conf, port=5123)
    assert "{{" not in out
    f = tmp_path / "start-connector.sh"
    f.write_bytes(out.encode("utf-8"))
    assert subprocess.run([*shell, "-n", str(f)], capture_output=True, timeout=30).returncode == 0
    r = subprocess.run([*shell, str(f)], capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert r.stdout.decode("utf-8").split("\n")[:3] == [AWKWARD, AWKWARD + "/clewpath.toml", "5123"]


# ---------------------------------------------------------------- 실제 파일(PENDING 에서 빠지면 켜진다)

def test_pending_list_is_honest():
    for name, p in PATHS.items():
        if name in PENDING:
            assert not p.exists(), f"{name} 를 만들었으면 tests/test_posix_scripts.py 의 PENDING 에서 빼세요"
        else:
            assert p.exists(), f"{name} 가 없다: {p}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_format(name):
    raw = _need(name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM 금지"
    assert b"\r" not in raw, "CR 금지(LF 만) — 편집기가 CRLF 로 저장해도 git 은 깨끗해 보인다"
    raw.decode("utf-8")
    lines = raw.split(b"\n")
    assert lines[0] == b"#!/bin/sh", "1행은 #!/bin/sh"
    if name == "start-connector.template.sh":
        assert b"clewpath-launcher v2" in lines[1], "2행에 런처 표식(ensure_launcher·사람이 알아보게)"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_is_posix_sh(name):
    hits = _violations(_text(name))
    assert not hits, f"{name}:\n  " + "\n  ".join(hits)


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_inline_python(name):
    bad = _python_problems(_text(name))
    assert not bad, f"{name}:\n  " + "\n  ".join(bad)


def _parse_targets(name: str, tmp_path) -> Path:
    """문법 검사 대상 — 런처 템플릿은 어색한 경로로 렌더한 결과를 본다(설치본이 실제로 돌리는 모양)."""
    p = _need(name)
    if name != "start-connector.template.sh":
        return p
    out = tmp_path / "start-connector.sh"
    # write_bytes — write_text 는 Windows 에서 줄끝을 CRLF 로 바꿔 dash 가 깨진다(설치본은 ensure_launcher 가 LF 로 쓴다)
    out.write_bytes(_posix.render_launcher(p.read_text(encoding="utf-8"), data_dir=PurePosixPath(AWKWARD),
                                           conf_file=PurePosixPath(AWKWARD + "/c.toml"), port=5100).encode("utf-8"))
    return out


@with_shells
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses(name, shell, tmp_path):
    target = _parse_targets(name, tmp_path)
    r = subprocess.run([*shell, "-n", str(target)], capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck 없음(리눅스 컨테이너에서 apt 로)")
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_shellcheck(name, tmp_path):
    target = _parse_targets(name, tmp_path)
    r = subprocess.run(["shellcheck", "-s", "sh", "-S", "warning", str(target)], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")


def _comsub_case_hits(src: str) -> list[str]:
    """맥의 /bin/sh(bash 3.2)는 $( … ) 의 끝을 괄호 수만 세어 찾는다(따옴표·역슬래시만 건너뛴다 — parse_matched_pair).
    안에 든 case 의 패턴이 `pat)` 이면 그 `)` 에서 명령 치환이 끝난 것으로 읽어 파일 전체가 문법 오류다(dash·새 bash 는
    통과해 리눅스에선 안 보인다). 그렇게 센 끝까지에 case 가 esac 보다 많으면 걸린다 — 패턴은 `(pat)` 로 연다."""
    hits, i, n = [], 0, len(src)
    while (j := src.find("$(", i)) >= 0:
        i = j + 2
        if src.startswith("$((", j) or src[src.rfind("\n", 0, j) + 1:j].lstrip().startswith("#"):
            continue
        k, depth, code = j + 2, 1, []
        while k < n and depth:
            c = src[k]
            if c == "\\":
                k += 2
            elif c == "'":
                e = src.find("'", k + 1)
                k = n if e < 0 else e + 1
            elif c == '"':                                  # 큰따옴표 안의 괄호는 세지 않는다(안의 $( … ) 는 따로 짝)
                k, dq = k + 1, 0
                while k < n and not (src[k] == '"' and dq == 0):
                    if src[k] == "\\":
                        k += 1
                    elif src.startswith("$(", k):
                        dq, k = dq + 1, k + 1
                    elif src[k] == ")" and dq:
                        dq -= 1
                    k += 1
                k += 1
            else:
                depth += (c == "(") - (c == ")")
                code.append(c)
                k += 1
        words = re.findall(r"\b(?:case|esac)\b", "".join(code))
        if words.count("case") > words.count("esac"):
            hits.append(f"{src.count(chr(10), 0, j) + 1}: {src[j:k].splitlines()[0][:80]}")
    return hits


@pytest.mark.parametrize("name", SCRIPTS)
def test_case_patterns_inside_command_substitution_open_with_paren(name):
    bad = 'x=$(ps | while read -r l; do\n  case "$l" in *"$1"*) echo y ;; esac\ndone)\n'
    good = ['x=$(ps | while read -r l; do\n  case "$l" in (*"$1"*) echo y ;; esac\ndone)\n',
            'case "$p" in *b*) x=$(echo "$(date) )") ;; esac\n', 'n=$((1 + 2)); y="$(printf \'%s)\' "a")"\n']
    assert _comsub_case_hits(bad) and not [g for g in good if _comsub_case_hits(g)], "검사 도구 자체"
    hits = _comsub_case_hits(_text(name))
    assert not hits, f"{name}: $( ) 안의 case 패턴은 ( 로 시작해야 한다(맥 bash 3.2):\n  " + "\n  ".join(hits)


PHYS_PWD = "phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }"


@pytest.mark.parametrize("name", ("get.sh", "install.sh", "uninstall.sh", "start-connector.template.sh"))
def test_physical_paths_use_the_shared_phys_pwd(name):
    """물리 경로는 /bin/pwd -P(맥 APFS 에서 디스크의 대소문자) — 셸 내장 pwd -P 는 친 글자를 그대로 둬, 대소문자만 다르게
    부르면 명령줄 판정(설치 경로 접두)이 빗나갔다(2회차 검토). 네 스크립트가 같은 본문을 쓰고, 그 밖에서 내장 pwd -P 를 부르지 않는다.
    러너는 -InstallRoot(파이썬 resolve·런처 phys_pwd 결과)를 그대로 써서 스스로 계산하지 않는다."""
    lines = _text(name).splitlines()
    assert [ln for ln in lines if ln.startswith("phys_pwd()")] == [PHYS_PWD], f"{name}: phys_pwd 본문이 약속과 다르다"
    stray = [ln for ln in lines if "pwd -P" in ln and not ln.startswith("phys_pwd()") and not ln.lstrip().startswith("#")]
    assert not stray, f"{name}: phys_pwd 밖에서 pwd -P:\n  " + "\n  ".join(stray)


@pytest.mark.parametrize("name", HOME_SCRIPTS)
def test_home_block_is_shared(name):
    assert _home_block(_text(name)) == HOME_BLOCK, f"{name} 의 공통 경로 블록이 다르다(바꾸면 넷 다, 그리고 HOME_BLOCK)"


def test_launcher_template_contract():
    t = _text("start-connector.template.sh")
    for needle in ("maintenance.flag", "update.inprogress", "launcher.lock", "runtime.json", "/api/health",
                   "launcher.log", "{{DATA_DIR}}", "{{CONF_FILE}}", "{{PORT}}", "start_new_session", '"ab"',
                   "CLEWPATH_UPDATER", "CLEWPATH_CONFIG", "session_manager.server"):
        assert needle in t, needle
    blocks = {b["name"]: b["body"] for b in _inline_python(t)}
    assert {"PROBE_PY", "SPAWN_PY"} <= set(blocks), sorted(blocks)
    for name in ("PROBE_PY", "SPAWN_PY"):
        assert not re.search(r"^\s*(?:import|from)\s+session_manager\b", blocks[name], re.M), \
            f"{name} 가 session_manager 를 import 한다(롤백 뒤 렌더 버전과 코드 버전이 어긋나도 돌아야 한다)"
    tree = ast.parse(blocks["SPAWN_PY"])
    scrub = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(isinstance(tg, ast.Name) and tg.id == "SCRUB" for tg in n.targets)]
    assert len(scrub) == 1, "SPAWN_PY 에 SCRUB = {…} 집합이 하나"
    assert ast.literal_eval(scrub[0]) == set(ptyd._SCRUB_EXACT) | {"CLEWPATH_UPDATER"}, "정리 목록은 브로커와 같은 규칙"
    assert "CLAUDE_CODE_" in {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}


def test_runner_spares_the_broker():
    """롤백이 실패본을 멈출 때 터미널 관리 프로세스(브로커)는 빼낸다 — test_ptyd 의 ps1 판과 같은 약속."""
    t = _text("update_runner.sh")
    assert "session_manager.ptyd" in t and "session_manager.server" in t
    assert re.search(r'^PAYLOAD="[^"]+"', t, re.M), "PAYLOAD 목록(test_updater.test_payload_lists_agree 가 대조)"


def test_uv_pins_match_windows_installer():
    t = _text("install.sh")
    ps = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    win = re.search(r'^\$UvDefault = "([0-9.]+)"', ps, re.M).group(1)
    m = re.search(r'^UV_DEFAULT="?([0-9.]+)"?\s*$', t, re.M)
    assert m and m.group(1) == win, f"install.sh UV_DEFAULT 는 install.ps1 $UvDefault({win}) 와 같아야 한다"
    body = re.search(r"^uv_pinned_sha256\(\)\s*\{(.*?)^\}", t, re.S | re.M)
    assert body, "uv_pinned_sha256() 함수가 없다"
    pins = dict(re.findall(r"""["']([0-9.]+\|[a-z0-9_-]+)["']\)\s*(?:echo|printf\s+'%s\\n')\s*["']?([0-9a-fA-F]*)""",
                           body.group(1)))
    mine = {k.split("|")[1]: v for k, v in pins.items() if k.split("|")[0] == win}
    assert set(mine) == UV_TARGETS, f"대상 4개: {sorted(mine)}"
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in mine.values()), "64자 소문자 hex(공식 .sha256 과 직접 계산이 같을 때만)"


# 함수 밖에 둘 수 있는 줄: set -u, 값 대입(NAME_PY=' … 의 닫는 ' 한 줄 포함), 함수 정의의 첫 줄, 맨 끝 main "$@"
_TOP_OK = re.compile(r"""set -u|[A-Za-z_][A-Za-z0-9_]*=\S*|'|[a-z_][a-z0-9_]*\(\) \{.*|main "\$@\"""")


def test_get_sh_runs_only_through_main():
    """curl | sh 는 받는 대로 실행한다 — 중간에 끊기면 반쪽 스크립트가 돈다. 그래서 맨 위(함수 밖)에는 주석·set -u·값 대입·
    함수 정의뿐이고, 일은 전부 main() 안에서 맨 끝 줄 `main "$@"` 로만 시작한다(3.1)."""
    src = _text("get.sh")
    assert src.rstrip("\n").split("\n")[-1] == 'main "$@"', "맨 끝 줄은 main \"$@\""
    depth, top = 0, []
    for i, line in enumerate(_code_view(src).split("\n"), 1):      # 문자열·주석·heredoc 은 공백 — 괄호만 센다
        if depth == 0 and line.strip():
            top.append((i, line.strip()))
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"{i}: 괄호 짝"
    bad = [f"{i}: {s[:60]}" for i, s in top if not _TOP_OK.fullmatch(s)]
    assert not bad, "함수 밖에서 도는 명령(끊긴 다운로드가 실행할 수 있다):\n  " + "\n  ".join(bad)
    assert [s for _, s in top].count('main "$@"') == 1 and top[-1][1] == 'main "$@"'


def test_get_sh_fetches_only_with_https_curl():
    """내려받기는 g_curl 한 곳 — https 만(시험용 --allow-http 때만 http), 리다이렉트로 내려가지 않게, TLS 1.2 이상(S7)."""
    view = _code_view(_text("get.sh"))
    calls = [ln.strip() for ln in view.split("\n")
             if re.search(r"(?<![\w$/-])curl(?![\w-])", ln) and not re.match(r"\s*for\s", ln)]
    assert len(calls) == 1, calls
    assert all(f in calls[0] for f in ("--proto ", "--proto-redir ", "--tlsv1.2")), calls[0]
    t = _text("get.sh")
    assert "_gp='=https'" in t and "_gp='=http,https'" in t and re.search(r'ALLOW_HTTP" = 1 \]; then _gp=\'=http,https\'', t)
    assert "--max-filesize" in t and "--max-time" in t


def test_get_sh_states_its_trust_model():
    """첫 설치의 신뢰는 TLS 뿐이고 서명은 여기서 보지 않는다는 사실을 머리말에 정직하게(get.ps1 과 같다, R7)."""
    head = _text("get.sh").split("\nset -u\n", 1)[0]
    for needle in ("TLS", "서명", "검증하지 않는다", "SHA-256", "공개키"):
        assert needle in head, needle


def test_hostctl_takes_tokens_from_env_only():
    t = _text("hostctl.py")
    assert not re.search(r"add_argument\(\s*['\"]--[\w-]*token", t, re.I), "토큰 인자 금지(ps 에 보인다, S17)"
    assert "CLEWPATH_CLIENT_TOKEN" in t and "CLEWPATH_AGENT_TOKEN" in t


def test_hostctl_top_level_imports_are_stdlib():
    """맨 위 import 는 표준 라이브러리만 — 의존성 하나가 깨져도 paths 같은 기본 명령은 돈다(명세 4.3)."""
    import sys
    tree = ast.parse(_text("hostctl.py"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top.add(node.module.split(".")[0])
    assert top - {"__future__"} <= set(sys.stdlib_module_names), sorted(top - set(sys.stdlib_module_names))


@with_shells
def test_colon_redirect_failure_ends_the_script(shell):
    """_FORBIDDEN 의 ':' 규칙의 근거 — 특수 내장(:)의 리디렉션이 실패하면 dash·bash --posix 는 `|| …` 로 넘어가지 않고
    스크립트를 끝낸다(러너가 커밋 표식을 못 쓰면 정리 없이 죽던 자리). 일반 명령(true)은 실패만 돌려준다."""
    bad = subprocess.run([*shell, "-c", '{ : >/nonexistent-dir/x; } 2>/dev/null || printf survived; printf end'],
                         capture_output=True, timeout=30)
    assert b"survived" not in bad.stdout and b"end" not in bad.stdout and bad.returncode != 0
    good = subprocess.run([*shell, "-c", '{ true >/nonexistent-dir/x; } 2>/dev/null || printf survived; printf end'],
                          capture_output=True, timeout=30)
    assert good.stdout == b"survivedend" and good.returncode == 0


def test_install_finds_python_outside_virtualenvs():
    """`uv python find` 는 cwd 의 .venv·VIRTUAL_ENV 를 먼저 답한다(0.12.1 실측) — 설치 루트에서 재설치하면 자기 venv 를
    '시스템 파이썬'으로 집는다. 늘 --system. 관리 런타임은 --no-bin(사용자 ~/.local/bin 을 건드리지 않게)."""
    view = _code_view(_text("install.sh"))
    finds = re.findall(r"python find[^\n]*", view)
    assert finds and all("--system" in f for f in finds), finds
    t = _text("install.sh")
    assert "--no-bin" in t and "UV_PYTHON_INSTALL_DIR" in t


@pytest.mark.parametrize("name", ["install.sh", "uninstall.sh"])
def test_scripts_leave_claude_files_alone(name):
    """claude 의 settings.json 은 읽기만 하고(훅 제거는 안내만 — 결정 Q6), projects 는 어떤 경우에도 지우지 않는다(S36·S37)."""
    for i, line in enumerate(_text(name).splitlines(), 1):
        code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        if "settings.json" in code:
            assert not re.search(r"(?:^|[\s;&|(])(?:rm|mv|cp|sed|tee|truncate)\s|>\s*\"?[^\"]*settings\.json", code), \
                f"{name}:{i}: settings.json 에 쓰거나 지운다: {line.strip()}"
        if re.search(r"(?:^|[\s;&|(])rm\s", code):
            assert "projects" not in code, f"{name}:{i}: projects 를 지운다: {line.strip()}"


def test_e2e_harness_never_touches_release_keys_or_production():
    d = _need("posix_e2e")
    bad = []
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.suffix in (".pyc",):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "e2e-guard" in line:
                continue
            if re.search(r"release-keys|keygen|pyongso\.com", line):
                bad.append(f"{p.relative_to(ROOT).as_posix()}:{i}: {line.strip()[:80]}")
    assert not bad, "시험 하네스는 운영 키·운영 CP 를 모른다(S19·S44):\n  " + "\n  ".join(bad)


def test_e2e_harness_shell_is_posix():
    """e2e 이미지 빌드에서 도는 셸(warm_cache.sh)도 다섯 스크립트와 같은 규칙 — LF·BOM 없음·#!/bin/sh·POSIX sh 만."""
    shs = sorted(_need("posix_e2e").glob("*.sh"))
    assert shs, "tests/posix_e2e 에 셸 스크립트가 없다"
    for p in shs:
        raw = p.read_bytes()
        assert raw.startswith(b"#!/bin/sh\n") and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf"), p.name
        hits = _violations(raw.decode("utf-8"))
        assert not hits, f"{p.name}:\n  " + "\n  ".join(hits)
        for sid, shell in _SHELLS.items():
            r = subprocess.run([*shell, "-n", str(p)], capture_output=True, timeout=30)
            assert r.returncode == 0, (p.name, sid, r.stderr.decode("utf-8", "replace"))

