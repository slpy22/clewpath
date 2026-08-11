"""FastAPI 서버 — ClewPath Host 백엔드 + 정적 UI 서빙."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, UploadFile, File, Form, Body, WebSocket, Request
from fastapi.responses import (
    FileResponse, JSONResponse, HTMLResponse, RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class LabelReq(BaseModel):
    """라벨 설정 요청 바디. 제공한 필드만 갱신된다."""
    labels: list[str] | None = None
    name: str | None = None
    color: str | None = None
    note: str | None = None

    def to_fields(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class AskReq(BaseModel):
    """일회성 fork 질의 요청 바디."""
    prompt: str
    skip_permissions: bool = True
    timeout: int = 300


class TitleReq(BaseModel):
    """네이티브 세션 이름(Ctrl+R custom-title) 설정 바디."""
    title: str
    force: bool = False  # 실행 중(busy) 세션 강제 여부


class ProfileReq(BaseModel):
    """세션 가드레일 프로파일 설정 바디(제공한 필드만 갱신)."""
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    model: str | None = None
    max_budget_usd: float | None = None
    system_prompt: str | None = None
    deny_patterns: list[str] | None = None
    max_prompt_chars: int | None = None
    max_output_chars: int | None = None

    def to_fields(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}

from session_manager import (
    scanner, viewer, search, stats, lifecycle, exporter, importer, resume,
    webterm, webapi, auth, labels, policy, profiles, native_title, owner2fa,
    devices, appconfig, config,
)

STATIC_DIR = Path(__file__).parent / "static"

# 서브경로 서빙 prefix. 예: nginx 에서 /session_mgr 로 리버스프록시하면 SM_BASE_PATH=/session_mgr.
# 로컬 루트 서빙이면 "" (기본). 프론트엔드 HTML 의 {{BASE}} 와 리다이렉트/쿠키 경로에 쓰인다.
BASE_PATH = os.environ.get("SM_BASE_PATH", "").rstrip("/")
COOKIE_PATH = (BASE_PATH + "/") if BASE_PATH else "/"

# 루프백(로컬) 요청은 인증 생략. SM_TRUST_LOCAL=0 이면 끔.
# 서버가 127.0.0.1 에만 바인딩되고, 외부는 nginx(도커 브리지 IP)로 들어오므로
# 클라이언트 IP 가 루프백이면 '이 PC 로컬 프로세스'임이 보장된다(TCP 소켓 주소, 위조 불가).
TRUST_LOCAL = os.environ.get("SM_TRUST_LOCAL", "1") != "0"
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _client_host(scope_or_ws) -> str | None:
    c = getattr(scope_or_ws, "client", None)
    if c is not None:  # WebSocket
        return getattr(c, "host", None)
    c = scope_or_ws.get("client") if isinstance(scope_or_ws, dict) else None
    return c[0] if c else None


def _is_local(scope_or_ws) -> bool:
    return TRUST_LOCAL and (_client_host(scope_or_ws) in _LOOPBACK)


def _display_title(md: dict, rec: dict | None = None) -> str:
    """표시 이름. 네이티브 custom-title(Ctrl+R) → AI제목 → slug → id 순.

    이름은 네이티브 custom-title 단일 소스. 사이드카는 태그(labels)만 담당.
    """
    return (md.get("custom_title")
            or md.get("ai_title")
            or md.get("slug")
            or md["session_id"][:8])


def _render_html(filename: str) -> str:
    """정적 HTML 을 읽어 {{BASE}} 를 실제 prefix 로 치환해 반환한다."""
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    return html.replace("{{BASE}}", BASE_PATH)


class AuthASGIMiddleware:
    """인증 미들웨어(순수 ASGI). 요청 바디를 절대 읽지 않아 다운스트림 body 파싱을 깨지 않는다.

    BaseHTTPMiddleware(@app.middleware) 는 uvicorn 에서 POST 바디를 소비해
    'error parsing the body' 를 유발하므로, scope(헤더/쿠키/쿼리)만 보고 판단한다.
    """

    def __init__(self, app, base_path: str = ""):
        self.app = app
        self.base_path = base_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)  # websocket 등은 통과

        # 로컬(루프백) 요청은 인증 생략
        if _is_local(scope):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        query = parse_qs(scope.get("query_string", b"").decode("latin1"))

        async def deny_json(status):
            await JSONResponse({"error": "unauthorized"}, status_code=status)(
                scope, receive, send)

        # 외부 API v1: API 토큰 전용
        if path.startswith("/api/v1/"):
            if not auth.api_enabled():
                return await JSONResponse(
                    {"error": "API 비활성(SM_API_TOKEN 미설정)"}, status_code=503
                )(scope, receive, send)
            tok = (query.get("token", [None])[0]
                   or auth.bearer(headers.get("authorization")))
            if auth.valid_api_token(tok):
                return await self.app(scope, receive, send)
            return await deny_json(401)

        # 웹 UI: 비밀번호 쿠키
        if not auth.enabled():
            return await self.app(scope, receive, send)
        if path in ("/login", "/favicon.ico"):
            return await self.app(scope, receive, send)
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        token = cookies[auth.COOKIE].value if auth.COOKIE in cookies else None
        if auth.valid(token):
            return await self.app(scope, receive, send)
        if path.startswith("/api/"):
            return await deny_json(401)
        return await RedirectResponse(
            f"{self.base_path}/login", status_code=303)(scope, receive, send)


# 기동한 uvicorn 서버 인스턴스. 업데이트 적용 시 스스로 내려가기 위해 붙잡아둔다.
_SERVER = None


def _shutdown_soon(delay: float = 1.5, hard_after: float = 15.0) -> None:
    """응답을 흘려보낸 뒤 스스로 종료한다.

    바로 죽으면 방금 만든 HTTP 응답이 클라이언트에 안 닿는다 → 잠깐 기다린다.
    graceful 종료가 매달리는 경우(웹터미널·릴레이 WS 가 붙잡을 수 있다)를 대비해
    hard_after 초 뒤에는 _exit 로 확실히 끝낸다. 어차피 교체될 프로세스다.
    """
    import threading

    def run():
        time.sleep(delay)
        if _SERVER is not None:
            _SERVER.should_exit = True
        time.sleep(hard_after)
        os._exit(0)

    threading.Thread(target=run, daemon=True).start()


async def _update_watch():
    """새 버전이 있는지 주기적으로 확인한다. 기본은 '알리기만' 하고 설치는 안 한다.

    확인은 스레드로 돌린다 — urllib 은 블로킹이라 이벤트 루프에서 직접 부르면
    그동안 세션 목록·터미널 스트림이 전부 멈춘다.
    체크가 실패해도 서비스에는 영향이 없어야 하므로 예외를 전부 삼킨다.
    """
    from session_manager import updater
    interval = max(1, appconfig.get_int("update", "check_interval_hours", 24)) * 3600
    auto = appconfig.get_bool("update", "auto_apply", False)
    on_restart = appconfig.get_bool("update", "apply_on_restart", True)
    # 운영자 소스 저장소에서는 자동적용 금지 — 자기 소스 트리(git)를 덮어쓰는 사고 방지.
    if updater.is_dev_checkout():
        on_restart = False
        auto = False
    await asyncio.sleep(20)          # 기동 직후는 붐빈다 — 조금 있다가 확인

    # 재기동 시 자동 적용: 새 버전이 있으면 이번 기동에서 바로 갈아끼운다.
    # 재기동은 사용자의 명시적 행동이므로(주기 백그라운드 교체보다 안전) 기본 ON.
    # 단, 방금 실패해 롤백된 버전은 건너뛴다(무한 롤백 루프 방지).
    if on_restart:
        try:
            st = await asyncio.to_thread(updater.check)
            latest = st.get("latest")
            if st.get("available") and latest and not updater.recently_failed(latest):
                print(f"[update] 재기동 자동적용: {st.get('current')} -> {latest}", flush=True)
                staged = await asyncio.to_thread(updater.check_and_stage)
                if staged.get("staged"):
                    m = updater.verify_manifest(staged["manifest"])
                    updater.apply(m, Path(staged["staged"]))
                    return   # updater 가 우리를 죽이고 새 버전으로 재기동한다
            elif st.get("available") and updater.recently_failed(latest):
                print(f"[update] {latest} 는 방금 실패해 롤백됨 - 재적용 건너뜀", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[update] 재기동 자동적용 확인 실패(무시): {type(e).__name__}: {e}", flush=True)

    while True:
        try:
            st = await asyncio.to_thread(updater.check)
            if st.get("available"):
                print(f"[update] 새 버전 {st.get('latest')} 이 있습니다 "
                      f"(현재 {st.get('current')})", flush=True)
                if auto:
                    print("[update] auto_apply=true - 지금 적용합니다.", flush=True)
                    staged = await asyncio.to_thread(updater.check_and_stage)
                    if staged.get("staged"):
                        m = updater.verify_manifest(staged["manifest"])
                        updater.apply(m, Path(staged["staged"]))
            elif st.get("error"):
                print(f"[update] 확인 실패: {st['error']}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[update] 확인 중 오류(무시): {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """서버 기동 시 (설정돼 있으면) 릴레이 모드를 내부 태스크로 함께 띄운다.

    통합 배포: 006 프로세스 하나가 로컬 UI 서빙 + 릴레이 아웃바운드 접속을 동시에.
    릴레이 로직은 connector 모듈에 격리돼 있고, 006은 오직 127.0.0.1 로만 자신을 부른다
    (경계 유지 → 필요해지면 별도 프로세스로 다시 분리 가능).
    """
    # 기동 시 '무엇을 찾았는지' 를 남긴다 — 세션 0개일 때 원인 파악의 출발점.
    try:
        from session_manager import config as _cfg, scanner as _sc
        _home = _cfg.claude_home()
        _n = len(_sc.scan_all())
        print(f"[data] claude_home={_home} (exists={_home.is_dir()}) sessions={_n}")
        if _n == 0:
            print("[data] 세션이 0개입니다. 클로드 폴더를 옮겨 쓴다면 CLAUDE_CONFIG_DIR 을 "
                  "설정하고 다시 시작하세요.")
        print(f"[data] claude_exe={_cfg.claude_exe()}")
    except Exception as e:  # noqa: BLE001
        print(f"[data] 점검 실패: {type(e).__name__}: {e}")

    upd_task = None
    if appconfig.get_bool("update", "auto_check", True):
        upd_task = asyncio.create_task(_update_watch())

    from session_manager import connector as _connector
    cfg = _connector.relay_config_from_env()
    task = None
    if cfg:
        task = asyncio.create_task(_connector.Connector(**cfg).run())
        print(f"[relay] mode ON → {cfg['relay_url']} room={cfg['room']} "
              f"(local {cfg['local_base']})")
    else:
        print("[relay] mode OFF (SM_RELAY_URL/ROOM/AGENT_TOKEN 미설정)")
    try:
        yield
    finally:
        for t in (task, upd_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


def create_app() -> FastAPI:
    app = FastAPI(title="ClewPath Host", lifespan=_lifespan)

    # ---- 인증(비밀번호 게이트 + API 토큰) ----
    # 순수 ASGI 미들웨어: 바디를 읽지 않아 POST body 파싱을 깨지 않는다.
    app.add_middleware(AuthASGIMiddleware, base_path=BASE_PATH)

    @app.get("/login")
    def login_page():
        return HTMLResponse(auth.login_html(BASE_PATH))

    @app.post("/login")
    def login(password: str = Form(...)):
        if auth.check_password(password):
            resp = RedirectResponse(f"{BASE_PATH}/", status_code=303)
            resp.set_cookie(auth.COOKIE, auth.issue_token(),
                            httponly=True, samesite="lax", path=COOKIE_PATH)
            return resp
        return HTMLResponse(auth.login_html(BASE_PATH, error=True), status_code=401)

    @app.get("/api/owner/diagnostics")
    def owner_diagnostics():
        """설치가 제대로 됐는지 한눈에 — 폴더를 찾았나, 세션이 보이나, claude 가 있나."""
        from session_manager import config as _cfg
        import shutil
        home = _cfg.claude_home()
        sessions = scanner.scan_all()
        exe = _cfg.claude_exe()
        from session_manager import updater as _upd
        return {
            "version": _upd.current_version(),
            "claude_home": str(home),
            "claude_home_exists": home.is_dir(),
            "projects_exists": (home / "projects").is_dir(),
            "session_count": len(sessions),
            "claude_exe": exe,
            "claude_exe_found": bool(shutil.which("claude")) or exe != "claude",
            "claude_config_dir_env": os.environ.get("CLAUDE_CONFIG_DIR"),
            # 포트를 바꾸려는 사용자가 파일을 어디서 찾아야 하는지 — 여기서만 알 수 있다.
            # (설치 폴더가 아니라 데이터 폴더에 있어서 눈에 잘 안 띈다)
            "config_file": str(appconfig.config_path()),
            "config_file_exists": appconfig.config_path().is_file(),
            "ok": home.is_dir() and len(sessions) > 0,
        }

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    def _with_labels(meta_dict: dict, recs: dict) -> dict:
        """세션 dict 에 라벨 레코드를 머지한다(labels/name/color/note). recs 는 1회 로드본."""
        rec = recs.get(meta_dict["session_id"], {})
        meta_dict["labels"] = rec.get("labels", [])
        meta_dict["label_name"] = rec.get("name")
        meta_dict["label_color"] = rec.get("color")
        meta_dict["title"] = _display_title(meta_dict, rec)
        return meta_dict

    # ---- 목록 / 통계 ----
    @app.get("/api/sessions")
    def list_sessions():
        sessions = scanner.scan_all()
        recs = labels.all_records()  # 라벨은 1회만 로드(세션마다 파일 읽지 않음)
        groups = scanner.group_by_project(sessions)
        project_list = []
        for folder, items in groups.items():
            items_sorted = sorted(items, key=lambda m: m.mtime, reverse=True)
            project_list.append({
                "project_folder": folder,
                "cwd_samples": sorted({s.cwd for s in items if s.cwd}),
                "session_count": len(items),
                "total_size_bytes": sum(s.size_bytes for s in items),
                "latest_mtime": max(s.mtime for s in items),
                "sessions": [_with_labels(s.to_dict(), recs) for s in items_sorted],
            })
        project_list.sort(key=lambda p: p["latest_mtime"], reverse=True)
        return {
            "total_sessions": len(sessions),
            "total_projects": len(project_list),
            "total_size_bytes": sum(s.size_bytes for s in sessions),
            "projects": project_list,
        }

    # ---- 라벨 (웹, 쿠키 인증) ----
    @app.get("/api/labels")
    def get_label_counts():
        return {"labels": labels.label_counts()}

    @app.post("/api/sessions/{session_id}/labels")
    def set_session_labels(session_id: str, body: LabelReq):
        rec = labels.set_record(session_id, **body.to_fields())
        return {"session_id": session_id, "record": rec}

    # 네이티브 이름(Ctrl+R custom-title) 설정 — JSONL 에 append + 사이드카 미러
    @app.post("/api/sessions/{session_id}/title")
    def set_native_title_web(session_id: str, body: TitleReq):
        r = native_title.set_native_title(session_id, body.title,
                                          allow_live=body.force)
        code = 200 if r.get("ok") else (409 if r.get("error") == "session_live" else 400)
        return JSONResponse(r, status_code=code)

    # ---- 세션 가드레일 프로파일 (웹, 쿠키 인증) ----
    @app.get("/api/sessions/{session_id}/profile")
    def get_session_profile(session_id: str):
        return {"session_id": session_id, "profile": profiles.get(session_id),
                "effective": policy.effective_guardrails(session_id)}

    @app.post("/api/sessions/{session_id}/profile")
    def set_session_profile(session_id: str, body: ProfileReq):
        rec = profiles.set_record(session_id, **body.to_fields())
        return {"session_id": session_id, "profile": rec}

    @app.get("/api/stats")
    def get_global_stats():
        return stats.global_stats()

    @app.get("/api/usage")
    def get_usage():
        return policy.usage_summary()

    # ---- 소유자 2FA(TOTP) — in-place 특권 재개 보호 ----
    @app.get("/api/owner/2fa/status")
    def owner_2fa_status():
        return {"required": owner2fa.required(), "has_secret": owner2fa.has_secret()}

    @app.post("/api/owner/2fa/toggle")
    def owner_2fa_toggle(request: Request, on: bool = Body(..., embed=True)):
        # on/off 전환. 켜기(on)는 비밀이 있어야 가능(없으면 provision 필요). 로컬 전용.
        if not _is_local(request):
            return JSONResponse({"error": "이 PC(로컬)에서만 변경할 수 있습니다."},
                                status_code=403)
        ok = owner2fa.set_enforced(on)
        if not ok:
            return JSONResponse({"error": "need_provision"}, status_code=409)
        return {"required": owner2fa.required(), "has_secret": owner2fa.has_secret()}

    @app.post("/api/owner/2fa/provision")
    def owner_2fa_provision(request: Request):
        # 비밀 노출 → 이 PC(루프백)에서만 허용. 외부에서 설정 불가.
        if not _is_local(request):
            return JSONResponse({"error": "이 기능은 이 PC(로컬)에서만 설정할 수 있습니다."},
                                status_code=403)
        r = owner2fa.provision()
        # otpauth URI 의 QR(스캔용). 스캐너 호환 위해 항상 검정/흰색.
        try:
            import segno
            qr = segno.make(r["otpauth"], error="m")
            r["qr_svg"] = qr.svg_data_uri(scale=5, border=2,
                                          dark="#000000", light="#ffffff")
        except Exception:  # noqa: BLE001
            r["qr_svg"] = None
        return r

    # ---- 연결 모드(컨트롤플레인/JWT vs 공유토큰 fallback) — 로컬 웹 배지용 ----
    @app.get("/api/owner/connection")
    def owner_connection():
        from session_manager import cp_client
        st = cp_client.status()
        return {
            "mode": st.get("mode"),            # jwt | fallback | shared
            "reason": st.get("reason"),
            "cp_url": st.get("cp_url"),
            "has_credentials": st.get("has_credentials"),
            "connector_id": st.get("connector_id"),
            "room_key": st.get("room_key"),
            "jwt_exp": st.get("jwt_exp"),
            "checked_at": st.get("checked_at"),
            "relay_room": os.environ.get("SM_RELAY_ROOM"),   # 수동 지정값(레거시). 없으면 CP 발급
            "active_room": _active_room(),                   # 실제 접속/QR 에 쓰이는 room
            "room_mismatch": st.get("room_mismatch"),
            "hostname": appconfig.machine_name(),            # 헤더에 표시할 이 PC 이름
            "app_url": os.environ.get("SM_APP_URL")           # 릴레이 앱 주소(로컬→릴레이 이동용)
                        or "https://clewpath.pyongso.com/relay/app",
        }

    # ---- 기기 페어링/폐기 (외부 릴레이 클라이언트의 기기별 인증) ----
    def _active_room() -> str | None:
        """지금 커넥터가 실제로 붙어 있는 room.

        표준 설계에서는 컨트롤플레인이 room 을 발급하므로 자격증명 파일이 권위 소스다.
        SM_RELAY_ROOM 은 CP 를 안 쓰는(레거시/오프라인) 배포용 폴백.
        QR 이 실제 room 과 달라지면 폰이 빈 방에 접속하게 되므로 이 순서가 중요하다.
        """
        try:
            from session_manager import cp_client
            if cp_client.cp_url():
                creds = cp_client.load_creds() or {}
                if creds.get("room_key"):
                    return creds["room_key"]
        except Exception:  # noqa: BLE001
            pass
        return os.environ.get("SM_RELAY_ROOM")

    def _pair_link(dev_token: str, client_cred: dict | None = None) -> str | None:
        """QR/딥링크: 스캔 시 PWA 가 room+자격+기기토큰으로 자동 페어링.

        client_cred 가 있으면 **기기 전용 자격증명**(cs/cp)을 싣는다 — 공유 client 토큰을
        나눠주지 않아도 되고, 유출 범위가 그 기기 하나로 국한된다.
        없으면(CP 미설정·발급 실패) 기존 공유 토큰으로 폴백.
        """
        app_url = os.environ.get("SM_APP_URL")          # 예: https://clewpath.pyongso.com/relay/app
        room = _active_room()
        cli = os.environ.get("SM_RELAY_CLIENT_TOKEN")   # 릴레이 접속용 공유 토큰(전송계층)
        if not (app_url and room):
            return None
        from urllib.parse import urlencode
        q = {"room": room, "dev": dev_token}
        if client_cred:
            q["cp"] = client_cred["client_public_id"]
            q["cs"] = client_cred["client_secret"]
        elif cli:
            q["token"] = cli
        return f"{app_url}?{urlencode(q)}"

    # ---- 자동 업데이트 ----
    # 확인은 누구나(로컬 UI), 적용은 **로컬 전용**이다. 폰에서 원격으로 코드 교체를
    # 시킬 수 있으면 기기 토큰 하나가 새는 순간 임의 코드 실행이 된다.
    @app.get("/api/owner/update/status")
    def owner_update_status(refresh: bool = False):
        from session_manager import updater
        if refresh:
            # 캐시(최근 백그라운드 체크, 기본 24h 주기) 대신 지금 확인 — 방금 게시된
            # 새 버전도 화면 열자마자 감지. CP 불통이면 조용히 캐시로 폴백.
            try:
                updater.check(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        st = updater.read_state()
        st["current"] = updater.current_version()
        st["signing_configured"] = bool(updater.RELEASE_KEYS)
        st["dev"] = updater.is_dev_checkout()   # 개발 소스면 자동적용 불가 → UI 가 안내만
        try:
            last = json.loads((updater.work_dir() / "last_apply.json")
                              .read_text(encoding="utf-8"))
            st["last_apply"] = last
        except Exception:  # noqa: BLE001
            pass
        return st

    @app.post("/api/owner/update/check")
    def owner_update_check():
        from session_manager import updater
        return updater.check()

    @app.post("/api/owner/update/apply")
    def owner_update_apply(request: Request):
        from session_manager import updater
        if not _is_local(request):
            return JSONResponse({"error": "업데이트 적용은 이 PC(로컬)에서만 가능합니다."},
                                status_code=403)
        if not updater.RELEASE_KEYS:
            return JSONResponse({"error": "이 빌드에는 릴리스 공개키가 없어 "
                                          "업데이트를 적용할 수 없습니다."}, status_code=400)
        if updater.is_dev_checkout():
            return JSONResponse({"error": "개발 소스에서 실행 중입니다 — 자동 적용이 소스 트리를 "
                                          "덮어쓸 수 있어 막았습니다. git 으로 갱신하세요."},
                                status_code=400)
        try:
            st = updater.check_and_stage()
            if not st.get("staged"):
                return {"applied": False, "reason": st.get("error") or "최신 버전입니다",
                        **st}
            token = st["manifest"]
            m = updater.verify_manifest(token)
            updater.apply(m, Path(st["staged"]))
        except updater.UpdateError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
        # 스스로 물러난다. 안 그러면 updater 가 60초를 기다렸다가 강제 종료하는데,
        # 그동안 서비스가 멈춰 있고 종료도 거칠어진다(실측으로 확인).
        _shutdown_soon()
        return {"applied": True, "version": m["ver"],
                "message": "적용 중입니다. 30초쯤 뒤 새로고침하세요."}

    @app.get("/api/owner/devices")
    def owner_devices_list():
        return {"devices": devices.list_devices(), **devices.status()}

    @app.post("/api/owner/devices")
    def owner_devices_add(request: Request, name: str = Body("", embed=True)):
        # 새 기기 등록 → 1회성 토큰 노출. 로컬 전용.
        if not _is_local(request):
            return JSONResponse({"error": "이 PC(로컬)에서만 기기를 추가할 수 있습니다."},
                                status_code=403)
        r = devices.add_device(name)
        # 이 기기 전용 client 자격증명을 CP 에서 발급(실패해도 공유 토큰으로 폴백)
        from session_manager import cp_client
        cc = cp_client.issue_client_credential(device_id=r["id"], name=name or "")
        if cc:
            devices.set_client_public_id(r["id"], cc["client_public_id"])
        r["client_scoped"] = bool(cc)
        link = _pair_link(r["token"], cc)
        r["pair_url"] = link
        r["room"] = _active_room()      # 수동 입력용 — 실제 접속 room 과 반드시 동일해야
        try:
            import segno
            payload = link or r["token"]
            qr = segno.make(payload, error="m")
            r["qr_svg"] = qr.svg_data_uri(scale=5, border=2,
                                          dark="#000000", light="#ffffff")
        except Exception:  # noqa: BLE001
            r["qr_svg"] = None
        return r

    @app.post("/api/owner/devices/{device_id}/reissue")
    def owner_devices_reissue(device_id: str, request: Request):
        """기기의 QR/링크를 다시 만든다(이전 QR 은 무효).

        비밀을 해시로만 보관하므로 '원래 QR 재표시'는 불가능하다 → 회전 발급한다.
        """
        if not _is_local(request):
            return JSONResponse({"error": "이 PC(로컬)에서만 변경할 수 있습니다."},
                                status_code=403)
        tok = devices.rotate_token(device_id)
        if not tok:
            return JSONResponse({"error": "not_found_or_revoked"}, status_code=404)
        name = next((d["name"] for d in devices.list_devices()
                     if d["id"] == device_id), "")
        from session_manager import cp_client
        old_cpub = devices.get_client_public_id(device_id)
        if old_cpub:
            cp_client.revoke_client_credential(old_cpub)   # 이전 자격 무효화
        cc = cp_client.issue_client_credential(device_id=device_id, name=name)
        if cc:
            devices.set_client_public_id(device_id, cc["client_public_id"])
        r = {"id": device_id, "name": name, "token": tok,
             "client_scoped": bool(cc), "room": _active_room()}
        r["pair_url"] = _pair_link(tok, cc)
        try:
            import segno
            qr = segno.make(r["pair_url"] or tok, error="m")
            r["qr_svg"] = qr.svg_data_uri(scale=5, border=2,
                                          dark="#000000", light="#ffffff")
        except Exception:  # noqa: BLE001
            r["qr_svg"] = None
        return r

    @app.post("/api/owner/devices/{device_id}/revoke")
    def owner_devices_revoke(device_id: str, request: Request):
        if not _is_local(request):
            return JSONResponse({"error": "이 PC(로컬)에서만 변경할 수 있습니다."},
                                status_code=403)
        return {"revoked": devices.revoke(device_id), **devices.status()}

    @app.post("/api/owner/devices/{device_id}/delete")
    def owner_devices_delete(device_id: str, request: Request):
        # 레지스트리에서 완전 삭제(토큰 무효화). 로컬 전용.
        if not _is_local(request):
            return JSONResponse({"error": "이 PC(로컬)에서만 변경할 수 있습니다."},
                                status_code=403)
        # 기기 전용 client 자격증명도 함께 회수(안 하면 QR 이 계속 유효)
        cpub = devices.get_client_public_id(device_id)
        if cpub:
            from session_manager import cp_client
            cp_client.revoke_client_credential(cpub)
        return {"deleted": devices.remove(device_id), **devices.status()}

    @app.post("/api/owner/devices/{device_id}/rename")
    def owner_devices_rename(device_id: str, request: Request,
                             name: str = Body(..., embed=True)):
        if not _is_local(request):
            return JSONResponse({"error": "이 PC(로컬)에서만 변경할 수 있습니다."},
                                status_code=403)
        return {"renamed": devices.rename(device_id, name)}

    @app.get("/api/sessions/{session_id}/stats")
    def get_session_stats(session_id: str):
        r = stats.session_stats(session_id)
        return r or JSONResponse({"error": "not found"}, status_code=404)

    # ---- 뷰어 / 검색 ----
    @app.get("/api/sessions/{session_id}/messages")
    def get_messages(session_id: str, offset: int = 0, limit: int = 200):
        r = viewer.read_messages(session_id, offset=offset, limit=limit)
        return r or JSONResponse({"error": "not found"}, status_code=404)

    # 깔끔한 대화만(메타/도구인자/thinking 제외) — 모바일 뷰어·재개 이력용
    @app.get("/api/sessions/{session_id}/conversation")
    def get_conversation(session_id: str, limit: int = 200):
        r = viewer.read_conversation(session_id, limit=limit)
        return r or JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/search")
    def do_search(q: str = "", date_from: str | None = None, date_to: str | None = None):
        return search.search_sessions(q, date_from, date_to)

    # ---- 삭제 / 폴더변경 ----
    @app.post("/api/sessions/{session_id}/delete")
    def delete(session_id: str, dry_run: bool = Body(True, embed=True)):
        return lifecycle.delete_session(session_id, dry_run=dry_run)

    @app.post("/api/sessions/{session_id}/change-cwd")
    def change_cwd(session_id: str,
                   new_cwd: str = Body(..., embed=True),
                   dry_run: bool = Body(True, embed=True)):
        return lifecycle.change_cwd(session_id, new_cwd, dry_run=dry_run)

    # ---- export / import ----
    @app.get("/api/sessions/{session_id}/export")
    def export_session(session_id: str, include_workdir: bool = False):
        tmp = Path(tempfile.gettempdir()) / f"session_export_{session_id}.zip"
        r = exporter.create_export(session_id, str(tmp), include_workdir=include_workdir)
        if "error" in r:
            return JSONResponse(r, status_code=404)
        return FileResponse(tmp, filename=f"session_{session_id[:8]}.zip",
                            media_type="application/zip")

    @app.post("/api/import")
    async def import_session(file: UploadFile = File(...),
                             new_cwd: str | None = Form(None),
                             overwrite: bool = Form(False),
                             restore_workdir: bool = Form(False)):
        tmp = Path(tempfile.gettempdir()) / f"session_import_{file.filename}"
        with tmp.open("wb") as f:
            f.write(await file.read())
        r = importer.import_session(str(tmp), new_cwd=new_cwd or None,
                                    overwrite=overwrite, restore_workdir=restore_workdir)
        try:
            tmp.unlink()
        except Exception:
            pass
        status = 400 if "error" in r else 200
        return JSONResponse(r, status_code=status)

    # ---- 재개 ----
    @app.get("/api/sessions/{session_id}/resume-command")
    def resume_cmd(session_id: str, skip: bool = True):
        return resume.resume_command(session_id, skip_permissions=skip)

    @app.post("/api/sessions/{session_id}/resume-launch")
    def resume_launch(session_id: str, skip: bool = True):
        return resume.launch_terminal(session_id, skip_permissions=skip)

    # ---- 웹 터미널 (브라우저에서 세션 이어가기) ----
    @app.websocket("/ws/terminal/{session_id}")
    async def ws_terminal(websocket: WebSocket, session_id: str):
        # WebSocket 은 HTTP 미들웨어를 거치지 않으므로 여기서 직접 인증한다(로컬은 생략).
        if not _is_local(websocket) and auth.enabled() \
                and not auth.valid(websocket.cookies.get(auth.COOKIE)):
            await websocket.close(code=1008)  # policy violation
            return
        # ?skip=0 이면 권한확인 유지, 기본(생략/1)은 --dangerously-skip-permissions ON
        skip = websocket.query_params.get("skip", "1") not in ("0", "false", "False")
        # ?fork=<uuid> 이면 원본을 건드리지 않는 새 fork 세션으로 재개
        fork_id = websocket.query_params.get("fork") or None
        await websocket.accept()
        await webterm.run_terminal(websocket, session_id,
                                   skip_permissions=skip, fork_id=fork_id)

    # ---- 외부 API v1: 세션 재개(구조화 JSON, 멀티턴) ----
    # 인증: API 토큰(Authorization: Bearer <t> 또는 ?token=<t>). 웹 비밀번호와 별개.
    @app.websocket("/api/v1/resume/{session_id}")
    async def ws_api_resume(websocket: WebSocket, session_id: str):
        token = (websocket.query_params.get("token")
                 or auth.bearer(websocket.headers.get("authorization")))
        if not _is_local(websocket):
            if not auth.valid_api_token(token):
                await websocket.close(code=1008)  # 토큰 없음/불일치
                return
            ok, reason, _ = policy.check_quota(token)
            if not ok:
                policy.audit({"event": "resume", "token": policy.token_id(token),
                              "session": session_id, "blocked": reason})
                await websocket.close(code=1008)
                return
        skip = websocket.query_params.get("skip", "1") not in ("0", "false", "False")
        # 외부 API 세션 사용은 무조건 fork(원본 불변) + 위험 도구 하드 차단
        import uuid as _uuid
        fork_id = str(_uuid.uuid4())
        policy.record_usage(token, 0, count_call=True)  # 호출 수만 카운트
        policy.audit({"event": "resume", "token": policy.token_id(token),
                      "session": session_id, "fork": fork_id})

        def _finish(cost):
            # 멀티턴 누적 비용을 사용량·감사에 반영(호출수는 이미 셌으므로 비용만)
            policy.record_usage(token, cost, count_call=False)
            policy.audit({"event": "resume_end", "token": policy.token_id(token),
                          "session": session_id, "fork": fork_id, "cost_usd": cost})

        await websocket.accept()
        await webapi.run_resume_api(websocket, session_id, skip_permissions=skip,
                                    fork_id=fork_id,
                                    guardrails=policy.effective_guardrails(session_id),
                                    on_finish=_finish)

    # ---- 외부 API v1: 세션 in-place 재개(소유자용, fork 아님) ----
    # 원본 /api/v1/resume 는 untrusted 호출자용(강제 fork). 이건 소유자가 '실제 세션'을
    # 그대로 이어가는 특권 동작 → 가드레일 없이 full power(웹터미널과 동일).
    # ⚠ 보안: 지금은 추가 인증 없음. 향후 2차 인증(TOTP/오너토큰)은 아래 HOOK 위치에 추가.
    @app.websocket("/api/v1/resume-inplace/{session_id}")
    async def ws_api_resume_inplace(websocket: WebSocket, session_id: str):
        token = (websocket.query_params.get("token")
                 or auth.bearer(websocket.headers.get("authorization")))
        if not _is_local(websocket):
            if not auth.valid_api_token(token):
                await websocket.close(code=1008)
                return
            ok, reason, _ = policy.check_quota(token)
            if not ok:
                policy.audit({"event": "resume_inplace", "token": policy.token_id(token),
                              "session": session_id, "blocked": reason})
                await websocket.close(code=1008)
                return
        # ── SECURITY HOOK ──────────────────────────────────────────────
        # in-place 는 실제 세션을 조작하는 특권 동작. 여기에 2차 인증을 추가한다:
        #   otp = websocket.query_params.get("otp")
        #   if not auth.verify_owner_2fa(otp): await websocket.close(4403); return
        # ───────────────────────────────────────────────────────────────
        skip = websocket.query_params.get("skip", "1") not in ("0", "false", "False")
        policy.record_usage(token, 0, count_call=True)
        policy.audit({"event": "resume_inplace", "token": policy.token_id(token),
                      "session": session_id, "inplace": True})

        def _finish(cost):
            policy.record_usage(token, cost, count_call=False)
            policy.audit({"event": "resume_inplace_end", "token": policy.token_id(token),
                          "session": session_id, "cost_usd": cost})

        await websocket.accept()
        await webapi.run_resume_api(websocket, session_id, skip_permissions=skip,
                                    fork_id=None,       # ← in-place(실제 세션)
                                    guardrails=None,    # ← full power(소유자)
                                    on_finish=_finish)

    # ---- 외부 API v1: 세션 목록 조회 (HTTP, 토큰 인증) ----
    # ---- Claude 훅 이벤트 수신 (루프백 전용 - 인증 미들웨어가 로컬은 통과) ----
    @app.post("/api/hooks/event")
    async def hooks_event(request: Request):
        from session_manager import hooks
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad_json"}, status_code=400)
        if isinstance(payload, dict):
            hooks.update_from_event(payload)
            try:                       # 푸시는 부가 기능 - 실패해도 상태 반영은 유지
                from session_manager import push
                push.notify_from_event(payload)
            except Exception as e:  # noqa: BLE001
                print(f"[push] 알림 처리 실패: {e}", flush=True)
        return {"ok": True}

    # ---- 웹푸시 구독 (로컬/원격 PWA 공용 - 원격은 커넥터 /api/ 프록시 경유) ----
    @app.get("/api/owner/push/vapid")
    def push_vapid():
        from session_manager import push
        return {"key": push.ensure_vapid()}

    @app.post("/api/owner/push/subscribe")
    async def push_subscribe(request: Request):
        from session_manager import push
        body = await request.json()
        sub = body.get("subscription") if isinstance(body, dict) else None
        if not isinstance(sub, dict):
            return JSONResponse({"error": "subscription 필요"}, status_code=400)
        try:
            created = push.add_subscription(sub, str(body.get("name") or ""))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True, "created": created}

    @app.post("/api/owner/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        from session_manager import push
        body = await request.json()
        ep = str((body or {}).get("endpoint") or "")
        return {"ok": True, "removed": push.remove_subscription(ep)}

    @app.get("/api/owner/push/subscriptions")
    def push_subscriptions():
        from session_manager import push
        return {"subscriptions": push.list_subscriptions()}

    # ---- 원격 권한 승인: 실행 중인 ClewPath 터미널의 권한 프롬프트에 키 주입 ----
    @app.post("/api/owner/sessions/{session_id}/respond")
    async def owner_session_respond(session_id: str, request: Request):
        from session_manager import webterm
        body = await request.json()
        action = str((body or {}).get("action") or "")
        # claude TUI 권한 다이얼로그: 숫자키 즉시 선택, Esc = 거부.
        # (키맵은 claude 버전에 따라 바뀔 수 있음 - qa-checklist 에서 실물 검증)
        keys = {"allow": "1", "allow_session": "2", "deny": "\x1b"}
        if action not in keys:
            return JSONResponse({"error": "action 은 allow/allow_session/deny"},
                                status_code=400)
        if not webterm.write_to(session_id, keys[action]):
            return JSONResponse(
                {"error": "no_terminal",
                 "hint": "이 세션이 ClewPath 터미널(권한확인 유지 모드)로 실행 중일 때만 원격 승인이 가능합니다"},
                status_code=409)
        return {"ok": True, "sent": action}

    @app.post("/api/owner/push/test")
    def push_test():
        # 전달 경로(브라우저/OS 알림 설정 포함)를 사용자가 즉석 검증하는 용도.
        # dedupe 를 안 타도록 매번 다른 세션키를 쓴다.
        from session_manager import push
        n = push.send(f"test-{int(time.time())}", "test",
                      "ClewPath 알림 테스트", "이 알림이 보이면 정상입니다")
        return {"ok": True, "sent": n}

    @app.get("/api/v1/sessions")
    def api_v1_sessions(q: str | None = None, label: str | None = None,
                        limit: int = 0):
        from session_manager import hooks
        runtime = hooks.status_map()   # 훅이 채운 세션별 '지금' 상태
        sessions = scanner.scan_all()
        recs = labels.all_records()  # 라벨 1회 로드
        out = []
        for s in sessions:
            folder = Path(s.jsonl_path).parent.name
            if q:
                hay = f"{s.cwd or ''}{s.slug or ''}{folder}".lower()
                if q.lower() not in hay:
                    continue
            rec = recs.get(s.session_id, {})
            if label and label not in rec.get("labels", []):
                continue
            md = {"session_id": s.session_id, "custom_title": s.custom_title,
                  "ai_title": s.ai_title, "slug": s.slug}
            out.append({
                "session_id": s.session_id,
                "cwd": s.cwd,
                "project_folder": folder,
                "slug": s.slug,
                "custom_title": s.custom_title,
                "ai_title": s.ai_title,
                "agent_name": s.agent_name,
                "title": _display_title(md, rec),
                "git_branch": s.git_branch,
                "message_count": s.message_count,
                "size_bytes": s.size_bytes,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "mtime": s.mtime,
                "labels": rec.get("labels", []),
                "label_name": rec.get("name"),
                # 훅 기반 실시간 상태(없으면 None). 신선도 판정은 화면 쪽 책임.
                "runtime": runtime.get(s.session_id),
            })
        out.sort(key=lambda x: x["ended_at"] or "", reverse=True)
        if limit and limit > 0:
            out = out[:limit]
        # now: 클라이언트가 runtime 타임스탬프의 신선도를 판정할 때 서버 시계
        # 기준으로 보정하게 한다(폰과 PC 시계가 어긋나도 뱃지가 안 깨지게).
        return {"total": len(out), "sessions": out, "now": int(time.time())}

    # ---- 외부 API v1: 일회성 fork 질의 (원본 불변, fork 자동삭제) ----
    @app.post("/api/v1/sessions/{session_id}/ask")
    def api_v1_ask(session_id: str, body: AskReq, request: Request):
        token = (request.query_params.get("token")
                 or auth.bearer(request.headers.get("authorization")))
        ok, reason, _ = policy.check_quota(token)
        if not ok:
            policy.audit({"event": "ask", "token": policy.token_id(token),
                          "session": session_id, "blocked": reason})
            return JSONResponse({"error": reason}, status_code=429)

        g = policy.effective_guardrails(session_id)  # 전역 ∩ 세션 프로파일
        prompt = body.prompt or ""
        # 내용 필터(입력): 길이 제한 + 금지 정규식
        if g["max_prompt_chars"] and len(prompt) > g["max_prompt_chars"]:
            prompt = prompt[:g["max_prompt_chars"]]
        hit = policy.match_denied(prompt, g["deny_patterns"])
        if hit:
            policy.audit({"event": "ask", "token": policy.token_id(token),
                          "session": session_id, "blocked": f"prompt matched deny: {hit}"})
            return JSONResponse({"error": "프롬프트가 세션 정책의 금지 패턴에 걸렸습니다."},
                                status_code=400)

        r = webapi.ephemeral_ask(session_id, prompt, skip_permissions=True,
                                 timeout=body.timeout, guardrails=g)

        # 내용 필터(출력): 금지 정규식 → 차단, 길이 제한 → 절단
        ans = r.get("answer")
        if ans:
            out_hit = policy.match_denied(ans, g["deny_patterns"])
            if out_hit:
                r["answer"] = r["result"] = "[세션 정책에 의해 응답이 차단되었습니다]"
                r["filtered"] = True
            elif g["max_output_chars"] and len(ans) > g["max_output_chars"]:
                r["answer"] = ans[:g["max_output_chars"]]
                r["truncated"] = True

        policy.record_usage(token, r.get("cost_usd"))
        policy.audit({"event": "ask", "token": policy.token_id(token),
                      "session": session_id, "fork": r.get("fork_id"),
                      "prompt": prompt[:200], "cost_usd": r.get("cost_usd"),
                      "is_error": r.get("is_error"),
                      "profile": {k: g[k] for k in ("permission_mode", "allowed_tools",
                                  "model") if g[k]}})
        return JSONResponse(r, status_code=(404 if "error" in r else 200))

    @app.get("/api/v1/labels")
    def api_v1_labels():
        return {"labels": labels.label_counts()}

    # ---- 세션 가드레일 프로파일 (외부 API, 토큰) ----
    @app.get("/api/v1/sessions/{session_id}/profile")
    def api_v1_get_profile(session_id: str):
        return {"session_id": session_id, "profile": profiles.get(session_id),
                "effective": policy.effective_guardrails(session_id)}

    @app.post("/api/v1/sessions/{session_id}/profile")
    def api_v1_set_profile(session_id: str, body: ProfileReq):
        rec = profiles.set_record(session_id, **body.to_fields())
        return {"session_id": session_id, "profile": rec}

    @app.post("/api/v1/sessions/{session_id}/labels")
    def api_v1_set_labels(session_id: str, body: LabelReq):
        rec = labels.set_record(session_id, **body.to_fields())
        return {"session_id": session_id, "record": rec}

    @app.post("/api/v1/sessions/{session_id}/title")
    def api_v1_set_title(session_id: str, body: TitleReq):
        r = native_title.set_native_title(session_id, body.title,
                                          allow_live=body.force)
        code = 200 if r.get("ok") else (409 if r.get("error") == "session_live" else 400)
        return JSONResponse(r, status_code=code)

    @app.get("/terminal")
    def terminal_page():
        return HTMLResponse(_render_html("terminal.html"), headers=_NO_CACHE)

    # ---- UI ----
    _NO_CACHE = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    def _serve_unified():
        # 통일 웹앱(반응형 PWA) — 로컬 모드로 서빙. 릴레이도 같은 파일을 relay 모드로 서빙.
        pwa = Path(__file__).parent.parent / "pwa" / "index.html"
        html = (pwa.read_text(encoding="utf-8")
                .replace("{{MODE}}", "local")
                .replace("{{BASE}}", BASE_PATH))
        return HTMLResponse(html, headers=_NO_CACHE)

    @app.get("/")
    def index():
        return _serve_unified()          # / 도 이제 통일 앱

    @app.get("/app")
    def unified_app():
        return _serve_unified()

    @app.get("/legacy")
    def legacy_ui():
        # 옛 데스크톱 UI(호환/폴백용, export/import 등). 통일 앱이 파리티 채우면 은퇴.
        return HTMLResponse(_render_html("index.html"), headers=_NO_CACHE)

    @app.get("/favicon.ico")
    def favicon():
        # favicon 404 방지 (빈 응답)
        return JSONResponse({}, status_code=204)

    @app.get("/sw.js")
    def service_worker():
        # 웹푸시용 서비스워커. SW 는 반드시 별도 파일이어야 한다(스코프 규칙).
        f = Path(__file__).parent.parent / "pwa" / "sw.js"
        if not f.is_file():
            return JSONResponse({"error": "sw_missing"}, status_code=404)
        return FileResponse(f, media_type="text/javascript", headers=_NO_CACHE)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()


def _port_free(host: str, port: int) -> bool:
    """그 포트에 실제로 bind 가 되는지 본다.

    "연결이 되나" 로 확인하면 안 된다 — 방화벽·다른 인터페이스 때문에 오판한다.
    우리가 뜰 때와 같은 조건(같은 host)으로 bind 를 시도해 보는 게 유일하게 정확하다.
    SO_REUSEADDR 은 켜지 않는다. 켜면 남이 쓰는 포트도 비어 보일 수 있다.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _clewpath_already_there(host: str, port: int) -> bool:
    """그 포트를 잡고 있는 게 우리 자신인지 확인한다.

    이게 없으면 이미 떠 있는 ClewPath 위로 두 번째 인스턴스가 옆 포트에 뜬다.
    사용자는 왜 북마크가 안 열리는지 모른 채 두 개가 같은 세션 폴더를 만지게 된다.
    """
    import json
    import urllib.request
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with urllib.request.urlopen(f"http://{probe}:{port}/api/owner/diagnostics",
                                    timeout=2) as r:
            return "claude_home" in json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False


def _write_runtime(host: str, port: int) -> None:
    """실제로 열린 주소를 파일로 남긴다 — 폴백이 일어났을 때 사용자가 찾을 단서."""
    import json
    import os
    import time
    try:
        p = config.data_dir() / "runtime.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "host": host, "port": port,
            "url": f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/",
            "pid": os.getpid(), "started_at": int(time.time()),
        }, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[serve] runtime.json 기록 실패(무시): {e}", flush=True)


def resolve_port(host: str, port: int, fallback: int) -> int | None:
    """쓸 수 있는 포트를 고른다. 없으면 None.

    - 요청한 포트가 비었으면 그대로.
    - 이미 ClewPath 가 잡고 있으면 폴백하지 않고 None (중복 기동 방지).
    - 남이 잡고 있으면 port+1 부터 fallback 개까지 훑는다.
    """
    if _port_free(host, port):
        return port
    if _clewpath_already_there(host, port):
        print(f"[serve] {host}:{port} 에 ClewPath 가 이미 떠 있습니다 - 기동을 중단합니다.",
              flush=True)
        return None
    if fallback <= 0:
        print(f"[serve] {host}:{port} 가 사용 중입니다. "
              f"port_fallback 이 0 이라 다른 포트를 찾지 않습니다.", flush=True)
        return None
    for cand in range(port + 1, port + 1 + fallback):
        if _port_free(host, cand):
            print(f"[serve] {port} 가 사용 중이라 {cand} 로 대신 엽니다.", flush=True)
            return cand
    print(f"[serve] {port}~{port + fallback} 이 전부 사용 중입니다.", flush=True)
    return None


def main():
    import os
    import sys

    import uvicorn

    from session_manager import systls
    systls.use_system_trust()   # 회사 TLS 인터셉션 대응 — httpx 가 OS 인증서를 쓰게(멱등)

    # 설정 파일을 env 로 펼친다. 반드시 앱을 만들기 전에 — BASE_PATH 등이 여기서 정해진다.
    filled = appconfig.apply_env()
    if filled:
        print(f"[config] {appconfig.config_path()} 적용: {', '.join(sorted(filled))}", flush=True)

    global BASE_PATH, COOKIE_PATH, app
    BASE_PATH = os.environ.get("SM_BASE_PATH", "").rstrip("/")
    COOKIE_PATH = (BASE_PATH + "/") if BASE_PATH else "/"
    app = create_app()          # 설정이 반영된 상태로 다시 만든다

    host = os.environ.get("SM_HOST", "127.0.0.1")
    want = int(os.environ.get("SM_PORT", "5100"))
    port = resolve_port(host, want, appconfig.get_int("server", "port_fallback", 10))
    if port is None:
        print("[serve] 기동하지 못했습니다. 이미 실행 중이 아닌지 확인하거나 "
              f"{appconfig.config_path()} 의 [server] port 를 바꾸세요.", flush=True)
        sys.exit(1)

    if auth.enabled():
        print("[auth] password gate ON (SM_PASSWORD set)")
    else:
        print("[auth] password gate OFF - local only. Set SM_PASSWORD before public exposure.")
    print(f"[serve] http://{host}:{port}")
    _write_runtime(host, port)
    # Claude 훅 등록 - 세션 상태(권한대기/작업중/턴종료)를 구조화 이벤트로 받는다.
    # 실패해도 기동은 계속(상태 뱃지만 없는 상태로 동작).
    if appconfig.get_bool("hooks", "auto_register", True):
        try:
            from session_manager import hooks
            hooks.ensure_registered(port)
        except Exception as e:  # noqa: BLE001
            print(f"[hooks] 등록 실패(기동은 계속): {e}", flush=True)
    # uvicorn.run() 대신 Server 를 직접 만든다 — 업데이트 적용 때 should_exit 로
    # 스스로 내려가려면 인스턴스를 붙잡고 있어야 한다.
    global _SERVER
    _SERVER = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    _SERVER.run()


if __name__ == "__main__":
    main()
