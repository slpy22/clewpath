"""ClewPath 터미널에 지금 떠 있는 선택지(권한 확인·질문) 읽기 — 2026-11-04 사장님.

왜: 🔔 할 일의 원격 승인 창이 '허용 · 이 세션에서 계속 허용 · 거부' 고정 버튼으로 1·2·Esc 를 그대로 보냈다.
claude 의 선택지는 도구·상황마다 다르다(예: 파일 쓰기 = 1. Yes / 2. Yes, and switch to accept edits … / 3. No —
2026-11-04 실측). 그래서 화면과 다른 버튼을 누르면 무엇이 골라질지 알 수 없었다. 이제 터미널 출력(이미 Host 가 버퍼에 들고
있는 것)을 작은 화면 해석기(modelprobe.Screen)로 그려 **지금 보이는 선택지 그대로** 돌려주고, 고를 때는 다시 읽어 같은
선택지일 때만 그 번호를 보낸다.

선택 창 판정: claude 는 선택 창의 고른 줄 앞에 ❯ 를, 선택 창이 없을 때는 맨 아래 입력칸 앞에 ❯ 를 그린다. 그래서 **맨 아래의 ❯ 가
번호 줄(❯ 1. Yes)에 있을 때만** 선택 창이 떠 있다고 본다(대답 뒤엔 입력칸 ❯ 가 맨 아래로 온다 — 실측).
원본(claude 파일·설정)은 읽지도 바꾸지도 않는다 — Host 가 띄운 PTY 의 출력만 본다.
"""
from __future__ import annotations

import re
import threading

from session_manager.modelprobe import Screen

# 번호 줄: (❯) 1. Yes — 화면 해석이 점을 놓치는 일이 있어(실측 '2  Yes') 점은 있어도 없어도 된다
_ROW = re.compile(r"^(\s*)(❯\s*)?(\d{1,2})(?:[.)]\s*|\s{2,})(\S.*?)\s*$")
_BOX = "│┃║╭╮╰╯"
_RULE = re.compile(r"^[\s─━═╌┄┈╍┅-]*$")
_MULTI = re.compile(r"^\[[ xX✓✔]\]|^[☐☑☒✓✔]")
_DEFAULT_SIZE = (60, 200)        # 크기를 모를 때(Host 재기동 뒤 화면이 아직 안 붙음) — 넉넉히


def _clean(line: str) -> str:
    s = line.rstrip()
    for ch in _BOX:
        s = s.replace(ch, " ")
    return s.rstrip()


def render(text: str, size: tuple[int, int] | None) -> str:
    rows, cols = size or _DEFAULT_SIZE
    sc = Screen(max(5, int(rows)), max(20, int(cols)))
    sc.feed(text or "")
    return sc.text()


def parse(screen_text: str) -> dict | None:
    """화면 글자판 → 선택 창 {question, context[], choices[{n,label,more,selected}], footer, multi} | None."""
    lines = [_clean(x) for x in (screen_text or "").splitlines()]
    k = max((i for i, x in enumerate(lines) if "❯" in x), default=-1)
    if k < 0:
        return None
    m = _ROW.match(lines[k])
    if not m or not m.group(2):
        return None                                   # 맨 아래 ❯ 가 입력칸 → 선택 창 없음
    col = len(m.group(1))                              # ❯ 칸(번호 줄의 들여쓰기 기준)

    def row(i):
        r = _ROW.match(lines[i])
        if not r:
            return None
        ind = len(r.group(1))
        return r if col - 1 <= ind <= col + 3 else None   # 같은 열의 번호 줄만(산문 속 '1. ' 과 구분)

    def cont(i):                                       # 번호 줄 아래 더 들여 쓴 이어지는 줄(줄바꿈·설명)
        s = lines[i]
        return bool(s.strip()) and (len(s) - len(s.lstrip())) > col + 1 and not row(i)

    top = k
    while top - 1 >= 0 and (row(top - 1) or cont(top - 1) or not lines[top - 1].strip()):
        top -= 1
    bot = k
    while bot + 1 < len(lines) and (row(bot + 1) or cont(bot + 1) or not lines[bot + 1].strip()):
        bot += 1
    choices, cur = [], None
    for i in range(top, bot + 1):
        r = row(i)
        if r:
            cur = {"n": int(r.group(3)), "label": r.group(4).strip(), "more": "", "selected": bool(r.group(2))}
            choices.append(cur)
        elif cur is not None and lines[i].strip():
            cur["more"] = (cur["more"] + " " + lines[i].strip()).strip()
    nums = [c["n"] for c in choices]
    if not choices or nums != list(range(1, len(choices) + 1)) or sum(c["selected"] for c in choices) != 1:
        return None
    above = [x.strip() for x in lines[:top] if x.strip()]
    question = above[-1] if above else ""
    context = []
    for x in reversed(above[:-1]):
        if _RULE.match(x) or len(context) >= 6:
            break
        context.insert(0, x)
    below = [x.strip() for x in lines[bot + 1:] if x.strip()]
    return {"question": question, "context": context, "choices": choices,
            "footer": below[0] if below else "",
            "multi": any(_MULTI.match(c["label"]) for c in choices) or len(choices) > 9}


def working(screen_text: str) -> bool:
    """맨 아래 몇 줄에 claude 의 작업 중 표시(esc to interrupt)가 있는가."""
    tail = [x for x in (screen_text or "").splitlines() if x.strip()][-6:]
    return any("esc to interrupt" in x.lower() for x in tail)


# 출력이 그대로면 다시 그리지 않는다(목록 갱신마다 부르므로) — 세션별 (누적 길이, 크기) → 결과
_CACHE: dict[str, tuple] = {}
_LOCK = threading.Lock()


def read(session_id: str) -> dict | None:
    """ClewPath 터미널의 지금 화면 → {terminal, prompt, working, last_input}. 그 세션 PTY 가 없으면 None."""
    from session_manager import webterm
    snap = webterm.snapshot(session_id)
    if snap is None:
        with _LOCK:
            _CACHE.pop(session_id, None)
        return None
    key = (snap["total"], snap["size"])
    with _LOCK:
        hit = _CACHE.get(session_id)
    if hit and hit[0] == key:
        out = dict(hit[1])
    else:
        txt = render(snap["text"], snap["size"])
        out = {"prompt": parse(txt), "working": working(txt)}
        with _LOCK:
            _CACHE[session_id] = (key, dict(out))
    out["terminal"] = True
    out["last_input"] = snap["last_input"]
    return out


def same_choice(p: dict | None, n: int, label: str) -> bool:
    """지금 화면의 n 번 선택지가 사람이 본 그 글자인가(그 사이 선택 창이 바뀌었으면 보내지 않는다)."""
    if not p:
        return False
    for c in p.get("choices") or []:
        if c["n"] == n:
            return c["label"] == (label or "").strip()
    return False


# ---------------------------------------------------------------- 대화 기록(jsonl) 쪽 — 읽기만
_TAIL_BYTES = 262_144
_INTERRUPT = "[Request interrupted by user"


def _tail_records(jsonl_path: str | None) -> list[dict]:
    import json
    import os
    if not jsonl_path:
        return []
    try:
        with open(jsonl_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > _TAIL_BYTES:
        lines = lines[1:]                             # 중간부터 읽었으면 첫 줄은 잘린 줄
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("type") in ("user", "assistant"):
            out.append(r)
    return out


def _ts(r: dict) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(r.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _blocks(r: dict) -> list:
    c = (r.get("message") or {}).get("content")
    return c if isinstance(c, list) else [{"type": "text", "text": c}] if isinstance(c, str) else []


def pending_tool(jsonl_path: str | None) -> dict | None:
    """결과가 아직 없는 마지막 도구 호출 → {name, detail} (선택 창이 무엇을 묻는지 보여 줄 때). 없으면 None."""
    recs = _tail_records(jsonl_path)
    done = {b.get("tool_use_id") for r in recs for b in _blocks(r) if isinstance(b, dict) and b.get("type") == "tool_result"}
    for r in reversed(recs):
        for b in reversed(_blocks(r)):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in done:
                inp = b.get("input") or {}
                detail = ""
                for k in ("command", "file_path", "path", "url", "pattern", "description", "prompt"):
                    if isinstance(inp.get(k), str) and inp[k].strip():
                        detail = inp[k].strip()
                        break
                if not detail and isinstance(inp.get("questions"), list) and inp["questions"]:
                    q = inp["questions"][0]
                    detail = str(q.get("question") or "") if isinstance(q, dict) else ""
                return {"name": str(b.get("name") or ""), "detail": detail[:400]}
    return None


def answered_since(jsonl_path: str | None, at: float) -> str | None:
    """at(초) 뒤에 대화가 이어졌나 → 'answered' | 'interrupted'(거부·중단) | None. 선택 창이 떠 있는 동안엔 대화 줄이 안 생긴다."""
    if not at:
        return None
    later = [r for r in _tail_records(jsonl_path) if _ts(r) > at]
    if not later:
        return None
    last = later[-1]
    if last.get("type") == "user":
        for b in _blocks(last):
            if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).startswith(_INTERRUPT):
                return "interrupted"
    return "answered"


WAIT_PHASES = ("permission", "waiting")


def reconcile(session_id: str, st: dict | None, jsonl_path: str | None) -> dict | None:
    """권한·입력 대기 상태가 이미 풀렸으면 훅 상태를 고친다(2026-11-04 사장님: 터미널에서 고른 뒤에도 ✋·🔔 가 남음).

    훅에는 '선택 창이 닫혔다' 는 사건이 없다(다음 훅은 턴이 끝나야 온다). 그래서
    ① ClewPath 터미널이면 화면에 선택 창이 사라졌고 그 뒤 키 입력이 있었는지, ② 아니면 대화 기록에 그 뒤 줄이 생겼는지로 판정한다.
    돌려주는 것 = 화면 읽기 결과(read) — 선택 창이 떠 있으면 목록이 '고를 수 있음' 을 표시한다. 터미널이 없으면 None.
    """
    from session_manager import hooks
    if not st or st.get("phase") not in WAIT_PHASES:
        return None
    ph = st["phase"]
    at = float(st.get(f"{ph}_at") or 0)
    tr = read(session_id)
    if tr and tr.get("prompt"):
        return tr                                     # 선택 창이 그대로 떠 있다
    if tr and tr.get("last_input", 0) > at:
        hooks.settle(session_id, "thinking" if tr.get("working") else "idle")
        return tr
    a = answered_since(jsonl_path, at)
    if a:
        hooks.settle(session_id, "idle" if a == "interrupted" else "thinking")
    return tr
