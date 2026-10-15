"""claude `/model` 고르기 화면을 숨은 터미널에서 읽어 이 계정이 고를 수 있는 모델 목록을 모은다.

2026-10-12 사장님 승인(불가침 원칙 2 협의 — 아래 부작용을 고지하고 승인받음):
- claude 를 한 번 띄운다 → 빈 세션 기록(jsonl)이 하나 생기고(미리 정한 --session-id, ClewPath 목록에서 숨김),
  사용자 훅(SessionStart 등)이 한 번 돈다. claude 가 자기 설정 파일에 기동 횟수 등을 쓰는 것은 claude 자신의 동작.
- 고르기 화면에서는 **Enter 를 누르지 않는다**(누르면 '새 세션 기본 모델' 이 바뀜). 화면을 여는 Enter 는
  입력칸에 `/model` 만 있는 것을 확인한 뒤 한 번, 그 뒤엔 아래 화살표(이동만)·Esc 만 보낸다.
- 시작 화면이 평소와 다르면(폴더 신뢰 질문·로그인·기타 질문) 아무것도 고르지 않고 Esc 로 끝낸다.
- 신뢰된 폴더 중 .mcp.json·프로젝트 훅 설정이 없는 곳에서 띄운다(프로젝트 MCP 서버를 깨우지 않게).
claude 파일은 읽기만 한다(~/.claude.json 의 폴더 신뢰 표시만).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

FAMILIES = ("Opus", "Sonnet", "Haiku", "Fable")
_NAME_RE = re.compile(r"\b(" + "|".join(FAMILIES) + r")\s+(\d+(?:\.\d+)?)(\s*\(1M context\))?")
_ROW_RE = re.compile(r"^\s*(?:[❯↓↑>]\s*)?(\d{1,2})\.\s+(.*)$")


# ---------------------------------------------------------------- 작은 화면 해석기(VT 일부)
class Screen:
    """claude TUI 가 쓰는 커서 이동·지우기만 해석해 지금 보이는 글자판을 만든다(바뀐 칸만 다시 그리는 출력 대응)."""

    def __init__(self, rows: int = 40, cols: int = 160):
        self.rows, self.cols = rows, cols
        self.g = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.saved = (0, 0)
        self._pend = ""

    def _put(self, ch: str) -> None:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if self.c + w > self.cols:
            self.c = 0
            self._lf()
        if 0 <= self.r < self.rows:
            self.g[self.r][self.c] = ch
            if w == 2 and self.c + 1 < self.cols:
                self.g[self.r][self.c + 1] = ""
        self.c += w

    def _lf(self) -> None:
        self.r += 1
        if self.r >= self.rows:
            self.g.pop(0)
            self.g.append([" "] * self.cols)
            self.r = self.rows - 1

    def _clamp(self) -> None:
        self.r = max(0, min(self.rows - 1, self.r))
        self.c = max(0, min(self.cols - 1, self.c))

    def feed(self, data: str) -> None:
        s = self._pend + data
        self._pend = ""
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == "\x1b":
                if i + 1 >= n:
                    self._pend = s[i:]
                    return
                nx = s[i + 1]
                if nx == "[":
                    m = re.match(r"\x1b\[([0-9;?<>=]*)([ -/]*)([@-~])", s[i:])
                    if not m:
                        self._pend = s[i:]
                        return
                    self._csi(m.group(1), m.group(3))
                    i += m.end()
                    continue
                if nx == "]":                       # OSC … BEL | ST
                    j = s.find("\x07", i)
                    k = s.find("\x1b\\", i)
                    ends = [x for x in (j, k) if x != -1]
                    if not ends:
                        self._pend = s[i:]
                        return
                    e = min(ends)
                    i = e + (1 if e == j else 2)
                    continue
                if nx == "7":
                    self.saved = (self.r, self.c)
                elif nx == "8":
                    self.r, self.c = self.saved
                i += 2 if nx not in "()" else 3
                continue
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                self.c = 0                          # ConPTY 출력은 줄바꿈 때 맨 앞으로 간다(실측)
                self._lf()
            elif ch == "\b":
                self.c = max(0, self.c - 1)
            elif ch >= " ":
                self._put(ch)
            i += 1

    def _csi(self, params: str, final: str) -> None:
        if params.startswith(("?", ">", "<", "=")):
            return
        nums = [int(x) if x.isdigit() else 0 for x in params.split(";")] if params else []
        a = (nums[0] if nums and nums[0] else 1)
        if final in "Hf":
            self.r = (nums[0] if nums and nums[0] else 1) - 1
            self.c = (nums[1] if len(nums) > 1 and nums[1] else 1) - 1
        elif final == "A":
            self.r -= a
        elif final == "B":
            self.r += a
        elif final == "C":
            self.c += a
        elif final == "D":
            self.c -= a
        elif final == "G":
            self.c = a - 1
        elif final == "K":
            mode = nums[0] if nums else 0
            row = self.g[self.r] if 0 <= self.r < self.rows else None
            if row is not None:
                rng = range(self.c, self.cols) if mode == 0 else range(0, self.c + 1) if mode == 1 else range(self.cols)
                for x in rng:
                    row[x] = " "
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode in (2, 3):
                self.g = [[" "] * self.cols for _ in range(self.rows)]
        self._clamp()

    def text(self) -> str:
        return "\n".join("".join(r).rstrip() for r in self.g)


# ---------------------------------------------------------------- 읽기
def parse_models(screen_text: str) -> dict[int, dict]:
    """고르기 화면 글자판 → {번호: {label, one_m}} ('Default (recommended)' 줄은 대표 별칭이라 뺀다)."""
    out: dict[int, dict] = {}
    if "Select model" not in screen_text:
        return out
    for line in screen_text.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        if rest.lstrip().startswith("Default"):
            continue
        nm = _NAME_RE.search(rest)
        if not nm or nm.start() > 4:
            continue
        label = f"{nm.group(1)} {nm.group(2)}"
        out[int(m.group(1))] = {"label": label + (" (1M)" if nm.group(3) else ""), "one_m": bool(nm.group(3)),
                                "family": nm.group(1), "ver": nm.group(2)}
    return out


def to_id(item: dict, known: dict[str, str] | None = None) -> str:
    """화면 이름 → 모델 id. 같은 이름의 알려진 id(운영측·실사용)가 있으면 그것(예: Haiku 4.5 → …-20251001)."""
    base_label = f"{item['family']} {item['ver']}"
    mid = (known or {}).get(base_label) or ("claude-" + item["family"].lower() + "-" + item["ver"].replace(".", "-"))
    return mid + ("[1m]" if item.get("one_m") else "")


# ---------------------------------------------------------------- 띄울 폴더
def pick_cwd() -> str | None:
    """claude 가 이미 신뢰한 폴더 중 프로젝트 MCP·훅 설정이 없는 곳(짧은 경로 우선). 없으면 None."""
    try:
        d = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    cands = []
    for k, v in (d.get("projects") or {}).items():
        if not isinstance(v, dict) or not v.get("hasTrustDialogAccepted"):
            continue
        p = Path(k)
        try:
            if not p.is_dir():
                continue
            if (p / ".mcp.json").exists() or (p / ".claude" / "settings.json").exists() \
                    or (p / ".claude" / "settings.local.json").exists():
                continue
        except OSError:
            continue
        cands.append(str(p))
    cands.sort(key=len)
    return cands[0] if cands else None


# ---------------------------------------------------------------- 실행
_BAD_START = ("trust this folder", "Select login method", "Do you want to use this API key", "Press Enter to continue",
              "Choose the text style")


def probe(exe: str, cwd: str, timeout: float = 30.0, spawn=None) -> dict:
    """숨은 터미널에서 /model 화면을 읽는다 → {ok, models:[{label,one_m,…}], session_id, error?}.
    spawn(argv, cwd, env, rows, cols) 는 테스트용 주입(기본 osdep.spawn_pty)."""
    sid = str(uuid.uuid4())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "SM_"))}
    rows, cols = 40, 160
    if spawn is None:
        from session_manager import osdep

        def spawn(argv, cwd, env, rows, cols):   # noqa: F811
            return osdep.spawn_pty(argv, cwd, rows, cols, env)
    scr = Screen(rows, cols)
    lock = threading.Lock()
    proc = spawn([exe, "--session-id", sid], cwd, env, rows, cols)
    stop = threading.Event()

    def rd():
        while not stop.is_set():
            try:
                data = proc.read(65536)
            except Exception:  # noqa: BLE001
                break
            if data:
                with lock:
                    scr.feed(data)

    threading.Thread(target=rd, name="model-probe-read", daemon=True).start()

    def screen() -> str:
        with lock:
            return scr.text()

    def wait_for(pred, secs) -> bool:
        t0 = time.time()
        while time.time() - t0 < secs:
            if pred(screen()):
                return True
            time.sleep(0.25)
        return False

    res: dict = {"ok": False, "models": [], "session_id": sid}
    try:
        # 1) 시작 화면: 배너 + 입력칸. 다른 질문이 떠 있으면 아무것도 고르지 않고 끝낸다.
        started = wait_for(lambda t: "Claude Code v" in t and "❯" in t or any(b in t for b in _BAD_START),
                           timeout)
        t = screen()
        if any(b in t for b in _BAD_START):
            res["error"] = "unexpected_dialog"
            return res
        if not started:
            res["error"] = "start_timeout"
            return res
        time.sleep(1.5)                                # 기동 직후 알림 줄이 다 그려지게
        if any(b in screen() for b in _BAD_START):
            res["error"] = "unexpected_dialog"
            return res
        # 2) 입력칸에 /model 만 있는지 확인한 뒤에만 Enter 1번
        proc.write("/model")
        if not wait_for(lambda t: re.search(r"❯\s*/model\b", t) is not None, 5):
            res["error"] = "no_input_echo"
            return res
        proc.write("\r")
        if not wait_for(lambda t: "Select model" in t, 10):
            res["error"] = "no_picker"
            return res
        time.sleep(0.6)
        found: dict[int, dict] = {}
        idle = 0
        for _ in range(30):                            # 아래 화살표 = 고르기 이동만(선택 아님)
            got = parse_models(screen())
            new = {k: v for k, v in got.items() if k not in found}
            found.update(got)
            idle = 0 if new else idle + 1
            if idle >= 4 and "+" not in "".join(x for x in screen().splitlines() if "models" in x and "…" in x):
                break
            proc.write("\x1b[B")
            time.sleep(0.35)
        res["models"] = [found[k] for k in sorted(found)]
        res["ok"] = bool(res["models"])
        if not res["ok"]:
            res["error"] = "parse_failed"
        return res
    finally:
        try:
            proc.write("\x1b")                          # 고르기 화면 닫기(선택 없이)
            time.sleep(0.5)
            proc.write("\x1b")
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        stop.set()
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass


def claude_version(exe: str) -> str | None:
    import subprocess
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        v = (r.stdout or "").strip().split()
        return v[0] if v else None
    except Exception:  # noqa: BLE001
        return None
