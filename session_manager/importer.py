"""세션 import — export된 .zip을 복원한다.

- manifest를 읽어 원본 cwd/세션ID를 파악한다.
- new_cwd가 주어지면 jsonl 내 cwd를 일괄 재지정하고, 대상 폴더도 새 경로로 인코딩한다.
- 작업폴더(workdir/)가 있고 new_cwd가 주어지면 해당 경로로 복원한다.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from session_manager import config
from session_manager.exporter import EXPORT_SCHEMA, LEGACY_EXPORT_SCHEMAS
from session_manager.pathenc import path_to_folder

# 받아주는 manifest 포맷. 현재 것 + 리네이밍 전 구 이름.
ACCEPTED_SCHEMAS = (EXPORT_SCHEMA, *LEGACY_EXPORT_SCHEMAS)


def _rewrite_cwd(src_jsonl: Path, dst_jsonl: Path, new_cwd: str | None) -> int:
    """jsonl을 복사하며 new_cwd로 cwd를 치환한다. 치환 줄 수 반환."""
    replaced = 0
    dst_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with src_jsonl.open(encoding="utf-8", errors="replace") as fin, \
         dst_jsonl.open("w", encoding="utf-8") as fout:
        for raw in fin:
            line = raw.rstrip("\n")
            if not line.strip():
                fout.write(line + "\n")
                continue
            if new_cwd is None:
                fout.write(line + "\n")
                continue
            try:
                obj = json.loads(line)
            except Exception:
                fout.write(line + "\n")
                continue
            if "cwd" in obj:
                obj["cwd"] = new_cwd
                replaced += 1
            fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return replaced


def import_session(zip_path: str, new_cwd: str | None = None,
                   overwrite: bool = False, restore_workdir: bool = False) -> dict:
    """export zip을 현재 ~/.claude로 import한다."""
    zp = Path(zip_path)
    if not zp.is_file():
        return {"error": "zip 파일을 찾을 수 없습니다.", "zip_path": zip_path}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            with zipfile.ZipFile(zp) as z:
                z.extractall(tmp)
        except Exception as e:
            return {"error": f"zip 해제 실패: {e}"}

        manifest_path = tmp / "manifest.json"
        if not manifest_path.is_file():
            return {"error": "manifest.json이 없습니다. 올바른 export 파일이 아닙니다."}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # 포맷 검사. 리네이밍(ClewPath) 전에 내보낸 zip 도 그대로 받아준다.
        schema = manifest.get("schema")
        if schema and schema not in ACCEPTED_SCHEMAS:
            return {"error": f"지원하지 않는 export 포맷입니다: {schema}"}

        session_id = manifest["session_id"]
        target_cwd = new_cwd or manifest.get("original_cwd")
        # 대상 폴더: 새 cwd가 있으면 그걸로 인코딩, 없으면 원본 폴더명 유지
        if new_cwd:
            target_folder = path_to_folder(new_cwd)
        else:
            target_folder = manifest.get("project_folder") or path_to_folder(target_cwd or "unknown")

        dst_dir = config.projects_dir() / target_folder
        dst_jsonl = dst_dir / f"{session_id}.jsonl"

        if dst_jsonl.exists() and not overwrite:
            return {"error": "이미 같은 세션이 존재합니다. overwrite=True로 덮어쓸 수 있습니다.",
                    "existing": str(dst_jsonl), "session_id": session_id}

        # jsonl 복원 (cwd 재지정)
        src_jsonl = tmp / "session.jsonl"
        replaced = _rewrite_cwd(src_jsonl, dst_jsonl, new_cwd)

        # 사이드 폴더 복원
        side_src = tmp / "side"
        side_restored = 0
        if side_src.is_dir():
            side_dst = dst_dir / session_id
            for p in side_src.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(side_src)
                    target = side_dst / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
                    side_restored += 1

        # 작업폴더 복원 (옵션)
        workdir_restored = 0
        workdir_src = tmp / "workdir"
        if restore_workdir and workdir_src.is_dir() and new_cwd:
            dest_root = Path(new_cwd)
            dest_root.mkdir(parents=True, exist_ok=True)
            for p in workdir_src.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(workdir_src)
                    target = dest_root / rel
                    if target.exists() and not overwrite:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
                    workdir_restored += 1

        return {
            "session_id": session_id,
            "target_folder": target_folder,
            "target_jsonl": str(dst_jsonl),
            "new_cwd": new_cwd,
            "cwd_lines_rewritten": replaced,
            "side_files_restored": side_restored,
            "workdir_files_restored": workdir_restored,
            "manifest": manifest,
        }
