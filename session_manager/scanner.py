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
    git_branch: str | None
    version: str | None      # Claude Code 버전
    started_at: str | None   # 첫 timestamp
    ended_at: str | None     # 마지막 timestamp
    message_count: int       # 파싱 가능한 메시지 수
    line_count: int          # 전체 줄 수 (빈 줄 제외)
    size_bytes: int
    mtime: float             # 파일 수정 시각 (epoch)
    has_side_dir: bool       # {uuid}/ 사이드 폴더 존재 여부

    def to_dict(self) -> dict:
        return asdict(self)


# 파일 경로 → (mtime, size, SessionMeta) 캐시.
# 파일이 안 바뀌면(mtime+size 동일) 다시 읽지 않는다 → 재조회가 매우 빨라짐.
_CACHE: dict[str, tuple[float, int, "SessionMeta"]] = {}


def _scan_one(jsonl_path: Path) -> SessionMeta:
    """단일 jsonl 파일에서 메타를 추출한다(변경 없으면 캐시 반환)."""
    stat = jsonl_path.stat()
    key = str(jsonl_path)
    cached = _CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    meta = _parse_meta(jsonl_path, stat)
    _CACHE[key] = (stat.st_mtime, stat.st_size, meta)
    return meta


def _parse_meta(jsonl_path: Path, stat) -> SessionMeta:
    """단일 jsonl 파일에서 메타데이터를 추출한다. 스트리밍 1-pass."""
    session_id = jsonl_path.stem
    project_folder = jsonl_path.parent.name

    # 메타 필드는 첫 줄에 없을 수 있다(사이드체인 메시지 등).
    # 각 필드를 '처음 등장하는 값'으로 채운다. 1-pass 유지.
    meta: dict[str, str | None] = {
        "cwd": None, "slug": None, "gitBranch": None, "version": None,
    }
    last_line: str = ""
    started_at: str | None = None
    last_ts: str | None = None   # 마지막으로 본 timestamp(마지막 줄이 아님)
    line_count = 0
    message_count = 0
    # 네이티브 이름 레코드 — '마지막 값'이 유효(rename 시마다 append 되므로)
    custom_title: str | None = None
    ai_title: str | None = None
    agent_name: str | None = None

    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                line_count += 1
                last_line = line
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                message_count += 1
                ts = obj.get("timestamp")
                if ts:
                    last_ts = ts
                    if started_at is None:
                        started_at = ts
                for key in meta:
                    if meta[key] is None and obj.get(key):
                        meta[key] = obj[key]
                t = obj.get("type")
                if t == "custom-title" and obj.get("customTitle"):
                    custom_title = obj["customTitle"]
                elif t == "ai-title" and obj.get("aiTitle"):
                    ai_title = obj["aiTitle"]
                elif t == "agent-name" and obj.get("agentName"):
                    agent_name = obj["agentName"]
    except Exception:
        # 파일 읽기 자체가 실패하면 최소 정보만 반환
        pass

    # 마지막 줄이 custom-title/snapshot(타임스탬프 없음)일 수 있으므로 last_ts 사용
    ended_at: str | None = last_ts

    side_dir = jsonl_path.parent / session_id

    return SessionMeta(
        session_id=session_id,
        project_folder=project_folder,
        jsonl_path=str(jsonl_path),
        cwd=meta["cwd"],
        slug=meta["slug"],
        custom_title=custom_title,
        ai_title=ai_title,
        agent_name=agent_name,
        git_branch=meta["gitBranch"],
        version=meta["version"],
        started_at=started_at,
        ended_at=ended_at,
        message_count=message_count,
        line_count=line_count,
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        has_side_dir=side_dir.is_dir(),
    )


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
