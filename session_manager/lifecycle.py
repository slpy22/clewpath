"""세션 생명주기 — 삭제 / 작업폴더(cwd) 변경.

안전 원칙:
- 모든 파괴적 작업은 dry_run=True로 먼저 영향 범위를 확인할 수 있다.
- 삭제는 연관 파일을 모두 찾아 제거한다(jsonl + 사이드폴더 + session-env + todos).
- cwd 변경은 jsonl을 새로 쓰되, 원본을 .bak으로 백업한다.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from session_manager import config
from session_manager.scanner import scan_one


def _trash_root() -> Path:
    """삭제된 세션을 잠시 보관하는 휴지통. 데이터 폴더 아래 — 재설치/업데이트가 안 건드림."""
    return config.data_dir() / "trash"


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
    """세션과 연관 파일을 삭제한다 — 하드 삭제가 아니라 **휴지통으로 이동**해 복구 가능.

    실수로 지워도 restore_session 으로 되살릴 수 있다(중요 세션 유실 방지).
    dry_run이면 삭제 대상만 반환. 오래된 휴지통(기본 30일)은 자동 영구삭제.
    """
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

    # 휴지통 버킷으로 '이동'(rmtree/unlink 아님). 같은 이름 충돌 방지 위해 인덱스 접두사.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bucket = _trash_root() / f"{session_id}_{stamp}"
    bucket.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    errors: list[str] = []
    items: list[dict] = []
    for i, p in enumerate(targets):
        try:
            stored = bucket / f"{i:02d}__{p.name}"
            shutil.move(str(p), str(stored))
            moved.append(str(p))
            items.append({"original": str(p), "stored": str(stored),
                          "type": "dir" if stored.is_dir() else "file"})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p}: {e}")

    (bucket / "manifest.json").write_text(
        json.dumps({"session_id": session_id, "deleted_at": stamp,
                    "items": items}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    _purge_old_trash()

    return {"dry_run": False, "session_id": session_id,
            "deleted": moved, "errors": errors,
            "trash": str(bucket), "recoverable": True}


def list_trash() -> list[dict]:
    """휴지통에 있는(복구 가능한) 삭제 세션 목록. 최근 삭제 순."""
    root = _trash_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for bucket in sorted(root.iterdir(), key=lambda b: b.name, reverse=True):
        mf = bucket / "manifest.json"
        if not mf.is_file():
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append({"bucket": bucket.name, "session_id": m.get("session_id"),
                    "deleted_at": m.get("deleted_at"),
                    "items": len(m.get("items", []))})
    return out


def restore_session(bucket_name: str) -> dict:
    """휴지통 버킷을 원위치로 복구한다. 이미 같은 경로가 있으면 덮어쓰지 않고 건너뜀."""
    bucket = _trash_root() / bucket_name
    mf = bucket / "manifest.json"
    if not mf.is_file():
        return {"error": "휴지통 항목을 찾을 수 없습니다.", "bucket": bucket_name}
    m = json.loads(mf.read_text(encoding="utf-8"))
    restored: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []
    for it in m.get("items", []):
        orig = Path(it["original"])
        stored = Path(it["stored"])
        try:
            if not stored.exists():
                skipped.append(f"{orig} (휴지통 파일 없음)")
                continue
            if orig.exists():
                skipped.append(f"{orig} (이미 존재 — 덮어쓰지 않음)")
                continue
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stored), str(orig))
            restored.append(str(orig))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{orig}: {e}")
    if restored and not errors and not skipped:
        try:
            shutil.rmtree(bucket)   # 전부 복구됐으면 버킷 정리
        except Exception:  # noqa: BLE001
            pass
    return {"session_id": m.get("session_id"), "restored": restored,
            "errors": errors, "skipped": skipped}


def scrub_auto_titles(session_id: str) -> dict:
    """세션 jsonl 에서 자동 제목 레코드(ai-title/agent-name)를 제거한다.

    용도(은닉 유지): 피커 미표시 세션(에이전트/포크 산물)을 ClewPath 터미널로
    재개해 대화형으로 쓰면 claude 가 자동 제목을 붙여 `--resume` 피커 목록에
    새로 나타난다 - 사용자가 만든 적 없는 세션이 갑자기 목록에 생겨 혼동을
    준다(실사고: 오삭제). 사용 종료 후 이 레코드만 걷어내 원래대로 목록 밖에
    둔다. 대화 내용·custom-title 은 건드리지 않는다.
    """
    meta = scan_one(session_id)
    if meta is None:
        return {"error": "세션을 찾을 수 없습니다.", "removed": 0}
    path = Path(meta.jsonl_path)
    kept: list[str] = []
    removed = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    t = json.loads(line).get("type")
                except Exception:  # noqa: BLE001
                    t = None
                if t in ("ai-title", "agent-name"):
                    removed += 1
                    continue
                kept.append(line)
        if removed:
            tmp = path.with_suffix(".jsonl.scrub")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "removed": 0}
    return {"removed": removed}


def _purge_old_trash(keep_days: int = 30) -> None:
    """휴지통에서 keep_days 지난 버킷만 영구 삭제(무한 성장 방지)."""
    root = _trash_root()
    if not root.is_dir():
        return
    cutoff = time.time() - keep_days * 86400
    for bucket in root.iterdir():
        try:
            if bucket.is_dir() and bucket.stat().st_mtime < cutoff:
                shutil.rmtree(bucket)
        except Exception:  # noqa: BLE001
            pass


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
