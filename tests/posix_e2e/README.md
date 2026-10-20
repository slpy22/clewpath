# 맥·리눅스 설치 e2e (리눅스 `*-test` 컨테이너)

`docs/designs/posix-install.md` 6.3·6.4 의 왕복 시험이다. 가짜 컨트롤플레인이 **시험 키로 서명한** 매니페스트와 변형 zip 을
주고, 컨테이너 안에서 진짜 `get.sh → install.sh → 런처 → Host → 업데이트 러너 → 롤백 → uninstall.sh` 를 돌린다.
운영 CP·릴레이·운영 서명 키는 쓰지 않는다. 운영 반영도 아니다.

## 한 번에 (pytest)

```powershell
# 작업 트리에서 — docker 가 필요하다. 기본 pytest 실행에서는 빠진다(pyproject addopts)
$env:CLEWPATH_E2E_OUT = "$env:TEMP\clewpath-posix-e2e"          # 결과 폴더(없으면 pytest tmp)
$env:CLEWPATH_E2E_UV_ARCHIVE = "…\uv-x86_64-unknown-linux-gnu.tar.gz"   # 선택 — 없으면 GitHub 공식 릴리스에서 받는다
python -m pytest -m posix_e2e tests/test_posix_e2e.py -p no:cacheprovider
```

결과: `$CLEWPATH_E2E_OUT/e2e-run.log`(드라이버 출력 전체 — 실패하면 host.log·launcher.log·apply 로그 꼬리 포함),
`e2e-result.json`(단계별 성공·실패와 근거), `docker-build.log`, `fixtures.log`. 통과했고 작업 트리가 깨끗하면
`ops/logs/posix-e2e-<ver>.json` = `{version, head, ok}` 를 남긴다 — `release.ps1 -Platforms` 에 mac·linux 가 들어간 릴리스는
같은 HEAD 의 이 기록을 요구한다.

## 손으로 (세 단계)

```powershell
# 0) 재료 — 저장소 밖 폴더(그 폴더가 곧 docker 빌드 컨텍스트). 네트워크: GitHub(uv) 만, --uv-archive 를 주면 그것도 안 쓴다
python tests/posix_e2e/build_fixtures.py --out "$env:TEMP\clewpath-posix-e2e\build" [--get-sh PATH] [--uv-archive PATH]
# 1) 이미지 — 빌드 때만 네트워크(apt·PyPI). 의존성 캐시 층은 재료가 바뀌어도 다시 데우지 않는다
docker build -t clewpath-posix-e2e-test "$env:TEMP\clewpath-posix-e2e\build"
# 2) 실행 — 이름은 -test 로 끝나야 이 PC 의 배포 가드를 지난다. 볼륨·운영 네트워크 없이, 끝나면 지워진다
docker run --rm --init --network none --name clewpath-posix-e2e-test clewpath-posix-e2e-test
```

## 파일

| 파일 | 어디서 | 하는 일 |
|---|---|---|
| `build_fixtures.py` | 호스트 | 기본 zip(pwsh 가 있으면 진짜 `ops/package-for-user.ps1`, 없으면 같은 목록의 파이썬 판) → 변형 zip(A~E·L·P·T·Z) → 시험 키 서명(`sign_release.cmd_sign`) → 컨텍스트(`Dockerfile`, `base/`, `ctx/`) → 키 가드 |
| `Dockerfile` | 빌드 | `python:3.11-slim` + curl·unzip(procps 없음), 사용자 tester(uid 1000), uv 캐시 데우기 |
| `warm_cache.sh` | 빌드 | A 트리로 `uv sync --frozen --no-dev` 한 번(편집 설치의 setuptools 포함) 뒤 venv 삭제 — 실행은 `UV_OFFLINE=1` |
| `fake_cp.py` | 컨테이너 | 127.0.0.1:8099 가짜 CP — 매니페스트(`current.jwt` 바꿔치기)·zip·get.sh·발급(횟수, 429 주입)·token 503, 요청 로그 |
| `e2e_driver.py` | 컨테이너 | 시나리오 S0~S9, 마지막 줄 `E2E-RESULT {json}`. `CLEWPATH_E2E_CONTAINER=1` 인 컨테이너가 아니면 거부 |

## 변형

| 이름 | 버전 | 바꾼 것 | 용도 |
|---|---|---|---|
| A | 90.0.0 | pyproject·uv.lock 루트 버전, `RELEASE_KEYS` 를 시험 키 하나로, README 첫 줄 표식(모든 변형 공통) | 첫 설치(S2) |
| B | 90.0.1 | + 런처 템플릿 3행 뒤 주석 한 줄(`# e2e-build: B`) | 수동 적용·재렌더(S5) |
| C | 90.0.2 | + `server.main` 첫 줄 `raise SystemExit` (템플릿 표식 C — 뜨지 못하므로 렌더되지 않는다) | 실패와 롤백(S6) |
| D | 90.0.3 | 정상(템플릿 표식 D) | 재기동 자동 적용(S7) |
| E | 90.0.4 | 정상(템플릿 표식 E, CP 로 내지 않음) | `bash --posix` 러너(S9) |
| L | 91.0.0 | install.sh·uninstall.sh 없음, platforms 클레임 없음(0.28.1 모양) | 거부 |
| L-noinstall | 91.0.4 | linux 클레임은 있으나 install.sh 없음 | 푼 뒤 거부 |
| P | 91.0.1 | `platforms=["win"]` | 거부 |
| Z-dotdot·Z-abs·Z-link | 91.0.2·91.0.5·91.0.6 | `../` 항목·절대 경로·심볼릭 링크 | 거부, 바깥에 파일 없음 |
| T(-sha·-size·-small) | 91.0.3 | 매니페스트 sha256·size 변조(같은 키로 다시 서명) | 거부 |
| M | — | 내용이 JSON 이 아닌 JWT | 거부 |

## 지키는 것

- 서명 키는 `build_fixtures.py` 가 프로세스 안에서 만든 시험 키뿐이다. 저장소 밖 임시 폴더에만 두고 서명이 끝나면 지운다.
  컨텍스트에는 공개키(zip 안 `updater.RELEASE_KEYS` 를 시험 키 하나로 바꾼 사본)만 간다. 운영 키는 읽지도 만들지도 않는다.
- 컨텍스트에 `*.pem`·키 폴더·개인키 머리글이 보이면(zip 안까지) 중단한다. `--out` 이 저장소 안이거나 남의 폴더면 거부한다.
- 시험 빌드는 버전 90.x·91.x 와 시험 kid 라 게시할 수 없다(`release.ps1` 의 zip 게이트가 RELEASE_KEYS 를 대조한다).
- 실행 단계는 `--network none` — 운영 CP·릴레이·GitHub 에 닿을 길이 없다. 드라이버는 컨테이너 표식이 없으면 아무것도 하지 않는다.
