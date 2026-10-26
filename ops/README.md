# ops/ — 운영자 전용 자산

이 폴더는 **배포 zip 에 절대 들어가지 않는다** (`package-for-user.ps1` 이 화이트리스트
방식이라 애초에 안 담기고, 키 파일은 발견 즉시 패키징이 중단된다).
라이브 비밀(start-public.ps1 의 토큰들)과 릴리스 서명 개인키가 여기 산다 —
**이 폴더를 클라우드 동기화하거나 통째로 공유하지 말 것.**

| 항목 | 역할 | 주의 |
|---|---|---|
| `start-public.ps1` | 운영자 PC 의 006 기동 (스케줄러 `SessionManager` → `deploy/start-hidden.vbs` 가 호출) | 라이브 비밀번호·토큰 포함 |
| `start-relay.ps1` | 릴레이 로컬 디버그 (운영은 도커 `util-session-relay`) | 8787 충돌 주의 |
| `package-for-user.ps1` | 배포 zip 생성 → `dist/` | 비밀·키 스캔 게이트, 맥·리눅스 `.sh` 형식 게이트(LF·BOM 없음·`#!/bin/sh`) 내장 |
| `tools/sign_release.py` | 릴리스 keygen/sign/verify/publish | 실행: `python ops/tools/sign_release.py ...` · `sign --platforms`(받는 OS, 기본 win,mac — win 은 늘 넣는다. 빠지면 sign 이 거부하고 verify 가 [FAIL]) |
| `get.sh` | 맥·리눅스 원라인 설치 부트스트랩(`curl … \| sh`) — CP 공개 전 보관 | 아직 서빙되지 않음. 공개할 때 007 `control_plane/`로 옮기고 라우트·nginx 추가(docs/designs/posix-install.md 7절) |
| `release-keys/` | ★ 릴리스 서명 개인키 | 유출=전 사용자 PC 장악 · 분실=업데이트 불능. 오프라인 백업 필수. 상세는 안의 README |
| `deploy/start-hidden.vbs` | 스케줄러가 006 을 창 없이 띄우는 셸 | 로그를 `logs/server.log` 로 리다이렉트 |
| `deploy/register-relay-task.ps1` | (레거시) 릴레이 태스크 등록 — 도커 전환 후 미사용 | |
| `dist/` | 패키징·서명 산출물 | 재생성 가능 |
| `logs/` | 운영자 006 stdout/err | 재생성 가능 |

## 릴리스 절차 — 한 방 (권장)

```powershell
pwsh -File ops/release.ps1 -Version X.Y.Z -Notes "사용자용 변경 요약" -PrepareChangelog   # 1) CHANGELOG.md 에 항목 추가
#   → pyproject version bump 와 함께 커밋
pwsh -File ops/release.ps1 -Version X.Y.Z                                            # 2) 패키지 + 서명 + 검증 (notes = CHANGELOG 항목)
pwsh -File ops/release.ps1 -Version X.Y.Z -Publish                                   #    + CP 게시(토큰은 -EnvFile 에서)
pwsh -File ops/release.ps1 -Version X.Y.Z -Publish -Apply                            #    + 이 PC 적용·헬스·조용한 재기동 확인
```
사전점검(CHANGELOG 항목·버전 일치·작업 트리·서명 키·pytest)에서 걸리면 즉시 중단. 로그 `ops/logs/release-<ver>.log`(토큰 없음).
받는 OS 는 `-Platforms`(기본 `win,mac` — 맥은 0.33.0 부터 정식, 리눅스는 준비 중이라 넣지 않는다). win 은 늘 들어가야 하고, `mac`·`linux`를 넣으면 같은 HEAD 에서 통과한
POSIX e2e 기록(`ops/logs/posix-e2e-<ver>.json`)이 있어야 한다 — 기록은 `python -m pytest -m posix_e2e tests/test_posix_e2e.py`
(docker 필요, 리눅스 `*-test` 컨테이너 왕복 — `tests/posix_e2e/README.md`)가 통과하고 작업 트리가 깨끗할 때 남는다.
패키지 직후 zip 내용 게이트(맥·리눅스 스크립트·이름 구분자 `/`·
`RELEASE_KEYS`가 저장소와 같음 — 시험 빌드 차단)를 지나야 서명한다.
CHANGELOG 항목 본문이 곧 매니페스트 notes(업데이트 화면 문구) — 한 문장을 한 곳에만 쓴다.
아래 수동 절차는 스크립트가 하는 일의 설명이다.

## 릴리스 절차 (수동, 참고)

```powershell
pwsh -File ops/package-for-user.ps1                      # → ops/dist/clewpath-host-<날짜>.zip
python ops/tools/sign_release.py sign --version X.Y.Z --artifact ops/dist/<zip> `
    --url https://clewpath.pyongso.com/cp/updates/download/X.Y.Z --out ops/dist/manifest-X.Y.Z.jwt
python ops/tools/sign_release.py verify --manifest ops/dist/manifest-X.Y.Z.jwt
python ops/tools/sign_release.py publish --cp http://127.0.0.1:5200 --admin-token <SM_CP_ADMIN_TOKEN> `
    --manifest ops/dist/manifest-X.Y.Z.jwt --artifact ops/dist/<zip>
```

버전 규칙·상세 절차는 저장소 루트 `README.md` 의 "릴리스 내보내기" 절 참조.
