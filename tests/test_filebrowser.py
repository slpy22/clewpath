"""📁 파일 보기(읽기 전용) — 뿌리 고정·탈출 거부·숨김·보호 파일·상한·인코딩·원격 2FA·감사 (file-browser.md §6·§9)."""
import os
import sys
from pathlib import Path

import pytest

from session_manager import connector, filebrowser as fb, policy
from tests.conftest import write_session

SID = "aaaaaaaa-1111-2222-3333-444444444444"


def _session(home: Path, cwd: Path | str, sid: str = SID):
    write_session(home, "F--fb", sid, [
        {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-10-12T00:00:00.000Z"}])


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    """pytest 임시 폴더는 진짜 AppData\\Local\\Temp 아래라 그대로면 '항상 숨김'에 걸린다 — 홈을 가짜로."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    return home


@pytest.fixture
def proj(fake_claude_home, tmp_path):
    p = tmp_path / "proj"
    (p / "docs").mkdir(parents=True)
    (p / "docs" / "report.md").write_bytes("# 보고서\n".encode())
    (p / "node_modules" / "x").mkdir(parents=True)
    (p / ".ssh").mkdir()
    (p / ".ssh" / "id_rsa").write_text("KEY", encoding="utf-8")
    (p / ".claude").mkdir()
    (p / ".env").write_text("SECRET=1", encoding="utf-8")
    (p / "ops" / "release-keys").mkdir(parents=True)
    (p / "ops" / "release-keys" / "private.pem").write_text("PEM", encoding="utf-8")
    (p / "ops" / "release-keys" / "readme.txt").write_text("보호 폴더 안", encoding="utf-8")
    (p / "app.py").write_bytes("print('안녕')\n".encode())
    (p / "euckr.txt").write_bytes("한글 cp949".encode("cp949"))
    (p / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (p / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    _session(fake_claude_home, p)
    return p


def test_list_dirs_first_heavy_blocked_and_hidden(proj):
    r = fb.list_dir(SID, "")
    names = [e["name"] for e in r["entries"]]
    assert r["root_name"] == "proj" and r["path"] == ""
    assert ".ssh" not in names and ".claude" not in names, "항상 숨김 폴더는 목록에도 없다"
    dirs = [e["name"] for e in r["entries"] if e["type"] == "dir"]
    assert names[:len(dirs)] == dirs, "폴더 먼저(VS Code 기본)"
    by = {e["name"]: e for e in r["entries"]}
    assert by["node_modules"].get("heavy") and by[".env"].get("blocked")
    assert not by["app.py"].get("blocked") and by["app.py"]["size"] == len("print('안녕')\n".encode())
    keys = fb.list_dir(SID, "ops/release-keys")["entries"]
    assert all(e.get("blocked") for e in keys), "release-keys/ 아래는 전부 보호"


def test_sort_by_mtime(proj):
    os.utime(proj / "app.py", (2_000_000_000, 2_000_000_000))
    files = [e["name"] for e in fb.list_dir(SID, "", "mtime")["entries"] if e["type"] == "file"]
    assert files[0] == "app.py"


@pytest.mark.parametrize("bad", ["..", "../x", "docs/../../x", "/etc", "C:/Windows", "C:\\Windows", "//srv/share"])
def test_escape_rejected(proj, bad):
    with pytest.raises(fb.FBError) as e:
        fb.list_dir(SID, bad)
    assert e.value.status in (400, 403)


def test_hidden_path_cannot_be_opened_directly(proj):
    for rel in (".ssh/id_rsa", ".claude", ".ssh"):
        with pytest.raises(fb.FBError) as e:
            fb.read_text(SID, rel) if "." in rel.split("/")[-1][1:] else fb.list_dir(SID, rel)
        assert e.value.status == 404


@pytest.mark.skipif(sys.platform != "win32", reason="정션은 Windows")
def test_junction_outside_root_hidden_and_refused(proj, tmp_path):
    import _winapi
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("밖", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(proj / "jump"))
    inside = proj / "docs"
    _winapi.CreateJunction(str(inside), str(proj / "docs_link"))
    names = {e["name"]: e for e in fb.list_dir(SID, "")["entries"]}
    assert "jump" not in names, "뿌리 밖을 가리키는 정션은 안 보인다"
    assert names["docs_link"].get("link") and names["docs_link"]["type"] == "dir", "안쪽 링크는 허용"
    with pytest.raises(fb.FBError) as e:
        fb.read_text(SID, "jump/x.txt")
    assert e.value.code == "outside_root"


def test_root_guards(fake_claude_home, tmp_path):
    _session(fake_claude_home, "C:\\" if sys.platform == "win32" else "/")
    with pytest.raises(fb.FBError) as e:
        fb.list_dir(SID, "")
    assert e.value.code == "root_is_drive"
    sid2 = "bbbbbbbb-1111-2222-3333-444444444444"
    _session(fake_claude_home, fake_claude_home / "projects", sid2)
    with pytest.raises(fb.FBError) as e:
        fb.list_dir(sid2, "")
    assert e.value.code == "root_blocked", "claude 데이터 폴더를 뿌리로 열 수 없다"
    with pytest.raises(fb.FBError) as e:
        fb.list_dir("cccccccc-0000-0000-0000-000000000000", "")
    assert e.value.code == "no_session"


def test_home_folder_session_hides_appdata(fake_claude_home, _fake_home):
    home = _fake_home
    (home / "AppData" / "Local").mkdir(parents=True)
    (home / "work").mkdir()
    _session(fake_claude_home, home)
    names = [e["name"] for e in fb.list_dir(SID, "")["entries"]]
    assert "work" in names and "AppData" not in names
    with pytest.raises(fb.FBError):
        fb.list_dir(SID, "AppData/Local")


def test_read_text_encodings_binary_image_protected(proj, monkeypatch):
    t = fb.read_text(SID, "app.py")
    assert t["kind"] == "text" and t["text"] == "print('안녕')\n" and t["encoding"] == "utf-8"
    assert fb.read_text(SID, "euckr.txt")["text"] == "한글 cp949"
    assert fb.read_text(SID, "blob.bin")["kind"] == "binary"
    assert fb.read_text(SID, "logo.png")["kind"] == "image"
    for rel in (".env", "ops/release-keys/private.pem", "ops/release-keys/readme.txt"):
        with pytest.raises(fb.FBError) as e:
            fb.read_text(SID, rel)
        assert e.value.code == "protected"
    monkeypatch.setattr(fb, "TEXT_MAX", 4)
    with pytest.raises(fb.FBError) as e:
        fb.read_text(SID, "app.py")
    assert e.value.status == 413 and e.value.extra["size"] > 4


def test_raw_image_only_and_download_limits(proj, monkeypatch):
    assert fb.read_raw(SID, "logo.png", relay=True, image_only=True)["mime"] == "image/png"
    with pytest.raises(fb.FBError) as e:
        fb.read_raw(SID, "app.py", relay=True, image_only=True)
    assert e.value.status == 415, "미리보기 경로로는 이미지 아닌 파일을 못 받는다(내려받기는 2FA 경로)"
    assert fb.read_raw(SID, "blob.bin", relay=True, image_only=False)["b64"]
    with pytest.raises(fb.FBError):
        fb.read_raw(SID, ".env", relay=False, image_only=False)
    monkeypatch.setattr(fb, "RAW_MAX_RELAY", 3)
    with pytest.raises(fb.FBError) as e:
        fb.read_raw(SID, "blob.bin", relay=True, image_only=False)
    assert e.value.status == 413
    assert fb.read_raw(SID, "blob.bin", relay=False, image_only=False)["size"] == 9, "로컬 상한은 따로"


def test_routes_remote_gate_and_audit(proj, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    logged = []
    monkeypatch.setattr(policy, "audit", lambda e: logged.append(e))
    with TestClient(S.create_app()) as c:
        r = c.get(f"/api/sessions/{SID}/files", params={"path": "docs"})
        assert r.status_code == 200 and r.json()["entries"][0]["name"] == "report.md"
        assert c.get(f"/api/sessions/{SID}/files", params={"path": "../x"}).status_code == 400
        assert c.get(f"/api/sessions/{SID}/file", params={"path": ".env"}).status_code == 403
        assert c.get(f"/api/sessions/{SID}/file", params={"path": "docs/report.md"}).json()["text"] == "# 보고서\n"
        assert not logged, "PC 로컬 열람은 감사 대상 아님"
        c.get(f"/api/sessions/{SID}/file", params={"path": "app.py"},
              headers={connector.VIA_HEADER: "relay", connector.DEVICE_HEADER: "dev1"})
        assert logged and logged[-1]["event"] == "file_view" and logged[-1]["device"] == "dev1"
        r = c.post(f"/api/sessions/{SID}/file/download", json={"path": "blob.bin"},
                   headers={connector.VIA_HEADER: "relay"})
        assert r.status_code == 200 and logged[-1]["event"] == "file_download"
        assert c.get(f"/api/sessions/{SID}/file", params={"path": "logo.png", "mode": "image"}).json()["b64"]
    assert connector._is_privileged_api(f"/api/sessions/{SID}/file/download"), "원격 내려받기는 2FA"
    assert not connector._is_privileged_api(f"/api/sessions/{SID}/file"), "보기는 페어링 기기 허용(§10)"


def test_guard_block_lists_stay():
    """차단 목록이 빠지면 실패 — ~/.claude, *.pem, release-keys (file-browser §9 가드)."""
    assert {".claude", ".ssh", "appdata"} <= fb.HIDDEN_NAMES
    assert fb.is_protected("ops/release-keys/private.pem") and fb.is_protected("x/server.PEM")
    assert fb.is_protected(".env.local") and fb.is_protected("cp_credentials.json")
    assert fb.is_protected("a/id_rsa.pub") and fb.is_protected("ptyd.key")
    assert not fb.is_protected("docs/report.md")
