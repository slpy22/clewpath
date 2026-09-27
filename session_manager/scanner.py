"""세션 스캐너 — ~/.claude/projects 를 스캔하여 세션 메타데이터를 추출한다.

방어적 설계 원칙:
- JSONL 스키마를 가정하지 않는다. 알려진 핵심 필드만 가볍게 읽는다.
- 파싱 실패한 줄은 건너뛰되 카운트는 유지한다.
- 원본 파일은 절대 수정하지 않는다 (읽기 전용).
- 큰 파일(수 MB)도 스트리밍으로 처리한다 (전체를 메모리에 올리지 않음).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
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
                 "interactive_markers", "ai_title", "agent_name", "last_model", "offset", "sig")

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
        self.offset = 0                       # 접은 바이트 수(완성된 줄 끝)
        self.sig = b""                        # 오프셋 직전 64바이트 — 앞부분 재작성 감지용

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
        elif t in ("mode", "permission-mode", "system"):
            self.interactive_markers += 1
        # 마지막 실사용 모델: assistant 레코드의 message.model(계속 덮어써 마지막값).
        # <synthetic>(시스템 생성)는 실모델이 아니라 제외 → 재개 계승 모델과 일치.
        if t == "assistant":
            _m = obj.get("message")
            if isinstance(_m, dict):
                _mdl = _m.get("model")
                if _mdl and _mdl != "<synthetic>":
                    self.last_model = _mdl

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
        )


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
