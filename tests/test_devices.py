"""devices 등록부(0.10.0, eng E-D3/E-D9): 쓰기 원자화(_mutate+RLock)·이름 정규화 한 곳·폰 보고 이름."""
from __future__ import annotations

import threading

import pytest

from session_manager import devices


@pytest.mark.parametrize("raw,fallback,expect", [
    ("  내 아이폰  ", "", "내 아이폰"),
    ("a\x00b\x1fc\x7fd", "", "abcd"),
    ("여러   공백\t탭\n줄", "", "여러 공백 탭 줄"),
    ("x" * 60, "", "x" * 40),
    ("", "기기", "기기"),
    (None, "PC", "PC"),
    ("\x01\x02", "기기", "기기"),
])
def test_clean_name(raw, fallback, expect):
    assert devices.clean_name(raw, fallback) == expect


def test_add_rename_use_clean_name(fake_claude_home):
    r = devices.add_device("  폰\x00 A  ")
    assert r["name"] == "폰 A"
    assert devices.rename(r["id"], "x" * 50)
    assert devices.list_devices()[0]["name"] == "x" * 40
    assert devices.rename(r["id"], "   ")                 # 빈 이름 → id 앞 8자
    assert devices.list_devices()[0]["name"] == r["id"][:8]


def test_set_name_if_empty_only_fills_auto_names(fake_claude_home):
    a = devices.add_device("")                            # 임시 이름(name_auto)
    b = devices.add_device("회사 노트북")                  # 사용자 이름
    assert devices.set_name_if_empty(a["id"], " iPhone\x00 15 ") == "iPhone 15"
    assert devices.set_name_if_empty(a["id"], "Galaxy") is None          # 한 번 채워지면 고정
    assert devices.set_name_if_empty(b["id"], "iPhone") is None          # 사용자 이름 보존
    assert devices.set_name_if_empty(a["id"], "\x01") is None            # 빈 값은 무시
    names = {d["id"]: d["name"] for d in devices.list_devices()}
    assert names[a["id"]] == "iPhone 15" and names[b["id"]] == "회사 노트북"
    assert devices.rename(a["id"], "")                    # 비우면 다시 임시 이름 → 다음 보고로 채워짐
    assert devices.set_name_if_empty(a["id"], "Pixel") == "Pixel"


def test_list_exposes_client_scoped(fake_claude_home):
    r = devices.add_device("a")
    assert devices.list_devices()[0]["client_scoped"] is False
    devices.set_client_public_id(r["id"], "cpub_1")
    assert devices.list_devices()[0]["client_scoped"] is True
    devices.set_client_public_id(r["id"], None)
    assert devices.get_client_public_id(r["id"]) is None


def test_concurrent_touch_and_revoke_do_not_lose_updates(fake_claude_home):
    """회귀(E-D3): 커넥터 touch 와 다른 스레드의 revoke/rename 이 겹쳐도 앞의 변경이 지워지지 않는다."""
    ids = [devices.add_device(f"d{i}")["id"] for i in range(4)]
    stop = threading.Event()
    errors: list[BaseException] = []

    def toucher():
        try:
            while not stop.is_set():
                for did in ids:
                    devices.touch(did)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=toucher, daemon=True)
    t.start()
    try:
        for i, did in enumerate(ids):
            assert devices.revoke(did) is True
            assert devices.rename(did, f"이름{i}") is True
            assert devices.set_client_public_id(did, f"cpub_{i}") is True
    finally:
        stop.set(); t.join(3)
    assert not errors
    rows = {d["id"]: d for d in devices.list_devices()}
    for i, did in enumerate(ids):
        assert rows[did]["revoked"] is True, "revoke 가 touch 에 덮여 사라짐"
        assert rows[did]["name"] == f"이름{i}"
        assert devices.get_client_public_id(did) == f"cpub_{i}"
        assert rows[did]["last_seen"] is not None


def test_mutate_skips_save_when_unchanged(fake_claude_home, monkeypatch):
    devices.add_device("a")
    saves = []
    real = devices._save
    monkeypatch.setattr(devices, "_save", lambda d: (saves.append(1), real(d)))
    assert devices.revoke("nope") is False
    assert devices.remove("nope") is False
    assert devices.rotate_token("nope") is None
    devices.touch("nope")
    assert saves == []
