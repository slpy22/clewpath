"""문서 계약 — 맥·리눅스 설치(docs/designs/posix-install.md 1.1-5, 9단계). 모든 OS 에서 돈다(파일만 읽는다).

- README 의 맥·리눅스 절: 시험용 표시, 한 줄 설치 주소(CP /get.sh 공개 — docs/deploy/get-sh-publish.md)가 get.sh 머리말의
  명령과 같은지, 'Windows 전용 릴리스'·'자동 시작 없음' 같은 말이 릴리스 도구·설치 스크립트의 지금 동작과 맞는지.
  그 절이 보여 주는 명령·옵션·환경변수·경로는 스크립트에 실제로 있어야 하고, 사용자가 쓸 하위 명령·제거 옵션은 빠짐없이 적는다.
- README 의 업데이트 교체 목록 = updater.PAYLOAD, 폴더 구조에 맥·리눅스 스크립트.
- multi-os.md 단계 표: 3a(공통 설치·업데이트)는 완료·브랜치·명세를 밝히고 말한 파일이 있다, 3b(맥 실기)는 대기.
- TODOS: 멀티 OS 3a 의 단계 1~9 진행, 남은 일(합침·맥 실기·CP /get.sh), 명세 8.1 R15 의 Windows 기존 결함 ①~⑦ 별건 항목.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from session_manager import updater

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TODOS = ROOT / "TODOS.md"
MULTI_OS = ROOT / "docs" / "designs" / "multi-os.md"
SPEC = ROOT / "docs" / "designs" / "posix-install.md"
SCRIPTS = {
    "get.sh": ROOT / "ops" / "get.sh",
    "install.sh": ROOT / "install.sh",
    "uninstall.sh": ROOT / "uninstall.sh",
    "start-connector.sh": ROOT / "session_manager" / "start-connector.template.sh",   # 설치본 런처의 원본
    "update_runner.sh": ROOT / "session_manager" / "update_runner.sh",
    "hostctl.py": ROOT / "session_manager" / "hostctl.py",
}
REPO_PATH = re.compile(r"^(?:ops|tests|docs|session_manager)/[\w./-]+$")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 마크다운 도구

def _sections(text: str) -> list[tuple[int, str, str]]:
    """(수준, 제목, 본문) — 코드 블록 안의 '# …' 줄(셸 주석)은 제목이 아니다."""
    out, cur, fence = [], None, False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#{1,6}) (.*)", line)
        if m:
            cur = [len(m.group(1)), m.group(2).strip(), ""]
            out.append(cur)
        elif cur is not None:
            cur[2] += line
    return [(lv, t, b) for lv, t, b in out]


def _section(text: str, pattern: str) -> str:
    """제목이 pattern 에 맞는 절 — 제목 줄 + 본문 + 더 깊은 하위 절(같거나 높은 수준의 다음 제목 앞까지)."""
    secs = _sections(text)
    for i, (lv, title, body) in enumerate(secs):
        if re.search(pattern, title):
            parts = [title + "\n", body]
            for lv2, t2, b2 in secs[i + 1:]:
                if lv2 <= lv:
                    break
                parts.append(f"{'#' * lv2} {t2}\n{b2}")
            return "".join(parts)
    raise AssertionError(f"제목을 찾지 못했습니다: {pattern}")


def _code(md: str) -> list[str]:
    """코드 조각 — 펜스 블록은 줄마다, 인라인 `…` 는 하나씩."""
    out, fence = [], False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            out.append(line)
        else:
            out += re.findall(r"`([^`\n]+)`", line)
    return out


def _table(md: str) -> list[list[str]]:
    """표의 행(머리 포함, 구분 줄 제외) — 칸 안의 `\\|` 는 칸 경계가 아니다."""
    rows = []
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|") or re.fullmatch(r"\|[\s|:-]+\|", s):
            continue
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", s)[1:-1]])
    return rows


_STOP = ("|", "||", "&&", ";", "&", "#")


def _invocations(snippets: list[str], script: str) -> list[list[str]]:
    """'…/script 인자…' 의 인자 목록들 — 셸 구분자·리디렉션·주석 앞까지."""
    found = []
    for s in snippets:
        toks = s.split()
        for i, t in enumerate(toks):
            if t.strip("'\"").rsplit("/", 1)[-1] != script:
                continue
            args = []
            for a in toks[i + 1:]:
                if a in _STOP or a.startswith((">", "2>", "#", "&", "|", "(")):
                    break
                args.append(a)
            found.append(args)
    return found


# ---------------------------------------------------------------- 셸 스크립트에서 받는 인자

def _func_body(src: str, name: str) -> str:
    m = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n(.*?)^\}}", src)
    assert m, f"함수 {name} 를 찾지 못했습니다"
    return m.group(1)


def _case_words(src: str) -> set[str]:
    """case 패턴의 낱말 — `  --a|--b)` → {--a, --b}. 한 줄짜리 case(`in '') … ;; --hold) … ;;`)도."""
    words: set[str] = set()
    for m in re.finditer(r"(?m)(?:^|;;|\bin)[ \t]*((?:[-A-Za-z0-9]+\|)*[-A-Za-z0-9]+)\)", src):
        words.update(m.group(1).split("|"))
    return words


def _options(script: str) -> set[str]:
    src = _read(SCRIPTS[script])
    body = _func_body(src, "parse_args" if script in ("install.sh", "get.sh") else "main")
    return {w for w in _case_words(body) if w.startswith("-")}


def _launcher() -> tuple[set[str], set[str]]:
    """(하위 명령, stop 의 플래그) — 런처 끝의 디스패치 case 와 cmd_stop."""
    src = _read(SCRIPTS["start-connector.sh"])
    m = re.search(r'(?ms)^case "\$C_CMD" in\n(.*?)^esac', src)
    assert m, "런처의 하위 명령 디스패치를 찾지 못했습니다"
    cmds = {w for w in _case_words(m.group(1)) if not w.startswith("-") and w != "help"}
    stop = {w for w in _case_words(_func_body(src, "cmd_stop")) if w.startswith("-")}
    return cmds, stop


def _assign(path: Path, name: str):
    for node in ast.parse(_read(path)).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} 에 {name} 가 없습니다")


def _home_defaults() -> tuple[str, str]:
    """공통 경로 블록(install.sh)의 기본 설치 위치 → README 표기(~ 와 XDG 꼴): (맥, 리눅스)."""
    src = _read(SCRIPTS["install.sh"])
    mac = re.search(r'Darwin\) CLEWPATH_HOME="\$\{CLEWPATH_HOME:-\$HOME/([^}"]+)\}"', src)
    xdg = re.search(r'_xdg="\$HOME/([^"]+)"', src)
    leaf = re.search(r'CLEWPATH_HOME="\$\{CLEWPATH_HOME:-\$_xdg/([^}"]+)\}"', src)
    assert mac and xdg and leaf, "install.sh 의 공통 경로 블록 모양이 바뀌었습니다(test_posix_scripts.HOME_BLOCK)"
    return f"~/{mac.group(1)}", "${XDG_DATA_HOME:-~/" + xdg.group(1) + "}/" + leaf.group(1)


# ---------------------------------------------------------------- 도구 자체

def test_markdown_helpers_on_synthetic_text():
    md = ("# A\n## 맥·리눅스 (시험용)\n본문 `sh x/install.sh --dry-run`\n```sh\n# 주석은 제목이 아니다\n"
          "sh uninstall.sh --yes   # 묻지 않고\nnohup sh start-connector.sh watch >/dev/null 2>&1 &\n```\n"
          "### 하위\n| a | b |\n|---|---|\n| `x \\| y` | 2 |\n## 다음\n끝\n")
    sec = _section(md, r"^맥·리눅스")
    assert "주석은 제목이 아니다" in sec and "### 하위" in sec and "끝" not in sec
    code = _code(sec)
    assert _invocations(code, "install.sh") == [["--dry-run"]]          # uninstall.sh 는 install.sh 가 아니다
    assert _invocations(code, "uninstall.sh") == [["--yes"]]
    assert _invocations(code, "start-connector.sh") == [["watch"]]
    assert _table(sec) == [["a", "b"], ["`x \\| y`", "2"]]
    assert _case_words("  --a|--b) x ;;\n  -h|--help) usage ;;\n  *) bad ;;\n") == {"--a", "--b", "-h", "--help"}
    assert _case_words("case \"${1:-}\" in '') H=0 ;; --hold) H=1 ;; *) bad ;; esac\n") == {"--hold"}


def test_script_argument_tables_are_found():
    """인자 표를 읽는 방법이 스크립트와 어긋나면(함수 이름·case 모양이 바뀌면) 아래 문서 시험이 헛돌지 않게."""
    assert {"--dry-run", "--reinstall", "--port", "--help"} <= _options("install.sh")
    assert {"--yes", "--purge-data", "--purge-uv", "--purge"} <= _options("uninstall.sh")
    assert {"--dir", "--port", "--cp-url"} <= _options("get.sh")
    cmds, stop = _launcher()
    assert {"start", "stop", "status", "resume", "watch"} <= cmds and stop == {"--hold"}


# ---------------------------------------------------------------- README: 맥·리눅스 절

def _posix() -> str:
    return _section(_read(README), r"^맥·리눅스")


ONE_LINER = "curl -fsSL https://clewpath.pyongso.com/get.sh | sh"


def test_readme_posix_section_is_marked_test_only_and_shows_the_one_liner():
    sec = _posix()
    head = sec.splitlines()[0]
    assert "시험용" in head, f"맥·리눅스 절 제목에 '시험용' 표시가 없습니다: {head}"
    assert ONE_LINER in "\n".join(_code(sec)), "맥·리눅스 절에 한 줄 설치 명령이 없습니다"
    assert not re.search(r"한 줄 설치[^\n]*(아직|공개 전)", sec), "한 줄 설치가 아직 공개 전이라는 말이 남아 있습니다"
    assert "1. 준비물" in _read(README) and "맥·리눅스" in _section(_read(README), r"^1\. 준비물"), \
        "Windows 준비물에서 맥·리눅스 절을 가리켜야 합니다"


def test_readme_publishes_the_get_sh_url():
    """CP 의 /get.sh 공개(명세 7절, docs/deploy/get-sh-publish.md). 사용자가 그대로 붙여 넣는 줄이라 get.sh 머리말의 명령과
    글자까지 같아야 하고, 안내하는 주소는 nginx 가 여는 /get.sh 하나다(/cp/get.sh 는 catch-all 로 열려 있어도 안내하지 않는다)."""
    text = _read(README)
    assert ONE_LINER in _read(SCRIPTS["get.sh"]).splitlines()[3], "get.sh 머리말의 한 줄 설치 명령과 다릅니다"
    assert ONE_LINER in "\n".join(_code(_section(text, r"^2\. 설치"))), "설치 절에 맥·리눅스 한 줄이 없습니다"
    assert ONE_LINER in _section(text, r"^1\. 준비물"), "준비물의 맥·리눅스 안내에 한 줄 설치가 없습니다"
    urls = set(re.findall(r"[^\s`(]*pyongso\.com/[^\s`)]*get\.sh[^\s`)]*", text))
    assert urls == {"https://clewpath.pyongso.com/get.sh"}, f"한 줄 설치 주소가 하나가 아닙니다: {sorted(urls)}"
    assert "/cp/get.sh" not in text
    assert "irm https://clewpath.pyongso.com/get | iex" in text            # Windows 한 줄 설치는 그대로


def test_readme_windows_only_release_matches_release_tools():
    """'게시되는 릴리스는 Windows 전용' 이라는 말 = 서명·릴리스 도구의 기본 받는 OS."""
    sec = _posix()
    assert 'platforms=["win"]' in sec
    assert _assign(ROOT / "ops" / "tools" / "sign_release.py", "DEFAULT_PLATFORMS") == "win"
    assert re.search(r'\[string\[\]\]\$Platforms = @\("win"\)', _read(ROOT / "ops" / "release.ps1"))


def test_readme_autostart_claim_matches_installer():
    """리눅스 자동 시작(systemd)은 결정 전 — 설치 스크립트가 아직 없다고 말하는 동안만 README 도 그렇게 말한다(맥은 launchd 가 있다)."""
    installer_says_none = "자동 시작은 아직 없습니다" in _read(SCRIPTS["install.sh"])
    readme_says_none = re.search(r"자동 시작[^\n]*아직 없", _posix()) is not None
    assert installer_says_none == readme_says_none


def test_readme_install_locations_and_python_match_installer():
    sec = _posix()
    mac, linux = _home_defaults()
    for p in (mac, linux):
        assert f"{p}/app" in sec, f"설치 위치 {p}/app 이 README 에 없습니다"
    assert "CLEWPATH_HOME" in sec
    assert re.search(r'\[ -n "\$DIR" \] \|\| DIR="\$CLEWPATH_HOME/app"', _read(SCRIPTS["get.sh"])), "get.sh 기본 --dir"
    pyver = re.search(r"(?m)^  PYVER=(\d+\.\d+)$", _read(SCRIPTS["install.sh"]))
    assert pyver and f"{pyver.group(1)} 이상" in sec, "리눅스 시스템 파이썬 최소 버전이 install.sh 와 다릅니다"


def test_readme_posix_commands_exist_in_scripts():
    """README 가 보여 주는 명령의 옵션·하위 명령이 실제로 받는 것인지 — 오타 하나가 시험하는 사람을 막는다."""
    code = _code(_posix())
    cmds, stop_flags = _launcher()
    bad = []
    for script in ("install.sh", "uninstall.sh", "get.sh"):
        ok = _options(script)
        for args in _invocations(code, script):
            bad += [f"{script} {a}" for a in args if a.startswith("-") and a.split("=", 1)[0] not in ok]
    piped = _invocations([re.sub(r"get\.sh \| sh -s --", "get.sh", s) for s in code if "get.sh | sh -s --" in s], "get.sh")
    assert all(piped), "`curl … get.sh | sh -s -- 옵션` 을 읽지 못했습니다"
    bad += [f"get.sh {a}" for args in piped for a in args if a.startswith("-") and a.split("=", 1)[0] not in _options("get.sh")]
    for args in _invocations(code, "start-connector.sh"):
        if not args:
            continue
        sub, rest = args[0], args[1:]
        if sub not in cmds:
            bad.append(f"start-connector.sh {sub}")
        elif sub == "stop":
            bad += [f"start-connector.sh stop {a}" for a in rest if a not in stop_flags]
        elif sub == "watch":
            bad += [f"start-connector.sh watch {a}" for a in rest if not a.isdigit()]
        elif rest:
            bad.append(f"start-connector.sh {' '.join(args)}")
    assert not bad, f"스크립트가 받지 않는 인자: {bad}"


def test_readme_posix_documents_every_user_command():
    """사용자가 쓸 하위 명령·제거 옵션은 빠짐없이(새로 생기면 README 도). foreground 는 다음 단계 launchd·systemd 용."""
    code = _code(_posix())
    cmds, stop_flags = _launcher()
    calls = _invocations(code, "start-connector.sh")
    seen = {a[0] for a in calls if a} | ({"start"} if [] in calls else set())
    assert cmds - {"foreground"} <= seen, f"README 에 없는 런처 하위 명령: {sorted(cmds - {'foreground'} - seen)}"
    assert any(a[:1] == ["stop"] and set(a[1:]) == stop_flags for a in calls), "stop --hold 가 없습니다"
    used = {a for args in _invocations(code, "uninstall.sh") for a in args}
    want = _options("uninstall.sh") - {"--root", "-y", "-h", "--help"}   # --root 는 임시 사본으로 다시 실행할 때 스스로 쓴다
    assert want <= used, f"README 에 없는 제거 옵션: {sorted(want - used)}"


def test_readme_posix_env_and_paths_exist():
    code = _code(_posix())
    script_text = "".join(_read(p) for p in SCRIPTS.values())
    envs = {e for s in code for e in re.findall(r"\b(CLEWPATH_[A-Z_]+|XDG_DATA_HOME)\b", s)}
    assert envs and all(e in script_text for e in envs), f"스크립트에 없는 환경변수: {sorted(e for e in envs if e not in script_text)}"
    paths = {t.rstrip(".,)") for s in code for t in s.split() if REPO_PATH.match(t.rstrip(".,)"))}
    assert paths, "맥·리눅스 절이 가리키는 저장소 파일이 하나도 없습니다(명세·e2e 안내)"
    assert all((ROOT / p).exists() for p in paths), f"없는 파일: {sorted(p for p in paths if not (ROOT / p).exists())}"
    tests = re.findall(r"-m posix_e2e (tests/\S+\.py)", "\n".join(code))
    assert tests and all((ROOT / t).is_file() for t in tests), "컨테이너 왕복 시험 명령(-m posix_e2e)이 없습니다"


# ---------------------------------------------------------------- README: 나머지 절

def test_readme_update_payload_matches_updater():
    line = next((ln for ln in _read(README).splitlines() if "갈아끼우는 것" in ln), None)
    assert line, "README 자동 업데이트 절에 '갈아끼우는 것' 줄이 없습니다"
    listed = [w.rstrip("/") for w in re.search(r"`([^`]+)`", line).group(1).split()]
    assert listed == list(updater.PAYLOAD)


def test_readme_folder_tree_and_tests_list_posix_files():
    text = _read(README)
    tree = "\n".join(_code(_section(text, r"^폴더 구조")))
    for name in ("install.sh", "uninstall.sh", "get.sh"):
        assert name in tree, f"폴더 구조에 {name} 이 없습니다"
    assert all(SCRIPTS[n].is_file() for n in ("install.sh", "uninstall.sh", "get.sh"))
    tests = "\n".join(_code(_section(text, r"^테스트")))
    assert "-m posix_e2e tests/test_posix_e2e.py" in tests
    py = _read(ROOT / "pyproject.toml")
    assert "not posix_e2e" in py and "posix_e2e:" in py                   # 기본 실행에서 빠지고 마커가 등록돼 있다


# ---------------------------------------------------------------- multi-os.md

# 이 파일의 시험은 기본 pytest 실행에 들어가 release.ps1 의 게이트(마지막 줄에 failed 면 중단)를 지난다. 그래서 문서를 코드와
# 묶을 뿐, 진행 상태(할 일 체크·단계 상태 칸·고친 항목 지우기)에는 묶지 않는다 — 다음 할 일을 체크했다고 이후 Windows 릴리스가
# 전부 막히면 안 된다. 아래 *_survives_* 시험이 그런 편집을 사본에 해 보고 여전히 통과하는지 본다.

def _check_stage_table(md: str) -> None:
    rows = _table(_section(md, r"^단계"))
    head, body = rows[0], {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert "상태" in head and {"1", "2", "3a", "3b"} <= set(body), f"단계 표: {list(body)}"
    for sid in ("1", "2", "3a"):                       # 끝난 단계는 계속 '완료'(합침·릴리스 기록이 붙어도)
        assert "완료" in body[sid]["상태"], f"{sid} 상태: {body[sid]['상태']}"
    a = body["3a"]
    whole = " ".join(a.values())
    assert "posix-install.md" in whole
    assert 'platforms=["win"]' in whole               # 3a 가 내보낸 릴리스 모양(그 단계의 기록)
    for word, path in (("get.sh", "get.sh"), ("install.sh", "install.sh"), ("uninstall.sh", "uninstall.sh"),
                       ("start-connector.sh", "start-connector.sh"), ("update_runner.sh", "update_runner.sh"),
                       ("hostctl", "hostctl.py")):
        assert word in whole and SCRIPTS[path].is_file(), f"3a 행의 {word}"
    b = body["3b"]
    assert b["상태"].strip(), "3b 상태 칸이 비었습니다"     # 값은 진행에 따라 바뀐다(대기 → 진행 중 → 완료)
    assert all(w in " ".join(b.values()) for w in ("launchd", "키체인", "/get.sh"))


def _set_status(md: str, sid: str, value: str) -> str:
    """단계 표에서 그 단계 행의 마지막 칸(상태)을 바꾼 사본."""
    out = []
    for line in md.splitlines(keepends=True):
        if line.startswith(f"| {sid} |"):
            cells = line.rstrip("\n").split("|")
            cells[-2] = f" {value} "
            line = "|".join(cells) + "\n"
        out.append(line)
    assert out != md.splitlines(keepends=True), f"{sid} 행이 없습니다"
    return "".join(out)


def test_multi_os_stage_table_reflects_posix_install():
    _check_stage_table(_read(MULTI_OS))


@pytest.mark.parametrize("sid,value", [
    ("3a", "완료 — main 합침 0.29.0(2026-10-22)"),     # 합칠 때 브랜치 이름이 빠진 상태(2단계가 그랬다)
    ("3b", "진행 중"),
    ("3b", "완료 — 0.31.0"),
])
def test_stage_table_check_survives_progress(sid, value):
    _check_stage_table(_set_status(_read(MULTI_OS), sid, value))


def test_stage_table_check_still_catches_drift():
    with pytest.raises(AssertionError):
        _check_stage_table(_set_status(_read(MULTI_OS), "3a", "대기"))          # 끝난 단계가 되돌아감
    with pytest.raises(AssertionError):
        _check_stage_table(_read(MULTI_OS).replace("posix-install.md", "posix-setup.md"))


def test_multi_os_install_table_matches_scripts():
    text = _read(MULTI_OS)
    sec = _section(text, r"^2\. 설치")
    mac, linux = _home_defaults()
    assert mac in sec and linux in sec
    plats = _assign(ROOT / "ops" / "tools" / "sign_release.py", "PLATFORMS")
    line = next((ln for ln in sec.splitlines() if "platforms" in ln and all(f"`{p}`" in ln for p in plats)), None)
    assert line, f"platforms 값({', '.join(plats)})을 밝힌 줄이 없습니다"


# ---------------------------------------------------------------- TODOS.md

def _check_todos_progress(todos: str) -> None:
    sec = _section(todos, r"멀티 OS")
    a = _section(sec, r"^3a ")
    assert "posix-install.md" in a
    done = set()
    for m in re.finditer(r"(?m)^- \[x\] (\d)(?:·(\d))?단계", a):
        done.update(int(g) for g in m.groups() if g)
    assert done == set(range(1, 10)), f"3a 단계 1~9 중 완료 표시: {sorted(done)}"
    assert re.search(r"(?m)^- \[[ x]\] [^\n]*main 합침", a), "main 합침·릴리스 항목이 없습니다"
    b = _section(sec, r"^3b ")
    assert re.search(r"(?m)^- \[[ x]\] [^\n]*launchd", b) and re.search(r"(?m)^- \[[ x]\] [^\n]*/get\.sh", b)


def test_todos_multi_os_progress():
    _check_todos_progress(_read(TODOS))


@pytest.mark.parametrize("sub,item", [("3a", r"main 합침"), ("3b", r"launchd"), ("3b", r"/get\.sh")])
def test_todos_progress_check_survives_checking_items_off(sub, item):
    todos = _read(TODOS)
    part = _section(_section(todos, r"멀티 OS"), rf"^{sub} ")
    line = next(ln for ln in part.splitlines() if re.match(rf"- \[ \] .*{item}", ln))
    _check_todos_progress(todos.replace(line, line.replace("- [ ]", "- [x]", 1), 1))


def _check_r15(spec: str, todos: str) -> None:
    """명세 R15(Windows 기존 결함 ①~⑦)를 TODOS 가 별건으로 들고 있다 — 고쳐서 체크한 항목·지운 항목·통째로 지운 절은
    괜찮다(진행). 명세에 없는 번호·순서 뒤바뀜, 남은 항목의 시작점 파일이 없는 것만 잡는다."""
    row = next(ln for ln in spec.splitlines() if ln.startswith("| R15 |"))
    marks = re.findall(r"[①-⑳]", row)
    assert len(marks) == 7, f"명세 R15 의 항목 수가 바뀌었습니다: {marks}"
    try:
        sec = _section(todos, r"R15")
    except AssertionError:
        return                                         # 다 고쳐 절을 지웠다
    items = re.findall(r"(?m)^- \[([ x])\] ([①-⑳])(.*)$", sec)
    got = [m for _, m, _ in items]
    assert got == [m for m in marks if m in got] and len(set(got)) == len(got), \
        f"TODOS 의 Windows 기존 결함 항목이 명세 R15 ①~⑦ 과 다릅니다: {got}"
    missing = []
    for box, mark, rest in items:
        if box == "x":
            continue                                   # 고친 것 — 시작점 파일이 그 뒤 바뀌었을 수 있다
        start = re.search(r"시작점: (.*)", rest)
        assert start, f"{mark} 에 시작점이 없습니다"
        paths = [p.split(":", 1)[0] for p in re.findall(r"`([^`]+)`", start.group(1))]
        assert paths, f"{mark} 의 시작점에 파일이 없습니다"
        missing += [f"{mark} {p}" for p in paths if not p.startswith("007/") and not (ROOT / p).exists()]
    assert not missing, f"없는 시작점 파일: {missing}"


def test_todos_lists_windows_defects_from_spec_r15():
    _check_r15(_read(SPEC), _read(TODOS))


def _r15_lines(todos: str) -> list[str]:
    """R15 절의 항목 줄(다른 절에도 ①… 목록이 있다)."""
    return [ln for ln in _section(todos, r"R15").splitlines() if re.match(r"- \[[ x]\] [①-⑦]", ln)]


def test_r15_check_survives_fixing_and_removing_items():
    spec, todos = _read(SPEC), _read(TODOS)
    one = _r15_lines(todos)[0]
    fixed = one.replace("- [ ]", "- [x]", 1).replace("session_manager/updater.py", "session_manager/gone.py")
    _check_r15(spec, todos.replace(one, fixed))                       # 고쳐서 체크(그 뒤 파일이 옮겨 감)
    _check_r15(spec, todos.replace(_r15_lines(todos)[2] + "\n", ""))   # 고친 항목을 지움
    head = next(ln for ln in todos.splitlines() if ln.startswith("## ") and "R15" in ln)
    start = todos.index(head)
    end = todos.index("\n## ", start + 1)
    _check_r15(spec, todos[:start] + todos[end + 1:])                 # 다 고쳐 절째 지움


def test_r15_check_still_catches_drift():
    spec, todos = _read(SPEC), _read(TODOS)
    lines = _r15_lines(todos)
    with pytest.raises(AssertionError):                               # 명세에 없는 번호
        _check_r15(spec, todos.replace(lines[-1], lines[-1] + "\n- [ ] ⑧ 새 결함 — 시작점: `install.ps1`"))
    with pytest.raises(AssertionError):                               # 순서 뒤바뀜
        _check_r15(spec, todos.replace(lines[0], "@@").replace(lines[1], lines[0]).replace("@@", lines[1]))
    with pytest.raises(AssertionError):                               # 남은 항목의 시작점 파일이 없다
        _check_r15(spec, todos.replace("session_manager/liveness.py", "session_manager/nowhere.py"))


@pytest.mark.parametrize("doc", [README, TODOS, MULTI_OS], ids=lambda p: p.name)
def test_docs_are_plain_utf8_lf(doc):
    raw = doc.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
