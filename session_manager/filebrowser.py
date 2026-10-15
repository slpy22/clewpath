"""세션 파일 보기(📁, 읽기 전용) — 세션 작업 폴더(cwd) 안의 폴더 목록·파일 내용.

설계: docs/designs/file-browser.md §5·§6. 핵심 규칙
- 뿌리 = 세션 cwd. 그 밖은 못 본다(`..`·절대 경로·심볼릭 링크/정션 탈출은 realpath 후 포함 검사로 거부).
  cwd 가 드라이브 맨 위(C:\\)면 거부.
- 항상 숨김(목록에도 안 나옴, 열기 404): ~/.claude(=claude 데이터·ClewPath 데이터), .ssh, .gnupg, AppData.
- 보호 파일(이름은 보이고 내용은 🔒): .env*, *.pem, *.key, … , release-keys/ 아래 전부.
- 쓰기 없음. claude 파일 불가침 원칙과 닿지 않도록 ~/.claude 는 아예 숨긴다.
"""
from __future__ import annotations

import base64
import fnmatch
import mimetypes
import os
import stat as _stat
from pathlib import Path, PurePosixPath

from session_manager import config

MAX_ENTRIES = 1000                    # 한 폴더 항목 상한(넘으면 truncated)
TEXT_MAX = 2 * 1024 * 1024            # 텍스트 보기 상한(릴레이 전송 고려)
RAW_MAX_RELAY = 10 * 1024 * 1024      # 이미지 미리보기·내려받기(릴레이 경유)
RAW_MAX_LOCAL = 20 * 1024 * 1024      # 같은 것, PC 로컬

# 무거운 폴더: 흐리게 표시하고 펼쳐도 되지만 항목 수를 미리 세지 않는다
HEAVY_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
                        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "target", ".gradle", ".idea"})

# 이름만으로 숨기는 폴더(어디 있든). 비교는 소문자.
HIDDEN_NAMES = frozenset({".claude", ".ssh", ".gnupg", "appdata"})

# 보호 파일 — 이름은 보이되 내용·내려받기 차단. 소문자 fnmatch.
PROTECTED_GLOBS = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.pfx", "*.p12", "*.keystore", "*.jks",
                   "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*credentials*", "*secret*", "*.kdbx",
                   ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", "devices.json", "ptyd.key")
PROTECTED_DIRS = frozenset({"release-keys"})          # 이 이름의 폴더 아래는 전부 보호

IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"})
_REPARSE = 0x400                                       # FILE_ATTRIBUTE_REPARSE_POINT(정션·심볼릭 링크)


class FBError(Exception):
    def __init__(self, code: str, status: int = 400, **extra):
        super().__init__(code)
        self.code, self.status, self.extra = code, status, extra

    def to_dict(self) -> dict:
        return {"error": self.code, **self.extra}


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _inside(child: str, root: str) -> bool:
    """정규화된 두 경로에서 child 가 root 자신이거나 그 아래인가."""
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:                     # 드라이브가 다름
        return False


def _hidden_roots() -> list[str]:
    """항상 숨기는 절대 경로들(정규화). 홈 폴더 세션이면 여기에 다 걸린다."""
    home = Path.home()
    from session_manager import osdep
    out = [config.claude_home(), config.data_dir(), home / ".ssh", home / ".gnupg", *osdep.hidden_dirs()]
    return [_norm(p) for p in out]


def is_protected(rel: str) -> bool:
    """뿌리 기준 상대 경로(POSIX) 가 보호 대상인가 — 이름 패턴 또는 release-keys/ 아래."""
    parts = [p.lower() for p in PurePosixPath(rel).parts]
    if not parts:
        return False
    if any(p in PROTECTED_DIRS for p in parts):
        return True
    name = parts[-1]
    return any(fnmatch.fnmatchcase(name, g) for g in PROTECTED_GLOBS)


def _is_hidden(real: str, name: str, hidden: list[str]) -> bool:
    if name.lower() in HIDDEN_NAMES:
        return True
    return any(_inside(real, h) for h in hidden)


def session_root(session_id: str) -> Path:
    """세션의 탐색 뿌리(cwd, 실재 폴더). 없거나 금지면 FBError."""
    from session_manager import scanner
    meta = scanner.scan_one(session_id)
    if meta is None:
        raise FBError("no_session", 404)
    cwd = scanner.resolve_launch_cwd(meta) or meta.cwd
    if not cwd or not os.path.isdir(cwd):
        raise FBError("no_cwd", 404)
    root = Path(os.path.realpath(cwd))
    if root.parent == root:                               # C:\ 같은 드라이브 맨 위
        raise FBError("root_is_drive", 403)
    if _is_hidden(_norm(root), root.name, _hidden_roots()):
        raise FBError("root_blocked", 403)
    return root


def _clean_rel(rel: str | None) -> str:
    """사용자 입력 상대 경로 → 정규화된 POSIX 상대 경로. 절대 경로·드라이브·.. 는 거부."""
    rel = (rel or "").replace("\\", "/").strip()
    if rel in ("", ".", "/"):
        return ""
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or rel.startswith("//"):
        raise FBError("bad_path", 400)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FBError("bad_path", 400)
    return "/".join(parts)


def resolve(root: Path, rel: str | None) -> tuple[Path, str]:
    """(실제 경로, 정규화된 상대 경로). 뿌리 밖·숨김이면 FBError. 존재 여부는 호출자가 본다."""
    rel = _clean_rel(rel)
    root_n = _norm(root)
    target = root / rel if rel else root
    real = _norm(target)
    if not _inside(real, root_n):                         # 링크·정션으로 뿌리 밖을 가리킴
        raise FBError("outside_root", 403)
    hidden = _hidden_roots()
    # 경로의 각 단계 이름도 본다(뿌리 안의 .ssh 같은 폴더 아래 파일을 직접 여는 경우)
    if any(p.lower() in HIDDEN_NAMES for p in PurePosixPath(rel).parts) or (
            rel and any(_inside(real, h) for h in hidden)):
        raise FBError("not_found", 404)
    return target, rel


def list_dir(session_id: str, rel: str | None = "", sort: str = "name") -> dict:
    root = session_root(session_id)
    target, rel = resolve(root, rel)
    if not target.is_dir():
        raise FBError("not_dir", 404)
    hidden = _hidden_roots()
    root_n = _norm(root)
    entries, truncated = [], False
    try:
        it = list(os.scandir(target))
    except PermissionError:
        raise FBError("denied", 403)
    for e in it:
        try:
            lst = e.stat(follow_symlinks=False)
            is_link = _stat.S_ISLNK(lst.st_mode) or bool(getattr(lst, "st_file_attributes", 0) & _REPARSE)
            real = _norm(e.path) if is_link else os.path.normcase(e.path)
            if is_link and not _inside(real, root_n):
                continue                                   # 뿌리 밖을 가리키는 링크는 보이지 않는다
            if _is_hidden(real, e.name, hidden):
                continue
            is_dir = e.is_dir()                            # 링크면 대상 기준
            st = e.stat() if is_link else lst
        except OSError:
            continue
        child_rel = f"{rel}/{e.name}" if rel else e.name
        item = {"name": e.name, "type": "dir" if is_dir else "file",
                "size": None if is_dir else st.st_size, "mtime": int(st.st_mtime)}
        if is_dir and e.name.lower() in HEAVY_DIRS:
            item["heavy"] = True
        if is_protected(child_rel):
            item["blocked"] = True
        if is_link:
            item["link"] = True
        entries.append(item)
    if sort == "mtime":
        entries.sort(key=lambda x: (x["type"] != "dir", -x["mtime"], x["name"].casefold()))
    else:
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].casefold(), x["name"]))
    if len(entries) > MAX_ENTRIES:
        entries, truncated = entries[:MAX_ENTRIES], True
    return {"root": str(root), "root_name": root.name, "path": rel, "entries": entries, "truncated": truncated}


def _file(session_id: str, rel: str | None) -> tuple[Path, str, os.stat_result]:
    root = session_root(session_id)
    target, rel = resolve(root, rel)
    if not rel or not target.is_file():
        raise FBError("not_file", 404)
    if is_protected(rel):
        raise FBError("protected", 403)
    return target, rel, target.stat()


def _decode(data: bytes) -> tuple[str, str] | None:
    """(본문, 인코딩) 또는 바이너리면 None. UTF-8(BOM 포함) → cp949 순."""
    if b"\x00" in data[:8192]:
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data[3:].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp949"), "cp949"
    except UnicodeDecodeError:
        return None


def read_text(session_id: str, rel: str | None) -> dict:
    target, rel, st = _file(session_id, rel)
    base = {"path": rel, "name": target.name, "size": st.st_size, "mtime": int(st.st_mtime)}
    ext = target.suffix.lower()
    if ext in IMAGE_EXT:
        return {**base, "kind": "image"}
    if st.st_size > TEXT_MAX:
        raise FBError("too_large", 413, limit=TEXT_MAX, **base)
    data = target.read_bytes()
    dec = _decode(data)
    if dec is None:
        return {**base, "kind": "binary"}
    text, enc = dec
    return {**base, "kind": "text", "encoding": enc, "text": text}


def read_raw(session_id: str, rel: str | None, *, relay: bool, image_only: bool) -> dict:
    """base64 본문. image_only=True 는 미리보기(보기 권한), False 는 내려받기(원격은 2FA 특권 경로)."""
    target, rel, st = _file(session_id, rel)
    if image_only and target.suffix.lower() not in IMAGE_EXT:
        raise FBError("not_image", 415)
    limit = RAW_MAX_RELAY if relay else RAW_MAX_LOCAL
    if st.st_size > limit:
        raise FBError("too_large", 413, size=st.st_size, limit=limit, path=rel, name=target.name)
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return {"path": rel, "name": target.name, "size": st.st_size, "mtime": int(st.st_mtime), "mime": mime,
            "b64": base64.b64encode(target.read_bytes()).decode()}
