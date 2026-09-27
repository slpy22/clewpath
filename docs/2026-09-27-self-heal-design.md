# Host 자기 회복 (v0.9.2, 2026-09-27)

## 사고 (2026-09-23 13:06)

0.9.1 적용 후 1시간 33분 만에 Host 프로세스(pid 81060)가 **흔적 없이** 사라졌다: Python 트레이스백 없음,
uvicorn 정상 종료 로그 없음, Windows 로그오프·종료·앱 오류 이벤트 없음, 그 시각에 활동한 claude 세션 없음,
세션 기록에 kill 류 명령 없음. 런처(`ClewPathHost` 작업)가 **로그온 시 1회**뿐이라 21:01 사장님이 발견할
때까지 8시간 다운. 사장님의 svcmon 은 13:07 `up → down` 을 기록했지만 복구 액션은 없었다.

원인은 미확정(외부 강제 종료 또는 무음 크래시). **원인 규명과 복구는 별개**(크로스체크 Codex) — 이 사이클은
"어떻게 죽든 5분 안에 되살아나고, 죽었다는 사실과 마지막 로그를 폰으로 받는다"를 만든다.

## 우선순위 결정 (크로스체크 2026-09-27)

Gemini(B 목록 성능 우선) vs Codex·Claude(A 자기 회복 우선), Copilot 무응답. A 는 작업량 소·검증 가능·장애 시
관제/알림/워커/복구 수단 전부 무력화 → A 먼저, B 는 바로 다음 사이클. 채택한 범위 규정:
- 멱등 런처: health **짧은 타임아웃** + 기동 **잠금**, health 실패만으로 기존 프로세스를 죽이거나 겹쳐 띄우지 않음,
  유지보수 중지(`maintenance.flag`) 존중, 기동 사유 기록(Codex)
- "비정상 종료 **의심**" 으로 표현, 마지막 로그 10줄을 푸시에 동봉하는 진단형 알림(Gemini)
- health 경로는 스캔에 의존하지 않음(`/api/health` 는 상수 응답) → B 의 지연이 A 오탐을 만들지 않음

## 설계

| 구성 | 역할 |
|---|---|
| `session_manager/start-connector.template.ps1` (**정본**, 첫 줄 `# clewpath-launcher v2`) | ① `maintenance.flag` 있으면 종료 ② `/api/health` 3초 → ok 면 종료 ③ `runtime.json` 의 pid 가 python 으로 살아 있으면 종료(기동 중/응답 불능 — 죽이지 않음, launcher.log 기록) ④ `launcher.lock`(2분 시체 무시) 안에서 Start-Process ⑤ 사유를 `launcher.log` 에 |
| `install.ps1` | 템플릿에서 런처 생성(없으면 v1 폴백). 런처가 v2 일 때만 작업에 **5분 반복 트리거**(로그온 트리거 유지, `MultipleInstances=IgnoreNew`). PS5.1 은 `RepetitionDuration MaxValue` |
| `session_manager/ensure_task.ps1` | 기존 설치본용. 런처가 v2 가 아니면 템플릿으로 재생성 → **그 다음에만** 작업에 반복 트리거 추가(순서가 안전의 전부: 멱등 아닌 런처에 반복을 달면 5분마다 겹쳐 뜬다). Host 가 기동 시 버전당 1회(`launcher-ensured-<ver>` 마커) 실행. 개발 트리(설치본 레이아웃 아님)는 건너뜀 |
| `session_manager/liveness.py` | `check_previous_exit()`: `runtime.json` 의 이전 pid 가 죽었고 `shutdown.json` 표식이 그 pid 로 없으면 **의심** → `incidents.jsonl` 기록(마지막 로그 10줄, ANSI 제거) → 기동 후 푸시 1건(`host-recovered`). 다른 인스턴스가 살아 있으면(개발 서버) 사고 아님. `mark_shutdown(reason)`: lifespan 종료(`normal`)·업데이트 적용 직전(`update`) |

## 검증

- 단위 9건(`test_liveness.py`): 표식 소비·의심 판정·ANSI 제거·다른 인스턴스 제외·pid 불일치 표식 무시·푸시 페이로드·
  템플릿/ensure 계약(순서). 전체 305 passed. PowerShell 4편 파서 검사 OK.
- 설치본: 0.9.2 적용 → `[liveness] 런처 보장: launcher: regenerated (v2) | task: repetition trigger added (5m)`,
  작업 트리거 = 로그온 + `PT5M`(IgnoreNew). 임시 root 에서 ensure 재생성(플레이스홀더 0) + 살아 있는 Host 앞에서
  멱등 런처 실행 → 중복 기동 0.
- **1회성 오탐(예상됨)**: 0.9.1 → 0.9.2 업데이트 재기동은 옛 프로세스가 표식을 모르고 죽어 "의심" 으로 기록·푸시됨.
  이후 업데이트는 새 updater 가 `update` 표식을 남기므로 조용하다.
- **강제 종료 → 자동 복구 e2e(이 PC)**: `taskkill /F` 18:45:18 → down 확인 18:45:21 → 반복 트리거가 18:45:56 런처
  실행(launcher.log `start: 이전 pid 100768 없음(health 없음) → 기동`) → 18:46:01 health OK. **43초**(트리거 주기 안에서
  운이 좋았고, 최악은 5분+기동). 새 Host 가 `[liveness] 비정상 종료 의심(pid 100768) → 복구 알림 1건`, incidents.jsonl 2건째.

## 한계 (Phase 2)

- Host 가 아예 못 뜨거나 PC 가 꺼지면 알림을 보낼 주체가 없다 → SaaS 단계에서 **릴레이가 heartbeat 단절을 감지**해 알림.
- 반복 트리거는 "PC 켜짐 + 사용자 로그온 + 스케줄러 정상" 전제. 회사 정책으로 스케줄러가 막힌 PC(HKCU Run 폴백)는
  로그온 1회뿐 — 그 경우 런처 자체를 상주 감시자로 바꾸는 방안 검토.
