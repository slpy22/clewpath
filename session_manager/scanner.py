"""세션 스캐너 — ~/.claude/projects 를 스캔하여 세션 메타데이터를 추출한다.

방어적 설계 원칙:
- JSONL 스키마를 가정하지 않는다. 알려진 핵심 필드만 가볍게 읽는다.
- 파싱 실패한 줄은 건너뛰되 카운트는 유지한다.
- 원본 파일은 절대 수정하지 않는다 (읽기 전용).
- 큰 파일(수 MB)도 스트리밍으로 처리한다 (전체를 메모리에 올리지 않음).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

from session_manager.config import projects_dir
from session_manager.pathenc import path_to_folder


@dataclass
class SessionMeta:
    session_id: str          # uuid (파일명 stem)
    project_folder: str      # 인코딩된 폴더명 (표시용, 역변환 불가)
    jsonl_path: str          # 절대 경로
    cwd: str | None          # 실제 작업 경로 (jsonl 내 권위 소스)
    slug: str | None         # 세션 슬러그(랜덤 코드네임)
    custom_title: str | None  # 네이티브 이름(Ctrl+R). JSONL custom-title 레코드 최신값
    ai_title: str | None      # AI 자동 제목(ai-title 레코드 최신값)
    agent_name: str | None    # branch/agent 이름(agent-name 레코드 최신값)
    last_model: str | None    # 마지막 assistant 응답의 message.model(<synthetic> 제외).
                              # claude --resume 이 --model 없이 이어받는 모델(실측 확정).
    git_branch: str | None
    version: str | None      # Claude Code 버전
    started_at: str | None   # 첫 timestamp
    ended_at: str | None     # 마지막 timestamp
    message_count: int       # 파싱 가능한 메시지 수
    line_count: int          # 전체 줄 수 (빈 줄 제외)
    size_bytes: int
    mtime: float             # 파일 수정 시각 (epoch)
    has_side_dir: bool       # {uuid}/ 사이드 폴더 존재 여부
    # claude 의 `--resume` 피커에 안 나오는 세션(에이전트/포크/헤드리스 산물).
    # 판정: (대화형 마커 mode/permission-mode/system) 또는 (agent-name) 유무
    # - 피커 표시 여부와 일치(실측 확정 2026-08-26). _parse_meta 참조.
    picker_hidden: bool = False
    # claude 가 이 대화를 이어간 새 세션 id(continued-in 레코드). 있으면 이 파일은 '옛 줄'.
    continued_in: str | None = None
    # 헤드리스(claude -p · Agent SDK) 단발 실행 기록: entrypoint 가 보였고 그중 'cli' 가 하나도 없다.
    # 다른 세션·스크립트가 일감 하나씩 돌린 산물이라 목록에서 접어 둔다(2026-10-12 번역 스크립트 56개 실측).
    headless: bool = False
    # 아직 끝났다는 표시가 없는 백그라운드 작업(쉘·모니터): [[task_id, "shell"|"monitor", 시작 epoch 초], ...]
    # claude 가 다시 뜨면 이전 작업은 함께 죽으므로, '지금 몇 개' 는 프로세스 시작 이후 것만 센다(bg_counts).
    bg_open: list = field(default_factory=list)
    # 여기서 끝 알림을 받았지만 여기서 시작하지 않은 작업 id — 하위 에이전트가 시작하고 그 에이전트가 먼저 끝나 부모로 온 끝(2026-10-29
    # 실측 34/900). bg_counts 가 하위 기록(subagents/**)의 열린 작업에서 뺀다
    bg_shut: list = field(default_factory=list)
    # 마지막 '대화' 시각(user·assistant·작업 알림 줄). 쉬는 세션 판정용 — 파일 mtime 은 claude 가 쉬는 동안에도
    # 끝에 시각 없는 메타 줄(last-prompt·mode·atis-latch 등)을 다시 써서 갱신되므로 못 쓴다(2026-10-13 실측).
    last_activity_at: str | None = None
    # 마지막 assistant 응답의 effort(low|medium|high|xhigh|max — claude 가 응답 줄 맨 위에 남긴다, 실측 2026-10-28).
    # last_model 과 같은 줄에서 읽는다. 옛 버전 기록엔 없어 None
    last_effort: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _Fold:
    """jsonl 을 한 줄씩 접어 메타를 만드는 누적기(v0.9.3 증분 파싱의 핵심).

    모든 필드가 '처음 값'(cwd/slug/gitBranch/version/started_at) 또는 '마지막 값·카운트'
    (제목/모델/timestamp/대화형 마커/줄 수)라, 파일 끝에 붙은 줄만 이어서 접으면 처음부터
    다시 읽은 것과 같은 결과가 된다. 오프셋은 **완성된 줄의 끝**에만 두고, 개행 없는 부분 줄은
    다음 스캔으로 미룬다(claude 가 쓰는 중인 줄을 반만 읽지 않게).
    """
    __slots__ = ("meta", "started_at", "last_ts", "line_count", "message_count", "custom_title",
                 "interactive_markers", "ai_title", "agent_name", "last_model", "last_effort", "continued_in", "offset", "sig",
                 "ep_cli", "ep_other", "bg", "last_act", "bg_shut")

    def __init__(self):
        # 메타 필드는 첫 줄에 없을 수 있다(사이드체인 메시지 등) → '처음 등장하는 값'
        self.meta: dict[str, str | None] = {"cwd": None, "slug": None, "gitBranch": None, "version": None}
        self.started_at: str | None = None
        self.last_ts: str | None = None      # 마지막으로 본 timestamp(마지막 줄이 아님)
        self.line_count = 0
        self.message_count = 0
        self.custom_title: str | None = None  # 네이티브 이름 — '마지막 값'(rename 마다 append)
        # 대화형 세션 마커: mode/permission-mode/system 레코드는 대화형 UI 를 거친 세션에만
        # 생긴다(헤드리스 -p/에이전트 dispatch 산물엔 0). claude --resume 픽커 기준과 일치
        # (실측 2026-08-26: gitBranch 동일한 세 세션에서 이 마커 유무만이 표시 여부를 갈랐다).
        self.interactive_markers = 0
        self.ai_title: str | None = None
        self.agent_name: str | None = None
        self.last_model: str | None = None    # 마지막 실사용 모델(재개 시 계승)
        self.last_effort: str | None = None   # 그 응답의 effort(목록 표시용)
        self.continued_in: str | None = None  # continued-in 레코드: 이 대화가 이어진 새 세션 id
        self.offset = 0                       # 접은 바이트 수(완성된 줄 끝)
        self.sig = b""                        # 오프셋 직전 64바이트 — 앞부분 재작성 감지용
        self.ep_cli = False                   # entrypoint 'cli'(대화형) 를 본 적 있나
        self.ep_other = False                 # 그 밖의 entrypoint(sdk-cli 등 헤드리스)를 본 적 있나
        self.bg: dict[str, tuple[str, float]] = {}   # 열린 백그라운드 작업 id → (종류, 시작 시각)
        self.last_act: str | None = None      # 마지막 대화 줄 timestamp(쉬는 동안 claude 가 스스로 쓰는 줄 제외)
        self.bg_shut: dict[str, None] = {}     # 여기서 시작하지 않은 작업의 끝(순서 유지·상한 _BG_SHUT_MAX)

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.line_count += 1
        try:
            obj = json.loads(line)
        except Exception:
            return
        self.message_count += 1
        ts = obj.get("timestamp")
        if ts:
            self.last_ts = ts
            if self.started_at is None:
                self.started_at = ts
        for key in self.meta:
            if self.meta[key] is None and obj.get(key):
                self.meta[key] = obj[key]
        t = obj.get("type")
        if t == "custom-title" and obj.get("customTitle"):
            self.custom_title = obj["customTitle"]
        elif t == "ai-title" and obj.get("aiTitle"):
            self.ai_title = obj["aiTitle"]
        elif t == "agent-name" and obj.get("agentName"):
            self.agent_name = obj["agentName"]
        elif t == "continued-in" and obj.get("continuedInSessionId"):
            # claude 가 대화를 새 세션 파일로 이어간 표식(옛 파일 끝에 남는다). 이 세션은 '옛 줄' —
            # 목록에선 접고, 재개는 새 id 로 보내야 대화가 두 갈래로 갈라지지 않는다(0.9.9).
            self.continued_in = str(obj["continuedInSessionId"])
        elif t in ("mode", "permission-mode"):
            self.interactive_markers += 1
        elif t == "system" and obj.get("subtype") != "stop_hook_summary":
            # stop_hook_summary 는 Stop 훅이 있으면 헤드리스(-p) 에도 남는다(claude 2.1.29x 실측) —
            # 대화형 마커로 세면 -p 산물이 일반 세션처럼 보인다(2026-10-12 번역 56개 사례).
            self.interactive_markers += 1
        if t in ("user", "attachment"):
            self._bg_feed(obj, ts)
        if ts and t in ("user", "assistant", "queue-operation") and not obj.get("isCompactSummary")                 and not obj.get("isMeta"):
            self.last_act = ts
        ep = obj.get("entrypoint")
        if ep:
            if ep == "cli":
                self.ep_cli = True
            else:
                self.ep_other = True
        # 마지막 실사용 모델: assistant 레코드의 message.model(계속 덮어써 마지막값).
        # <synthetic>(시스템 생성)는 실모델이 아니라 제외 → 재개 계승 모델과 일치.
        if t == "assistant":
            _m = obj.get("message")
            if isinstance(_m, dict):
                _mdl = _m.get("model")
                if _mdl and _mdl != "<synthetic>":
                    self.last_model = _mdl
                    _eff = obj.get("effort")
                    if isinstance(_eff, str) and _eff:
                        self.last_effort = _eff

    def _bg_feed(self, obj: dict, ts) -> None:
        """백그라운드 쉘·모니터·서브에이전트의 시작/끝 표식(claude 가 jsonl 에 남기는 도구 결과·작업 알림, 실측 2.1.x).
        시작: Bash 결과 'Command running in background with ID: X' / '…moved to the background (ID: X)',
              Monitor 결과 'Monitor started (task X, …', Agent 결과 'Async agent launched successfully. … agentId: X',
              Workflow 결과 'Workflow launched in background. Task ID: X'(2026-10-28 실측 — 에이전트로 센다).
              끝: '<task-notification>' 의 <status>, TaskStop 결과,
              모니터 만료 알림(<status> 없이 사건 모양으로 온다 — _expired_task).
        문장 **앞머리**만 본다 — 다른 도구 출력에 같은 글귀가 인용돼도(이 파일을 grep 한 결과 등) 세지 않게."""
        for text, is_result in _bg_texts(obj):
            if is_result:
                m = _BG_START.match(text)
                if m:
                    tid = m.group(2) or m.group(3) or m.group(4) or m.group(5) or m.group(6)
                    kind = "monitor" if m.group(4) else "agent" if (m.group(5) or m.group(6)) else "shell"
                    self.bg[tid] = (kind, _epoch(ts))
                    continue
                m = _BG_STOPPED.match(text)
                if m:
                    self._close(m.group(1))
                    continue
            if text.startswith("<task-notification>"):
                # 알림 머리(첫 <event> 앞)만 본다 — 감시 출력 속에 인용된 다른 알림의 <status>·<task-id> 로 끝내지 않게
                i = text.find("<event>")
                head = text if i < 0 else text[:i]
                if "<status>" in head:
                    for tid in _TASK_ID.findall(head):
                        self._close(tid)
                else:
                    tid = _expired_task(text)
                    if tid and self.bg.get(tid, ("",))[0] == "monitor":
                        self.bg.pop(tid, None)

    def _close(self, tid: str) -> None:
        if self.bg.pop(tid, None) is None:
            self.bg_shut[tid] = None
            if len(self.bg_shut) > _BG_SHUT_MAX:
                self.bg_shut.pop(next(iter(self.bg_shut)))

    def feed_bytes(self, chunk: bytes) -> int:
        """완성된 줄만 접고, 접은 바이트 수를 돌려준다(부분 줄은 접지 않음)."""
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return 0
        done = chunk[:nl + 1]
        for bline in done.split(b"\n"):
            if bline:
                self.feed(bline.decode("utf-8", "replace"))
        self.offset += len(done)
        self.sig = done[-_SIG_BYTES:] if len(done) >= _SIG_BYTES else (self.sig + done)[-_SIG_BYTES:]
        return len(done)

    def to_meta(self, jsonl_path: Path, stat) -> "SessionMeta":
        session_id = jsonl_path.stem
        return SessionMeta(
            session_id=session_id,
            project_folder=jsonl_path.parent.name,
            jsonl_path=str(jsonl_path),
            cwd=self.meta["cwd"],
            slug=self.meta["slug"],
            custom_title=self.custom_title,
            ai_title=self.ai_title,
            agent_name=self.agent_name,
            last_model=self.last_model,
            git_branch=self.meta["gitBranch"],
            version=self.meta["version"],
            started_at=self.started_at,
            # 마지막 줄이 custom-title/snapshot(타임스탬프 없음)일 수 있으므로 last_ts 사용
            ended_at=self.last_ts,
            last_activity_at=self.last_act,
            last_effort=self.last_effort,
            message_count=self.message_count,
            line_count=self.line_count,
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
            has_side_dir=(jsonl_path.parent / session_id).is_dir(),
            # 픽커 표시 규칙(실측 확정 2026-08-26, E:\004·E:\020 교차검증):
            # claude --resume 픽커는 (대화형 마커 mode/permission-mode/system) 또는
            # (agent-name 레코드)가 있으면 나열한다. agent-name 만 있고 대화 내용이
            # 없는 스텁 세션(fork/named 산물)도 픽커에 뜬다 - 이 케이스를 놓쳐 흐리게
            # 오판하던 버그. 둘 다 없는 순수 헤드리스(-p) 세션만 미표시.
            picker_hidden=(self.interactive_markers == 0 and self.agent_name is None
                           and self.line_count > 0),
            continued_in=self.continued_in,
            headless=(self.ep_other and not self.ep_cli),
            bg_open=[[k, v[0], v[1]] for k, v in self.bg.items()],
            bg_shut=list(self.bg_shut),
        )


_BG_START = re.compile(r"(Command running in background with ID: (\w+)"
                       r"|Command did not complete within .{0,40}?moved to the background \(ID: (\w+)\)"
                       r"|Monitor started \(task (\w+)"
                       # 서브에이전트: 첫 줄 뒤 안내문 다음 줄에 'agentId: X'(2026-10-28 실측 103건)
                       r"|Async agent launched successfully\.[\s\S]{0,600}?agentId: (\w+)"
                       r"|Workflow launched in background\. Task ID: (\w+))")
_BG_STOPPED = re.compile(r'\{"message":"Successfully stopped task: (\w+)')
_TASK_ID = re.compile(r"<task-id>(\w+)</task-id>")
# 모니터 만료 문구(claude 가 붙인다): 2.1.27x~ '[Monitor expired after Nm with … Re-arm it …]'(N = 20·25·30 실측),
# 그 전 'timeout Nms' 모니터는 '[Monitor timed out — re-arm if needed.]'(2026-10-21 이 PC 기록 전체 실측)
_BG_EXPIRED = re.compile(r"\s*\[Monitor (?:expired after|timed out)\b")


def _expired_task(text: str) -> str | None:
    """모니터 만료 알림이면 그 모니터의 작업 id. 만료 문구가 첫 <event> **맨 앞**일 때만 끝으로 본다 —
    감시 출력 줄 속에 같은 글귀·다른 작업 알림이 인용돼도 끝내지 않는다. id 는 그 사건 앞의 첫 <task-id>(알림 자신)."""
    i = text.find("<event>")
    if i < 0 or not _BG_EXPIRED.match(text, i + len("<event>")):
        return None
    m = _TASK_ID.search(text, 0, i)
    return m.group(1) if m else None


def _epoch(ts) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _bg_texts(obj: dict):
    """(문자열, 도구 결과인가) — user 메시지의 tool_result·본문, queued_command 첨부의 prompt."""
    if obj.get("type") == "attachment":
        a = obj.get("attachment") or {}
        if a.get("type") == "queued_command" and isinstance(a.get("prompt"), str):
            yield a["prompt"], False
        return
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return
    c = msg.get("content")
    if isinstance(c, str):
        yield c, False
        return
    if not isinstance(c, list):
        return
    for b in c:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            rc = b.get("content")
            if isinstance(rc, str):
                yield rc, True
            elif isinstance(rc, list):
                for x in rc:
                    if isinstance(x, dict) and isinstance(x.get("text"), str):
                        yield x["text"], True
        elif b.get("type") == "text" and isinstance(b.get("text"), str):
            yield b["text"], False


_BG_SHUT_MAX = 500
# 하위 에이전트(Agent·Workflow)가 띄운 작업은 끝 표식이 아예 없는 것이 있다(2026-10-29 실측 12/900 — 쉘 11·모니터 1).
# 그런 유령이 세션을 계속 '백그라운드'로 붙잡지(완료 알림도 미룸) 않게, 끝 표식이 없어도 이만큼 지나면 세지 않는다
SUB_BG_MAX_S = 2 * 3600


def sub_bg_open(meta: "SessionMeta", since: float) -> list:
    """하위 에이전트 기록(<세션>/subagents/**/*.jsonl)에서 아직 안 끝난 작업 [[id, 종류, 시작]] — 지금 프로세스가 뜬 뒤 바뀐 파일만
    읽는다(그 전 파일엔 지금 프로세스의 작업이 없다). 2026-10-29: 전엔 본 기록만 봐서 하위 에이전트의 쉘이 남아도 '완료'였다."""
    out: list = []
    # scandir 의 항목 정보로 mtime 을 본다(Windows 는 폴더 읽기에 딸려 와 파일마다 stat 하지 않음 — 20초 목록마다 도는 길)
    stack = [str(Path(meta.jsonl_path).with_suffix("") / "subagents")]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.name.endswith(".jsonl") and e.stat(follow_symlinks=False).st_mtime + 2 >= since:
                        out.extend(_scan_one(Path(e.path)).bg_open or [])
                except Exception:  # noqa: BLE001
                    continue
    return out


def bg_counts(meta: "SessionMeta", since: float | None, now: float | None = None) -> dict:
    """지금 살아 있는 claude 프로세스(시작 시각 since, epoch 초) 이후에 시작해 아직 안 끝난 쉘·모니터·서브에이전트 수.
    하위 에이전트가 띄운 것도 센다(부모로 온 끝 알림은 빼고, SUB_BG_MAX_S 넘은 것은 유령으로 본다)."""
    out = {"shells": 0, "monitors": 0, "agents": 0}
    if since is None:
        return out
    import time
    now = time.time() if now is None else now
    shut = set(meta.bg_shut or [])
    mine = [(x, False) for x in meta.bg_open or []]
    subs = [(x, True) for x in sub_bg_open(meta, since)]
    for (tid, kind, t0), from_sub in mine + subs:
        if t0 and t0 + 2 < since:      # 이전 프로세스의 작업 = 함께 죽었다(2초 여유: 시계 오차)
            continue
        if from_sub and (tid in shut or not t0 or now - t0 > SUB_BG_MAX_S):
            continue
        out[{"shell": "shells", "agent": "agents"}.get(kind, "monitors")] += 1
    return out


_SIG_BYTES = 64
_READ_CHUNK = 1 << 20

# 파일 경로 → (mtime, size, SessionMeta, 누적기) 캐시.
# 안 바뀌면(mtime+size 동일) 그대로, 자랐으면 새 바이트만 접는다(증분), 그 외는 전체 재파싱.
_CACHE: dict[str, tuple[float, int, "SessionMeta", _Fold]] = {}
# 증분/전체 횟수(진단·테스트용)
STATS = {"incremental": 0, "full": 0}


def invalidate(jsonl_path) -> None:
    """이 파일을 **재작성**한 코드(세션 이사 cwd 치환·복구·가져오기)는 반드시 부른다 —
    append-only 전제가 깨진 파일을 증분으로 접으면 옛 상태 위에 새 줄을 얹는다."""
    _CACHE.pop(str(jsonl_path), None)


def _read_tail_sig(f, offset: int) -> bytes:
    if offset <= 0:
        return b""
    start = max(0, offset - _SIG_BYTES)
    f.seek(start)
    return f.read(offset - start)


def _fold_from(jsonl_path: Path, fold: _Fold) -> _Fold:
    """fold.offset 부터 파일 끝까지 완성된 줄을 접는다(증분·전체 공용)."""
    with jsonl_path.open("rb") as f:
        f.seek(fold.offset)
        pending = b""
        while True:
            chunk = f.read(_READ_CHUNK)
            if not chunk:
                break
            buf = pending + chunk
            used = fold.feed_bytes(buf)
            pending = buf[used:]
        # pending = 개행 없는 마지막 부분 줄 → 접지 않고 다음 스캔으로(오프셋도 그 앞)
    return fold


def _scan_one(jsonl_path: Path) -> SessionMeta:
    """단일 jsonl 파일에서 메타를 추출한다(변경 없으면 캐시, 자랐으면 증분, 아니면 전체)."""
    stat = jsonl_path.stat()
    key = str(jsonl_path)
    cached = _CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    fold: _Fold | None = None
    if cached and stat.st_size >= cached[3].offset > 0:
        # 증분 후보. 오프셋 직전 서명이 그대로여야 '뒤에만 붙은' 파일이다(축소·교체·앞부분
        # 재작성은 전체 재파싱 — Gemini·Codex 지적: mtime 만 믿으면 정합성이 깨진다).
        try:
            with jsonl_path.open("rb") as f:
                same = _read_tail_sig(f, cached[3].offset) == cached[3].sig
        except OSError:
            same = False
        if same:
            fold = cached[3]
            STATS["incremental"] += 1
    if fold is None:
        fold = _Fold()
        STATS["full"] += 1
    try:
        _fold_from(jsonl_path, fold)
    except Exception:
        # 파일 읽기 자체가 실패하면 지금까지의 누적으로 최소 정보만 반환
        pass
    meta = fold.to_meta(jsonl_path, stat)
    _CACHE[key] = (stat.st_mtime, stat.st_size, meta, fold)
    return meta


def _parse_meta(jsonl_path: Path, stat) -> SessionMeta:
    """단일 jsonl 파일에서 메타데이터를 처음부터 추출한다(캐시 무관, 테스트·비교용)."""
    fold = _Fold()
    try:
        _fold_from(jsonl_path, fold)
    except Exception:
        pass
    return fold.to_meta(jsonl_path, stat)


def scan_all() -> list[SessionMeta]:
    """모든 프로젝트의 모든 세션을 스캔한다. 최근 수정순 정렬."""
    base = projects_dir()
    if not base.is_dir():
        return []

    results: list[SessionMeta] = []
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_path in project_dir.glob("*.jsonl"):
            if not jsonl_path.is_file():
                continue
            try:
                results.append(_scan_one(jsonl_path))
            except Exception:
                continue

    results.sort(key=lambda m: m.mtime, reverse=True)
    return results


def scan_one(session_id: str) -> SessionMeta | None:
    """특정 세션 ID로 메타를 찾는다."""
    base = projects_dir()
    if not base.is_dir():
        return None
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return _scan_one(candidate)
    return None


def latest_session_id(session_id: str, max_hops: int = 8) -> str:
    """continued-in 체인을 따라 '지금 살아 있는 줄' 의 세션 id 를 돌려준다.

    이어받은 파일이 없으면(삭제·다른 PC) 마지막으로 확인된 id 에서 멈춘다. 순환·과다 홉은 끊는다.
    재개·중복 검사·bg 점유 검사는 전부 이 id 기준이어야 옛 줄에 프로세스가 붙어 대화가 갈라지는
    사고(2026-09-29 타 PC 실측)를 막는다.
    """
    cur = session_id
    seen = {cur}
    for _ in range(max_hops):
        m = scan_one(cur)
        nxt = m.continued_in if m else None
        if not nxt or nxt in seen or scan_one(nxt) is None:
            break
        seen.add(nxt)
        cur = nxt
    return cur


def resolve_launch_cwd(meta: SessionMeta) -> str | None:
    """claude --resume 이 이 세션을 찾을 수 있는 '실행 디렉토리'를 반환한다.

    claude 는 실행 위치(cwd)를 인코딩해 ~/.claude/projects/<enc>/ 에서 세션을 찾는다.
    보통은 세션의 cwd 가 저장 폴더와 일치한다. 그러나 fork(브랜치)로 cwd 가 하위/다른
    폴더로 바뀐 세션은 cwd 인코딩이 실제 저장 폴더와 어긋나 'No conversation found' 가 난다.
    이 경우 같은 폴더의 다른 세션 cwd 중 폴더명과 일치하는 실재 디렉토리를 대신 사용한다.
    """
    folder = Path(meta.jsonl_path).parent.name
    cwd = meta.cwd
    if cwd and path_to_folder(cwd) == folder and os.path.isdir(cwd):
        return cwd  # 정상: cwd 인코딩이 저장 폴더와 일치

    # fork 등 불일치 → 같은 폴더의 형제 세션에서 폴더명과 일치하는 실재 cwd 탐색
    proj_dir = Path(meta.jsonl_path).parent
    try:
        siblings = list(proj_dir.glob("*.jsonl"))
    except Exception:  # noqa: BLE001
        siblings = []
    for jf in siblings:
        if jf.stem == meta.session_id:
            continue
        try:
            sib = _scan_one(jf)  # 캐시됨 → 빠름
        except Exception:  # noqa: BLE001
            continue
        if sib.cwd and path_to_folder(sib.cwd) == folder and os.path.isdir(sib.cwd):
            return sib.cwd

    # 폴백: 세션 cwd(존재 시), 없으면 None
    return cwd if (cwd and os.path.isdir(cwd)) else None


def group_by_project(sessions: list[SessionMeta]) -> dict[str, list[SessionMeta]]:
    """프로젝트 폴더별로 세션을 그룹핑한다."""
    groups: dict[str, list[SessionMeta]] = {}
    for s in sessions:
        groups.setdefault(s.project_folder, []).append(s)
    return groups
