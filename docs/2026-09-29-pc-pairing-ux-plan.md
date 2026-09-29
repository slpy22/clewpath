# 타깃 PC 추가·삭제·연결 해제 — UI/UX 재기획 (2026-09-29, 검토 입력본)

사장님 지적: "구현은 됐는데 기획면에서 완벽하지 않다." 이 문서는 **현재 구현을 코드에서 그대로 옮긴 인벤토리**와
그 위에서 보이는 문제를 적은 것이다. 재기획의 출발점이지 결론이 아니다.

## 1. 등장인물과 용어(지금 코드가 쓰는 말)

| 화면 | 상대를 부르는 말 | 저장소 | 진짜 정체 |
|---|---|---|---|
| PC 로컬 웹(127.0.0.1:5100) | **기기**(📱 외부 접속 기기) | Host `devices` 레지스트리(이름·토큰 해시·client_public_id·revoked·last_seen) | 이 PC 에 접속을 허락한 폰/브라우저 |
| 폰·원격 브라우저·앱(릴레이 화면) | **PC**(🖥 저장된 PC) | 브라우저 `localStorage sm_pcs = [{room, cs, cp, dev, name, added, last}]` + 활성 PC 미러(`sm_room/sm_cs/sm_cp/sm_devtoken`) + E2EE 키 `sm_e2ee_<room>` | 이 브라우저가 자격을 가진 PC(=릴레이 room) |
| 서버(CP) | room / client credential | `connectors`, client credential(cpub) | Host 1대 = room 1개, 기기 1개 = cpub 1개 |

같은 관계를 양쪽이 다른 이름으로 부른다: PC 는 "기기를 등록", 폰은 "PC 를 추가". 관계 자체(PC↔기기 **페어링**)를
가리키는 말은 UI 에 없다.

## 2. 현재 흐름 인벤토리 (코드 기준)

### 2-1. PC 쪽(로컬 웹만 가능 — 원격에선 🔒 잠금 표시)
- **첫 실행 힌트**(`maybeShowFirstRunHint`): 등록 기기 0 이면 목록 위에 배너 "📱 폰에서도 쓰려면 기기를 하나 등록하세요 → ＋새 기기 추가 / 나중에". '나중에' 는 영구 소거(`sm_hint_done`).
- **📱 외부 접속 기기**(`showDevices`, ⚙ 설정 → 보안): 상태 줄(등록 0 = "🛑 전면 차단중"), 기기 행(📱이름 · 최근 접속 · 폐기됨) + `🔄 QR`(재발급, 이전 QR 무효) + `🗑 삭제`(토큰·cpub 회수, 즉시 차단) + `＋ 새 기기 추가`(이름 입력 → QR + 페어링 링크 1회 표시, 닫으면 재표시 불가).
- API: add/reissue/revoke/delete/rename 전부 **로컬 전용**(원격에서 호출 403). revoke 는 API 만 있고 UI 없음.

### 2-2. 폰/원격 쪽 — 처음(자격 없음)
- **웹 릴레이 화면** `#pair`: 안내문(PC 로컬 웹 → 📱 → 새 기기 추가 → QR 스캔 또는 링크 붙여넣기) · 저장된 PC 버튼 목록(`renderPairPcs`, 있을 때만) · `📷 QR 스캔`(앱만, 2026-09-29) · 페어링 링크 입력 + 연결 · "직접 입력(고급)"(room / secret / cpub).
- **네이티브 앱 첫 실행**: 브리지가 **별도 오버레이**(`#cb-pair`: 3단계 안내 · 📷 · 링크 붙여넣기)를 `#pair` 위에 덮는다. 즉 첫 페어링 UI 가 **2벌**(문구·레이아웃 다름).
- **카메라 앱으로 QR 스캔**: https 링크 → 브라우저(웹 릴레이) 열림 → 부트 파서가 fragment 에서 자격을 저장·`pcsUpsert`·자동 접속. 앱으로 열리게 하는 Universal Links 는 오늘 서버 준비, 앱 1.9 실기 대기.
- 페어링 성공 시 PC 이름은 **`PC <room 앞 6자>`**. Host 는 auth 응답에 `hostname` 을 주지만 화면은 쓰지 않는다 → 사용자가 ✎ 로 손수 이름을 붙여야 어느 PC 인지 안다.

### 2-3. 폰/원격 쪽 — 연결된 뒤
- 헤더 `#pcname` select: 저장된 PC 들(전환) (+ 데스크톱 브라우저에선 `🖥 localhost`). 모바일/앱에선 localhost 숨김.
- ⚙ 설정 → 연결·PC: 현재 PC 이름·버전·E2EE 표시 · **🔄 PC 전환·관리**(`showPcs`: ✅현재 / 전환 / ✎ 이름 / 🗑 목록에서 제거 / 📷 QR 스캔으로 PC 추가(앱)) · **🚪 연결 해제**(`showLogoutChoice`: 🔌 연결 해제 = 현재 PC 자격만 삭제, 목록 유지 / 🧹 완전 로그아웃 = 자격·목록·E2EE 키 전부 삭제).
- `🖧 btn-pcs` 헤더 버튼은 코드(`refreshPcsBtn`)만 남고 마크업엔 없다(죽은 코드).
- **자격 해제 감지**(`showAuthLost`): PC 에서 기기를 삭제/재발급하면 다음 요청에 `auth_required/auth_invalid` → 모달 "🔒 접속 자격 해제됨 … 🔗 새 QR/링크로 다시 연결"(현재 PC 자격만 지우고 리로드 → `#pair`).
- 로컬(PC) 화면의 헤더 select 에는 "릴레이 PC 목록"이 팝업 postMessage 동기화로만 들어온다(`🔄 릴레이에서 PC 목록 동기화`).

### 2-4. 삭제의 네 가지 얼굴
| 동작 | 어디서 | 효과 | 상대편 상태 |
|---|---|---|---|
| 🗑 기기 삭제 | PC 로컬 | 토큰·cpub 회수, 즉시 차단 | 폰 목록엔 그 PC 가 **그대로** → 다음 접속 때 showAuthLost |
| 🔄 QR 재발급 | PC 로컬 | 이전 자격 무효 | 폰은 새 QR 을 다시 스캔해야 함(같은 PC 항목이 갱신됨) |
| 🗑 목록에서 제거 | 폰 | localStorage 항목만 삭제 | PC 의 기기 등록은 **그대로**(자격은 살아 있으나 폰이 잊음) |
| 🔌 연결 해제 / 🧹 완전 로그아웃 | 폰 | 활성 자격 / 전부 삭제 | PC 의 기기 등록은 **그대로** |

어느 쪽에서 지워도 상대편은 모른다. "폰에서 이 PC 와의 페어링을 끊는다(PC 쪽 등록까지 회수)" 는 동작이 없다
(revoke/delete 가 로컬 전용이라 원격에서 부를 수 없음 — 보안상 의도).

## 3. 보이는 문제(가설, 검토 대상)

1. **모델이 둘로 갈라져 있다**: PC 는 "기기 목록", 폰은 "PC 목록". 실체는 하나의 페어링 관계인데 양쪽 화면·용어·삭제 의미가 대칭이 아니다.
2. **첫 페어링 UI 2벌**(웹 `#pair` vs 앱 `#cb-pair`), 안내 문구·순서가 다르고, 앱 오버레이는 페어링 뒤엔 다시 못 본다.
3. **PC 이름 자동화 없음** — 매 PC 가 `PC a1b2c3` 로 보인다. hostname 이 이미 도착하는데 안 쓴다.
4. **삭제·해제 용어 4종**(기기 삭제 / QR 재발급 / 목록에서 제거 / 연결 해제 / 완전 로그아웃)이 서로 무엇을 남기는지 화면이 설명해야만 이해된다.
5. **상대편 통지 없음**: PC 에서 폰을 삭제해도 폰은 다음 접속 실패 때야 안다. 폰에서 제거해도 PC 의 등록은 남아 "유령 기기" 가 쌓인다.
6. **빈 상태 3종**이 제각각: PC(기기 0) = 배너 / 웹(PC 0) = `#pair` / 앱(PC 0) = 오버레이. "연결 해제 뒤" 와 "처음" 이 같은 화면인데 문구는 처음 기준.
7. **재페어링 경로가 길다**: 자격 해제 → 모달 → 리로드 → `#pair` → PC 로 가서 🔄 QR → 스캔. 같은 PC 에 다시 붙는 최단 경로가 없다.
8. **로컬 화면의 릴레이 PC 목록 동기화**는 팝업 + postMessage 라는 숨은 장치에 의존하고 사용자는 그 존재를 모른다.
9. 죽은 코드: `#btn-pcs`(🖧) 버튼.

## 4. 이 문서가 답해야 할 질문(재기획 범위)

- 사용자 머릿속 모델을 하나로: "**PC ↔ 이 기기의 페어링**" 을 양쪽에서 같은 말·같은 목록으로 보여줄 수 있나?
- 빈 상태(첫 사용·연결 해제 뒤·자격 해제 뒤)를 **한 화면**으로 통일할 수 있나(웹·앱 동일)?
- 삭제/해제를 **2개 이하**의 동작으로 줄일 수 있나(예: "이 PC 와 페어링 끊기" / "이 기기 초기화")?
- PC 이름·기기 이름 자동화(hostname / 기기 모델명) — 이름을 손수 붙이는 단계 제거.
- 상대편에 변화가 전달되는 경로(릴레이 제어 프레임 / CP)가 있어야 하나, 있다면 최소 형태는?

## 5. 제약(바꾸지 않는 것)
- 기기 등록·삭제·재발급은 **PC 로컬에서만**(보안 설계, 006 기기 페어링 메모리). 원격에서 PC 의 등록을 지우는 API 는 열지 않는다.
- 페어링 비밀은 fragment(#)로만, E2EE 룸 키는 브라우저/앱 로컬에만.
- claude 파일 무접촉(불가침 원칙) — 이 영역은 무관.

## 6. 리뷰 결과 (2026-09-29)
`/plan-ceo-review` 완료 — 정본은 `docs/designs/pc-pairing-ux.md`. 접근안 B(대칭 페어링 모델) 채택, 확장 4건 수용(E-2·E-3·E-4·E-5),
3건 연기(E-1·E-6·X-1). Codex 외부 시각이 **P1 보안 결함**(커넥터 프록시로 원격 기기 API 호출 가능·폐기 뒤 스트림 지속·authed 미초기화)을
찾아 선행 핫픽스 T1·T2 로. 유령 재발 경로((room,cpub) 식별·재발급 순서·3상태 큐·CP 401 폴백)까지 설계에 포함.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR | 7 proposals, 4 accepted, 3 deferred |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 (plan) | issues_found → 4/4 tension resolved | 10 findings, 3 verified as code defects (P1) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 (2026-09-10, stale) | NOT CLEAR for this plan | 3 issues (prior plan) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** 10건 중 #1·#2·#9 코드로 확인(P1 핫픽스 T1·T2), #3~#8 설계 보강(B-5·B-7·B-8·E-2), #10 범위 축소(E-1 연기). 전부 사장님 결정으로 반영.
- **CROSS-MODEL:** 리뷰(Claude)는 UI·모델 통일에 집중, Codex 는 권한 경계·폐기 실효성을 지적 — 겹치지 않는 관점이 합쳐져 플랜이 보안 우선 순서로 재배열됨.
- **VERDICT:** CEO CLEARED — eng review required (기존 eng review 는 2026-09-10 다른 플랜, 이 플랜엔 `/plan-eng-review` 필요)

NO UNRESOLVED DECISIONS
