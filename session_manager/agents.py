"""claude 에이전트/세션 점유 스냅샷 — `claude agents --json`(공식 스크립팅 인터페이스) 기반.

왜 필요한가(실사용자 사고 2026-08-21): claude 네이티브 bg 에이전트(--bg)가
세션을 점유하면 `--resume` 이 "still running as a background agent" 로 거부된다.
사용자는 잊었거나 존재조차 모르는 잡에 막혀 "정상 사용 불가"로 체감했다.
세션 매니저인 ClewPath 가 이 점유를 보여주지도 풀어주지도 못한 것이 결함.

실험으로 확정한 사실(2026-08-21, 스크래치 세션 재현):
- 잠금의 유일한 원인은 daemon-backed bg 잡. 고아 프로세스(부모 사망 대화형)는
  agents 목록에도 없고 재개를 잠그지도 않는다(재개 성공 실측).
- `claude agents --json` 은 대화형 세션까지 pid·sessionId·busy/idle/waiting 으로
  실시간 노출한다 → 동시 재개 판정도 추측(훅 신선도) 대신 이 확정 데이터가 1차.

읽기 전용(CLI 호출뿐) — 불가침 원칙과 무관.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any

_CACHE_TTL = 10.0          # PWA 목록 폴링 대비 CLI(실측 0.5s) 호출 절약
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "list": []}


def _fetch() -> list[dict]:
    exe = shutil.which("claude")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "agents", "--json"], capture_output=True,
                             timeout=15).stdout.decode("utf-8", "replace")
        data = json.loads(out or "[]")
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def snapshot(max_age: float = _CACHE_TTL) -> list[dict]:
    """활성 세션 목록(대화형+백그라운드). 실패/미지원 CLI 는 빈 목록(기능 축소만)."""
    with _lock:
        now = time.time()
        if now - _cache["at"] > max_age:
            try:
                _cache["list"] = _fetch()
            except Exception:  # noqa: BLE001  (어떤 실패도 기능 축소로만)
                _cache["list"] = []
            _cache["at"] = now
        return _cache["list"]


def occupancy_map() -> dict[str, dict]:
    """sessionId → 점유 정보. 세션 목록 API 가 세션별로 조인해 내려준다.

    반환 필드: kind(interactive|background), status(busy|idle|waiting),
    name, pid, waiting_for(있으면). bg 는 재개 잠금을 예고하고,
    interactive 는 동시 재개 충돌을 예고한다.
    """
    out: dict[str, dict] = {}
    for it in snapshot():
        sid = str(it.get("sessionId") or "")
        if not sid:
            continue
        out[sid] = {
            "kind": it.get("kind"),
            "status": it.get("status"),
            "name": it.get("name"),
            "pid": it.get("pid"),
            "waiting_for": it.get("waitingFor"),
        }
    return out


def holder_of(session_id: str) -> dict | None:
    """이 세션을 지금 잡고 있는 에이전트(없으면 None). 재개 게이트용."""
    return occupancy_map().get(session_id)
