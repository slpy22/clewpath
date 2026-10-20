"""정기 작업(2026-10-12 사장님): 쉬는 세션 정리 · 모델 목록 갱신 — Host 안의 작업 줄 하나가 차례로 돈다.

설계
- **작업 줄 하나(스레드 1개 + 큐)**: 정해진 때가 된 작업과 사용자가 누른 「지금 실행」이 같은 줄에 서서
  하나씩 돈다 — 같은 작업이 겹쳐 돌지 않는다. 기동 직후(업데이트·재시작)엔 5분 쉬었다 시작.
- **세션 끄기/띄우기 충돌**: 쉬는 세션 정리는 webterm.stop_if 로 끈다 — 세션별 수명 잠금 안에서 조건을
  다시 확인하므로, 같은 순간 관리 세션이 그 워커를 깨우거나(start) 누가 화면을 열면 끄지 않는다.
- 상태(마지막 실행·결과·켜짐/꺼짐·기준 시간)는 data_dir/housekeeping.json. claude 파일은 읽기만 한다.

쉬는 세션 정리(idle_sessions) — 대상은 **ClewPath 가 띄운 터미널(PTY)만**(터미널에서 직접 띄운 claude 는 손대지
않는다). 아래가 모두 맞을 때만 끈다:
  · 마지막 활동(jsonl 대화 줄 시각·훅 이벤트·키 입력·화면 연 시각·터미널을 띄운 시각) 이후 기준 시간(기본 4시간) 경과
    — jsonl 파일 수정 시각은 안 쓴다(쉬는 동안에도 claude 가 메타 줄을 다시 씀). 화면이 열려 있기만 한 것은 활동 아님
  · 권한 승인 대기 아님 · 백그라운드 쉘·모니터 0개 · 「항상 켜 두기」 아님
끈 세션은 대화 기록이 그대로라 다시 열면 이어진다(재개).
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path

from session_manager import config

FIRST_DELAY_S = 300          # 기동 직후엔 쉰다(업데이트 헬스체크 창·재연결 정리와 겹치지 않게)
TICK_S = 60
IDLE_HOURS_DEFAULT = 4
IDLE_HOURS_CHOICES = (1, 2, 4, 8, 12, 24)

JOBS = {
    "idle_sessions": {"name": "쉬는 세션 정리", "interval": 15 * 60, "default_on": True,
                      "desc": "오래 아무 일도 안 한 ClewPath 터미널을 끕니다(기록은 남아 다시 열면 이어짐)"},
    "models": {"name": "모델 목록 갱신", "interval": 24 * 3600, "default_on": True,
               "desc": "claude /model 화면(claude 가 업데이트됐을 때·7일마다 숨은 터미널로 읽음) + 이 PC 실사용 모델로 재개 모델 목록을 새로 만듭니다(화면을 못 읽었으면 내장 기본)"},
    "worklog": {"name": "작업 장부 채우기", "interval": 5 * 60, "default_on": True,
                "desc": "모든 세션의 요청·답 요약을 비서용 장부에 이어 적습니다(읽기만 · claude 가 기록을 지워도 장부는 남음)"},
    "serve_keep": {"name": "서빙 세션 보관 유지", "interval": 24 * 3600, "default_on": True,
                   "desc": "API 가 요청마다 fork 해서만 쓰는 세션(가드레일 프로필이 있는 세션)은 원본이 갱신되지 않아 claude 가 보관 기간 뒤 지웁니다. 지워지기 3일 전에 원본과 한 번 짧게 주고받아 기한을 늘립니다"},
    "cleanup_watch": {"name": "지워질 세션 예고", "interval": 24 * 3600, "default_on": True,
                      "desc": "claude 가 보관 기간(기본 30일)이 지난 대화 기록을 지우기 전에, 3일 안에 지워질 세션을 폰으로 한 번 알립니다"},
    "fork_cleanup": {"name": "API 사본 정리", "interval": 15 * 60, "default_on": True,
                     "desc": "외부 API 가 원본 대신 쓰고 남긴 사본(fork) 중 끝날 때 바로 못 지운 것을 휴지통으로 옮깁니다(아직 실행 중이면 다음에)"},
}
# 화면에 '자동(항상)' 으로만 보여 주는 기존 주기 작업(켜고 끄는 대상 아님)
BUILTIN_INFO = [
    {"name": "업데이트 확인", "every": "기동 때 · 주기적", "desc": "새 Host 버전이 있으면 ⚙ 에 점"},
    {"name": "기기 자격 동기화", "every": "5분", "desc": "폰 페어링·폐기를 CP 와 맞춤"},
    {"name": "관제 호출 실패 감지", "every": "2초", "desc": "관제 그룹 관리 세션의 SendMessage 실패 알림"},
    {"name": "팀 명부 감시", "every": "1분", "desc": "구성원 교체·일감 기한"},
    {"name": "Host 자기 회복", "every": "5분(예약 작업)", "desc": "꺼져 있으면 다시 띄움"},
]

# 내장 기본 재개 모델 목록 — claude /model 화면을 아직 못 읽었을 때(또는 읽기 실패) 쓴다. 이 PC 실사용 모델과 합쳐진다(refresh_models).
# 0.22.1: 모델 목록은 각 PC 가 관리한다(운영측 CP 목록은 쓰지 않음 — 고를 수 있는 모델은 계정마다 다르고, 그 정답은 그 PC 의 화면).
BUILTIN_MODELS = [      # claude /model 화면 순서(2026-10-12, Claude Code 2.1.296)
    {"id": "claude-opus-5-5", "label": "Opus 5.5"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5"},
    {"id": "claude-opus-5", "label": "Opus 5"},
    {"id": "claude-fable-5", "label": "Fable 5"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
]

_lock = threading.Lock()
_q: "queue.Queue[str]" = queue.Queue()
_thread: threading.Thread | None = None
_stop = threading.Event()
_running: str | None = None


# ---------------------------------------------------------------- 상태 파일
def _file() -> Path:
    return config.data_dir() / "housekeeping.json"


def _load() -> dict:
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def _job_state(d: dict, jid: str) -> dict:
    return d.setdefault("jobs", {}).setdefault(jid, {})


def enabled(jid: str, d: dict | None = None) -> bool:
    d = d if d is not None else _load()
    v = _job_state(d, jid).get("enabled")
    return JOBS[jid]["default_on"] if v is None else bool(v)


def idle_hours(d: dict | None = None) -> int:
    d = d if d is not None else _load()
    try:
        h = int(d.get("idle_hours") or IDLE_HOURS_DEFAULT)
    except (TypeError, ValueError):
        h = IDLE_HOURS_DEFAULT
    return h if h in IDLE_HOURS_CHOICES else IDLE_HOURS_DEFAULT


def set_options(job: str | None = None, on: bool | None = None, hours: int | None = None) -> dict:
    with _lock:
        d = _load()
        if job in JOBS and on is not None:
            _job_state(d, job)["enabled"] = bool(on)
        if hours is not None:
            if int(hours) not in IDLE_HOURS_CHOICES:
                raise ValueError("hours")
            d["idle_hours"] = int(hours)
        _save(d)
    return status()


def status() -> dict:
    d = _load()
    jobs = []
    for jid, j in JOBS.items():
        st = _job_state(d, jid)
        jobs.append({"id": jid, "name": j["name"], "desc": j["desc"], "interval_s": j["interval"],
                     "enabled": enabled(jid, d), "last_run": st.get("last_run"), "last_ok": st.get("last_ok"),
                     "last_summary": st.get("last_summary"), "running": _running == jid})
    return {"jobs": jobs, "idle_hours": idle_hours(d), "idle_hours_choices": list(IDLE_HOURS_CHOICES),
            "builtin": BUILTIN_INFO}


# ---------------------------------------------------------------- 쉬는 세션 정리
def _ts_max(*vals) -> float:
    m = 0.0
    for v in vals:
        try:
            m = max(m, float(v or 0))
        except (TypeError, ValueError):
            pass
    return m


def idle_candidates(now: float | None = None, hours: int | None = None) -> list[dict]:
    """끌 후보(조건을 모두 만족). 실제 끄기는 reap_idle 이 세션 잠금 안에서 다시 확인한 뒤."""
    from session_manager import bgtasks, hooks, labels, peers, scanner, webterm
    now = now or time.time()
    limit = (hours or idle_hours()) * 3600
    recs = labels.all_records()
    pmap = peers.peer_map()
    out = []
    for key in list(webterm._ACTIVE.keys()):
        a = webterm.activity(key)
        if a is None or not a.get("persist", True):
            continue                                   # 포크(일회용)는 화면과 함께 끝난다 — 대상 아님
        reason = _skip_reason(key, a, now, limit, recs, pmap, scanner, hooks, bgtasks)
        if reason:
            continue
        meta = scanner.scan_one(key)
        out.append({"session_id": key, "title": _title(key, meta, recs),
                    "idle_s": int(now - _last_activity(key, a, meta, hooks))})
    return out


def _last_activity(sid, a, meta, hooks) -> float:
    rt = hooks.status_of(sid) or {}
    rt_ts = [v for k, v in rt.items() if k.endswith("_at")]
    # 파일 mtime 은 쓰지 않는다: claude 가 쉬는 동안에도 끝에 메타 줄을 다시 써서 늘 '방금' 이 된다(2026-10-13 실측).
    from session_manager.scanner import _epoch
    talk = _epoch(getattr(meta, "last_activity_at", None)) or 0
    return _ts_max(a.get("started_at"), a.get("last_input"), a.get("last_attach"), talk, *rt_ts)


def _skip_reason(sid, a, now, limit, recs, pmap, scanner, hooks, bgtasks) -> str | None:
    if (recs.get(sid) or {}).get("keep"):
        return "keep"
    # 화면이 붙어 있는 것만으로는 지키지 않는다 — 밤새 열어 둔 탭도 입력·대화가 없으면 정리(2026-10-13 사장님).
    # 보는 중에 claude 가 일하면 대화 줄이, 사람이 치면 입력 시각이 새로워져 'recent' 로 빠진다.
    rt = hooks.status_of(sid) or {}
    if rt.get("phase") == "permission":
        return "permission"
    meta = scanner.scan_one(sid)
    if bgtasks.busy(bgtasks.counts(meta, pmap.get(sid))):
        return "background"
    if now - _last_activity(sid, a, meta, hooks) < limit:
        return "recent"
    return None


def _title(sid, meta, recs) -> str:
    rec = recs.get(sid) or {}
    return (rec.get("name") or getattr(meta, "custom_title", None) or getattr(meta, "ai_title", None)
            or getattr(meta, "slug", None) or sid[:8])


def reap_idle(now: float | None = None) -> dict:
    from session_manager import bgtasks, hooks, labels, peers, scanner, webterm
    hours = idle_hours()
    limit = hours * 3600
    stopped, kept = [], 0
    for c in idle_candidates(now, hours):
        sid = c["session_id"]

        def still_idle(a, _sid=sid):
            t = time.time()
            return _skip_reason(_sid, a, t, limit, labels.all_records(), peers.peer_map(),
                                scanner, hooks, bgtasks) is None
        r = webterm.stop_if(sid, still_idle)
        if r == "stopped":
            stopped.append(c)
        elif r == "busy":
            kept += 1
    if stopped:
        try:
            from session_manager import policy
            for c in stopped:
                policy.audit({"event": "idle_stop", "session_id": c["session_id"], "idle_s": c["idle_s"]})
        except Exception:  # noqa: BLE001
            pass
        try:
            from session_manager import push
            names = ", ".join(c["title"] for c in stopped[:3]) + (" 외" if len(stopped) > 3 else "")
            push.send("housekeeping", "idle-stop", f"쉬던 세션 {len(stopped)}개를 껐습니다",
                      f"{hours}시간 넘게 활동 없음 — {names}. 다시 열면 이어집니다")
        except Exception:  # noqa: BLE001
            pass
    summary = (f"{len(stopped)}개 끔" if stopped else "끌 세션 없음") + (f" · {kept}개는 방금 활동해 둠" if kept else "")
    return {"stopped": stopped, "kept_at_last_check": kept, "hours": hours, "summary": summary}


# ---------------------------------------------------------------- 모델 목록 갱신
_MODEL_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-(\d{8}))?$")


def model_label(mid: str) -> str:
    m = _MODEL_RE.match(mid or "")
    if not m:
        return mid
    fam, major, minor = m.group(1), m.group(2), m.group(3)
    return f"{fam.capitalize()} {major}" + (f".{minor}" if minor else "")


def observed_models(metas=None) -> list[tuple[str, float]]:
    """이 PC 세션들의 마지막 실사용 모델(assistant message.model) — 최근에 쓴 순."""
    from session_manager import scanner
    seen: dict[str, float] = {}
    for m in (metas if metas is not None else scanner.scan_all()):
        mid = getattr(m, "last_model", None)
        if mid and _MODEL_RE.match(mid):
            seen[mid] = max(seen.get(mid, 0.0), float(getattr(m, "mtime", 0) or 0))
    return sorted(seen.items(), key=lambda kv: kv[1], reverse=True)


def _models_file() -> Path:
    return config.data_dir() / "models.json"


PROBE_MAX_AGE_S = 7 * 86400     # claude 버전이 그대로여도 이만큼 지나면 화면을 다시 읽는다
_PROBE_KEEP_IDS = 50


def _maybe_probe(d: dict, force: bool = False) -> tuple[dict | None, str]:
    """claude /model 화면 읽기(modelprobe) — claude 가 업데이트됐거나 7일 지났을 때만. → (마지막 성공 결과, 메모)."""
    from session_manager import config as _cfg, modelprobe
    pr = d.get("probe") or {}
    exe = _cfg.claude_exe()
    ver = modelprobe.claude_version(exe) if exe else None
    due = force or not pr.get("ok_models") or (ver and ver != pr.get("version"))         or time.time() - float(pr.get("at") or 0) > PROBE_MAX_AGE_S
    if not due:
        return pr, f"화면 목록 {len(pr.get('ok_models') or [])}개(claude {pr.get('version')}, 다시 읽을 때 아님)"
    cwd = modelprobe.pick_cwd()
    if not exe or not cwd:
        return pr, "화면 읽기 건너뜀(" + ("claude 없음" if not exe else "띄울 신뢰 폴더 없음") + ")"
    r = modelprobe.probe(exe, cwd)
    ids = (d.get("probe_sessions") or []) + [r["session_id"]]
    d["probe_sessions"] = ids[-_PROBE_KEEP_IDS:]
    pr = dict(pr, at=int(time.time()), version=ver, last_error=r.get("error"))
    if r.get("ok"):
        pr["ok_models"] = r["models"]
        note = f"화면 목록 {len(r['models'])}개 새로 읽음(claude {ver})"
    else:
        note = f"⚠ 화면 읽기 실패({r.get('error')}) — 이전 목록 사용"
    d["probe"] = pr
    return pr, note


def hidden_session_ids() -> set[str]:
    """화면 읽기용으로 잠깐 띄운 claude 가 남긴 빈 세션 — 목록에서 숨긴다."""
    return set(_load().get("probe_sessions") or [])


def refresh_models(metas=None, probe: bool = True) -> dict:
    """재개 모델 목록 합본: ① claude /model 화면(이 계정이 고를 수 있는 것, 화면 순서) ② 이 PC 실사용.
    화면을 아직 못 읽었으면 ① 대신 내장 기본(BUILTIN_MODELS)."""
    from session_manager import modelprobe
    base = [dict(x) for x in BUILTIN_MODELS]
    observed = observed_models(metas)
    known = {}
    for x in base:
        known.setdefault(model_label(x["id"]), x["id"])
    for mid, _t in observed:
        known.setdefault(model_label(mid), mid)
    note = ""
    shown = []
    if probe:
        with _lock:
            d = _load()
        pr, note = _maybe_probe(d)
        with _lock:
            d2 = _load()
            for k in ("probe", "probe_sessions"):
                if k in d:
                    d2[k] = d[k]
            _save(d2)
        for it in (pr or {}).get("ok_models") or []:
            shown.append({"id": modelprobe.to_id(it, known), "label": it["label"], "picker": True})
    models, have = [], set()
    for x in (shown or base):
        if x["id"] not in have:
            models.append(x)
            have.add(x["id"])
    added = []
    for mid, _t in observed:
        if mid not in have:
            added.append({"id": mid, "label": model_label(mid), "seen": True})
            have.add(mid)
    models = (models + added) if shown else (added + models)
    doc = {"models": models, "updated_at": int(time.time()), "source": "picker" if shown else "builtin",
           "picker_count": len(shown), "observed_added": [x["id"] for x in added]}
    f = _models_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    summ = f"{len(models)}개" + (f" (실사용 추가: {', '.join(model_label(a) for a in doc['observed_added'])})" if added else "")
    return {"count": len(models), "added": doc["observed_added"], "source": doc["source"],
            "summary": summ + (" · " + note if note else "")}


def saved_models() -> dict | None:
    try:
        d = json.loads(_models_file().read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("models"), list) and d["models"]:
            return d
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------- 작업 줄
# ---------------------------------------------------------------- 지워질 세션 예고(2026-10-13)
def watch_cleanup(metas=None, now: float | None = None) -> dict:
    """claude 가 곧(ALERT_DAYS 안) 지울 세션을 세션마다 한 번만 폰으로 알린다. claude 파일은 읽기만."""
    from session_manager import retention, scanner
    now = now or time.time()
    days = retention.period_days()
    if not days:
        return {"soon": [], "summary": "claude 자동 삭제 꺼짐(보관 기간 설정 없음/0) — 예고 안 함"}
    hidden = hidden_session_ids()
    metas = [m for m in (metas if metas is not None else scanner.scan_all())
             if not getattr(m, "headless", False) and m.session_id not in hidden]
    soon = retention.soon(metas, retention.ALERT_DAYS, now)
    with _lock:
        # 세션 → 알린 시각. 7일 지나면 잊는다(그 사이 열어서 기한이 늘었다가 다시 다가오면 또 알린다)
        told = {k: v for k, v in (_load().get("cleanup_told") or {}).items() if v > now - 7 * 86400}
    new = [c for c in soon if c["session_id"] not in told]
    if new:
        try:
            from session_manager import push
            names = ", ".join(c["title"] for c in new[:3]) + (" 외" if len(new) > 3 else "")
            push.send("housekeeping", new[0]["session_id"], f"claude 가 곧 지울 세션 {len(new)}개",
                      f"{retention.ALERT_DAYS}일 안에 대화 기록이 지워집니다 — {names}. 열어서 한 번 주고받으면 기한이 늘어납니다")
        except Exception:  # noqa: BLE001
            pass
        for c in new:
            told[c["session_id"]] = int(now)
    with _lock:
        d = _load()
        d["cleanup_told"] = told
        _save(d)
    summary = (f"{retention.ALERT_DAYS}일 안에 지워질 세션 {len(soon)}개" + (f" · {len(new)}개 새로 알림" if new else "")
               if soon else f"{retention.ALERT_DAYS}일 안에 지워질 세션 없음") + f" (보관 {days}일)"
    return {"soon": soon, "notified": [c["session_id"] for c in new], "days": days, "summary": summary}


KEEP_PROMPT = "(ClewPath 보관 유지 점검 — 방문자 질문이 아님) 'ok' 라고만 답하라."


def keep_serving(metas=None, now: float | None = None, run=None) -> dict:
    """fork 로만 쓰는 서빙 세션(프로필 있음)을 claude 의 보관 기간 삭제에서 지킨다(2026-10-19 사장님 승인 —
    북한법 챗봇 기준 세션 614cb260 이 fork 만 받다가 30일 만에 지워진 사고).

    지워지기 ALERT_DAYS 안에 든 것만, 원본을 `-p --resume` 으로 한 번 짧게 주고받는다(claude 가 기록을 이어 쓰며
    수정 시각이 늘어난다 — ClewPath 는 파일을 직접 건드리지 않는다). 가드레일은 fork 때와 같다.
    지금 떠 있는 세션은 건드리지 않는다(프로세스 2개 = 대화 분기)."""
    import os as _os
    import subprocess
    from session_manager import peers, policy, profiles, ptyd, retention, scanner, webapi, webterm
    now = now or time.time()
    if not retention.period_days():
        return {"kept": [], "failed": [], "summary": "claude 자동 삭제 꺼짐 — 할 일 없음"}
    recs = profiles.all_records()
    metas = [m for m in (metas if metas is not None else scanner.scan_all()) if m.session_id in recs]
    soon = retention.soon(metas, retention.ALERT_DAYS, now)
    live = peers.peer_map()
    kept, failed, skipped = [], [], []
    for c in soon:
        sid, cwd = c["session_id"], c.get("cwd")
        if webterm.has_terminal(sid) or sid in live:
            skipped.append(sid)
            continue
        if not cwd or not _os.path.isdir(cwd):
            failed.append({"session_id": sid, "error": "no_cwd"})
            continue
        from session_manager import config as _cfg
        argv = [_cfg.claude_exe(), "-p", "--resume", sid, "--output-format", "json"]
        argv += webapi._guardrail_args(policy.effective_guardrails(sid), True)
        try:
            with webterm.session_lock(sid):
                r = (run or subprocess.run)(argv, input=KEEP_PROMPT, cwd=cwd, env=ptyd.clean_env(), capture_output=True,
                                            text=True, encoding="utf-8", timeout=300,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (kept if r.returncode == 0 else failed).append(
                sid if r.returncode == 0 else {"session_id": sid, "error": f"exit {r.returncode}"})
        except Exception as e:  # noqa: BLE001
            failed.append({"session_id": sid, "error": str(e)[:200]})
    summary = (f"서빙 세션 {len(kept)}개 기한 연장" if kept else "기한이 다가온 서빙 세션 없음") +               (f" · 실패 {len(failed)}" if failed else "") + (f" · 떠 있어서 건너뜀 {len(skipped)}" if skipped else "")
    return {"kept": kept, "failed": failed, "skipped": skipped, "summary": summary}


def _run_fork_cleanup() -> dict:
    from session_manager import lifecycle
    return lifecycle.retry_forks()


def _run_worklog() -> dict:
    from session_manager import worklog
    return worklog.collect()


_RUNNERS = {"idle_sessions": reap_idle, "models": refresh_models, "serve_keep": keep_serving,
            "cleanup_watch": watch_cleanup, "worklog": _run_worklog, "fork_cleanup": _run_fork_cleanup}


def run_job(jid: str) -> dict:
    """작업 하나를 지금 이 스레드에서 실행하고 결과를 기록한다(작업 줄과 테스트가 쓴다)."""
    global _running
    _running = jid
    t0 = time.time()
    try:
        res = _RUNNERS[jid]()
        ok = True
    except Exception as e:  # noqa: BLE001  작업 하나의 실패가 Host 를 죽이면 안 된다
        res = {"summary": f"실패: {type(e).__name__}: {e}"}
        ok = False
    finally:
        _running = None
    with _lock:
        d = _load()
        st = _job_state(d, jid)
        st.update(last_run=int(t0), last_ok=ok, last_summary=str((res or {}).get("summary") or "")[:300])
        _save(d)
    return res


def kick(jid: str) -> bool:
    """「지금 실행」 — 작업 줄 뒤에 세운다(이미 같은 작업이 서 있으면 한 번만)."""
    if jid not in JOBS:
        return False
    if jid not in list(_q.queue):
        _q.put(jid)
    return True


def _due(now: float) -> list[str]:
    d = _load()
    out = []
    for jid, j in JOBS.items():
        if not enabled(jid, d):
            continue
        last = _job_state(d, jid).get("last_run") or 0
        if now - float(last) >= j["interval"]:
            out.append(jid)
    return out


def start(first_delay: float = FIRST_DELAY_S) -> threading.Thread:
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()

    def loop():
        if _stop.wait(first_delay):
            return
        while not _stop.is_set():
            for jid in _due(time.time()):
                kick(jid)
            try:
                jid = _q.get(timeout=TICK_S)
            except queue.Empty:
                continue
            run_job(jid)
            # 큐에 남은 것은 다음 바퀴에서 차례로(한 번에 하나씩)

    _thread = threading.Thread(target=loop, name="housekeeping", daemon=True)
    _thread.start()
    return _thread


def stop() -> None:
    _stop.set()
