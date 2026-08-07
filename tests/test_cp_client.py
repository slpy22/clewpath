"""T7 테스트 — 커넥터의 CP 연동: 자동 발급 · JWT 조달 · ★fail-open★ · 상태 배지.

가장 중요한 건 fail-open 회귀 테스트다: CP 가 죽어도 커넥터는 예외 없이
공유토큰 경로로 접속을 계속해야 한다(기존 사용자 접속을 끊지 않음).
"""
import asyncio
import json
import os

import httpx
import pytest

from session_manager import cp_client


_REAL_CLIENT = httpx.Client   # 패치 전 원본(람다 안에서 재귀 방지)


def _mock_client(monkeypatch, handler):
    """cp_client 가 만드는 httpx.Client 를 MockTransport 로 대체."""
    monkeypatch.setattr(
        cp_client.httpx, "Client",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """자격증명 파일과 상태를 테스트마다 격리."""
    monkeypatch.setattr(cp_client.config, "data_dir", lambda: tmp_path)
    cp_client._state.update({"mode": "shared", "reason": "cp_not_configured",
                            "cp_url": None, "connector_id": None,
                            "checked_at": None, "jwt_exp": None})
    monkeypatch.delenv("SM_CP_URL", raising=False)
    yield


# ---- CP 미설정: 완전 no-op (기존 배포 동작 유지) ----
def test_no_cp_configured_is_noop():
    assert cp_client.cp_url() is None
    assert cp_client.fetch_jwt() is None
    st = cp_client.status()
    assert st["mode"] == "shared"
    assert st["reason"] == "cp_not_configured"
    assert st["has_credentials"] is False


# ---- 정상 경로: 자동 발급 → 저장 → JWT ----
def test_provision_then_jwt(monkeypatch, tmp_path):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/provision/anonymous":
            return httpx.Response(200, json={
                "connector_id": "c_abc", "room_key": "rm_xyz",
                "credential_public_id": "pub_1", "secret": "cxs_s",
                "claim_secret": "clm_c"})
        if request.url.path == "/connector/token":
            body = json.loads(request.content)
            assert body == {"public_id": "pub_1", "secret": "cxs_s"}
            return httpx.Response(200, json={"token": "jwt.aaa.bbb",
                                             "expires_at": 9999999999,
                                             "room_key": "rm_xyz"})
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)
    out = cp_client.fetch_jwt()
    assert out["token"] == "jwt.aaa.bbb"
    assert out["room_key"] == "rm_xyz"
    # 자격증명이 로컬 파일로 저장됨(비밀은 이 PC 에만)
    saved = json.loads((tmp_path / "cp_credentials.json").read_text(encoding="utf-8"))
    assert saved["connector_id"] == "c_abc"
    assert saved["secret"] == "cxs_s"
    assert saved["claim_secret"] == "clm_c"
    st = cp_client.status()
    assert st["mode"] == "jwt" and st["reason"] == "ok"
    # 두 번째 호출은 재발급 없이 저장된 자격증명 사용
    calls.clear()
    cp_client.fetch_jwt()
    assert not any("provision" in c for c in calls)


# ---- ★CRITICAL 회귀★ CP 불통 → 예외 없이 None(fail-open) ----
def test_cp_down_fails_open_no_exception(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("CP down")

    _mock_client(monkeypatch, handler)
    out = cp_client.fetch_jwt()          # 예외를 던지면 커넥터가 죽는다 → 절대 금지
    assert out is None
    st = cp_client.status()
    assert st["mode"] == "fallback"
    assert st["reason"].startswith("provision_error")   # 무음 실패 금지: 이유 기록


def test_token_endpoint_5xx_fails_open(monkeypatch, tmp_path):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client.save_creds({"cp_url": "http://cp.test", "connector_id": "c_1",
                          "room_key": "rm_1", "credential_public_id": "pub_1",
                          "secret": "cxs_1"})

    _mock_client(monkeypatch, lambda r: httpx.Response(503))
    assert cp_client.fetch_jwt() is None
    assert cp_client.status()["mode"] == "fallback"
    assert cp_client.status()["reason"] == "token_http_503"


def test_rate_limited_provision_fails_open(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    _mock_client(monkeypatch, lambda r: httpx.Response(429, json={"error": "rate_limited"}))
    assert cp_client.fetch_jwt() is None
    assert cp_client.status()["reason"] == "provision_rate_limited"


def test_provision_5xx_fails_open(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    _mock_client(monkeypatch, lambda r: httpx.Response(500))
    assert cp_client.fetch_jwt() is None
    assert cp_client.status()["reason"] == "provision_http_500"


def test_token_network_error_fails_open(monkeypatch):
    """자격증명은 있는데 토큰 교환 중 네트워크 예외 → fallback."""
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client.save_creds({"cp_url": "http://cp.test", "connector_id": "c_1",
                          "room_key": "rm_1", "credential_public_id": "pub_1",
                          "secret": "cxs_1"})

    def handler(request):
        raise httpx.ReadTimeout("slow")

    _mock_client(monkeypatch, handler)
    assert cp_client.fetch_jwt() is None
    st = cp_client.status()
    assert st["mode"] == "fallback"
    assert st["reason"] == "token_error:ReadTimeout"
    assert st["connector_id"] == "c_1"


def test_reprovision_failure_after_401_fails_open(monkeypatch):
    """폐기(401) 후 재발급까지 실패하면 조용히 fallback."""
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client.save_creds({"cp_url": "http://cp.test", "connector_id": "c_old",
                          "room_key": "rm_old", "credential_public_id": "pub_old",
                          "secret": "cxs_old"})

    def handler(request):
        if request.url.path == "/connector/token":
            return httpx.Response(401, json={"error": "invalid_credential"})
        return httpx.Response(429, json={"error": "rate_limited"})   # 재발급도 실패

    _mock_client(monkeypatch, handler)
    assert cp_client.fetch_jwt() is None
    assert cp_client.status()["mode"] == "fallback"


def test_token_error_after_successful_reprovision_fails_open(monkeypatch):
    """★fail-open 마지막 분기★ 재발급은 됐는데 그 직후 토큰 호출이 끊겨도 예외 없이 fallback."""
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client.save_creds({"cp_url": "http://cp.test", "connector_id": "c_old",
                          "room_key": "rm_old", "credential_public_id": "pub_old",
                          "secret": "cxs_old"})
    state = {"provisioned": False}

    def handler(request):
        if request.url.path == "/provision/anonymous":
            state["provisioned"] = True
            return httpx.Response(200, json={
                "connector_id": "c_new", "room_key": "rm_new",
                "credential_public_id": "pub_new", "secret": "cxs_new",
                "claim_secret": "clm_new"})
        # 첫 토큰 호출은 401(폐기), 재발급 후 두 번째 호출은 네트워크 예외
        if state["provisioned"]:
            raise httpx.ConnectError("dropped")
        return httpx.Response(401, json={"error": "invalid_credential"})

    _mock_client(monkeypatch, handler)
    assert cp_client.fetch_jwt() is None          # 예외 누출 금지
    assert state["provisioned"] is True
    assert cp_client.status()["mode"] == "fallback"
    assert cp_client.status()["reason"].startswith("token_error")


def test_creds_ignored_when_cp_url_changed(monkeypatch, tmp_path):
    """CP 주소가 바뀌면 기존 자격증명을 쓰지 않고 새로 발급받는다."""
    monkeypatch.setenv("SM_CP_URL", "http://cp-new.test")
    cp_client.save_creds({"cp_url": "http://cp-old.test", "connector_id": "c_old",
                          "room_key": "rm_old", "credential_public_id": "pub_old",
                          "secret": "cxs_old"})
    seen = {"provisioned": False}

    def handler(request):
        if request.url.path == "/provision/anonymous":
            seen["provisioned"] = True
            return httpx.Response(200, json={
                "connector_id": "c_new", "room_key": "rm_new",
                "credential_public_id": "pub_new", "secret": "cxs_new",
                "claim_secret": "clm_new"})
        return httpx.Response(200, json={"token": "t", "expires_at": 1,
                                         "room_key": "rm_new"})

    _mock_client(monkeypatch, handler)
    cp_client.fetch_jwt()
    assert seen["provisioned"] is True
    assert cp_client.load_creds()["cp_url"] == "http://cp-new.test"


def test_load_creds_tolerates_corrupt_file(tmp_path, monkeypatch):
    """자격증명 파일이 깨져도 예외 없이 None(→ 재발급 경로)."""
    (tmp_path / "cp_credentials.json").write_text("{ not json", encoding="utf-8")
    assert cp_client.load_creds() is None


# ---- 자격증명 폐기 → 자동 재발급 ----
def test_revoked_credential_reprovisions(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client.save_creds({"cp_url": "http://cp.test", "connector_id": "c_old",
                          "room_key": "rm_old", "credential_public_id": "pub_old",
                          "secret": "cxs_old"})
    seen = {"provisioned": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/connector/token":
            body = json.loads(request.content)
            if body["public_id"] == "pub_old":
                return httpx.Response(401, json={"error": "invalid_credential"})
            return httpx.Response(200, json={"token": "jwt.new", "expires_at": 1,
                                             "room_key": "rm_new"})
        if request.url.path == "/provision/anonymous":
            seen["provisioned"] = True
            return httpx.Response(200, json={
                "connector_id": "c_new", "room_key": "rm_new",
                "credential_public_id": "pub_new", "secret": "cxs_new",
                "claim_secret": "clm_new"})
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)
    out = cp_client.fetch_jwt()
    assert seen["provisioned"] is True
    assert out["token"] == "jwt.new"
    assert cp_client.load_creds()["connector_id"] == "c_new"


# ---- ★회귀★ 로그가 콘솔 인코딩 때문에 기능을 깨뜨리지 않아야 한다 ----
def test_log_survives_unencodable_console(monkeypatch, capsys):
    """cp949 처럼 좁은 인코딩에서도 log() 는 예외를 던지지 않는다.

    T7 실사고: print('⚠') → UnicodeEncodeError → 커넥터 접속 루프 무한 크래시.
    """
    class NarrowStdout:
        encoding = "cp949"

        def write(self, s):
            s.encode("cp949")   # 인코딩 불가 문자면 예외
            return len(s)

        def flush(self):
            pass

    monkeypatch.setattr("sys.stdout", NarrowStdout())
    cp_client.log("[cp] 경고 ⚠ … ok")     # 예외가 새어나오면 실패
    from session_manager import connector as C
    C._log("[connector] ⚠ → ok")


def test_connector_and_cp_logs_are_console_safe():
    """실제 print()/log() 호출 인자에 cp949 로 인코딩 못 하는 글자를 넣지 못하게 막는다.

    AST 로 호출 인자만 검사한다(주석·docstring 은 대상 아님 — 설명 목적의 '⚠' 는 허용).
    """
    import ast
    import pathlib

    def literals(node):
        """호출 인자 안의 문자열 리터럴(f-string 포함)을 모두 수집."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub.value

    # 전 모듈 검사. 원래 connector/cp_client 만 보다가, server.py 의 print("… → …") 가
    # 빠져 있어 깨끗한 사용자 PC(전역 PYTHONIOENCODING 없음, cp949 스트림)에서
    # 서버가 기동 중 죽는 사고가 실제로 났다. 개발 PC 는 전역 env 때문에 재현이 안 됐다.
    targets = sorted(pathlib.Path("session_manager").glob("*.py")) \
        + sorted(pathlib.Path("relay").glob("*.py")) \
        + sorted(pathlib.Path("control_plane").glob("*.py"))
    for rel in targets:
        tree = ast.parse(pathlib.Path(rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name not in ("print", "log", "_log", "set_mode"):
                continue
            for arg in node.args:
                for text in literals(arg):
                    try:
                        text.encode("cp949")
                    except UnicodeEncodeError as e:
                        bad = e.object[e.start:e.end]
                        raise AssertionError(
                            f"{rel}:{node.lineno} {name}() 인자에 cp949 불가 문자 {bad!r} "
                            f"— 콘솔 인코딩 사고 재발 위험(ASCII 로 바꾸세요)")


# ---- ★회귀★ 신규 설치(room 없음)에서도 릴레이 모드가 켜져야 한다 ----
def test_relay_config_requires_room_or_cp(monkeypatch, tmp_path):
    """T8 실사고: 신규 사용자는 room 을 모른다(CP 가 발급). room 필수로 두면 릴레이가 꺼진다."""
    from session_manager import connector as C
    monkeypatch.setattr(cp_client.config, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("SM_RELAY_URL", "wss://relay.test/ws")
    monkeypatch.setenv("SM_RELAY_AGENT_TOKEN", "rly-agt-x")
    monkeypatch.delenv("SM_RELAY_ROOM", raising=False)

    # CP 도 없고 room 도 없으면 OFF (기존 동작 유지)
    monkeypatch.delenv("SM_CP_URL", raising=False)
    assert C.relay_config_from_env() is None

    # CP 가 있으면 room 없이도 ON (room 은 CP 가 발급)
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cfg = C.relay_config_from_env()
    assert cfg is not None and cfg["room"] == ""

    # 이미 발급받은 자격증명이 있으면 그 room 으로 시작
    cp_client.save_creds({"cp_url": "http://cp.test", "connector_id": "c_1",
                          "room_key": "rm_saved", "credential_public_id": "pub",
                          "secret": "cxs"})
    assert C.relay_config_from_env()["room"] == "rm_saved"


def test_manual_room_still_works_without_cp(monkeypatch):
    """기존 배포(수동 room + CP 없음)는 그대로 동작해야 한다."""
    from session_manager import connector as C
    monkeypatch.setenv("SM_RELAY_URL", "wss://relay.test/ws")
    monkeypatch.setenv("SM_RELAY_AGENT_TOKEN", "rly-agt-x")
    monkeypatch.setenv("SM_RELAY_ROOM", "home-manual")
    monkeypatch.delenv("SM_CP_URL", raising=False)
    cfg = C.relay_config_from_env()
    assert cfg["room"] == "home-manual"


# ---- 커넥터 URI 구성: JWT 첨부 / fallback ----
def _connector():
    from session_manager.connector import Connector
    return Connector(relay_url="ws://relay.test/ws", room="home-manual",
                     token="rly-agt-x", local_base="http://127.0.0.1:5100")


def test_build_uri_without_cp_keeps_shared_token(monkeypatch):
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert mode == "shared"
    assert "role=agent" in uri and "token=rly-agt-x" in uri
    assert "jwt=" not in uri
    assert "room=home-manual" in uri


def _jwt_with_room(room):
    return lambda display_name="": {"token": "JWTTOK", "expires_at": 1, "room_key": room}


def test_build_uri_attaches_jwt(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    monkeypatch.setattr(cp_client, "fetch_jwt", _jwt_with_room("home-manual"))
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert mode == "jwt"
    assert "jwt=JWTTOK" in uri
    assert "token=rly-agt-x" in uri        # 공유토큰도 함께(P1a: 릴레이는 아직 공유토큰 검증)
    assert "room=home-manual" in uri


def test_manual_room_is_not_migrated(monkeypatch):
    """★회귀★ 수동 room 이 있으면 CP 발급 room 으로 이사하지 않는다.

    이사하면 기존에 페어링된 폰·QR(구 room)이 무음으로 끊긴다(T7 에서 실제 발생).
    """
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    monkeypatch.setenv("SM_RELAY_ROOM", "home-manual")
    monkeypatch.delenv("SM_CP_ADOPT_ROOM", raising=False)
    cp_client.clear_room_mismatch()
    monkeypatch.setattr(cp_client, "fetch_jwt", _jwt_with_room("rm_from_cp"))
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert mode == "jwt"
    assert "room=home-manual" in uri and "rm_from_cp" not in uri
    assert c.room == "home-manual"
    # 무음 실패 금지: 불일치가 상태로 노출되어야 한다
    mm = cp_client.status().get("room_mismatch")
    assert mm and mm["cp_room"] == "rm_from_cp" and mm["used_room"] == "home-manual"


def test_room_adopted_when_no_manual_room(monkeypatch):
    """신규 사용자(수동 room 없음)는 CP 발급 room 을 그대로 채택."""
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    monkeypatch.delenv("SM_RELAY_ROOM", raising=False)
    monkeypatch.setattr(cp_client, "fetch_jwt", _jwt_with_room("rm_from_cp"))
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert "room=rm_from_cp" in uri
    assert c.room == "rm_from_cp"


def test_room_adopted_when_opt_in(monkeypatch):
    """명시적 옵트인(SM_CP_ADOPT_ROOM=1)이면 이사한다."""
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    monkeypatch.setenv("SM_RELAY_ROOM", "home-manual")
    monkeypatch.setenv("SM_CP_ADOPT_ROOM", "1")
    monkeypatch.setattr(cp_client, "fetch_jwt", _jwt_with_room("rm_from_cp"))
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert "room=rm_from_cp" in uri
    assert c.room == "rm_from_cp"


def test_build_uri_fails_open_when_cp_down(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    monkeypatch.setattr(cp_client, "fetch_jwt", lambda display_name="": None)
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert mode == "fallback"
    assert "jwt=" not in uri
    assert "token=rly-agt-x" in uri and "room=home-manual" in uri   # 기존 경로 그대로


def test_build_uri_survives_cp_exception(monkeypatch):
    """★회귀★ CP 코드가 예외를 던져도 접속 URI 는 만들어져야 한다."""
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")

    def boom(display_name=""):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cp_client, "fetch_jwt", boom)
    c = _connector()
    uri, mode = asyncio.run(c._build_uri())
    assert mode == "fallback"
    assert "token=rly-agt-x" in uri
