"""세션 생명주기 — 삭제 / 작업폴더(cwd) 변경.

안전 원칙:
- 모든 파괴적 작업은 dry_run=True로 먼저 영향 범위를 확인할 수 있다.
- 삭제는 연관 파일을 모두 찾아 제거한다(jsonl + 사이드폴더 + session-env + todos).
- cwd 변경은 jsonl을 새로 쓰되, 원본을 .bak으로 백업한다.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from session_manager import config
from session_manager.scanner import scan_one


def _related_paths(session_id: str) -> list[Path]:
    """세션과 연관된 모든 파일/폴더 경로를 수집한다(존재하는 것만)."""
    paths: list[Path] = []
    meta = scan_one(session_id)
    if meta is not None:
        jsonl = Path(meta.jsonl_path)
        paths.append(jsonl)
        side = jsonl.parent / session_id
        if side.is_dir():
            paths.append(side)

    # session-env/{uuid}
    senv = config.session_env_dir() / session_id
    if senv.exists():
        paths.append(senv)

    # todos/ 안에 uuid가 포함된 파일들
    tdir = config.todos_dir()
    if tdir.is_dir():
        for p in tdir.glob(f"*{session_id}*"):
            paths.append(p)

    return paths


def delete_session(session_id: str, dry_run: bool = True) -> dict:
    """세션과 연관 파일을 삭제한다. dry_run이면 삭제 대상만 반환."""
    targets = _related_paths(session_id)
    target_info = [
        {"path": str(p), "type": "dir" if p.is_dir() else "file",
         "size_bytes": _path_size(p)}
        for p in targets
    ]

    if dry_run:
        return {"dry_run": True, "session_id": session_id,
                "would_delete": target_info,
                "total_bytes": sum(t["size_bytes"] for t in target_info)}

    if not targets:
        return {"dry_run": False, "session_id": session_id,
                "deleted": [], "error": "세션을 찾을 수 없습니다."}

    deleted: list[str] = []
    errors: list[str] = []
    for p in targets:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            deleted.append(str(p))
        except Exception as e:
            errors.append(f"{p}: {e}")

    return {"dry_run": False, "session_id": session_id,
            "deleted": deleted, "errors": errors}


def change_cwd(session_id: str, new_cwd: str, dry_run: bool = True) -> dict:
    """세션 jsonl 내의 모든 'cwd' 값을 new_cwd로 치환한다.

    실제 적용 시 원본을 {파일}.bak 으로 백업한 뒤 새로 쓴다.
    """
    meta = scan_one(session_id)
    if meta is None:
        return {"error": "세션을 찾을 수 없습니다.", "session_id": session_id}

    jsonl = Path(meta.jsonl_path)
    old_cwd = meta.cwd
    replaced = 0
    total = 0

    # 미리 치환 카운트 계산
    new_lines: list[str] = []
    with jsonl.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                new_lines.append(line)
                continue
            total += 1
            try:
                obj = json.loads(line)
            except Exception:
                new_lines.append(line)  # 파싱 불가 줄은 원형 보존
                continue
            if "cwd" in obj:
                obj["cwd"] = new_cwd
                replaced += 1
            new_lines.append(json.dumps(obj, ensure_ascii=False))

    if dry_run:
        return {"dry_run": True, "session_id": session_id,
                "old_cwd": old_cwd, "new_cwd": new_cwd,
                "lines_with_cwd": replaced, "total_lines": total}

    # 백업 후 새로 쓰기
    backup = jsonl.with_suffix(jsonl.suffix + ".bak")
    shutil.copy2(jsonl, backup)
    with jsonl.open("w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

    return {"dry_run": False, "session_id": session_id,
            "old_cwd": old_cwd, "new_cwd": new_cwd,
            "lines_changed": replaced, "backup": str(backup)}


def _path_size(p: Path) -> int:
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size
    except Exception:
        return 0
