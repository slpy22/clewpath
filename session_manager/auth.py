"""비밀번호 게이트 — 공개 인터넷 노출 시 최소 인증.

- 비밀번호는 환경변수 SM_PASSWORD 로 설정. 설정돼 있으면 인증 ON, 없으면 OFF(로컬 전용).
- 로그인 성공 시 임의 토큰을 발급해 쿠키에 저장하고, 메모리 집합으로 검증.
- HTTP 라우트는 미들웨어로, WebSocket 은 엔드포인트에서 직접 쿠키를 검사한다.
- 서버 재시작하면 토큰 전부 무효화(메모리 보관).

주의: 웹 터미널은 이 PC 전체를 제어할 수 있으므로, 공개 노출 시 SM_PASSWORD 는 필수다.
"""
from __future__ import annotations

import hmac
import os
import secrets

COOKIE = "sm_auth"
_TOKENS: set[str] = set()


def password() -> str | None:
    pw = os.environ.get("SM_PASSWORD")
    return pw if pw else None


def enabled() -> bool:
    return password() is not None


def issue_token() -> str:
    t = secrets.token_urlsafe(32)
    _TOKENS.add(t)
    return t


def valid(token: str | None) -> bool:
    return bool(token) and token in _TOKENS


def check_password(pw: str) -> bool:
    real = password()
    if not real:
        return False
    return hmac.compare_digest(pw, real)


# ---- 외부 API 토큰 (웹 로그인 비밀번호와 별개) ----

def api_tokens() -> set[str]:
    """SM_API_TOKEN / SM_API_TOKENS(쉼표구분) 환경변수에서 허용 토큰 집합을 만든다."""
    raw = (os.environ.get("SM_API_TOKEN", "") + "," +
           os.environ.get("SM_API_TOKENS", ""))
    return {t.strip() for t in raw.split(",") if t.strip()}


def api_enabled() -> bool:
    return len(api_tokens()) > 0


def valid_api_token(token: str | None) -> bool:
    if not token:
        return False
    toks = api_tokens()
    return any(hmac.compare_digest(token, t) for t in toks)


def bearer(header_value: str | None) -> str | None:
    """Authorization 헤더에서 Bearer 토큰을 추출한다."""
    if header_value and header_value.lower().startswith("bearer "):
        return header_value[7:].strip()
    return None


def login_html(base: str = "", error: bool = False) -> str:
    """로그인 페이지 HTML. base 는 서브경로 prefix(예: /session_mgr) 로 폼 action 에 반영."""
    msg = "비밀번호가 올바르지 않습니다." if error else ""
    err = f'<p class="err">{msg}</p>' if msg else ""
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>로그인 — ClewPath</title>
<style>
  html,body{{height:100%;margin:0;background:#0d1117;color:#e6edf3;
    font-family:ui-sans-serif,system-ui,"Segoe UI","Malgun Gothic",sans-serif;}}
  .wrap{{height:100%;display:flex;align-items:center;justify-content:center;}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px 26px;width:320px;}}
  h1{{font-size:18px;margin:0 0 4px;}}
  p.sub{{color:#8b949e;font-size:13px;margin:0 0 18px;}}
  input{{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;
    border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-size:14px;}}
  button{{width:100%;margin-top:12px;padding:10px;border-radius:8px;border:0;
    background:#238636;color:#fff;font-size:14px;cursor:pointer;}}
  button:hover{{background:#2ea043;}}
  p.err{{color:#f85149;font-size:13px;margin:10px 0 0;}}
</style></head><body><div class="wrap"><form class="card" method="post" action="{base}/login">
  <h1>🧭 ClewPath</h1>
  <p class="sub">비밀번호를 입력하세요.</p>
  <input type="password" name="password" placeholder="비밀번호" autofocus autocomplete="current-password">
  <button type="submit">로그인</button>
  {err}
</form></div></body></html>"""
