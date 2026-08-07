# ops/ — 운영자 전용 자산

이 폴더는 **배포 zip 에 절대 들어가지 않는다** (`package-for-user.ps1` 이 화이트리스트
방식이라 애초에 안 담기고, 키 파일은 발견 즉시 패키징이 중단된다).
라이브 비밀(start-public.ps1 의 토큰들)과 릴리스 서명 개인키가 여기 산다 —
**이 폴더를 클라우드 동기화하거나 통째로 공유하지 말 것.**

| 항목 | 역할 | 주의 |
|---|---|---|
| `start-public.ps1` | 운영자 PC 의 006 기동 (스케줄러 `SessionManager` → `deploy/start-hidden.vbs` 가 호출) | 라이브 비밀번호·토큰 포함 |
| `start-relay.ps1` | 릴레이 로컬 디버그 (운영은 도커 `util-session-relay`) | 8787 충돌 주의 |
| `package-for-user.ps1` | 배포 zip 생성 → `dist/` | 비밀·키 스캔 게이트 내장 |
| `tools/sign_release.py` | 릴리스 keygen/sign/verify/publish | 실행: `python ops/tools/sign_release.py ...` |
| `release-keys/` | ★ 릴리스 서명 개인키 | 유출=전 사용자 PC 장악 · 분실=업데이트 불능. 오프라인 백업 필수. 상세는 안의 README |
| `deploy/start-hidden.vbs` | 스케줄러가 006 을 창 없이 띄우는 셸 | 로그를 `logs/server.log` 로 리다이렉트 |
| `deploy/register-relay-task.ps1` | (레거시) 릴레이 태스크 등록 — 도커 전환 후 미사용 | |
| `dist/` | 패키징·서명 산출물 | 재생성 가능 |
| `logs/` | 운영자 006 stdout/err | 재생성 가능 |

## 릴리스 절차 (요약)

```powershell
pwsh -File ops/package-for-user.ps1                      # → ops/dist/clewpath-host-<날짜>.zip
python ops/tools/sign_release.py sign --version X.Y.Z --artifact ops/dist/<zip> `
    --url https://clewpath.pyongso.com/cp/updates/download/X.Y.Z --out ops/dist/manifest-X.Y.Z.jwt
python ops/tools/sign_release.py verify --manifest ops/dist/manifest-X.Y.Z.jwt
python ops/tools/sign_release.py publish --cp http://127.0.0.1:5200 --admin-token <SM_CP_ADMIN_TOKEN> `
    --manifest ops/dist/manifest-X.Y.Z.jwt --artifact ops/dist/<zip>
```

버전 규칙·상세 절차는 저장소 루트 `README.md` 의 "릴리스 내보내기" 절 참조.
