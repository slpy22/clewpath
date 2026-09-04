"""오케스트레이션 관제 — 여러 세션의 jsonl 을 실시간 tail·병합해 타임라인 이벤트로.

관리 에이전트가 하위 세션들을 `claude -p --resume <UUID> "프롬프트"` 로 부리는
소통을 위에서 관전한다. 관찰 대상이 "나↔세션"이 아니라 "에이전트↔에이전트".

설계(office-hours + plan-eng-review 2026-09-03, 실측 스파이크 통과):
- jsonl 은 append-only → **오프셋 증분 tail** 로 읽기전용 관전(파일 무수정, 무침습).
- 그룹(관리 + 하위 N)의 각 jsonl 을 tail 해 **timestamp 병합** → 단일 타임라인.
- **호출선(→)**: 세션의 Bash tool_use command 에서 `--resume <그룹멤버 UUID>` 를
  뽑아 source→target 을 잇는다. 그룹 밖 UUID 는 화살표 생략(우아한 저하).
- 하위 응답·내부 진행은 **하위 세션 자체 jsonl** 에서(더 깨끗). 관리 tool_result
  내용은 파싱 불필요.
- 링버퍼로 메모리 상한(webterm 패턴), 재연결 시 tail 리플레이.

이 모듈은 **순수 동기**다. 폴링 루프/WS 는 server.py 가 driving 한다(테스트 용이).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from session_manager.viewer import parse_record

# 오케스트레이션은 보통 세션 2~5개. 이벤트 링버퍼 상한(webterm _BUF_CAP 대응).
_RING_CAP = 4000
# 재연결/최초 진입 시 되감아 보여줄 tail 바이트(파일별). 큰 세션도 열기 빠르게.
_PRIME_TAIL_BYTES = 256_000
# 세션별 고정 색(타임라인 구분용). 인덱스로 순환 배정.
_PALETTE = ["#4f9cff", "#ff8c42", "#38b26a", "#c05cff",
            "#ffb01f", "#ff5d6c", "#00b3b3", "#8a8f98"]

# 호출선 정규식(실측 스파이크 확정 2026-09-03). --resume/-r 뒤 UUID.
# claude 명령 안에서만(다른 도구가 UUID 를 언급해도 오탐 안 되게 claude\b 요구).
_RESUME_RE = re.compile(
    r"claude\b[^\n]*?(?:--resume|-r)\s+"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
# 프롬프트: UUID 뒤 첫 따옴표 문자열(표시용, best-effort).
_PROMPT_RE = re.compile(r'"([^"]*)"' r"|'([^']*)'")


def parse_calllines(command: str, group_uuids: set[str]) -> list[dict]:
    """셸 command 에서 그룹 멤버를 향한 `claude --resume <UUID>` 호출을 추출한다.

    그룹 안 UUID 만 호출선으로 인정(밖이면 화살표 생략 = 저하). 프롬프트는
    UUID 뒤 첫 따옴표 문자열(없으면 빈 문자열, 화살표는 그려짐).
    반환: [{"target": uuid, "prompt": str}, ...]
    """
    if not command:
        return []
    out: list[dict] = []
    for m in _RESUME_RE.finditer(command):
        uid = m.group(1)
        if uid not in group_uuids:
            continue  # 그룹 밖 → 화살표 생략(우아한 저하)
        rest = command[m.end():]
        pm = _PROMPT_RE.search(rest)
        prompt = ""
        if pm:
            prompt = pm.group(1) if pm.group(1) is not None else (pm.group(2) or "")
        out.append({"target": uid, "prompt": prompt})
    return out


class Tailer:
    """단일 jsonl 파일의 바이트 오프셋 tail. append-only 전제, 파일 무수정.

    - 완전한 줄(개행까지)만 소비하고 오프셋을 바이트 단위로 전진(한글 등 멀티바이트
      안전). 개행 전 부분 줄은 다음 폴링까지 보류.
    - 파일이 줄어들면(트렁케이트/회전) 오프셋을 0 으로 리셋해 다시 읽는다.
    """

    def __init__(self, path: str | None):
        self.path = path
        self.offset = 0

    def _size(self) -> int:
        if not self.path:
            return 0
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def seek_tail(self, max_bytes: int = _PRIME_TAIL_BYTES) -> None:
        """최초 진입용: 최근 max_bytes 부근부터 시작(첫 부분 줄은 버림)."""
        size = self._size()
        if size <= max_bytes or not self.path:
            self.offset = 0
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(size - max_bytes)
                f.readline()               # 잘린 첫 줄 폐기
                self.offset = f.tell()
        except OSError:
            self.offset = 0

    def read_new(self) -> list[dict]:
        """오프셋 이후 새로 완성된 줄들을 파싱해 dict 리스트로. 오프셋 전진."""
        if not self.path:
            return []
        size = self._size()
        if size < self.offset:             # 트렁케이트/회전 → 처음부터
            self.offset = 0
        if size <= self.offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
        except OSError:
            return []
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return []                      # 아직 완성된 줄 없음(부분 줄 보류)
        complete = chunk[:nl + 1]
        self.offset += len(complete)
        objs: list[dict] = []
        for bline in complete.split(b"\n"):
            line = bline.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line.decode("utf-8", "replace")))
            except Exception:  # noqa: BLE001  깨진 줄은 건너뛰되 오프셋은 이미 전진
                continue
        return objs


@dataclass
class _Session:
    session_id: str
    path: str | None
    label: str
    color: str
    role: str = "sub"                      # "manager" | "sub" (표시용)
    tailer: Tailer = field(init=False)

    def __post_init__(self):
        self.tailer = Tailer(self.path)


class MonitorGroup:
    """관제 그룹 하나: 관리 + 하위 N 세션. poll() 로 병합 타임라인 이벤트를 뽑는다.

    순수 동기. 폴링 주기·WS 전송은 외부(server)가 담당.
    """

    def __init__(self, sessions: list[_Session]):
        self.sessions = sessions
        self.uuids = {s.session_id for s in sessions}
        self.buffer: list[dict] = []
        self._seq = 0

    # ---- 이벤트 생성 ----

    def _tag(self, ev: dict, sess: _Session) -> dict:
        """parse_record 이벤트에 세션 정체성(색·이름·역할)을 붙인다."""
        self._seq += 1
        out = dict(ev)
        out["session_id"] = sess.session_id
        out["session_label"] = sess.label
        out["color"] = sess.color
        out["session_role"] = sess.role
        out["seq"] = self._seq
        out["ts"] = ev.get("timestamp")
        # 호출선(→): 이 이벤트의 tool_calls 중 그룹 멤버를 부른 것
        calls_out: list[dict] = []
        for tc in ev.get("tool_calls") or []:
            cmd = ((tc.get("input") or {}).get("command")
                   if isinstance(tc.get("input"), dict) else None)
            if not cmd:
                continue
            for edge in parse_calllines(cmd, self.uuids):
                calls_out.append({"target_session_id": edge["target"],
                                  "prompt": edge["prompt"],
                                  "tool_use_id": tc.get("id")})
        out["calls_out"] = calls_out
        return out

    def _push(self, ev: dict) -> None:
        self.buffer.append(ev)
        if len(self.buffer) > _RING_CAP:    # 링버퍼: 앞에서 버려 상한 유지
            del self.buffer[:len(self.buffer) - _RING_CAP]

    # ---- 공개 API ----

    def prime(self) -> list[dict]:
        """최초 진입: 각 파일 최근 tail 부터 시작해 버퍼를 채우고 반환."""
        for s in self.sessions:
            s.tailer.seek_tail()
        return self.poll()

    def poll(self) -> list[dict]:
        """모든 세션의 새 이벤트를 읽어 timestamp 로 병합·태깅·버퍼링 후 반환.

        한 폴링 주기 내에서 모아 timestamp 안정 정렬(파일 간 미세 순서 보정).
        폴링 간에는 도착 순서(append-only 라 파일 내부는 이미 시간순).
        """
        batch: list[tuple[str, _Session, dict]] = []
        for sess in self.sessions:
            for obj in sess.tailer.read_new():
                ev = parse_record(obj)
                if ev is None:
                    continue
                batch.append((ev.get("timestamp") or "", sess, ev))
        batch.sort(key=lambda t: t[0])      # timestamp 안정 정렬
        out: list[dict] = []
        for _ts, sess, ev in batch:
            tagged = self._tag(ev, sess)
            self._push(tagged)
            out.append(tagged)
        return out

    def snapshot(self) -> list[dict]:
        """재연결 시 되감기용: 현재 링버퍼 전체."""
        return list(self.buffer)

    # ---- 동적 그룹 변경 ----

    def has(self, session_id: str) -> bool:
        return session_id in self.uuids

    def add_session(self, spec: dict) -> list[dict]:
        """관전 중 세션을 동적 추가. 최근 tail 부터 시작해 그 세션의 근래 이벤트를
        즉시 반환(버퍼에도 적재). 이미 있으면 빈 리스트."""
        sid = spec["session_id"]
        if sid in self.uuids:
            return []
        from session_manager.scanner import scan_one
        meta = scan_one(sid)
        path = meta.jsonl_path if meta else None
        label = (spec.get("label")
                 or (meta and (meta.custom_title or meta.ai_title or meta.slug))
                 or sid[:8])
        color = _PALETTE[len(self.sessions) % len(_PALETTE)]
        sess = _Session(sid, path, label, color, spec.get("role", "sub"))
        sess.tailer.seek_tail()             # 처음부터가 아니라 최근부터
        self.sessions.append(sess)
        self.uuids.add(sid)
        # 추가 직후 그 세션의 근래 이벤트를 뽑아 즉시 표시(맥락 제공)
        out: list[dict] = []
        for obj in sess.tailer.read_new():
            ev = parse_record(obj)
            if ev is None:
                continue
            tagged = self._tag(ev, sess)
            self._push(tagged)
            out.append(tagged)
        return out

    def remove_session(self, session_id: str) -> bool:
        """관전 중 세션을 동적 제거. 기존 버퍼 이벤트는 남고 새 이벤트만 멈춘다."""
        before = len(self.sessions)
        self.sessions = [s for s in self.sessions if s.session_id != session_id]
        self.uuids.discard(session_id)
        return len(self.sessions) != before


def build_group(specs: list[dict]) -> MonitorGroup:
    """세션 지정 목록으로 그룹을 만든다. path·label 은 스캐너로 해석.

    specs: [{"session_id": str, "label"?: str, "role"?: "manager"|"sub"}, ...]
    """
    from session_manager.scanner import scan_one
    sessions: list[_Session] = []
    for i, spec in enumerate(specs):
        sid = spec["session_id"]
        meta = scan_one(sid)
        path = meta.jsonl_path if meta else None
        label = (spec.get("label")
                 or (meta and (meta.custom_title or meta.ai_title
                               or meta.slug))
                 or sid[:8])
        color = _PALETTE[i % len(_PALETTE)]
        role = spec.get("role", "sub")
        sessions.append(_Session(sid, path, label, color, role))
    return MonitorGroup(sessions)
