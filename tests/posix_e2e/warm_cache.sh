#!/bin/sh
# e2e 이미지 빌드 때만(네트워크: PyPI) — uv 캐시를 데운다. 실행 단계는 --network none·UV_OFFLINE=1 이라 설치·업데이트·롤백의
# `uv sync --frozen --no-dev` 가 이 캐시로만 돈다(편집 설치의 빌드 백엔드 setuptools 포함). 데운 뒤 venv 는 지운다.
# 트리는 build_fixtures.py 가 A 변형에서 꺼낸 최소본(pyproject.toml·uv.lock·LICENSE·session_manager/__init__.py)이다.
set -eu
B=/opt/e2e-base
W=$(mktemp -d)
tar -xzf "$B"/uv-*.tar.gz -C "$W"
UV=$(find "$W" -type f -name uv | head -n 1)
[ -n "$UV" ] || { printf '%s\n' "warm_cache: uv 가 tarball 안에 없습니다" >&2; exit 1; }
cp -R "$B/warm" "$W/tree"
cd "$W/tree"
UV_PYTHON_PREFERENCE=only-system UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT="$W/venv" \
  "$UV" sync --frozen --no-dev
cd /
rm -rf "$W"
printf '%s\n' "warm_cache: 완료 ($(du -sh "$HOME/.cache/uv" 2>/dev/null | cut -f 1))"
