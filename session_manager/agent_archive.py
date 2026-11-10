"""에이전트 기록 보관(docs/designs/agent-unit.md '에이전트 탭·세션 관리·기록 보관' H9, 판정 K12~K14·K17·K19).

claude 는 보관 기간(기본 30일)이 지난 세션 기록을 지운다. 에이전트는 오래 일하므로, 에이전트에 묶인 세션 기록을 ClewPath 데이터 폴더에
사본으로 둔다 — 지난 대화 찾기(agent_history)가 원본이 지워진 뒤에도 원문으로 찾게. **원본은 읽기만 한다**(불가침).

- 원본의 마지막 완전한 줄까지만 옮긴다(쓰다 만 줄 제외).
- 보관본이 원본의 앞부분과 같으면(앞 4KB + 보관본 끝 64KB 대조) 늘어난 끝만 이어 붙이고, 아니면 옛 보관본을 `<id>.<시각>.jsonl` 로 남긴 뒤
  통째로 새로 복사한다(임시 파일 → 바꿔치기). 옛 보관본을 지워 공간을 만들지 않는다.
- 한 번 실행에 512MB 예산, 여유 공간 2GB 미만이면 새 보관을 멈춘다. 떼어 낸 세션은 보관하지 않는다(이미 만든 보관본은 둔다).
- 보관본은 원본 그대로다(가림 없음). 보여 줄 때 agent_history 가 가린다.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from session_manager import config, team

HEAD = 4096
TAIL = 64 * 1024
RUN_BUDGET = 512 * 1024 * 1024
FREE_MIN = 2 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024

_lock = threading.Lock()


def _dir(uid: str) -> Path:
    return config.data_dir() / "agents" / str(uid) / "archive"


def _targets(agent_id: str | None = None) -> list[dict]:
    """보관 대상 = 에이전트에 묶인(떼어 내지 않은) 세션."""
    c = team._read()
    try:
        q = ("SELECT a.id AS agent_id, COALESCE(a.uid, a.id) AS uid, s.session_id FROM agent_sessions s JOIN agents a ON a.id=s.agent_id"
             " WHERE COALESCE(s.detached,0)=0" + (" AND a.id=?" if agent_id else "") + " ORDER BY s.started DESC")
        return [dict(r) for r in c.execute(q, (agent_id,) if agent_id else ())]
    finally:
        c.close()


def path_for(sid: str) -> Path | None:
    """이 세션의 보관본(있으면). 떼어 낸 세션도 보관본이 있으면 돌려준다 — 범위 검사는 부르는 쪽(agent_history.scope)이 한다."""
    c = team._read()
    try:
        r = c.execute("SELECT COALESCE(a.uid, a.id) AS uid FROM agent_sessions s JOIN agents a ON a.id=s.agent_id WHERE s.session_id=?", (sid,)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    p = _dir(r["uid"]) / f"{sid}.jsonl"
    return p if p.is_file() else None


def _source(sid: str) -> str | None:
    from session_manager import scanner
    m = scanner.scan_one(sid)
    p = getattr(m, "jsonl_path", None) if m is not None else None
    return p if p and os.path.isfile(p) else None


def _complete_len(path: str) -> int:
    """마지막 완전한 줄 끝(개행 바로 뒤)까지의 길이."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = size
        while pos > 0:
            step = min(CHUNK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step)
            i = buf.rfind(b"\n")
            if i >= 0:
                return pos + i + 1
    return 0


def _read(path, off: int, n: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(off)
        return f.read(n)


def _prefix_ok(src: str, dst: Path, dsz: int) -> bool:
    """보관본이 원본의 앞부분과 같은가(앞 4KB + 보관본 끝 64KB)."""
    h = min(HEAD, dsz)
    if _read(src, 0, h) != _read(dst, 0, h):
        return False
    t = min(TAIL, dsz)
    return _read(src, dsz - t, t) == _read(dst, dsz - t, t)


def _copy_range(src: str, dst, off: int, end: int, mode: str) -> None:
    with open(src, "rb") as fi, open(dst, mode) as fo:
        fi.seek(off)
        left = end - off
        while left > 0:
            b = fi.read(min(CHUNK, left))
            if not b:
                break
            fo.write(b)
            left -= len(b)
        fo.flush()
        os.fsync(fo.fileno())


def _free(d: Path) -> int:
    try:
        return shutil.disk_usage(d).free
    except OSError:
        return 0


def archive_one(uid: str, sid: str, src: str, budget: int | None = None) -> dict:
    """한 세션 보관. status: same·appended·copied·no_space·over_budget(이번엔 건너뜀)."""
    d = _dir(uid)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{sid}.jsonl"
    end = _complete_len(src)
    if end == 0:
        return {"status": "same", "bytes": 0}
    dsz = dst.stat().st_size if dst.is_file() else -1
    same_prefix = dsz >= 0 and dsz <= end and _prefix_ok(src, dst, dsz)
    if same_prefix and dsz == end:
        return {"status": "same", "bytes": 0}
    need = end - dsz if same_prefix else end
    if budget is not None and need > budget:
        return {"status": "over_budget", "bytes": 0}
    if _free(d) < FREE_MIN + need:
        return {"status": "no_space", "bytes": 0}
    if same_prefix:
        _copy_range(src, dst, dsz, end, "ab")
        return {"status": "appended", "bytes": need}
    if dsz >= 0:                                          # 원본이 다시 쓰였다 — 옛 보관본은 지우지 않고 남긴다
        dst.rename(d / f"{sid}.{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    tmp = d / f"{sid}.jsonl.tmp"
    _copy_range(src, tmp, 0, end, "wb")
    os.replace(tmp, dst)
    return {"status": "copied", "bytes": need}


def run(budget: int = RUN_BUDGET) -> dict:
    """정기 작업 본체: 에이전트 세션을 모두 이어 보관한다."""
    done = new = missing = skipped = 0
    stopped = False
    left = budget
    with _lock:
        for t in _targets():
            src = _source(t["session_id"])
            if not src:
                missing += 1
                continue
            try:
                r = archive_one(t["uid"], t["session_id"], src, left)
            except OSError:
                skipped += 1
                continue
            if r["status"] == "no_space":
                stopped = True
                break
            if r["status"] == "over_budget":
                skipped += 1
                continue
            left -= r["bytes"]
            done += 1
            new += 1 if r["status"] in ("appended", "copied") else 0
    total = sum(f.stat().st_size for f in (config.data_dir() / "agents").glob("*/archive/*.jsonl")) if (config.data_dir() / "agents").is_dir() else 0
    parts = [f"보관 {done}개", f"이번에 갱신 {new}개", f"보관 크기 {total / 1e6:.1f}MB"]
    if missing:
        parts.append(f"원본 없음 {missing}개(보관본은 그대로)")
    if skipped:
        parts.append(f"다음에 {skipped}개")
    if stopped:
        parts.append("여유 공간이 2GB 아래라 새 보관을 멈췄습니다")
    return {"archived": done, "updated": new, "missing": missing, "skipped": skipped, "no_space": stopped, "bytes": total, "summary": " · ".join(parts)}


def archive_now(agent_id: str, session_ids: list[str]) -> dict:
    """지우기 전에 보관(K13): 그 에이전트의 세션만, 예산 없이. 줄별 ok — ok 인 것만 지워도 된다."""
    mine = {t["session_id"]: t for t in _targets(agent_id)}
    out = {}
    with _lock:
        for sid in session_ids or []:
            t = mine.get(str(sid))
            if not t:
                out[sid] = {"ok": False, "status": "not_this_agent"}
                continue
            src = _source(sid)
            if not src:
                out[sid] = {"ok": path_for(sid) is not None, "status": "no_source"}
                continue
            try:
                r = archive_one(t["uid"], sid, src)
            except OSError as e:
                out[sid] = {"ok": False, "status": type(e).__name__}
                continue
            out[sid] = {"ok": r["status"] in ("same", "appended", "copied"), "status": r["status"]}
    return {"items": out}


def archive_soon(sid: str) -> None:
    """갈아타기가 끝난 옛 세션을 바로 보관(K19). 실패해도 정기 작업이 다시 한다."""
    def go():
        try:
            for t in _targets():
                if t["session_id"] == sid:
                    src = _source(sid)
                    if src:
                        with _lock:
                            archive_one(t["uid"], sid, src)
                    return
        except Exception as e:  # noqa: BLE001
            print(f"[agent_archive] 바로 보관 실패 {sid[:8]}: {type(e).__name__}", flush=True)
    threading.Thread(target=go, daemon=True, name=f"archive-{sid[:8]}").start()


def info(agent_id: str) -> dict:
    """에이전트 상세용: 세션별 보관·원본 여부와 합계."""
    from session_manager import scanner
    c = team._read()
    try:
        a = c.execute("SELECT COALESCE(uid, id) AS uid FROM agents WHERE id=?", (agent_id,)).fetchone()
        sids = [r["session_id"] for r in c.execute("SELECT session_id FROM agent_sessions WHERE agent_id=?", (agent_id,))]
    finally:
        c.close()
    if not a:
        return {"count": 0, "bytes": 0, "sessions": {}}
    d = _dir(a["uid"])
    out, total, n = {}, 0, 0
    for sid in sids:
        p = d / f"{sid}.jsonl"
        size = p.stat().st_size if p.is_file() else 0
        m = scanner.scan_one(sid)
        out[sid] = {"archived": size > 0, "bytes": size, "original": bool(m is not None and os.path.isfile(getattr(m, "jsonl_path", "") or ""))}
        total += size
        n += 1 if size else 0
    return {"count": n, "bytes": total, "sessions": out}
