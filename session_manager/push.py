"""웹푸시 - Host 가 직접 발송한다 (릴레이 무경유).

Web Push 는 프로토콜 자체가 종단 암호화다: 페이로드는 구독 시 브라우저가
만든 키(p256dh/auth)로 암호화되어 푸시 중계자(FCM/Mozilla)조차 내용을 못
읽는다. 발송 주체가 이 PC(Host)라서 우리 릴레이는 아예 경유하지 않는다
- blind 원칙 유지, 알림 본문이 어디에도 안 남는다.

- VAPID 키쌍: 데이터 폴더 vapid_private.pem (최초 1회 생성, 이후 고정.
  바꾸면 모든 기기가 재구독해야 하므로 절대 재생성하지 않는다)
- 구독 저장: push_subscriptions.json (endpoint 로 식별, 기기 여러 대 지원)
- 발송: 훅 이벤트에서 호출. (세션,종류) 60초 dedupe. 410/404 는 만료
  구독이므로 자동 제거.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

from session_manager import appconfig, config

DEDUPE_WINDOW_S = 60
_dedupe: dict[tuple[str, str], float] = {}
_lock = threading.Lock()

VAPID_SUB = "mailto:push@clewpath.pyongso.com"


# ---------------------------------------------------------------- 저장 경로

def _vapid_pem() -> Path:
    return config.data_dir() / "vapid_private.pem"


def _subs_file() -> Path:
    return config.data_dir() / "push_subscriptions.json"


# ---------------------------------------------------------------- VAPID

def ensure_vapid() -> str:
    """VAPID 공개키(base64url, applicationServerKey 용)를 돌려준다. 없으면 생성."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = _vapid_pem()
    if pem.is_file():
        priv = serialization.load_pem_private_key(pem.read_bytes(), password=None)
    else:
        priv = ec.generate_private_key(ec.SECP256R1())
        pem.parent.mkdir(parents=True, exist_ok=True)
        pem.write_bytes(priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------------------------------------------------------------- 구독 관리

def _load_subs() -> list[dict]:
    try:
        data = json.loads(_subs_file().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_subs(subs: list[dict]) -> None:
    f = _subs_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(subs, ensure_ascii=False, indent=1), encoding="utf-8")


def add_subscription(subscription: dict, name: str = "") -> bool:
    """구독 등록(endpoint 기준 upsert). 반환: 새로 추가됐는가."""
    ep = str(subscription.get("endpoint") or "")
    keys = subscription.get("keys") or {}
    if not ep or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("endpoint/p256dh/auth 가 필요합니다")
    with _lock:
        subs = _load_subs()
        for s in subs:
            if s.get("endpoint") == ep:
                s["keys"] = keys
                if name:
                    s["name"] = name
                _save_subs(subs)
                return False
        subs.append({"endpoint": ep, "keys": keys, "name": name,
                     "added_at": int(time.time())})
        _save_subs(subs)
        return True


def _sub_id(endpoint: str) -> str:
    import hashlib
    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


def remove_subscription(endpoint_or_id: str) -> bool:
    """endpoint 전체 또는 목록의 id 로 구독 제거."""
    with _lock:
        subs = _load_subs()
        kept = [s for s in subs
                if s.get("endpoint") != endpoint_or_id
                and _sub_id(s.get("endpoint", "")) != endpoint_or_id]
        if len(kept) == len(subs):
            return False
        _save_subs(kept)
        return True


def list_subscriptions() -> list[dict]:
    # endpoint 는 사실상 비밀(아는 사람은 푸시를 보낼 수 있음) - 앞부분만 노출.
    # id 는 관리(삭제)용 안정 식별자.
    return [{"id": _sub_id(s.get("endpoint", "")),
             "endpoint_hint": s.get("endpoint", "")[:60] + "...",
             "name": s.get("name", ""), "added_at": s.get("added_at")}
            for s in _load_subs()]


# ---------------------------------------------------------------- 발송

def _webpush_send(sub: dict, body: str) -> None:
    """한 구독에 발송. 만료(404/410)면 구독 제거. 예외는 삼킨다(베스트에포트)."""
    from pywebpush import WebPushException, webpush
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
            data=body,
            vapid_private_key=str(_vapid_pem()),
            vapid_claims={"sub": VAPID_SUB},
            timeout=10,
        )
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            remove_subscription(sub["endpoint"])
            print(f"[push] 만료 구독 제거({code}): {sub['endpoint'][:40]}...", flush=True)
        else:
            print(f"[push] 발송 실패({code}): {e}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[push] 발송 오류: {e}", flush=True)


def send(kind: str, session_id: str, title: str, body: str) -> int:
    """모든 구독 기기에 발송(스레드, 논블로킹). 반환: 시도한 구독 수."""
    now = time.time()
    with _lock:
        last = _dedupe.get((session_id, kind), 0)
        if now - last < DEDUPE_WINDOW_S:
            return 0
        _dedupe[(session_id, kind)] = now
        if len(_dedupe) > 2000:
            cutoff = now - DEDUPE_WINDOW_S
            for k in [k for k, v in _dedupe.items() if v < cutoff]:
                _dedupe.pop(k, None)
    subs = _load_subs()
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "kind": kind,
                          "sid": session_id, "tag": f"{session_id}:{kind}"},
                         ensure_ascii=False)
    for sub in subs:
        threading.Thread(target=_webpush_send, args=(sub, payload),
                         daemon=True).start()
    return len(subs)


# ---------------------------------------------------------------- 훅 연결

def notify_from_event(payload: dict) -> None:
    """훅 이벤트를 알림으로. 토픽별 on/off 는 설정([push])."""
    sid = str(payload.get("session_id") or "")
    if not sid:
        return
    ev = str(payload.get("hook_event_name") or "")
    cwd = str(payload.get("cwd") or "")
    proj = Path(cwd).name if cwd else "세션"

    if ev == "Notification":
        from session_manager import hooks
        kind = hooks._classify_notification(str(payload.get("message") or ""))
        if kind == "permission" and appconfig.get_bool("push", "permission", True):
            # 본문은 Claude 의 알림 문구 그대로(도구명 수준) - 코드/경로 상세 없음
            send("permission", sid, f"[{proj}] 권한 요청",
                 str(payload.get("message") or "승인이 필요합니다")[:180])
        elif kind == "waiting" and appconfig.get_bool("push", "waiting", False):
            send("waiting", sid, f"[{proj}] 입력 대기", "세션이 입력을 기다립니다")
    elif ev == "Stop" and appconfig.get_bool("push", "ready", True):
        send("ready", sid, f"[{proj}] 작업 완료", "턴이 끝났습니다 - 확인해 주세요")
