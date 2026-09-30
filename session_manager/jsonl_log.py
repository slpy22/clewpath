"""공용 JSONL 추가 기록(design E-D10): 잠금 + 크기 상한 회전 1세대.

policy.audit(api_audit.jsonl)·devices.audit(pairing-audit.jsonl) 이 같이 쓴다 — 로그가 영원히 자라지 않는다.
회전: 파일이 max_bytes 를 넘으면 `<name>.1` 로 옮기고(이전 .1 은 덮어씀) 새 파일에 이어 쓴다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
_LOCKS: dict[str, threading.Lock] = {}
_META = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _META:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = _LOCKS[key] = threading.Lock()
        return lk


def rotated_path(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def append(path: Path, rec: dict, max_bytes: int = DEFAULT_MAX_BYTES, ts_key: str | None = "ts") -> None:
    """rec 를 한 줄로 추가. ts_key 가 있고 rec 에 없으면 epoch 초를 넣는다. 실패는 호출자 기능에 영향 없어야 하므로 예외를 삼킨다."""
    path = Path(path)
    line = json.dumps(({**({ts_key: int(time.time())} if ts_key and ts_key not in rec else {}), **rec}),
                      ensure_ascii=False) + "\n"
    try:
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if max_bytes and path.exists() and path.stat().st_size >= max_bytes:
                    os.replace(path, rotated_path(path))
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001  로그 기록 실패가 기능을 막으면 안 된다
        pass


def tail(path: Path, n: int = 50) -> list[dict]:
    """최근 n 줄(회전 파일 포함). 진단·테스트용."""
    path = Path(path)
    out: list[dict] = []
    for p in (rotated_path(path), path):
        try:
            if not p.exists():
                continue
            for ln in p.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except Exception:  # noqa: BLE001
                    continue
        except OSError:
            continue
    return out[-n:]
