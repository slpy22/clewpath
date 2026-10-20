# ClewPath 맥·리눅스 설치 e2e 시험장 — docs/designs/posix-install.md 6.3·6.4.
# 컨텍스트는 build_fixtures.py 가 만든 폴더다(저장소가 아니다 — 키가 섞일 길을 막는다). 네트워크는 빌드 때만(apt·PyPI):
#   docker build -t clewpath-posix-e2e-test <out>
#   docker run --rm --init --network none --name clewpath-posix-e2e-test clewpath-posix-e2e-test
FROM python:3.11-slim
# /bin/sh 는 dash(실측). procps(ps·pgrep)는 일부러 넣지 않는다 — 스크립트가 /proc 길로 도는지 본다
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 tester
# 잘 안 바뀌는 층(uv tarball·의존성 캐시)을 먼저 — 매번 바뀌는 시험 재료(ctx)가 PyPI 데우기를 다시 부르지 않게
COPY --chown=tester:tester base/ /opt/e2e-base/
USER tester
RUN sh /opt/e2e-base/warm_cache.sh
COPY --chown=tester:tester ctx/ /opt/e2e/
ENV UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never CLEWPATH_E2E_CONTAINER=1
WORKDIR /home/tester
CMD ["python3", "-I", "/opt/e2e/e2e_driver.py"]
