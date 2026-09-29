"""관제 호출 실패 감지 — 저장 그룹의 **관리 세션 jsonl 만** 얇게 tail 해 하위 호출 실패를 푸시.

훅으로는 못 보는 것: 관리 에이전트가 `claude -p --resume <하위>` 로 부른 호출이 오류로
끝났는지(비-0 종료·타임아웃). 그 사실은 관리 세션의 Bash tool_result(is_error) 에만 남는다.
하위 세션의 훅(Stop)은 정상 응답일 때만 오므로, 실패는 여기서만 잡힌다.

비용: 그룹당 파일 1개(관리 세션), 2초 폴링, 완성된 줄만 오프셋 증분(monitor.Tailer 재사용),
시작 시점 이후 줄만(과거 오류 재알림 없음). 원본 jsonl 무접촉(읽기만) — 불가침 원칙 준수.

구조: 순수 동기 코어(Watcher.poll — 테스트 용이) + 데몬 스레드 러너(start/stop).
그룹 목록은 폴링마다 다시 읽어 저장·삭제·알림 플래그 변경이 즉시 반영된다.
"""
from __future__ import annotations

import re
import threading

from session_manager import appconfig, mongroups, push
from session_manager.monitor import Tailer, parse_calllines, resolve_peer

_SENDMSG_FAIL_RE = re.compile(r'"success"\s*:\s*false')


def _sendmsg_failed(text: str) -> bool:
    return bool(_SENDMSG_FAIL_RE.search(text or ""))

POLL_INTERVAL_S = 2.0
_PATH_RETRY_POLLS = 15           # 관리 세션 파일이 아직 없으면 30초마다 재탐색
_MAX_PENDING = 200               # tool_use_id → 대상 매핑 상한(짝 없는 호출이 쌓일 때)


def _path_of(session_id: str) -> str | None:
    """관리 세션 jsonl 경로만 싸게 찾는다(메타 파싱 없이 프로젝트 폴더 순회)."""
    from session_manager.scanner import projects_dir
    base = projects_dir()
    try:
        if not base.is_dir():
            return None
        for project_dir in base.iterdir():
            cand = project_dir / f"{session_id}.jsonl"
            if cand.is_file():
                return str(cand)
    except OSError:
        return None
    return None


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text")
    return ""


class _GroupTail:
    """그룹 하나의 관리 세션 tail 상태."""

    def __init__(self, group: dict, path: str | None):
        self.group = group
        self.manager = str(group.get("manager") or "")
        self.path = path
        self.tailer = Tailer(path)
        self.tailer.seek_end()
        self.pending: dict[str, str] = {}    # tool_use_id → 호출된 하위 sid
        self.polls_since_miss = 0

    @property
    def subs(self) -> set[str]:
        return set(self.group.get("subs") or [])


class Watcher:
    """동기 코어. poll() 한 번 = 그룹 목록 동기화 + 각 관리 세션의 새 줄 처리."""

    def __init__(self, notify=None):
        self.tails: dict[str, _GroupTail] = {}
        self._notify = notify or notify_error

    def poll(self) -> int:
        """반환: 이번 폴링에서 발송한 알림 수."""
        try:
            groups = mongroups.list_groups()
        except Exception:  # noqa: BLE001
            return 0
        wanted = {str(g.get("id")): g for g in groups
                  if g.get("manager") and mongroups.norm_notify(g.get("notify"))["error"]}
        for gid in list(self.tails):
            t = self.tails[gid]
            if gid not in wanted or t.manager != str(wanted[gid].get("manager") or ""):
                self.tails.pop(gid)              # 삭제·플래그 off·관리 세션 교체
        sent = 0
        for gid, g in wanted.items():
            t = self.tails.get(gid)
            if t is None:
                t = _GroupTail(g, _path_of(str(g["manager"])))
                self.tails[gid] = t
            else:
                t.group = g                      # 이름·라벨·하위 목록은 최신으로
            if t.path is None:                   # 아직 세션 파일이 없음(막 만든 그룹 등)
                t.polls_since_miss += 1
                if t.polls_since_miss >= _PATH_RETRY_POLLS:
                    t.polls_since_miss = 0
                    p = _path_of(t.manager)
                    if p:
                        t.path = p
                        t.tailer = Tailer(p)
                        t.tailer.seek_end()
                continue
            sent += self._drain(t)
        return sent

    def _follow_continuation(self, t: _GroupTail, new_id: str) -> None:
        """관리 세션이 새 세션으로 이어졌다(continued-in) → 그룹의 manager 를 갱신하고 새 파일을 tail.
        (이걸 안 하면 그룹이 죽은 id 를 가리켜 관제·알림이 멈춘다 — 타 PC 실측 2026-09-29)"""
        try:
            mongroups.set_manager(t.group.get("id"), new_id)
        except Exception:  # noqa: BLE001
            return
        t.manager = new_id
        t.group["manager"] = new_id
        t.path = _path_of(new_id)
        t.tailer = Tailer(t.path)
        t.tailer.seek_end()
        t.pending = {}
        print(f"[monwatch] [{t.group.get('name')}] 관리 세션 이어받음 → {new_id[:8]} 로 추적 전환")

    def _drain(self, t: _GroupTail) -> int:
        n = 0
        subs = t.subs
        for obj in t.tailer.read_new():
            if isinstance(obj, dict) and obj.get("type") == "continued-in" and obj.get("continuedInSessionId"):
                self._follow_continuation(t, str(obj["continuedInSessionId"]))
                break                                     # 새 파일은 다음 폴링부터
            if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    tid = b.get("id")
                    if not tid:
                        continue
                    target = None
                    if b.get("name") == "SendMessage":
                        # 워커 방식(v0.9.0): to=이름|uds:파이프 → 피어 레지스트리(+마지막으로 본 이름)로
                        # 세션 해석. 그래도 모르면 그룹 라벨과 같은 이름으로(사용자가 붙인 워커 이름).
                        to = str(inp.get("to") or "")
                        target = resolve_peer(to)
                        if target not in subs:
                            lbl = (t.group.get("labels") or {})
                            target = next((s for s in subs if lbl.get(s) == to), None)
                    else:
                        # 원본 command 그대로(뷰어의 절삭 없이) — 긴 프롬프트 뒤 UUID 도 잡는다
                        cmd = inp.get("command")
                        if isinstance(cmd, str):
                            calls = parse_calllines(cmd, subs)
                            if calls:
                                target = calls[0]["target"]
                    if target:
                        t.pending[str(tid)] = target
                        if len(t.pending) > _MAX_PENDING:
                            for k in list(t.pending)[:len(t.pending) - _MAX_PENDING]:
                                t.pending.pop(k, None)
                elif bt == "tool_result":
                    target = t.pending.pop(str(b.get("tool_use_id") or ""), None)
                    if not target:
                        continue
                    text = _result_text(b)
                    # Bash 실패는 is_error, SendMessage 실패는 200 결과 안의 "success":false
                    # (죽은 워커: "No agent named … is reachable")
                    if b.get("is_error") or _sendmsg_failed(text):
                        n += int(self._notify(t.group, target, text) or 0)
        return n


def notify_error(group: dict, target: str, text: str) -> int:
    """`[그룹] 하위라벨 호출 실패` 푸시. 본문은 오류 출력 앞부분(공백 정리, 120자)."""
    if not appconfig.get_bool("push", "monitor", True):
        return 0
    name = str(group.get("name") or "관제")
    label = mongroups.label_of(group, target)
    body = " ".join(str(text or "").split())[:120] or "하위 호출이 오류로 끝났습니다"
    n = push.send("mon-error", target, f"[{name}] {label} 호출 실패", body,
                  {"gid": str(group.get("id") or "")})
    print(f"[monwatch] [{name}] {label} 호출 실패 감지 → 알림 {n}건", flush=True)
    return n


# ---------------------------------------------------------------- 스레드 러너

_thread: threading.Thread | None = None
_stop: threading.Event | None = None


def start(interval: float = POLL_INTERVAL_S) -> threading.Thread:
    """데몬 스레드로 폴링 시작(이미 돌고 있으면 그대로). 기동 경로를 막지 않는다."""
    global _thread, _stop
    if _thread is not None and _thread.is_alive():
        return _thread
    stop_ev = threading.Event()              # 스레드마다 제 이벤트(재시작 경합 방지)
    w = Watcher()

    def run():
        while not stop_ev.wait(interval):
            try:
                w.poll()
            except Exception as e:  # noqa: BLE001  감시 실패가 Host 를 죽이면 안 된다
                print(f"[monwatch] {type(e).__name__}: {e}", flush=True)

    _stop = stop_ev
    _thread = threading.Thread(target=run, name="monwatch", daemon=True)
    _thread.start()
    return _thread


def stop() -> None:
    if _stop is not None:
        _stop.set()
