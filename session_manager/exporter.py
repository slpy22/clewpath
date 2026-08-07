"""세션 export — 세션 + manifest + (선택) 작업폴더를 .zip으로 묶는다.

작업폴더 번들 시 .gitignore를 존중한다(git repo면 git ls-files, 아니면 denylist).
"""
from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from session_manager.scanner import scan_one

# manifest 의 포맷 식별자. 리네이밍 전 zip 은 "session-manager-export/1" 을 갖는데,
# importer 가 둘 다 받아주므로 예전 백업도 그대로 열린다.
EXPORT_SCHEMA = "clewpath-export/1"
LEGACY_EXPORT_SCHEMAS = ("session-manager-export/1",)

# git이 아닐 때 제외할 무거운 디렉토리
_DENY_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              "dist", "build", ".next", "target", ".pytest_cache",
              ".mypy_cache", ".ruff_cache", "egg-info"}


def _workdir_files(cwd: Path) -> list[Path]:
    """작업폴더에서 번들할 파일 목록. git repo면 추적+미무시 파일, 아니면 walk+denylist."""
    if not cwd.is_dir():
        return []
    # git repo 시도
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "ls-files", "-co", "--exclude-standard"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if out.returncode == 0:
            files = []
            for rel in out.stdout.splitlines():
                rel = rel.strip()
                if not rel:
                    continue
                p = cwd / rel
                if p.is_file():
                    files.append(p)
            return files
    except Exception:
        pass
    # 폴백: walk + denylist
    files = []
    for p in cwd.rglob("*"):
        if p.is_file() and not any(part in _DENY_DIRS for part in p.parts):
            files.append(p)
    return files


def create_export(session_id: str, out_path: str, include_workdir: bool = False) -> dict:
    """세션을 out_path(.zip)로 export한다."""
    meta = scan_one(session_id)
    if meta is None:
        return {"error": "세션을 찾을 수 없습니다.", "session_id": session_id}

    jsonl = Path(meta.jsonl_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    workdir_files: list[Path] = []
    cwd_path = Path(meta.cwd) if meta.cwd else None
    if include_workdir and cwd_path and cwd_path.is_dir():
        workdir_files = _workdir_files(cwd_path)

    manifest = {
        # 포맷 식별자. 구 이름("session-manager-export/1")으로 내보낸 zip 도
        # importer 가 계속 받아준다(importer.SCHEMAS 참고).
        "schema": EXPORT_SCHEMA,
        "session_id": session_id,
        "original_cwd": meta.cwd,
        "project_folder": meta.project_folder,
        "original_jsonl_name": jsonl.name,
        "slug": meta.slug,
        "git_branch": meta.git_branch,
        "version": meta.version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "includes_workdir": bool(workdir_files),
        "workdir_file_count": len(workdir_files),
    }

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.write(jsonl, "session.jsonl")
        # 사이드 폴더
        side = jsonl.parent / session_id
        if side.is_dir():
            for p in side.rglob("*"):
                if p.is_file():
                    z.write(p, f"side/{p.relative_to(side).as_posix()}")
        # 작업폴더
        if workdir_files and cwd_path:
            for p in workdir_files:
                try:
                    z.write(p, f"workdir/{p.relative_to(cwd_path).as_posix()}")
                except Exception:
                    continue

    return {"session_id": session_id, "out_path": str(out),
            "size_bytes": out.stat().st_size, "manifest": manifest}
