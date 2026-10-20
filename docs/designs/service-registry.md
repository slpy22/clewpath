# 외부 서비스 대장 (service registry) — 설계

상태: 초안 (2026-10-20) · 담당: Host · 관련: 0.23.9 API 사본 정리, serve_keep, 북한법 챗봇(NKBOT)

## 1. 왜 필요한가

외부 서비스(북한법 챗봇 등)가 ClewPath 를 통해 claude 세션을 "답변 엔진"으로 쓴다. 지금 API 토큰은
`SM_API_TOKEN(S)` 환경변수 하나로, **세션에 묶여 있지 않다.** 토큰을 가진 쪽이 할 수 있는 일(현재 코드 기준):

| 엔드포인트 | 할 수 있는 일 | 위험 |
|---|---|---|
| `GET /api/v1/sessions` | 이 PC 의 모든 세션 목록·제목·폴더 | 정보 노출 |
| `WS /api/v1/resume/{id}`, `POST /api/v1/sessions/{id}/ask` | **아무 세션이나** fork 해서 질의 | 개발 세션 대화 전체(비밀값 포함)를 맥락으로 답을 뽑아낼 수 있음 |
| `WS /api/v1/resume-inplace/{id}` | 원본에 직접 이어 쓰기, 기본 skip(자동 허용) | 2차 인증 자리만 있고 미구현 — 사실상 PC 전권 |
| `POST /api/v1/sessions/{id}/profile` | 가드레일(시스템 프롬프트·허용 도구·권한 모드) 변경 | 서비스가 스스로 제한을 풂 |
| `POST …/labels`, `…/title` | 라벨·이름 변경 | 장부 오염 |

`/api/v1/team*`, `/api/v1/worklog*` 는 이미 로컬 전용이라 해당 없음.

### 현재 사용처 (004 회신 2026-10-20)

유일한 외부 사용자 `nklaw-backend`(컨테이너, `http://host.docker.internal:5100`, env `SM_API_TOKEN`)는 3종만 쓴다 — 모두 운영 경로:

| 호출 | 위치 | 용도 |
|---|---|---|
| `GET /api/v1/sessions?label=…&limit=1` (없으면 `limit=0` 전체 목록 폴백) | `app/chat/router.py:82·93`, `app/services/law_service.py:181·189` | 라벨 「북한법 에이전트」「북한법 생성기」→ 원본 id 찾기(둘 다 1ca057f1) |
| `WS /api/v1/resume/{id}?skip=1` | `app/chat/router.py:193·198` | 챗봇 매 턴(fork) |
| `POST /api/v1/sessions/{id}/ask` | `app/services/law_service.py:210` | diff 리포트 원샷(fork), 실패 시 Gemini 폴백 |

- profile·labels·title·resume-inplace 는 **안 쓴다** → S1 차단은 nklaw 에 영향 없음.
- 목록 조회는 env `SM_AGENT_SESSION`·`SM_GEN_SESSION` 에 원본 id 를 고정하면 없앨 수 있다(004 제안). 서비스 토큰 이행 때 고정 방식으로 한다.
- 한 원본(1ca057f1)을 **두 용도**로 쓴다: 챗봇(조회 도구 허용 allowlist)과 생성기(`/ask`, 도구 전부 차단 전제). 프로필이 원본 단위라 둘이 충돌 → §9-2.

## 2. 원칙

1. **Host 가 전부 맡는다.** 서비스 대장·토큰 발급·폐기·검사·차단은 Host 의 기능이다. CP 는 Host 들을 관리할 뿐
   Host 안의 서비스는 모른다(사장님 결정 2026-10-20). 승인 알림이 폰으로 가는 기존 경로만 그대로 쓴다.
2. **토큰 하나 = 서비스 하나 = 원본 하나.** 서비스 토큰으로는 자기 원본의 **사본(fork) 질의만** 된다.
3. **원본은 ClewPath 가 만들고 지킨다.** 서비스는 원본을 직접 열지 못하고(in-place 금지), 사본은 ClewPath 가 만들고 지운다.
4. **원본 폴더는 서비스가 정하되, Host 가 검사하고 사장님이 승인한다.** 폴더는 claude 가 자동으로 읽는 범위
   (`CLAUDE.md`, `.claude/settings.json`, 기억, 폴더 안 파일)를 정하므로 보안의 핵심이다.
5. **가드레일 변경은 사장님만.** 프로필은 로컬 화면·로컬 루프백·승인 요청으로만 바뀐다.
6. **역할 경계(사장님 2026-10-20):** ClewPath = 원본 생성·보안 설정·보관·사본 정리. 서비스 = 어느 원본을 쓸지 정하고 연결
   (라벨 지정·env 고정). ClewPath 가 서비스 쪽 연결(라벨 이동 등)을 대신 하지 않는다.

## 3. 데이터

`data_dir/services.json` (Host 자체 저장소, claude 파일 아님)

```json
{
  "services": {
    "nklaw": {
      "name": "북한법 챗봇",
      "base_session": "1ca057f1-…",
      "folder": "E:/004_북한법/nklaw_chatbot",
      "token_hash": "sha256:…",          // 원문은 저장하지 않음
      "token_hint": "cps_nklaw_…a91f",   // 화면 표시용 끝 4자리
      "limits": {"daily_usd": 5, "daily_calls": 200, "max_call_usd": 0.5},
      "ops": ["resume", "ask"],
      "status": "active",                // active | suspended | revoked
      "created": 1792…, "approved_via": "phone", "approval": "ap_…",
      "requested_by": "<신청 세션 uuid>", "purpose": "북한법 질의 챗봇"
    }
  },
  "requests": { "sr_…": { "…신청 내용…", "claim_hash": "…", "status": "pending|approved|rejected|claimed" } }
}
```

가드레일 자체는 지금처럼 `profiles.json`(세션별)에 둔다 — 서비스 대장은 "누가 어느 원본을 쓰는가"만 갖는다.
프로필이 있는 원본은 기존 `serve_keep` 정기 작업이 30일 삭제를 자동으로 막는다.

## 4. 토큰

- 형식 `cps_<서비스id>_<랜덤 32바이트 hex>`. 접두사로 서비스를 바로 찾고, 해시(sha256)를 상수 시간 비교.
- **원문은 한 번만 건넨다:** 승인 후 신청자가 신청 때 받은 `claim` 비밀로 `POST /api/services/requests/{id}/claim`
  을 한 번 부르면 토큰을 받는다(이후 claimed, 다시 못 받음). 사람이 복사·붙여넣기 할 필요 없음.
- 재발급: 화면에서 🔄 → 새 토큰 claim 링크(비밀) 발급, 옛 토큰은 claim 순간 폐기(무중단 교체).
- 폐기: 화면에서 ⛔ → 즉시 401. 진행 중인 WS 는 끊는다.
- 감사 기록(`api_audit.jsonl`)에 `token` 대신 `service` 를 적는다.

## 5. 권한표 (변경 후)

호출자 종류: **로컬**(루프백·릴레이 아님) · **소유자 토큰**(기존 `SM_API_TOKEN`, §9 결정 필요) · **서비스 토큰**

| 엔드포인트 | 로컬 | 소유자 토큰 | 서비스 토큰 |
|---|---|---|---|
| `GET /api/v1/sessions`, `/labels` | ✅ | ✅ | ❌ 403 |
| `WS /api/v1/resume/{id}` | ✅ | ✅ | **id == 자기 base 일 때만** (fork 강제) |
| `POST /api/v1/sessions/{id}/ask` | ✅ | ✅ | id == base 일 때만 |
| `WS /api/v1/resume-inplace/{id}` | ✅ | 2FA 필요(미구현 자리 채움) | ❌ |
| `GET …/profile` | ✅ | ✅ | 자기 base 만(읽기) |
| `POST …/profile`, `…/labels`, `…/title` | ✅ | ❌ (로컬로 이동) | ❌ |
| `GET /api/v1/service/me` (신규) | — | — | 자기 서비스 정보·오늘 사용량·한도 |

서비스 토큰의 한도는 서비스별 `limits` 를 쓴다(지금은 전역 `SM_API_DAILY_*` 를 토큰별로 적용).

## 6. 등록 흐름

```
서비스 개발 세션(이 PC 의 claude, 로컬 루프백)
  POST /api/services/requests  {id, name, folder, purpose, limits?, base_session?}
    → Host 폴더 검사(§7) → 결과를 신청에 붙임
    → 승인 요청(approvals kind="service_register") → 사장님 폰 알림
    → 응답: {request_id, claim}   ← claim 은 이때 한 번만 보임
사장님 승인(폰 탭/2FA 또는 로컬 화면)
    → Host 가 원본 준비(§8) → services.json 기록 → 신청 approved
서비스 개발 세션
  POST /api/services/requests/{id}/claim {claim} → {token}  (1회)
```

- 신청은 **로컬 전용**이다. 컨테이너(외부 IP)는 신청할 수 없다 — 등록은 그 서비스를 만드는 사람(세션)이 이 PC 에서 한다.
- `base_session` 을 주면 "기존 세션 입양". 그 세션의 cwd 가 `folder` 와 같아야 한다. **권장하지 않는다** — 아래 실측.

> **실측(NKBOT-7, 2026-10-20): 원본의 대화 기록이 시스템 프롬프트를 이긴다.** 1ca057f1(설정·시험 대화 266KB)을 fork 하면
> 보강한 [최우선 규칙]이 있어도 경로·권한·`! Get-Content`·`/permissions` 안내가 새고, 기록 속 학보 내용이 답에 섞였다
> (프롬프트를 교체 방식 `--system-prompt` 로 바꿔도 동일). 같은 프로필의 **빈 세션**은 범위 밖 질의 3종 모두 고정 문구 한 줄,
> 법령 질의 정상, 턴 비용 약 1/3. → 원본은 **항상 새로 만든다**(§8-2, 첫 턴은 「준비 완료」만). 원본에 사람이 직접 대화하지 않는다
> (점검·설정 대화는 fork 로). 입양은 이행 편의로만 두고 승인 화면에 "대화 기록 N KB — 새로 만들기 권장" 경고.
> 시스템 프롬프트는 덧붙이기(append) 유지 — 교체 방식은 빈 세션에서 영어 혼잣말이 한 줄 새는 경우가 있었다.

## 7. 원본 폴더 검사

거부(승인 불가):
- 드라이브 루트(`E:/`), 사용자 홈과 그 바로 아래(`C:/Users/x`, `~/Desktop` 등), `~/.claude` 와 그 아래
- 다른 세션의 작업 폴더를 품는 상위 폴더(스캐너의 cwd 목록과 대조) — 예: `E:/004_북한법` 는 거부, `…/nklaw_chatbot` 은 가능
- 이미 다른 서비스가 쓰는 폴더
- 존재하지 않는 폴더(만들지 않는다 — 서비스가 먼저 만든다)

경고(사장님이 보고 승인 가능):
- 폴더 안(하위 포함, 깊이 3)에 `.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, `secrets*`
- `CLAUDE.md`, `.claude/settings.local.json`, `.mcp.json` 존재(내용 요약을 승인 화면에 보임)
- `.claude/settings.json` 이 없거나 `permissions.deny` 가 비었음 → "기본 차단 설정을 써 넣을까요?" 선택지
- 폴더 크기·파일 수가 큼(검색 도구로 읽힐 범위가 넓음)

## 8. 원본 준비 (승인 후)

1. (선택된 경우) `folder/.claude/settings.json` 에 기본 차단 설정 작성 — 없을 때만, 있으면 건드리지 않고 경고만.
   기본값: 홈 `.claude/**`(자기 tool-results 제외)·`.env*`·다른 프로젝트 tool-results 의 Read/Grep/Glob deny (NKBOT T21 결과를 템플릿화).
2. 새 원본이면 `claude -p --session-id <uuid> --output-format json` + 프로필 가드레일로 첫 턴("준비 완료"만 답)을 돌려 세션 생성.
3. `profiles.set_record(base, …)` — 신청의 시스템 프롬프트·허용 도구 + 서비스용 기본(permission_mode dontAsk, 위험 도구 deny).
4. 라벨 「서비스: <name>」, 이름(custom-title+agent-name) 「<name> 원본」.
5. 실측 점검(선택): T21 과 같은 허용/거부 프로브를 한 번 돌려 결과를 대장에 남김.

1·4 는 claude 가 읽는 파일을 쓰는 일이라 **불가침 원칙의 승인 예외로 CLAUDE.md 에 추가**해야 한다(사장님 승인 화면 경유·없을 때만 생성).

> **S3 구현(0.26.0)은 claude 파일을 쓰지 않는다** — 그래서 새 예외가 필요 없다. 1(settings.json)은 검사·경고만, 4 의 이름은 claude 이름(custom-title) 대신 ClewPath 라벨 레코드(`labels.json` 의 name)로 「<name> 원본(<용도>)」. 2 의 새 원본 기록은 claude -p 가 만든다(비서 세션 만들기와 같은 방식). 5(실측 점검)는 S4 이후.

## 9. 열린 결정 (사장님)

1. **기존 `SM_API_TOKEN` 의 운명** — (A) "소유자 토큰"으로 남기되 위험 동작(in-place·프로필)은 로컬로 이동 (B) 서비스 이행 후 폐기.
   권장 A→B: 북한법 이행이 끝나면 폐기. 이 PC 안의 MCP(`mcp_server.py`)는 루프백이라 토큰 없이 동작.
2. **한 서비스에 원본 여러 개?** — 북한법이 이미 두 용도(챗봇·생성기)를 한 원본에 섞어 가드레일이 맞지 않는다.
   권장: 서비스 하나에 **용도별 원본**(`bases: {"chat": id, "gen": id}`)을 두고, 토큰 하나로 그 원본들만 허용. 용도마다 프로필이 따로.
3. **토큰 만료** — 권장: 만료 없음 + 재발급·폐기 버튼(서비스가 자동 갱신을 못 하므로).

## 10. 단계

| 단계 | 내용 | 효과 |
|---|---|---|
| S1 차단 ✅ 0.24.0 | 권한표(§5) 중 소유자 토큰 축소: profile/labels/title POST 로컬 전용, in-place 원격 거부 | 지금 구멍의 대부분이 바로 닫힘. nklaw 미사용 확인(004 회신). 가드: test_service_s1.py |
| S2 대장·토큰 ✅ 0.25.0 | services.json(`bases:{용도:id}`), 서비스 토큰 발급·재발급·정지·폐기(`/api/owner/services`, 로컬 전용 — 신청·claim 은 S3), resume/ask 에서 base 일치 검사(루프백도), 서비스별 한도·감사(`service`), 정지 시 WS 다음 턴 끊김. 가드레일 프로필 없는 원본·다른 서비스 원본은 거부 | 서비스별 격리. 가드: test_service_s2.py |
| S3 등록 흐름 ✅ 0.26.0 | `POST /api/services/requests`(로컬 전용) → 폴더 검사(§7, 거부면 신청 불가) → 승인 kind `service_register`(폰/2FA) → `…/claim` 에서 원본 준비(§8-2 -p 첫 턴)·서비스 등록·토큰 1회. 화면 '실행' 으로는 준비 안 됨(claim_required). 준비 중 실패는 claim 재시도로 이어서(만든 원본 재사용) | 새 서비스가 스스로 등록. 가드: test_service_s3.py |
| S4 화면 ✅ 0.27.0 | 설정 「🔌 외부 서비스」(PC 로컬 전용): 목록·원본·오늘 사용량·정지/다시 켜기·재발급(새 토큰 한 번 표시)·폐기(확인창). 승인 시트 `service_register`: 서비스·폴더·한도·원본 가드레일(시스템 프롬프트 포함)·검사 경고를 읽기 좋게(폰에서 승인) | 사장님 관리. 가드: tests/pwa/services.test.mjs |
| S5 이행 ✅ 0.27.1 (2026-10-20) | 북한법: 004 가 신청(chat=d5b86b3b 입양, 생성기도 같은 원본) → 사장님 폰 승인 → claim → 서비스 모니터가 nklaw-backend `SM_API_TOKEN` 을 cps_nklaw_ 로 교체(대장 46b471f), 챗봇·diff 리포트 정상. 옛 공용 토큰은 `clewpath.toml [server] legacy_api_token = false` 로 끔(환경변수는 업데이트 재기동마다 옛 셸 값이 이어져 설정 파일 스위치로) | 레거시 토큰 제거 |

## 11. 테스트

- 서비스 토큰: 다른 세션 id 로 resume/ask → 403, base 로 → fork 강제·사본 정리, in-place → 403, 목록/프로필 POST → 403
- 폐기 즉시 401, 재발급 claim 후 옛 토큰 401, claim 두 번째 → 410
- 폴더 검사: 루트·홈·`~/.claude`·상위 폴더 거부, `.env` 경고, settings.json 있으면 무접촉
- 신청은 로컬 전용(릴레이·외부 IP 403), 승인 전 claim → 409
- 서비스별 한도 초과 → 429, 감사 기록에 service id
- 회귀: 로컬 PWA·MCP(루프백) 경로는 그대로

## 12. 결정: Docker 컨테이너는 로컬로 본다 (사장님 2026-10-20)

실측: Docker Desktop 은 컨테이너의 `host.docker.internal:5100` 연결을 중계 프로그램(`com.docker.backend`)이 대신 열어
Host 에는 127.0.0.1 로 들어온다 → `_is_local` 참 → 컨테이너는 토큰 없이 로컬 전권(PC 전용 `/api/owner/services` 200,
가짜 토큰도 통과). 서비스 토큰을 내밀면 ask·resume 은 토큰 기준(자기 원본만)으로 판정하지만, 토큰을 빼면 그 제한도 없다.

**결정: 지금은 그대로 둔다** — 이 PC 의 컨테이너는 모두 사장님이 직접 설정·운영하는 것이라 로컬로 봐도 된다.
감수하는 위험: 인터넷 입력을 받는 컨테이너(nklaw-backend)가 뚫리면 그 권한이 곧 PC 로컬 권한(세션 열람·가드레일 해제·원본 직접 재개).
현재는 백엔드가 방문자 입력을 claude 사본에 넘길 뿐 직접 실행하지 않아 낮다고 판단.

다시 볼 때: 외부 서비스가 늘거나, 사장님이 운영하지 않는 컨테이너가 생기거나, SaaS 로 다른 사용자가 쓸 때.
그때의 안(검토만 함): Host 가 연결을 연 프로세스를 보고(Docker·WSL 중계면 원격) 판정 — 포트·서비스 설정 변경 없음.
(외부 서비스 전용 포트 안은 사장님이 원치 않음.)
참고: 인증 미들웨어의 `_via_relay` 는 dict scope 에서 헤더를 못 읽어 릴레이 요청도 미들웨어에선 로컬로 통과한다 —
폰 경로가 그것에 기대어 동작하므로(커넥터가 기기 인증을 먼저 함) 손대려면 함께 설계할 것.

