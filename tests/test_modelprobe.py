"""claude /model 화면 읽기(2026-10-12 사장님 승인): 화면 해석기·이름→id·신뢰 폴더 고르기·안전한 키 입력."""
import json
import threading
import time

import pytest

from session_manager import modelprobe as mp

ESC = "\x1b"


def test_tests_never_spawn_the_real_claude():
    """conftest 가드: spawn 주입이 없는 화면 읽기는 시험 중에 거절된다(진짜 claude 가 진짜 ~/.claude 에 쓰던 사고)."""
    with pytest.raises(RuntimeError, match="진짜 claude"):
        mp.probe("claude", ".")


def _row(n, text, sel=False):
    # claude 출력처럼: 줄 이동 + 칸 위치(\x1b[NG)로 단어마다 찍기
    words = text.split(" ")
    out, col = "", 10
    for w in words:
        out += f"{ESC}[{col}G{w}"
        col += len(w) + 1
    return f"\n{ESC}[3C{ESC}[1B" + ("❯" if sel else " ") + f"{ESC}[6G{n}." + out


def picker(rows, more=0, top=1):
    s = f"{ESC}[2J{ESC}[H\n{ESC}[3C{ESC}[1BSelect{ESC}[11Gmodel"
    s += f"\n{ESC}[5C{ESC}[2B1.{ESC}[10GDefault (recommended) Opus 5.5 · Best"
    for i, t in enumerate(rows):
        s += _row(top + 1 + i, t, sel=(i == 0))
    if more:
        s += f"\n{ESC}[4C{ESC}[1B  … +{more} models{ESC}[K"
    return s


def test_screen_and_parse():
    scr = mp.Screen(40, 160)
    scr.feed(picker(["Opus 5.5 ✔ For complex work", "Fable 5.1 For your toughest", "Haiku 4.5 Fastest",
                     "Opus 5.5 (1M context) For long"], more=2))
    got = mp.parse_models(scr.text())
    assert [v["label"] for _, v in sorted(got.items())] == ["Opus 5.5", "Fable 5.1", "Haiku 4.5", "Opus 5.5 (1M)"]
    assert 1 not in got, "Default(추천) 줄은 별칭이라 뺀다"
    # 조각 출력(ESC 가 청크 경계에서 잘림)도 같은 결과
    scr2 = mp.Screen(40, 160)
    data = picker(["Sonnet 5 Efficient"])
    for i in range(0, len(data), 7):
        scr2.feed(data[i:i + 7])
    assert [v["label"] for v in mp.parse_models(scr2.text()).values()] == ["Sonnet 5"]
    assert mp.parse_models("1. Opus 5.5") == {}, "고르기 화면이 아니면 읽지 않는다"
    # 한글(2칸) 경로가 있어도 칸이 밀리지 않는다
    scr3 = mp.Screen(5, 40)
    scr3.feed("F:\\기술스택\r\nok")
    assert scr3.text().splitlines()[0] == "F:\\기술스택" and scr3.text().splitlines()[1] == "ok"


def test_to_id():
    it = lambda f, v, m=False: {"family": f, "ver": v, "one_m": m}
    assert mp.to_id(it("Opus", "5.5")) == "claude-opus-5-5"
    assert mp.to_id(it("Sonnet", "5")) == "claude-sonnet-5"
    assert mp.to_id(it("Haiku", "4.5"), {"Haiku 4.5": "claude-haiku-4-5-20251001"}) == "claude-haiku-4-5-20251001"
    assert mp.to_id(it("Fable", "5.1", True)) == "claude-fable-5-1[1m]"


def test_pick_cwd(tmp_path, monkeypatch):
    a = tmp_path / "plain"; a.mkdir()
    b = tmp_path / "b"; b.mkdir(); (b / ".mcp.json").write_text("{}")
    c = tmp_path / "c"; (c / ".claude").mkdir(parents=True); (c / ".claude" / "settings.json").write_text("{}")
    d = tmp_path / "zzzz-longer"; d.mkdir()
    home = tmp_path / "home"; home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {
        str(b): {"hasTrustDialogAccepted": True}, str(c): {"hasTrustDialogAccepted": True},
        str(d): {"hasTrustDialogAccepted": True}, str(a): {"hasTrustDialogAccepted": True},
        str(tmp_path / "gone"): {"hasTrustDialogAccepted": True}, str(home): {"hasTrustDialogAccepted": False}}}))
    monkeypatch.setattr(mp.Path, "home", lambda: home)
    assert mp.pick_cwd() == str(a), "MCP·프로젝트 훅 없는 신뢰 폴더, 짧은 경로 우선"


class FakeClaude:
    """키 입력에 반응해 화면을 내보내는 가짜 claude."""

    def __init__(self, start, list_pages):
        self.writes = []
        self.out = [start]
        self.pages = list_pages
        self.page = 0
        self.ev = threading.Event()
        self.ev.set()
        self.alive = True
        self.typed = ""

    def read(self, n):
        while self.alive:
            if self.out:
                return self.out.pop(0)
            time.sleep(0.02)
        raise EOFError

    def write(self, s):
        self.writes.append(s)
        if s == "/model":
            self.typed = s
            self.out.append(f"\n{ESC}[2C{ESC}[30B❯ /model")
        elif s == "\r" and self.typed == "/model":
            self.typed = ""
            self.out.append(self.pages[0])
        elif s == f"{ESC}[B":
            self.page = min(self.page + 1, len(self.pages) - 1)
            self.out.append(self.pages[self.page])

    def terminate(self, force=False):
        self.alive = False


START = f"{ESC}[2J{ESC}[H Claude Code v2.1.296\n Opus 5.5 · Claude Max\n❯ Try something"


def _run(fake, **kw):
    return mp.probe("claude.exe", "E:/t", timeout=3, spawn=lambda argv, cwd, env, r, c: fake, **kw)


def test_probe_reads_all_pages_and_never_selects():
    p1 = picker(["Opus 5.5 x", "Fable 5.1 y", "Sonnet 5.5 z"], more=2)
    p2 = picker(["Fable 5.1 y", "Sonnet 5.5 z", "Opus 4.7 old", "Sonnet 4.6 old"], top=2)
    fake = FakeClaude(START, [p1, p2])
    r = _run(fake)
    assert r["ok"] and [m["label"] for m in r["models"]] == ["Opus 5.5", "Fable 5.1", "Sonnet 5.5", "Opus 4.7", "Sonnet 4.6"]
    i_open = fake.writes.index("\r")
    assert fake.writes.count("\r") == 1, "Enter 는 /model 을 여는 한 번뿐"
    assert set(fake.writes[i_open + 1:]) <= {f"{ESC}[B", ESC}, "고르기 화면에선 아래 화살표·Esc 만"
    assert fake.writes[-1] == ESC and not fake.alive
    assert len(r["session_id"]) == 36


def test_probe_aborts_on_trust_dialog():
    fake = FakeClaude(" Quick safety check: Is this a project you trust?\n ❯ No, exit\n Yes, I trust this folder", [""])
    r = _run(fake)
    assert not r["ok"] and r["error"] == "unexpected_dialog"
    assert "\r" not in fake.writes and "/model" not in fake.writes, "질문 화면에선 아무것도 입력하지 않는다"
    assert not fake.alive


def test_probe_failure_keeps_screen_tail_and_waits_60s():
    """시작 화면이 안 뜨면 start_timeout + 닫기 전 화면 꼬리(빈 줄 빼고, 이메일 가림). 기다리는 시간 기본은 60초(2026-10-29)."""
    import inspect
    fake = FakeClaude(f"{ESC}[2J{ESC}[H Loading…\n\n signed in as boss.kim@example.co.kr\n Checking for updates", [""])
    r = mp.probe("claude.exe", "E:/t", timeout=1, spawn=lambda argv, cwd, env, rr, c: fake)
    assert not r["ok"] and r["error"] == "start_timeout"
    assert [x.strip() for x in r["screen"]] == ["Loading…", "signed in as <이메일>", "Checking for updates"]
    assert "/model" not in fake.writes and "\r" not in fake.writes and not fake.alive
    assert inspect.signature(mp.probe).parameters["timeout"].default == 60.0
    assert len(mp.screen_tail("\n".join(f"줄{i}" for i in range(30)))) == mp.SCREEN_TAIL_LINES
    ok = _run(FakeClaude(START, [picker(["Opus 5.5 x"])]))
    assert ok["ok"] and "screen" not in ok, "성공하면 화면을 남기지 않는다"


def test_probe_reads_with_plain_gt_prompt():
    """claude 2.1.312 는 입력칸을 `>\\xa0` 로 그린다(2026-10-29 실패 화면 꼬리 — ❯ 만 찾다가 60초 기다리고 start_timeout)."""
    class Gt(FakeClaude):
        def write(self, s):
            if s == "/model":
                self.writes.append(s)
                self.typed = s
                self.out.append(f"\n{ESC}[30B>\xa0/model")
            else:
                super().write(s)
    start = (f"{ESC}[2J{ESC}[H ▐▛███▛█   Claude Code v2.1.312\n▝▜██████▀  Opus 5.5 with high effort · Claude Max\n"
             f"{'─' * 40}\n>\xa0Try \"how do I log an error?\"\n{'─' * 40}\n  ⏵⏵ auto mode on (shift+tab to cycle)")
    fake = Gt(start, [picker(["Opus 5.5 x", "Sonnet 5.5 z"])])
    r = _run(fake)
    assert r["ok"] and [m["label"] for m in r["models"]] == ["Opus 5.5", "Sonnet 5.5"]
    assert fake.writes.count("\r") == 1
    assert mp._PROMPT_RE.search("❯ Try something") and not mp._PROMPT_RE.search("Opus 5.5 > Sonnet")


def test_probe_no_echo_no_enter():
    class Mute(FakeClaude):
        def write(self, s):
            self.writes.append(s)      # 입력이 화면에 안 보임(다른 상태) → Enter 금지
    fake = Mute(START, [""])
    r = _run(fake)
    assert r["error"] == "no_input_echo" and "\r" not in fake.writes
