"""세션 검색 — 키워드/날짜로 세션을 필터링한다."""
from __future__ import annotations

from session_manager import scanner


def _matches_keyword(jsonl_path: str, keyword: str, max_hits: int = 3) -> list[str]:
    """파일에서 키워드를 포함하는 줄의 스니펫을 최대 max_hits개 반환."""
    kw = keyword.lower()
    hits: list[str] = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                if kw in raw.lower():
                    snippet = raw.strip()
                    # 키워드 주변 추출
                    pos = snippet.lower().find(kw)
                    start = max(0, pos - 60)
                    end = min(len(snippet), pos + len(keyword) + 60)
                    hits.append(("…" if start > 0 else "") + snippet[start:end] + ("…" if end < len(snippet) else ""))
                    if len(hits) >= max_hits:
                        break
    except Exception:
        pass
    return hits


def search_sessions(keyword: str = "", date_from: str | None = None,
                    date_to: str | None = None) -> dict:
    """키워드(파일 내용)와 날짜(세션 시작 시각) 범위로 세션을 검색한다.

    date_from/date_to는 ISO 문자열 prefix 비교 (예: '2026-06-01').
    """
    sessions = scanner.scan_all()
    results = []
    for s in sessions:
        # 날짜 필터 (started_at 기준)
        if date_from and (s.started_at or "") < date_from:
            continue
        if date_to and (s.started_at or "") > date_to + "￿":
            continue
        # 키워드 필터
        snippets: list[str] = []
        if keyword:
            snippets = _matches_keyword(s.jsonl_path, keyword)
            if not snippets:
                continue
        results.append({**s.to_dict(), "snippets": snippets})

    return {
        "keyword": keyword,
        "date_from": date_from,
        "date_to": date_to,
        "match_count": len(results),
        "results": results,
    }
