"""2차 인증 코드는 한 번만(RFC 6238 5.2) — 같은 코드·앞 스텝 코드를 다시 받지 않는다(pclink 두 Host e2e 에서 발견)."""
from __future__ import annotations

import time

from session_manager import owner2fa


def _setup(monkeypatch, secret="JBSWY3DPEHPK3PXP"):
    monkeypatch.setattr(owner2fa, "secret", lambda: secret)


def test_same_code_is_accepted_once(monkeypatch, fake_claude_home):
    _setup(monkeypatch)
    step = int(time.time()) // 30
    code = owner2fa._hotp("JBSWY3DPEHPK3PXP", step)
    assert owner2fa.verify(code) is True
    assert owner2fa.verify(code) is False
    prev = owner2fa._hotp("JBSWY3DPEHPK3PXP", step - 1)                 # 앞 스텝 코드도 이제 안 됨
    assert owner2fa.verify(prev) is False


def test_new_secret_starts_fresh(monkeypatch, fake_claude_home):
    _setup(monkeypatch, "JBSWY3DPEHPK3PXP")
    step = int(time.time()) // 30
    assert owner2fa.verify(owner2fa._hotp("JBSWY3DPEHPK3PXP", step)) is True
    _setup(monkeypatch, "KRSXG5CTMVRXEZLU")                                # 재설정
    assert owner2fa.verify(owner2fa._hotp("KRSXG5CTMVRXEZLU", step)) is True
