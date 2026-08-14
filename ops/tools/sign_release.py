"""릴리스 서명 도구 — 운영자 전용, 오프라인에서 돈다.

왜 CP 의 JWKS 키를 쓰지 않는가
------------------------------
CP 의 서명 개인키는 CP 자신의 DB(signing_keys)에 산다. 그 키로 업데이트를 서명하면
"CP 가 뚫려도 남의 PC 에 코드를 못 넣는다"가 성립하지 않는다 — CP 를 장악한 쪽이
키도 같이 갖기 때문이다. 그래서 릴리스 서명키는 **서버에 절대 올리지 않는 별도 키**다.
CP 는 서명된 매니페스트를 그냥 보관·배포만 한다(서명 능력 없음).

공개키는 배포되는 코드(session_manager/updater.py)에 **박아서** 보낸다. 네트워크로
받아오면 그 경로가 또 하나의 공격면이 된다. 키 교체는 두 개를 동시에 신뢰하는
기간을 두고(구키+신키) 업데이트를 한 번 굴리면 된다.

키를 어디에 두는가
------------------
기본 위치는 ops/release-keys/ 다. 프로젝트와 같이 다녀 찾기 쉽지만, 그만큼
새어나갈 경로가 두 개 생긴다(사용자 배포 zip / 소스 저장소). 둘 다 하드 게이트로 막았다:
package-for-user.ps1 은 스테이지에서 키 파일을 발견하면 패키징을 **중단**하고,
.gitignore 가 release-keys/ 와 *.pem 을 막는다. tests/test_release_key_safety.py 가
그 장치들이 살아 있는지 검사한다. 자세한 건 release-keys/README.md.

사용법
------
    # 1) 최초 1회 — 키 생성. private.pem 은 이 PC 밖으로 나가면 안 된다.
    python ops/tools/sign_release.py keygen

    # 2) 릴리스마다 — zip 을 서명해 매니페스트 생성 (--key 생략 시 기본 위치)
    python ops/tools/sign_release.py sign \
        --version 0.4.0 \
        --artifact dist\\clewpath-host-20260804.zip \
        --url https://clewpath.pyongso.com/cp/updates/download/0.4.0 \
        --out dist\\manifest-0.4.0.jwt

    # 3) CP 에 올리기
    python ops/tools/sign_release.py publish \
        --cp https://clewpath.pyongso.com/cp --admin-token <SM_CP_ADMIN_TOKEN> \
        --manifest dist\\manifest-0.4.0.jwt --artifact dist\\clewpath-host-20260804.zip
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ISSUER = "clewpath-release"
AUDIENCE = "clewpath-host"

# 기본 키 위치 — 이 저장소의 release-keys/. 배포 zip 과 git 양쪽에서 차단된다.
REPO_ROOT = Path(__file__).resolve().parents[2]      # ops/tools/ 두 단계 위
DEFAULT_KEY_DIR = REPO_ROOT / "ops" / "release-keys"
DEFAULT_KEY = DEFAULT_KEY_DIR / "private.pem"
# 매니페스트 유효기간. 무한이면 오래된(취약한) 버전을 계속 정답처럼 들이밀 수 있다.
DEFAULT_TTL_DAYS = 120


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _kid(pub) -> str:
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def cmd_keygen(a) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    priv_path = out / "private.pem"
    if priv_path.exists() and not a.force:
        print(f"[!] 이미 있습니다: {priv_path} (덮어쓰려면 --force)")
        return 1
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_path.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (out / "public.pem").write_bytes(pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kid = _kid(pub)
    print(f"[OK] 키 생성: {out}")
    print(f"     kid = {kid}")
    print()
    print("  session_manager/updater.py 의 RELEASE_KEYS 에 아래 줄을 추가하세요:")
    print(f'      "{kid}": "{_b64url(raw)}",')
    print()
    print(f"  ⚠ {priv_path} 는 이 PC 밖으로 내보내지 마세요(서버 업로드 금지).")
    return 0


def cmd_sign(a) -> int:
    if not Path(a.key).is_file():
        print(f"[!] 서명 키가 없습니다: {a.key}")
        print("    먼저 만드세요:  python ops/tools/sign_release.py keygen")
        return 1
    art = Path(a.artifact)
    if not art.is_file():
        print(f"[!] 파일이 없습니다: {art}")
        return 1
    data = art.read_bytes()
    # 버전 대조 게이트: zip 안의 pyproject 버전과 --version 이 다르면 중단.
    # (실사고: 같은 날짜 파일명 때문에 패키징이 중단됐는데 구 zip 을 새 버전으로
    #  서명·게시 - 수정이 빠진 릴리스가 나감. 이 게이트가 그 사고를 막는다)
    try:
        import io
        import re
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            m = re.search(r'^version = "([^"]+)"',
                          zf.read("pyproject.toml").decode("utf-8"), re.M)
        zip_ver = m.group(1) if m else None
    except Exception as e:  # noqa: BLE001
        print(f"[!] zip 버전 확인 실패({e}) - 아티팩트가 배포 zip 이 맞는지 확인하세요")
        return 1
    if zip_ver != a.version:
        print(f"[!] 버전 불일치: --version {a.version} vs zip 내부 {zip_ver}")
        print("    패키징을 다시 하세요:  pwsh -File ops/package-for-user.ps1 -Force")
        return 1
    digest = hashlib.sha256(data).hexdigest()
    priv = serialization.load_pem_private_key(Path(a.key).read_bytes(), password=None)
    kid = _kid(priv.public_key())
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": AUDIENCE,
        "ver": a.version,
        "url": a.url,
        "sha256": digest,
        "size": len(data),
        "filename": art.name,
        "notes": a.notes or "",
        "iat": now,
        "exp": now + a.ttl_days * 86400,
    }
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    tok = pyjwt.encode(claims, pem, algorithm="EdDSA", headers={"kid": kid})
    Path(a.out).write_text(tok, encoding="utf-8")
    print(f"[OK] 매니페스트 서명: {a.out}")
    print(f"     ver={a.version}  sha256={digest[:16]}...  size={len(data)}  kid={kid}")
    return 0


def cmd_publish(a) -> int:
    import httpx
    manifest = Path(a.manifest).read_text(encoding="utf-8").strip()
    r = httpx.post(f"{a.cp.rstrip('/')}/admin/updates/publish",
                   headers={"X-Admin-Token": a.admin_token,
                            "X-Manifest": manifest,
                            "Content-Type": "application/zip"},
                   content=Path(a.artifact).read_bytes(), timeout=300)
    print(r.status_code, r.text[:500])
    return 0 if r.status_code < 300 else 1


def cmd_verify(a) -> int:
    """배포 전 자체 점검 — 배포된 코드가 실제로 이 매니페스트를 받아들이는지."""
    sys.path.insert(0, str(REPO_ROOT))
    from session_manager import updater
    tok = Path(a.manifest).read_text(encoding="utf-8").strip()
    try:
        m = updater.verify_manifest(tok)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 배포본이 이 매니페스트를 거부합니다: {e}")
        print("       -> updater.RELEASE_KEYS 에 이 kid 의 공개키가 있는지 확인하세요.")
        return 1
    print("[OK] 검증 통과")
    print(json.dumps(m, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="sign_release", description="ClewPath 릴리스 서명")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="릴리스 서명 키쌍 생성(최초 1회)")
    p.add_argument("--out", default=str(DEFAULT_KEY_DIR))
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("sign", help="zip 에 서명해 매니페스트 생성")
    p.add_argument("--key", default=str(DEFAULT_KEY))
    p.add_argument("--version", required=True)
    p.add_argument("--artifact", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("publish", help="CP 에 매니페스트+아티팩트 업로드")
    p.add_argument("--cp", required=True)
    p.add_argument("--admin-token", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--artifact", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("verify", help="배포본이 이 매니페스트를 받아들이는지 확인")
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_verify)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
