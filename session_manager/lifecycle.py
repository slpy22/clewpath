"""세션 생명주기 — 삭제 / 작업폴더(cwd) 변경.

안전 원칙:
- 모든 파괴적 작업은 dry_run=True로 먼저 영향 범위를 확인할 수 있다.
- 삭제는 연관 파일을 모두 찾아 제거한다(jsonl + 사이드폴더 + session-env + todos).
- cwd 변경은 jsonl을 새로 쓰되, 원본을 .bak으로 백업한다.
"""
from __future__ import annotations

import os
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

    # 우리 사이드카 라벨도 함께 제거(B안): 삭제된 세션의 유령 라벨이 labels.json
    # 에 쌓이지 않게. 복구해도 라벨은 안 돌아온다(사용자가 다시 붙임). claude
    # 파일이 아니라 ClewPath 자체 저장소만 건드리므로 원칙과 무관.
    try:
        from session_manager import labels as _labels
        _labels.delete(session_id)
    except Exception:  # noqa: BLE001
        pass

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


# [원칙] ClewPath 는 claude 의 세션 파일을 임의로 수정하지 않는다.
# 한때 '은닉 유지'(재개 후 ai-title 레코드 자동 제거)를 구현했으나, claude 자체
# 동작에 영향을 주는 무단 파일 수정이라 원칙 위반으로 제거했다(2026-08-13 확정).
# 피커 미표시 세션의 대화형 재개는 '재개 전 고지 + 사용자 승인'으로 대체한다.


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


def move_session(session_id: str, new_cwd: str, move_content: bool = False,
                 dry_run: bool = True) -> dict:
    """세션을 다른 작업 폴더로 '이사'시킨다 (claude 가 미지원하는 폴더 이동 지원).

    claude 는 세션을 projects/<cwd 인코딩>/uuid.jsonl 로 저장하고, '어느 인코딩
    폴더에 있는지'로 세션의 프로젝트를 판단한다. 그래서 작업 폴더를 옮기려면
    cwd 값만 바꿔선 안 되고(그러면 claude 픽커가 못 찾음), 세션 파일을 새 cwd 의
    인코딩 폴더로 물리 이동해야 한다(실증 2026-08-31: 이동+치환 후 새 폴더에서
    --resume 정상 인식).

    동작:
      1) 세션 jsonl 을 새 폴더(projects/<new 인코딩>/)로 이동하며 cwd 값 치환
      2) 사이드 폴더(uuid/)도 함께 이동 (session-env/todos 는 uuid 기반이라 무관)
      3) move_content=True 면 실제 작업 폴더 내용도 new_cwd 로 이동(콘텐츠 이사)

    [원칙] claude 세션 파일의 물리 이동 - 승인된 예외(2026-08-31 사장님 승인).
    안전: 실행 중 세션 차단, 경로 검증, dry_run 지원.
    """
    from session_manager.pathenc import path_to_folder

    meta = scan_one(session_id)
    if meta is None:
        return {"error": "세션을 찾을 수 없습니다.", "session_id": session_id}

    # 실행 중 세션은 파일 이동 중 손상 위험 → 차단
    try:
        from session_manager import webterm
        if webterm.has_terminal(session_id):
            return {"error": "이 세션은 실행 중입니다. 터미널을 종료한 뒤 이동하세요.",
                    "session_id": session_id}
    except Exception:  # noqa: BLE001
        pass

    src_jsonl = Path(meta.jsonl_path)
    old_cwd = meta.cwd
    new_folder = path_to_folder(new_cwd)
    dst_dir = config.projects_dir() / new_folder
    dst_jsonl = dst_dir / src_jsonl.name
    side_src = src_jsonl.parent / session_id
    side_dst = dst_dir / session_id

    # 대상 폴더가 지금 폴더와 같으면 = 이동 불필요(cwd 만 치환)
    same_folder = (src_jsonl.parent.resolve() == dst_dir.resolve()
                   if dst_dir.exists() else src_jsonl.parent.name == new_folder)

    plan = {
        "session_id": session_id, "old_cwd": old_cwd, "new_cwd": new_cwd,
        "session_file_move": None if same_folder else {
            "from": str(src_jsonl), "to": str(dst_jsonl)},
        "side_dir_move": ({"from": str(side_src), "to": str(side_dst)}
                          if side_src.is_dir() and not same_folder else None),
        "content_move": None,
    }
    if move_content and old_cwd and os.path.isdir(old_cwd):
        entries = sorted(os.listdir(old_cwd))
        plan["content_move"] = {"from": old_cwd, "to": new_cwd, "items": entries}
    elif move_content:
        plan["content_move"] = {"error": f"원본 작업 폴더가 없습니다: {old_cwd}"}

    if dry_run:
        return {"dry_run": True, **plan}

    if dst_jsonl.exists() and not same_folder:
        return {"error": "대상 폴더에 같은 세션 파일이 이미 있습니다.",
                "session_id": session_id}

    moved_ops: list[str] = []
    try:
        # 1) 세션 jsonl: cwd 치환하며 새 위치로 (같은 폴더면 제자리 치환)
        dst_dir.mkdir(parents=True, exist_ok=True)
        out_lines: list[str] = []
        with src_jsonl.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.strip():
                    try:
                        obj = json.loads(line)
                        if "cwd" in obj:
                            obj["cwd"] = new_cwd
                        line = json.dumps(obj, ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        pass
                out_lines.append(line)
        target = src_jsonl if same_folder else dst_jsonl
        target.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        if not same_folder:
            src_jsonl.unlink()
            moved_ops.append(f"세션 파일 → {dst_jsonl}")
            # 2) 사이드 폴더
            if side_src.is_dir():
                shutil.move(str(side_src), str(side_dst))
                moved_ops.append(f"사이드 폴더 → {side_dst}")
        else:
            moved_ops.append("cwd 치환(제자리 - 같은 폴더)")

        # 3) 콘텐츠 이동(옵션)
        content_result = None
        if move_content and old_cwd and os.path.isdir(old_cwd) \
                and os.path.abspath(old_cwd) != os.path.abspath(new_cwd):
            os.makedirs(new_cwd, exist_ok=True)
            moved_items, skipped = [], []
            for name in os.listdir(old_cwd):
                s = os.path.join(old_cwd, name)
                d = os.path.join(new_cwd, name)
                if os.path.exists(d):
                    skipped.append(f"{name} (대상에 이미 존재)")
                    continue
                shutil.move(s, d)
                moved_items.append(name)
            content_result = {"moved": moved_items, "skipped": skipped}
            moved_ops.append(f"콘텐츠 {len(moved_items)}개 → {new_cwd}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"이동 실패: {e}", "session_id": session_id,
                "partial": moved_ops}

    from session_manager import scanner as _sc
    _sc._CACHE.clear()   # 캐시 무효화(경로 바뀜)
    return {"dry_run": False, "session_id": session_id,
            "old_cwd": old_cwd, "new_cwd": new_cwd,
            "operations": moved_ops,
            "content": content_result if move_content else None}
