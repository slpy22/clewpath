"""ClewPath 리네이밍 호환 창(window) 회귀.

브랜드명을 바꾸면서 두 개의 프로토콜 문자열이 함께 바뀌었다:
  - JWT iss:  session-manager-control → clewpath-control
  - export manifest schema: session-manager-export/1 → clewpath-export/1

둘 다 "구 값도 받아준다"는 전제로 바꿨다. 그 전제가 조용히 사라지면
 (a) CP·릴레이 재배포 순서가 어긋난 몇 분 동안 모든 커넥터가 4401 로 끊기고,
 (b) 리네이밍 전에 내보낸 백업 zip 을 영영 못 읽는다.
아래 테스트가 그 두 가지를 붙잡는다. 전환이 끝나 구 값을 정말 버릴 때는
이 파일도 같이 지운다(그때는 의도된 삭제다).
"""
from __future__ import annotations

import json
import zipfile

import pytest

from session_manager import importer
from session_manager.exporter import EXPORT_SCHEMA, LEGACY_EXPORT_SCHEMAS


# 참고: CP 발급자 ↔ 릴레이 ACCEPTED_ISSUERS 호환 검증은 두 서버 모듈이 함께 있는
# private 서버 저장소(clewpath-server/tests/test_issuer_compat.py)로 이관됐다.


def test_importer_accepts_legacy_export_schema():
    assert EXPORT_SCHEMA == "clewpath-export/1"
    for legacy in LEGACY_EXPORT_SCHEMAS:
        assert legacy in importer.ACCEPTED_SCHEMAS, legacy
    assert EXPORT_SCHEMA in importer.ACCEPTED_SCHEMAS


def _make_zip(path, schema):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "schema": schema, "session_id": "s1",
            "original_cwd": "C:/x", "project_folder": "C-x",
        }))
        z.writestr("session.jsonl", '{"type":"user"}\n')


@pytest.mark.parametrize("schema", ["clewpath-export/1", "session-manager-export/1"])
def test_import_does_not_reject_known_schemas(tmp_path, monkeypatch, schema):
    """구/신 schema 모두 '포맷' 사유로는 거부되지 않아야 한다."""
    monkeypatch.setattr(importer.config, "projects_dir", lambda: tmp_path / "projects")
    zp = tmp_path / "e.zip"
    _make_zip(zp, schema)
    r = importer.import_session(str(zp))
    assert "지원하지 않는 export 포맷" not in (r.get("error") or "")


def test_import_rejects_foreign_schema(tmp_path, monkeypatch):
    """반대로 남의 포맷은 분명히 거부해야 한다(검사가 무의미해지지 않게)."""
    monkeypatch.setattr(importer.config, "projects_dir", lambda: tmp_path / "projects")
    zp = tmp_path / "e.zip"
    _make_zip(zp, "someone-else-export/9")
    r = importer.import_session(str(zp))
    assert "지원하지 않는 export 포맷" in (r.get("error") or "")
