"""편집 범위 감시(브리지 '🚨 경계 이탈' 대체, 2026-10-05): write_scope 밖 Write/Edit·파괴적 Bash → 위반·알림(1시간 묶음)."""
from __future__ import annotations

import datetime
import json

import pytest

from session_manager import team, teamlog

ROOT = r"E:\019_KoreaPedia"


def _ts(sec):
    return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def tool(tid, name, inp, t=1000, cwd=ROOT):
    return {"type": "assistant", "timestamp": _ts(t), "cwd": cwd, "message": {"id": "m" + tid, "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


@pytest.fixture
def env(fake_claude_home, monkeypatch):
    proj = fake_claude_home / "projects" / "E--019"
    proj.mkdir(parents=True)
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: sid)
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    from session_manager import peers, push
    monkeypatch.setattr(peers, "snapshot", lambda *a, **k: [])
    sent = []
    monkeypatch.setattr(push, "send", lambda *a, **k: sent.append(a) or 1)
    t = team.create_team("KoreaPedia", code="KP", root=ROOT, manager_session="s-pm")
    team.add_member(t["id"], "논문", session_id="s-tr", write_scope=["treatise/", "00_PM/comm/"])
    team.add_member(t["id"], "자유", session_id="s-free")                       # 범위 없음 → 검사 안 함
    import sqlite3
    c = sqlite3.connect(str(team.db_path())); c.execute("UPDATE agent_sessions SET started=0"); c.commit(); c.close()

    def write(sid, recs):
        with (proj / f"{sid}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"write": write, "sent": sent, "team": t}


def _viol():
    import sqlite3
    c = sqlite3.connect(str(team.db_path()))
    return [json.loads(p) for (p,) in c.execute("SELECT payload_json FROM events WHERE kind='scope_violation' ORDER BY id")]


def test_write_edit_and_bash_rules_match_bridge(env):
    env["write"]("s-tr", [
        tool("a", "Write", {"file_path": r"E:\019_KoreaPedia\treatise\x.md"}),                 # 안
        tool("b", "Edit", {"file_path": "treatise/y.md"}),                                     # 상대 경로 안
        tool("c", "Write", {"file_path": r"E:\019_KoreaPedia\00_PM\comm\reply.md"}),            # 회신 폴더 허용
        tool("d", "Write", {"file_path": r"E:\019_KoreaPedia\KoreaEnc\steal.md"}),              # 밖 ✗
        tool("e", "Bash", {"command": r"cat E:\019_KoreaPedia\KoreaEnc\a.md"}),                # 읽기는 통과
        tool("f", "Bash", {"command": r"cd E:\x && rm -rf E:\019_KoreaPedia\Samhung\old"}),    # 파괴적 밖 ✗
        tool("g", "Bash", {"command": r"echo hi > E:\019_KoreaPedia\treatise\log.txt"}),       # 리다이렉트 안
        tool("h", "Bash", {"command": r"echo hi >> E:\tmp\out.txt"}),                          # 리다이렉트 밖 ✗
        tool("i", "Write", {"file_path": r"C:\Users\me\.gstack\projects\x\design.md"}),         # gstack 허용(경로는 홈 기준이라 아래에서 별도)
    ])
    teamlog.ingest_all()
    bad = [(v["tool"], v["path"]) for v in _viol()]
    assert ("Write", r"E:\019_KoreaPedia\KoreaEnc\steal.md") in bad
    assert ("Bash", r"E:\019_KoreaPedia\Samhung\old") in bad
    assert ("Bash", r"E:\tmp\out.txt") in bad
    assert not any("treatise" in p or "00_PM" in p for _, p in bad)
    assert all(v["scope"] == "treatise/, 00_PM/comm/" for v in _viol())


def test_always_allowed_and_no_scope_member(env):
    import os
    home = os.path.expanduser("~")
    env["write"]("s-tr", [tool("a", "Write", {"file_path": os.path.join(home, ".gstack", "x.md")}),
                          tool("b", "Write", {"file_path": os.path.join(os.environ.get("LOCALAPPDATA", home), "Temp", "claude", "s.py")})])
    env["write"]("s-free", [tool("c", "Write", {"file_path": r"D:\anywhere\x.md"})])
    teamlog.ingest_all()
    assert _viol() == [], "메모리·스크래치·gstack 은 늘 허용, 범위 없는 구성원은 검사 안 함"


def test_task_scope_adds_and_glob_suffix(env):
    k = team.create_task("KP", "문서", "논문", write_scope={"논문": ["Research/**"]})
    body = f"결론: x\n[cw] task={k['id']} type=ASK"
    env["write"]("s-tr", [{"type": "user", "uuid": "u1", "timestamp": _ts(900), "message": {"role": "user", "content":
                           f'Another Claude session sent a message:\n<cross-session-message from="uds:p" from-name="pm" from-mode="bypass">{body}</cross-session-message>'}},
                          tool("a", "Write", {"file_path": r"E:\019_KoreaPedia\Research\r.md"}, t=1001)])
    teamlog.ingest_all()
    assert _viol() == [], "일감의 write_scope(글롭 접미 **)도 허용 범위"


def test_violations_grouped_hourly_and_pushed_once(env):
    env["write"]("s-tr", [tool(str(i), "Write", {"file_path": rf"E:\019_KoreaPedia\KoreaEnc\{i}.md"}, t=1000 + i) for i in range(5)]
                 + [tool("late", "Write", {"file_path": r"E:\019_KoreaPedia\KoreaEnc\late.md"}, t=1000 + 4000)])
    teamlog.ingest_all()
    assert len(_viol()) == 6, "이벤트는 전부 남는다"
    teamlog.judge("KP", now=10 ** 9)
    v = [x for x in teamlog.violations("KP") if x["rule"] == "out_of_scope"]
    assert len(v) == 2, "같은 시간대는 한 묶음"
    teamlog.notify_violations("KP")
    pushes = [a for a in env["sent"] if str(a[0]).startswith("team-violation:v:scope:")]
    assert len(pushes) == 2 and "편집 범위 이탈" in pushes[0][2] and "KoreaEnc" in pushes[0][3]
