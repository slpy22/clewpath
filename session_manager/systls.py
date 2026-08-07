"""OS 신뢰 저장소를 파이썬 TLS 에 주입 — 회사 프록시(TLS 인터셉션) 대응.

httpx 는 기본으로 certifi 번들만 믿는다. 사내 방화벽이 HTTPS 를 가로채는 환경
(회사 루트 CA 를 Windows 저장소에 심는 방식)에서는 CP 호출이
'invalid peer certificate: UnknownIssuer' 로 죽는다. truststore 로 OS(Windows)
인증서 저장소를 ssl 전역에 주입하면 httpx·websockets·urllib 이 모두 회사 CA 를
신뢰한다. websockets/urllib 은 이미 OS 저장소를 쓰지만 httpx(certifi) 때문에
주입이 필요하다.

실패해도(패키지 없음·플랫폼 미지원) 조용히 넘어간다 — 일반 PC 는 certifi 로
충분하고, 주입은 '있으면 더 넓게 신뢰' 하는 개선일 뿐이라 없어도 동작한다.
"""
from __future__ import annotations

_done = False


def use_system_trust() -> None:
    """프로세스 시작 시 한 번 호출. 멱등(두 번째부터 no-op)."""
    global _done
    if _done:
        return
    _done = True
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        # truststore 없거나 주입 실패 → certifi 기본 동작 유지(일반 환경엔 무영향).
        pass
