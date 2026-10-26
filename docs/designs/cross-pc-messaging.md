# PC 사이 세션 통신(pclink) — 기획 (2026-10-22)

> 상태: **설계 검토 끝(plan-eng-review 2026-10-22~23, 결정 D3~D15·Codex 독립 검토 반영) — 구현 대기(devflow STEP 4).** 빈틈 서술이 있어 깃에는 아직 올리지 않는다(사장님 지시).
> 저장소 `CLAUDE.md` 불가침 원칙 2(claude 의 의도치 않은 동작을 부를 수 있는 기능은 먼저 협의)에 해당 — 이 문서가 그 협의다.
> 구현할 때 「원격 묻기 = 허용된 세션의 도구 없는 복제본에 묻기」를 `CLAUDE.md` 승인된 예외에 적는다.
> 백로그: `TODOS.md` 「PC 사이 세션 통신」, 미룬 항목 스펙 `docs/spec-pclink-hub-handoff.md`.

## 0. 결정 (2026-10-22 사장님)

| 항목 | 결정 |
|---|---|
| 방법 | ClewPath 중계로 만든다(사장님 릴레이·E2EE). 공식 Remote Control 은 Anthropic 서버 경유·claude.ai 에 세션 기록 저장이라 고르지 않음. 단, 어느 쪽이든 Claude 가 처리하는 글은 모델 제공자에게 간다(자체 중계가 없애는 것은 claude.ai 기록 저장뿐) |
| 장기 목표 | 세션들 사이의 **네트워킹 허브**(계층 프로토콜). **별도 프로젝트**로 한다. 이 프로젝트는 지금 할 수 있는 것만 |
| 이번 범위 | **조회 전용 원격 묻기까지**(크로스체크 3/4 합의 — Gemini·Codex·Claude, Copilot 은 실시간 먼저) |
| 쓰임새 | ③ 다른 PC 세션에 묻기 — **이번에 됨**. ② 운영 세션끼리 연락 — **묻기까지**. ① 다른 PC 워커에 일 시키기 — **허브 프로젝트로** |
| 받는 쪽 | 세션마다 허용(기본 거부) + 첫 연락 폰 승인 |
| 운영 서버 보강 | 릴레이 fail-closed·토큰 세대 대조, CP 내부 API 인증 — **같이 의뢰하되 기능을 켜는 조건은 아님**(짝 통로는 짝 전용 키로 Host 가 직접 검사해, 릴레이가 장악돼도 짝 글을 위조하지 못한다) |

## 1. 하려는 것

PC A 의 Claude 세션이 ClewPath 로 짝지은 PC B 의 세션 x 에게 **묻고 답을 받는다.**
B 는 세션 x 의 **복제본**(도구·MCP·훅 없음)에게 물어 답만 돌려준다. 세션 x 자체는 바뀌지 않고, B 에서 아무것도 실행되지 않는다.

## 2. Phase

### Phase 1 — 이번 사이클(이 프로젝트에서 완성)

| # | 항목 | 내용 |
|---|---|---|
| 1-0 | 프로토콜 약속 v0 문서 | `docs/designs/pclink-protocol-v0.md`(1~2쪽 + 예제). 3층 경계·봉투·주소·상태·버전 협상·거부 규칙. **허브 프로젝트의 씨앗** |
| 1-1 | 짝 통로 보안 | 짝별 키(폰 방 키와 분리), 평문 거부, 봉투 머리(버전·id·종류·보낸·받는·만료)를 암호 인증 데이터에 묶음, nonce 재사용 금지, 만료 검사, **재시작 뒤에도 남는 중복 id 장부** |
| 1-2 | PC 짝짓기 | B 가 **초대 문자열**(1회용·24시간, 1회용 초대 비밀·B 의 짝짓기용 자격·두 PC 신원)을 보여 주고 A 에 붙여 넣는다(D5·D8) → 두 Host 가 X25519 로 **운영 짝 키를 새로** 만들고(초대 비밀로 인증, D12) B 가 새 자격을 주고 초대 자격 폐기 → 반대 방향 자격도 짝 통로로 넘겨 **한 번에 양쪽**(D6). **양쪽 폰 승인**(승인 대기열 재사용). **PC 전용 자격**(pclink·ping 만). 초대·받기·세션 허용 켜기는 PC 화면과 폰(D9), 폰 경유는 **E2EE 봉투 + 2FA 필수**(D11). 해제 = 내 발급분 즉시 폐기·상대 발급분 내 쪽 삭제+알림(상대 꺼져 있으면 정리 대기). 끄기 스위치(설정 + 폰)·상대 Host 버전 확인 |
| 1-3 | 조회 전용 원격 묻기 | A: 로컬 API 로 묻기(로컬 전용), Host A 가 보낸 세션을 프로세스로 확인(같은 세션 id 중복 주장이면 거부). A 는 **물을 때만** B 방에 붙는다(60초 재사용, D7). B: 세션별 「원격 묻기 받기」(기본 끔) + 첫 연락 폰 승인 = **그 세션의 지금과 앞으로의 대화 전체 열람, 30일**(D13) → 커넥터는 **접수 즉시 답·별도 작업**(D4) → 도구·MCP·훅 없는·기록 없는 복제본(D3·실험 4) → 결과를 짝 키로 봉인해 그 cid 로, **10분 보관·같은 id 재조회**(D14). 한도는 실행 전 예약(묻기 8,000자·답 20,000자·180초·분 6/일 200·묻기당 0.5달러·Host 동시 2·대기 4), 양쪽 감사 장부(본문 없음) |
| 1-4 | 세션용 안내 | 세션이 원격 목록 보기·묻기를 하는 법(스킬 한 절 또는 새 스킬 — 설치는 기존 「📦 스킬 설치」 버튼 방식) |
| 1-5 | 운영 서버 보강 의뢰(병행) | 007: 릴레이 fail-closed·토큰 세대 대조, CP `/internal/*` 인증 → 서비스 모니터 의뢰. 조건 아님 |

### Phase 2 — 허브 프로젝트로 넘김(이 프로젝트에서는 만들지 않음)

스펙: `docs/spec-pclink-hub-handoff.md`
- 실시간 주고받기(살아 있는 세션에 글 전달·답장) — 우체국 방식 실험 결과 포함
- 원격 작업 지시(쓰임새 ①), 운영 세션 실시간 연락(② 나머지)
- 관제·팀 연동(다른 PC 세션을 팀 멤버로, 폰 타임라인)
- 다자간·다중 홉·브로드캐스트·오프라인 큐·라우팅 허브·SDK

### Deferred — 판단 보류

- 폰 연결에도 E2EE 강제·방 키 교체·재전송 방지(옛 폰 앱·PWA 호환 확인 뒤)
- 원격 `/respond`(권한 창 키 입력)를 2FA 특권으로 올릴지
- 주인 단위로 PC 묶기(CP — SaaS 때)

## 3. 지금 있는 것 (조사 결과, 2026-10-22)

| 항목 | 지금 |
|---|---|
| PC 안 세션 통신 | Claude 의 SendMessage. ClewPath 는 주소(`~/.claude/sessions/*.json` 의 `messagingSocketPath`)를 읽기만 한다 |
| 세션에 묻기 | `POST /api/v1/sessions/{id}/ask` = `claude -p --resume <id> --fork-session` 복제본에 묻고 복제본 삭제(원본 불변). 가드레일(금지 패턴·한도·길이), 서비스 토큰은 자기 원본만. 릴레이 메서드 `ask` 도 같은 길 |
| 릴레이 | 방 하나 = Host(agent) 1 + 폰·브라우저(client) 여럿. 방 사이 전달 없음. Host 가 다른 Host 에 보내는 코드 없음 |
| CP | Host 등록·JWT(Ed25519; agent 10분, client 12시간). 한 주인의 여러 PC 를 묶는 단위 없음 |
| 폰의 여러 PC | PC 마다 QR 페어링 → 기기 토큰 + 방 키. 한 번에 한 방 |
| 기기 인증 | 요청마다 활성 기기·토큰 세대 검사(항상 켜짐). 원격 요청 표시 `X-ClewPath-Via: relay`, 로컬 전용 경로 덴리스트, 2FA 특권 |
| 알려진 빈틈 | Host 가 `[e2ee] require` 꺼짐이면 평문 요청 수용(이 PC 꺼짐) · 방 키 공유·미교체 · 재전송 방지 없음 · 릴레이 토큰 세대 미대조·검증 불통 시 통과(운영 `RELAY_JWT_VERIFY=1`·`RELAY_DB_VERIFY=1` 은 켜짐)·공유 토큰 접속 열림 · CP `/internal/*` 무인증(내부망) |

## 4. Phase 1 설계

### 4.1 세 층 (허브 프로젝트로 그대로 옮길 경계)

| 층 | 맡는 것 | 모르는 것 |
|---|---|---|
| 전송·암호 (`pclink/transport`) | 짝 채널(지금은 릴레이 방 client 연결), 봉투 봉인·개봉(AEAD), nonce·만료·중복 id 장부, 짝 키 | 봉투 본문의 뜻 |
| 정책 (`pclink/policy`) | 짝 목록·PC 전용 자격, 세션별 허용·첫 연락 승인·범위, 한도, 끄기 스위치, 감사 장부 | 어떻게 보내는지, Claude 를 어떻게 부르는지 |
| 실행 어댑터 (`pclink/adapters`) | 지금은 `ask_fork`(도구 없는 복제본에 묻기) 하나. 나중에 허브가 다른 어댑터를 더한다 | 암호·짝 |

**모델은 주소·권한·재시도를 정하지 않는다.** 모든 판단은 Host 코드가 한다.

### 4.2 봉투 (v0 — 정본은 `pclink-protocol-v0.md`)

```json
{"v": 0, "id": "<uuid>", "reply_to": null, "kind": "ask",
 "from": {"host": "<host_id>", "session": "<uuid>"}, "to": {"host": "<host_id>", "session": "<uuid>"},
 "expires_at": 1792650000, "body": {"prompt": "…"}}
```
- 주소는 `host_id/session_id`. 이름·방 id·파이프 경로는 표시·연결용 매핑일 뿐 주소가 아니다(이름은 바뀐다).
- `kind`: `hello`(버전·기능 협상) · `ask` · `answer` · `refused` · `error`. 모르는 `v`·`kind` 는 거부.
- 상태: 접수(accepted) ≠ 답(answered). 만료·시간 초과·거부 사유를 정해 둔다.
- 머리(v·id·kind·from·to·expires_at)를 AEAD 의 인증 데이터에 묶는다 — 릴레이가 주소·종류를 바꾸면 개봉이 실패한다.

### 4.3 PC 짝짓기와 PC 전용 자격 (검토 결정 D5·D6·D8·D9·D11·D12)

- **등급:** PC 짝은 Host 기기 목록에 `kind=pc` 로 들어가고, 커넥터는 이 기기에 `pclink.*`·ping 만 허락한다(api 대리 호출·terminal·monitor·ask·export/import·grant 거부). PC 짝에는 B 의 방 키를 주지 않는다. pclink 요청은 바깥이 평문이어도 안쪽이 짝 키로 봉인돼 있어야만 받는다(E2EE 강제 설정과 무관).
- **초대(B):** PC 화면 또는 폰에서 「초대 만들기」 → B 폰 승인(승인 대기열 `pclink_pair`) → **초대 문자열**: 1회용 초대 비밀 · B 의 짝짓기용 자격 · 두 PC 신원(host_id) · B 방 정보. **1회용·24시간.**
- **받기(A):** PC 화면 또는 폰에서 붙여넣기 → A 폰 승인 → A 가 짝짓기용 자격으로 B 방에 client 로 붙는다 → 일회용 **X25519 공개키 교환**(초대 비밀로 인증, 두 host_id 를 묶음) → HKDF 로 **운영 짝 키**를 새로 만든다 → B 가 새 기기 자격을 짝 통로로 주고 초대 자격은 CP 에서 폐기 → A 가 자기 방 자격을 짝 통로로 B 에 넘겨 **한 번에 양쪽**이 된다. 쓰고 난 초대 사본은 운영 키도 B 방 출입증도 아니다.
- **폰 경유 관리:** 초대 만들기·받기·세션 허용 켜기·pclink 승인 결정은 PC 화면과 폰에서 되지만, 폰(릴레이 경유)은 **E2EE 봉투로 온 요청만 + 2FA 필수**(꺼져 있으면 PC 화면만). 끄는 동작(해제·허용 끄기·끄기 스위치)은 폰에서 그대로.
- **해제:** 내가 발급한 상대 자격은 CP 에서 즉시 폐기(내 방에서 상대 즉시 차단, CP 실패=불명→pending), 상대가 발급한 내 자격·짝 키는 내 쪽에서 즉시 삭제, 상대에게 짝 통로로 알림(상대가 꺼져 있으면 「상대 쪽 정리 대기」).
- **저장:** 짝 키·상대 자격은 방 키처럼 보호(Windows DPAPI, POSIX 0600).
- 상대 Host 의 pclink 버전이 낮으면 짝짓기를 거절하고 안내한다(hello).

### 4.4 짝짓기·조회 전용 원격 묻기 흐름 (검토 결정 반영)

```
[짝짓기]
 B 화면/폰(폰은 E2EE+2FA) 「초대 만들기」 → B 폰 승인 → 초대 문자열(1회용·24시간)
      └ 사장님이 옮김 ─────────────→ A 화면/폰(폰은 E2EE+2FA) 「붙여넣기」 → A 폰 승인
 A ─(짝짓기용 자격, B 방 client)─ X25519 교환(초대 비밀로 인증·두 host_id 묶음) ─ B
      → 운영 짝 키(HKDF) · B: 새 자격 발급 + 초대 자격 폐기 · A: 자기 방 자격 전달(양쪽)

[묻기]
 세션 a ─curl(로컬 전용)→ Host A: 보낸 세션 확인(중복 주장 거부) · 끄기 스위치 · 한도 예약
 Host A ─(물을 때만, 60초 재사용) B 방 client─ pclink.ask  [짝 키 AEAD, 머리 v·id·kind·from·to·expires_at 인증]
 Host B 커넥터: 개봉·만료·중복 장부 → 같은 id 면 보관된 답/「진행 중」 → 아니면 즉시 accepted (프레임 루프 안 막음)
   작업 스레드(Host 동시 2·대기 4): 세션 x 허용(기본 끔)? 첫 연락 승인(30일, 그 세션 지금·앞으로 전체)?
     → 복제본: claude -p --resume x --fork-session --no-session-persistence --tools "" --strict-mcp-config
               --settings {"disableAllHooks":true} --max-budget-usd 0.5 (180초)
     → 결과를 짝 키로 봉인해 그 cid 로(A 가 떠났으면 버림·방 전체에 안 뿌림) + 요청 id 로 10분 보관
 세션 a ←── 응답: 답(외부 데이터 표시) | pending_approval | not_allowed | paused | limit | busy | peer_offline | timeout+요청 id
```
- 답은 A 세션이 부른 API 의 응답으로 돌아간다(세션 a 에 글을 밀어 넣지 않는다). 답은 신뢰하지 않는 외부 데이터로 표시한다.
- 첫 연락 승인 대기 중이면 즉시 `pending_approval`(폰 승인 뒤 다시 묻기) — 동기 호출이 오래 묶이지 않게.
- 시간 초과면 요청 id 를 돌려줘, 같은 id 로 다시 물으면 다시 실행하지 않고 보관된 답을 받는다.

### 4.5 보안 — 위협과 막는 법 (검토 반영)

| 위협 | 막는 법 |
|---|---|
| T1 원격 실행 | 복제본에 도구·MCP·훅 없음(실행 자체가 불가, 실 e2e 계약 시험으로 버전마다 확인), 원본 불변, 받는 세션 기본 거부·첫 연락 승인 |
| T2 PC 짝이 폰 권한을 얻음 | PC 전용 자격 — `pclink.*`·ping 만, 방 키 안 줌 |
| T3 릴레이·CP 장악 → 가짜 글·재전송·주소 바꾸기 | 짝 키 AEAD + 머리 인증 + nonce·만료·영구 중복 장부, 안쪽 봉인 없는 pclink 거부 |
| T4 다른 Claude 세션이 보낸 세션(from)을 거짓으로 댐 | 연결 연 프로세스 → claude 조상 → 레지스트리로 확인, 한 세션 id 를 두 항목이 주장하면 거부. **막지 못하는 것:** 같은 사용자 권한의 악성 프로그램·레지스트리 파일을 고치는 세션(그 권한이면 짝 키도 읽는다) — 세션별 허용은 인증이 아니라 노출 줄이기 |
| T5 B 세션 내용 노출 | 받는 세션 허용(기본 끔) · 첫 연락 승인 = 그 세션 지금·앞으로 대화 전체 열람(카드에 명시) · 30일 · 허용 끄면 승인 폐기 · 답 크기 한도 |
| T6 폭주·비용 | 실행 전 예약(횟수·예산), Host 동시 2·대기 4, 분 6·일 200, 크기·180초·묻기당 0.5달러 |
| T7 사후 추적 | 양쪽 감사 장부(누가·언제·어느 세션·결과, 본문 없음), 폰에서 보기 |
| T8 해제 뒤 남는 접속 | 내 발급분 즉시 CP 폐기·연결 끊기, 상대 발급분 즉시 삭제·상대 알림(꺼져 있으면 정리 대기) |
| T9 승인 통로 사칭(장악된 릴레이 + 평문 폰 프레임) | 폰 경유 짝짓기·허용·pclink 승인은 E2EE 봉투 + 2FA 필수 |
| T10 옮긴 통로에 남은 초대 사본 | 1회용·24시간, 초대엔 1회용 비밀만(운영 키는 X25519 로 새로), 초대 자격은 짝짓기 뒤 폐기 |

### 4.6 맥 대응 작업과 충돌하지 않게

| 이 기능 | 맥 측(`mac/<주제>`) |
|---|---|
| 새 패키지 `session_manager/pclink/`, 새 시험 `tests/test_pclink_*.py`, 문서 `docs/designs/pclink-*`, `docs/spec-pclink-*` | 시험(기존)·런처 템플릿·`install.sh`·`uninstall.sh`·`update_runner.sh`·`ops/get.sh`·`hostctl.py`·`osdep/`·`docs/mac-*` |

- 공통 진입점은 최소로만 고친다: `server.py`(라우터 한 줄), `connector.py`(pclink 동작 분기·PC 등급), `devices.py`(기기 `kind`). 맥 쪽이 고치지 않는 파일이다.
- `tests/conftest.py`·의존성 파일은 고치지 않는다(필요하면 맥 브랜치를 합친 뒤에).
- PWA(`pwa/index.html`)는 릴레이가 작업 트리를 그대로 서빙하므로 **작업 트리에서만** 고친다.
- 작업은 scratchpad 작업 트리 `pclink/<단계>` 브랜치. 합치기 전 main 에 rebase, 맥 브랜치와 합치는 순서는 Windows 측(이 세션)이 정한다.
- 007(릴레이·CP) 보강은 다른 저장소라 충돌 없음 — 배포는 서비스 모니터.

## 5. 실험 기록 — 우체국 방식(Phase 2 근거)

2026-10-22, 이 PC, Claude Code 2.1.308, scratchpad `pcmsg-spike/`. 받는 쪽은 이 개발 세션 자신(자동 허용 모드).

| 실험 | 결과 |
|---|---|
| 일회용 배달원(`-p`, haiku, `--tools SendMessage`) → 살아 있는 세션 | 전달됨, 13초·0.0044달러. 받는 쪽에 `from-name`(`-n` 이름)·`from-mode`·보낸 쪽 주소. 글 속 미끼 지시는 배달원이 따르지 않음. **모델이 옮기며 글자 손실(`C:\work\a b` → `C:\work b`)**. 배달원이 끝나면 답장 주소가 사라짐 |
| 계속 떠 있는 우체국(스트림 입출력) | 답장이 새 차례로 들어오고 `result.origin.body` 에 원문 그대로, `origin` 에 `kind/from/name/fromMode/msg_id`(공식 계약 아님 — 버전별 계약 시험 필요) |
| 도구 묶기 | `--tools` 만으로는 MCP 도구가 남음 → `--strict-mcp-config` 를 더해야 도구 1개·MCP 0개 |
| 우체국 규칙 | "받음만 답하라"를 SendMessage 회신으로 실행 → 규칙은 "도구를 쓰지 않는다" |

받는 Claude 는 다른 세션의 글을 "동료의 요청 — 이 세션 권한 안에서 처리"로 감싼다 → 자동 허용 세션은 원격 지시를 그대로 실행한다. 실시간 전달을 이번 범위에서 뺀 핵심 이유.

## 6. 설계 검토(plan-eng-review, 2026-10-22~23)에서 정해진 것

예전에 열려 있던 질문은 모두 닫혔다(근거와 답은 아래 「Decision ledger」 R1~R12):
- 짝 키: 초대엔 1회용 비밀만, 운영 키는 짝 맺을 때 X25519 로 새로(D12) · 짝 하나에 양쪽(D6) — 운영 키 하나를 두 방향이 같이 쓰고 봉투 머리의 from·to 로 방향을 묶는다.
- 짝 채널: 물을 때만 붙고 60초 재사용(D7).
- 보낸 세션 확인: Windows `psutil.net_connections`, 리눅스 /proc, 맥은 출시 뒤 맥 측 실험(D15 TODO). 확인 못 하거나 중복 주장이면 거부. 위협 범위는 4.5 T4.
- 복제본 격리: `--tools ""`·`--strict-mcp-config`·`--settings {"disableAllHooks":true}`·`--no-session-persistence`(실험 4로 확인) + 실 e2e 계약 시험. 큰 세션은 묻기당 0.5달러·180초로 묶고 출시 전 다시 잰다.
- 첫 연락 승인 문구·범위: 그 세션의 지금과 앞으로 대화 전체, 30일(D13). 감사 장부는 본문 없이 양쪽.
- 맥: 이 PC ↔ 맥 짝은 맥 정식 출시 뒤(S8). Phase 1 은 Windows 사이(이 PC ↔ 노트북)부터.

---

## 기획 검토 기록 — plan-eng-review (2026-10-22)

- 대상: `docs/designs/cross-pc-messaging.md`(이 문서, 깃 미커밋 — 사장님 지시). 검토자: Claude(claude-opus-5-5), gstack 1.91.176.0.
- 질문 번호: D1(gstack 업그레이드 — 지금 업그레이드)·D2(setup 실패 — Bun 올리고 다시, 완료)는 검토 도구 준비. 검토 결정은 D3 부터.

### 범위 점검 A — 대상 평가

**이미 있는 것(재사용):**
- 기기 등록부·쓰기 잠금 `devices.py:33,68-74`(`_LOCK = threading.RLock()`, `_mutate`) — 지난 교훈 「devices.py 잠금 없음」은 낡음(0.10.0 a5df0ed 에서 해결).
- 기기 인증(요청마다 활성·토큰 세대) `connector.py:397-403`, 로컬 전용 덴리스트 `connector.py:493-495`, 2FA 특권 `connector.py:497-500`.
- 복제본 묻기 `webapi.ephemeral_ask` `webapi.py:311-382`, 가드레일 인자 `webapi.py:93-111`.
- E2EE 봉투 `connector.py:760-784`(방 키, AES-GCM). 폰 페어링·CP 기기 자격(`/client/credential`·`/client/token`)·사람 증명 승인(`approvals.py`, `server.py` `_human_proof`).
- 릴레이 `role=client` 접속(007 `relay/relay.py:368-371`, Explore 조사) — 릴레이를 고치지 않고 A 의 Host 가 B 방에 붙을 수 있다.

**최소 변경으로 되는가:** 됨. 단 커넥터가 프레임을 하나씩 처리해(`connector.py:794-795`) 원격 묻기를 인라인으로 기다리면(`connector.py:444-446`, 대기 한도 310초 `connector.py:249`) B 의 다른 기기 프레임이 막힌다 — 아래 S1.

**복잡도(예상):** 새 파일 약 13(패키지 모듈 5~6·시험 5·문서 1·스킬 1), 고치는 파일 약 7(`server.py`·`connector.py`·`devices.py`·`webapi.py`·`pwa/index.html`·`CLAUDE.md`·`TODOS.md`), 새 클래스·서비스 약 4(짝 연결·정책 저장소·묻기 어댑터·짝짓기) [estimated] → **8 파일·2 클래스 문턱을 넘음 → B 의 구조 질문.**

**검색(웹, Aside 없음 → WebSearch):**
- 짝짓기: 별도 경로로 넘기는 짝 값은 **실제 두 PC 의 신원과 묶여야 한다**(오결합 공격, arXiv 1902.07550). 짧은 코드는 PAKE 필요(SPAKE2 RFC 9382, J-PAKE 는 결함으로 폐기) **[Layer 1]**: 높은 엔트로피 비밀을 이미 신뢰하는 통로로 넘기는 지금의 폰 QR 방식과 같은 틀.
- AEAD: 인증 데이터는 통째 재전송을 막지 못한다 → 중복 장부·만료 필요, AES-GCM nonce 재사용은 치명 **[Layer 1]**(무작위 96비트 nonce + 낮은 통량 + 장부).
- 맥: `psutil.net_connections` 는 맥에서 권한 오류(AccessDenied) → 보낸 세션 확인은 맥에서 다른 길(내 사용자 프로세스별 연결·`lsof`) **[Layer 3]**, 맥 측 실험으로 확인할 것.

**TODOS 교차:** `TODOS.md:46`(커넥터 느린 요청이 다른 프레임을 막음, 낮음 — 원격 묻기가 키움), `TODOS.md:281`(X-1 6자리 코드 페어링 — 짝짓기 방식과 겹침), 노트북 이전 선행 ②(CP `/internal` 인증 — 1-5 로 병행 의뢰).

**배포:** 새 산출물 없음(Host 릴리스에 포함). 두 PC 모두 새 Host 여야 함. **맥은 아직 출시 대상이 아님(`platforms=["win"]`, `TODOS.md:66`)** → 이 PC ↔ 맥 짝은 맥 정식 출시 뒤. 이 PC ↔ Windows 노트북은 바로 가능.

### 현실 점검(reality rows)

| 전제 | 주장 | 근거 | 판정 |
|---|---|---|---|
| P1 | 릴레이 방 = Host 1 + client 여럿, 방 사이 라우팅 없음 | 007 `relay/relay.py:128-136,487-505`(Explore) | 확인 |
| P2 | Host 는 `[e2ee] require` 꺼지면 평문 req 수용 | `connector.py:776-784` | 확인 |
| P3 | 커넥터는 프레임을 하나씩, `ask` 를 인라인 대기 | `connector.py:794-795`, `:444-446`, `:249` | 확인 |
| P4 | 기기 인증은 요청마다 | `connector.py:397-403` | 확인 |
| P5 | 복제본 묻기에 MCP·훅을 끄는 인자가 없음 | `webapi.py:93-111,328-331` | 확인 |
| P6 | 도구 0·MCP 0·훅 끔·기록 없음 복제본이 원본 맥락으로 답함 | 실험 4 `pcmsg-spike/probe4.py`(라이브, 7.3초·0.00096달러, 복제본 파일 안 생김) | 확인 |
| P7 | 운영 릴레이 JWT·방 검증 켜짐 | `docker inspect util-session-relay`(라이브, `RELAY_JWT_VERIFY=1`·`RELAY_DB_VERIFY=1`) | 확인 |
| P8 | 이 PC Host 는 E2EE 강제 꺼짐 | `clewpath.toml` 에 `[e2ee]` 절 없음(라이브 grep) | 확인 |
| P9 | 기기 등록부 쓰기에 잠금 있음 | `devices.py:33,68-74` | 확인 |
| P10 | CP 에 한 주인의 PC 묶음 없음 | 007 `control_plane/models.py:24-42`(Explore) | 확인 |
| P11 | 맥에서 `psutil.net_connections` 는 권한 오류 | 웹 문서(ClearML Q&A·psutil 소스) — 우리 맥에서 미실측 | **미확인** |
| P12 | 맥은 아직 출시 대상 아님 | `TODOS.md:66` | 확인 |

unverified=1

REALITY: premise-table finding 12개 전제 중 1개 미확인(맥 프로세스 조회) docs/designs/cross-pc-messaging.md:4.4
REALITY: already-done pass 2026-10-22 이후 관련 파일 커밋 없음·pclink 기호 없음(git log origin/main --since=2026-10-22) session_manager/connector.py:1
REALITY: surface-check pass claude 2.1.308 `--tools`·`--strict-mcp-config`·`--no-session-persistence`·`--settings`·`--fork-session`·`--session-id`·`-n` 를 --help 와 실험 4 로 확인, `disableAllHooks` 는 실험 4(훅 이벤트 0)로 확인 session_manager/webapi.py:328
REALITY: binding-decisions finding 2026-09-29 결정(CP 호출 실패=불명→pending, connect() 는 auth 결과를 돌려줌, devices 쓰기는 _mutate)을 짝짓기 설계가 아직 적지 않음 — S3 에 반영 session_manager/devices.py:68
REALITY: numbers pass 배달 13초·0.0044달러 [measured: scratchpad/pcmsg-spike/probe1.py:22] · 복제본 7.3초·0.00096달러 [measured: scratchpad/pcmsg-spike/probe4.py:33] · 대기 한도 300초 [measured: session_manager/webapi.py:312] · 310초 [measured: session_manager/connector.py:249] · 큰 세션 복제 시간·비용 [estimated — re-measure before gate] session_manager/connector.py:249
REALITY: deferred-asks pass 연결된 이슈 없음. 사장님 요청 중 ① 일 시키기·② 실시간은 허브 프로젝트로(docs/spec-pclink-hub-handoff.md) docs/spec-pclink-hub-handoff.md:1
REALITY: pr-count pass ship-policy 없음 → 한 물결에 한 묶음(이 저장소는 작업 트리 → main 합침). Phase 1 을 단계별로 나눠 합치면 합칠 때마다 Windows·리눅스 전체 시험 비용(약 15분) docs/designs/cross-pc-messaging.md:4.6

## Decision ledger

### 범위 기록(범위 점검 B)
feature answers: 제안한 기능 삭감 없음(크로스체크로 이미 최소 범위 — 2026-10-22 사장님 범위 답 「조회 전용 묻기까지」); structure: B Smaller arrangement (D3, 2026-10-22 사장님 답); accepted scope: Phase 1 의 1-0~1-5 그대로, 코드 배치는 `session_manager/pclink/` 새 모듈 4개 — `protocol.py`(봉투·봉인·중복 장부) / `link.py`(B 방에 client 로 붙는 짝 연결) / `policy.py`(짝·허용·첫 연락 승인·한도·끄기·감사·짝짓기 상태) / `routes.py`(로컬 API·B 쪽 처리), 묻기 실행은 `webapi.ephemeral_ask` 를 넓혀 재사용(도구·MCP·훅 끄기·기록 없음 옵션); pending remedies: S1, S2, S3, S4

### 범위 점검 C — 발견
- S1 [P1] (confidence: 9/10) session_manager/connector.py:794-795 `if t == "req": await self._handle_req(frame)` · :444-446 `r = await self.http.post(f"{self.local_base}/api/v1/sessions/{sid}/ask", json=body)` · :249 `httpx.AsyncClient(timeout=310` — 원격 묻기를 이 길로 받으면 B 의 다른 기기 프레임이 묻는 동안 멈춘다. (Claude)
- S2 [P1] (confidence: 8/10) 설계 4.3·6장 — 짝짓기(짝 키·A 가 B 자격을 받는 길) 미정, 오결합 방지·2026-09-29 묶인 결정(CP 실패=불명→pending, connect() 는 auth 결과) 미반영. (Claude)
- S3 [P2] (confidence: 7/10) 설계 6장 — 짝 방향(양쪽/방향마다) 미정. (Claude)
- S4 [P2] (confidence: 7/10) 설계 6장 — 짝 연결 유지(물을 때만/계속) 미정. (Claude)
- S5 [P2] (confidence: 9/10) session_manager/connector.py:776-784 — PC 짝에 방 키를 주지 않고, pclink 요청은 안쪽 짝 키 봉인일 때만 받는다(require 와 무관). 승인된 1-1·1-2 의 필수 구현(2026-10-22 범위 답) — 질문 없음.
- S6 [P2] (confidence: 9/10) session_manager/webapi.py:93-111,328-331 — 실험 4 인자(`--tools ""`·`--strict-mcp-config`·`--settings {"disableAllHooks":true}`·`--no-session-persistence`)로 복제본 실행, 복제본 파일이 안 생겨 지우기 불필요. 승인된 1-3 + D3 의 필수 구현 — 질문 없음.
- S7 [P3] (confidence: 5/10) 전제 P11 — 맥의 보낸 세션 확인 경로 미확인 → 구현 때 맥 측 실험(`docs/mac-dev-loop.md`). 필수 확인 — 질문 없음.
- S8 [P3] (confidence: 9/10) TODOS.md:66 — 맥은 출시 대상이 아님 → 이 PC ↔ 맥 짝은 맥 정식 출시 뒤. NOT in scope 에 기록.

### R1: 원격 묻기를 받는 B 커넥터의 처리 방식 (S1)
Finding: S1, P1, confidence 9/10, session_manager/connector.py:794-795·:444-446·:249, Claude
Plan baseline: 설계 4.4 흐름도는 B 가 묻기를 받아 복제본에 묻고 답을 돌려준다고만 함 — 커넥터 처리 방식은 정하지 않음(원래 제안 없음)
Runtime evidence: 프레임은 하나씩 처리(connector.py:794-795), 폰 `ask` 는 인라인 대기(:444-446, 한도 310초 :249). 동시 처리는 E2EE 응답 평문 유출·저장소 경합으로 되돌려짐(fadb3b4). 복제본 묻기 7.3초(작은 세션, 실험 4), 큰 세션은 미측정
Comparison grid:
| 선택 | 지금 | A 별도 작업 | B 인라인 | C 인라인+짧은 한도 |
|---|---|---|---|---|
| R1 처리 방식 | 미정(대기) | 접수 즉시 답, 묻기는 스레드, 결과는 그 짝 기기(cid)에 따로 | 폰 ask 처럼 인라인 대기 | 인라인 대기, 한 건 30초 한도 |
| R1 B 프레임 막힘 상한 | 미정 | 0초(루프 안 막음) | 최대 310초 | 최대 30초 |
| R1 큰 세션 묻기 | 미정 | 한도까지 가능 | 가능 | 30초 넘으면 실패 |
| S2 짝짓기 방식 | 대기 | 대기 | 대기 | 대기 |
| S3 짝 방향 | 대기 | 대기 | 대기 | 대기 |
| S4 연결 유지 | 대기 | 대기 | 대기 | 대기 |
Question D4:
D4 — 다른 PC 가 보낸 원격 묻기를 B 의 커넥터가 어떻게 처리할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 범위 점검 S1.
ELI10: B 의 커넥터는 폰·다른 PC 가 보낸 요청을 한 줄로 세워 하나씩 처리합니다. 원격 묻기는 복제본이 답할 때까지 수 초에서 수십 초가 걸리는데, 그동안 줄 뒤의 요청(폰 터미널 입력 포함)이 모두 멈춥니다. 예전에 이것을 통째로 동시 처리로 바꿨다가 암호 응답이 평문으로 나가는 사고 위험 때문에 되돌린 적이 있습니다.
Stakes if we pick wrong: 인라인이면 누가 원격으로 물을 때마다 사장님 폰의 B 터미널이 멈추고, 넓은 동시 처리면 되돌린 사고가 다시 납니다.
Recommendation: A 별도 작업 because 원격 묻기 하나만 좁게 떼어 접수 즉시 답하므로 줄이 안 막히고, 답은 짝 키로 봉인해 보내 되돌린 평문 유출 길과 무관합니다.
Completeness: A=10/10, B=5/10, C=6/10
Pros / cons:
A) 별도 작업 (recommended)
  ✅ 커넥터는 묻기를 접수만 하고 바로 다음 프레임으로 가 폰 터미널이 멈추지 않습니다
  ✅ 결과는 짝 키로 안쪽을 봉인해 그 짝 기기에만 보내므로 방 키 봉투 처리와 섞이지 않습니다
  ❌ 동시성이 생기는 한 곳(결과 보내기·장부 쓰기)에 잠금과 시험이 필요합니다(human ~1일 / CC ~40분)
B) 인라인
  ✅ 지금 폰 ask 와 같은 길이라 새 동시성이 전혀 없습니다(human ~2시간 / CC ~10분)
  ❌ 묻기 동안 B 의 모든 기기 프레임이 최대 310초까지 멈춥니다
C) 인라인 + 짧은 한도
  ✅ 막힘을 최대 30초로 묶고 구현은 인라인만큼 단순합니다(human ~3시간 / CC ~15분)
  ❌ 큰 세션 묻기는 30초 안에 못 끝나 실패하고, 30초 막힘은 남습니다
Net: 작은 동시성 한 곳을 지고 막힘을 없앨지, 단순함을 위해 B 의 다른 요청이 멈추는 것을 받아들일지입니다.
Header: D4 처리 방식
Options:
A) 별도 작업 (recommended)
접수 즉시 답하고 묻기는 스레드에서, 결과는 짝 키로 봉인해 그 짝 기기에만 따로 보냄. B 프레임 막힘 0초, 큰 세션도 한도까지. 동시성은 결과 보내기·장부 쓰기 한 곳(잠금·시험). human ~1일 / CC ~40분.
B) 인라인
폰 ask 와 같이 그 자리에서 기다림. 새 동시성 없음. 묻는 동안 B 의 모든 기기 프레임이 최대 310초 멈춤. human ~2시간 / CC ~10분.
C) 인라인 + 짧은 한도
인라인으로 기다리되 한 건 30초 한도. 막힘 최대 30초, 큰 세션 묻기는 실패. human ~3시간 / CC ~15분.

State: approved
Actual answer: A) 별도 작업 (D4, 2026-10-22 사장님 답)
Accepted scope: B 커넥터는 pclink 묻기를 접수 즉시 답하고(프레임 루프 막힘 0초) 묻기는 스레드에서 실행, 결과는 짝 키로 안쪽을 봉인해 그 짝 기기(cid)에만 따로 보낸다. 큰 세션도 묻기 한도까지 허용. 동시성은 결과 보내기·장부 쓰기 한 곳 — 잠금과 그 시험 포함. S2·S3·S4 는 그대로 대기
History: (없음)

### R2: PC 짝짓기 방식 — 짝 키와 B 자격을 A 에 넘기는 길 (S2)
Finding: S2, P1, confidence 8/10, 설계 4.3·6장, Claude
Plan baseline: 설계 4.3 「폰 페어링과 같은 틀(B 화면 코드 → A 입력)·양쪽 폰 승인·짝 키는 짝짓기 때 따로」 — 키를 만드는 법·넘기는 길은 6장에서 미정
Runtime evidence: 폰 페어링은 링크 조각(#)에 방 키·자격을 싣는다(server.py:695-725, Explore). 사장님 폰은 이 PC·맥 모두와 짝(맥 2회차 R5). 웹 조사: 별도 경로의 짝 값은 두 기기 신원과 묶여야 함(오결합), 짧은 코드는 PAKE 필요. 미룬 TODO X-1 6자리 코드 페어링(TODOS.md:281, CP 중개). 묶인 결정 2026-09-29: CP 호출 실패=불명→pending, connect() 는 auth 결과를 돌려줌
Comparison grid:
| 선택 | 지금 | A 폰이 넘겨주기 | B 초대 문자열 복사 | C 6자리 코드 + PAKE |
|---|---|---|---|---|
| R2 짝 값이 지나는 길 | 미정 | 폰의 E2EE 통로(B→폰→A) | 사장님이 고른 아무 통로(메신저·메일 등) | 릴레이(PAKE 로 보호) |
| R2 사람 입력 | 미정 | 없음(폰에서 두 번 승인) | 긴 문자열 붙여넣기 | 6자리 입력 |
| R2 새 의존성 | 없음 | 없음 | 없음 | PAKE 라이브러리(spake2 등) |
| R2 짝 값을 두 PC 신원(host_id)에 묶기 | 미정 | 묶음 | 묶음 | 묶음(PAKE 대화에 포함) |
| R2 승인 | 양쪽 폰 승인(설계 4.3) | 폰 승인 두 번(B 초대·A 받기) | 양쪽 폰 승인 | 양쪽 폰 승인 |
| 공통: CP 자격 발급 실패=불명→pending 재시도, A 첫 접속 성공은 auth 결과로 판정 | 미반영 | 포함 | 포함 | 포함 |
| R1 처리 방식 | 승인(A 별도 작업, D4) | 같음 | 같음 | 같음 |
| S3 짝 방향 | 대기 | 대기 | 대기 | 대기 |
| S4 연결 유지 | 대기 | 대기 | 대기 | 대기 |
Question D5:
D5 — PC 를 짝지을 때 짝 키와 B 의 접속 자격을 A 에 어떻게 넘길까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 범위 점검 S2.
ELI10: 두 PC 가 서로를 믿으려면 둘만 아는 비밀(짝 키)과 A 가 B 의 방에 들어갈 출입증이 필요합니다. 이 둘을 어떤 길로 A 에 건네느냐가 보안의 핵심입니다. 사장님 폰은 이미 두 PC 와 각각 암호 통로로 이어져 있어서, 폰이 B 에게 받아 A 에게 건네면 사람이 비밀을 손으로 옮길 일이 없습니다.
Stakes if we pick wrong: 비밀이 메신저 같은 곳에 남거나 다른 기기와 잘못 짝지어지면, 그 PC 가 B 의 세션에 물을 수 있게 됩니다.
Recommendation: A 폰이 넘겨주기 because 이미 믿는 두 통로만 쓰고 사람이 비밀을 옮기지 않으며 새 암호 라이브러리도 필요 없습니다.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 폰이 넘겨주기 (recommended)
  ✅ 짝 키는 B→폰→A 의 기존 E2EE 통로로만 가고 사람이 문자열을 옮기지 않아 새어 나갈 곳이 없습니다
  ✅ 폰에서 「B 초대 승인」「A 받기 승인」 두 번 누르면 끝나 양쪽 승인이 자연스럽습니다(새 의존성 없음)
  ❌ PWA 에 두 PC 를 오가는 짝짓기 흐름을 새로 만들어야 합니다(작업 트리에서, human ~2일 / CC ~1.5시간)
B) 초대 문자열 복사
  ✅ PWA 흐름 없이 Host 화면만으로 되어 개발이 가장 적습니다(human ~1일 / CC ~45분)
  ❌ 짝 키가 든 문자열을 사장님이 옮기는 통로(메신저·메일·클립보드 동기화)에 남을 수 있습니다
C) 6자리 코드 + PAKE
  ✅ 6자리만 입력하면 되고 릴레이가 엿봐도 코드를 알아낼 수 없습니다(SPAKE2, RFC 9382)
  ❌ 새 암호 의존성과 프로토콜 구현·시험이 커서 이번 범위에 무겁습니다(human ~4일 / CC ~3시간)
Net: PWA 흐름 개발을 지고 비밀을 사람 손에서 떼어 낼지, 개발을 줄이고 옮기는 통로의 위험을 질지, 새 암호 구현을 질지입니다.
Header: D5 짝짓기
Options:
A) 폰이 넘겨주기 (recommended)
폰이 B 화면의 「PC 짝짓기」에서 초대(짝 키·B 자격·두 PC 신원)를 받아 A 로 넘김. 폰 E2EE 통로만, 사람 입력 없음, 폰 승인 두 번. CP 실패=불명→pending, 성공은 auth 결과로. PWA 흐름 새로(작업 트리). human ~2일 / CC ~1.5시간.
B) 초대 문자열 복사
B 화면의 긴 초대 문자열(짝 키 포함)을 A 화면에 붙여 넣음. 양쪽 폰 승인. 개발 최소, 옮기는 통로에 비밀이 남을 위험. human ~1일 / CC ~45분.
C) 6자리 코드 + PAKE
B 가 6자리 코드를 보이고 A 에 입력, 릴레이를 거친 PAKE 로 짝 키 합의. 양쪽 폰 승인. 새 암호 의존성·구현 큼. human ~4일 / CC ~3시간.

State: approved
Actual answer: B) 초대 문자열 복사 (D5, 2026-10-22 사장님 답 — 권장안 A 대신 선택)
Accepted scope: B 화면이 긴 초대 문자열(짝 키·B 자격·두 PC 신원을 담음)을 보여 주고, 사장님이 그것을 A 화면에 붙여 넣는다. 짝 값은 두 PC 신원(host_id)에 묶는다. 양쪽 폰 승인. PWA 의 두 PC 오가는 흐름은 만들지 않는다. 공통: CP 자격 발급 실패=불명→pending 재시도, A 첫 접속 성공은 auth 결과로 판정. 감수한 위험: 사장님이 옮기는 통로(메신저·메일·클립보드 동기화)에 문자열이 남을 수 있음. R1 은 승인값 그대로, S3·S4 는 대기
History: (없음)

### R3: 짝 방향 — 한 번의 짝짓기로 양쪽이 서로 물을 수 있게 할지 (S3)
Finding: S3, P2, confidence 7/10, 설계 6장 「짝 하나에 키 하나 vs 방향마다 키」, Claude
Plan baseline: 설계 4.3·1-2 는 「B 가 A 를 PC 기기로 페어링」 한 방향만 그림. 양방향은 미정
Runtime evidence: 쓰임새 ②(운영 세션끼리 연락)는 양쪽에서 묻는다(이 PC 서비스 모니터 ↔ 노트북 운영 세션). D5 로 짝짓기는 초대 문자열 복사(B→A). 받는 세션은 어느 쪽이든 기본 거부(세션마다 허용)
Comparison grid:
| 선택 | 지금 | A 한 번에 양쪽 | B 방향마다 따로 |
|---|---|---|---|
| R3 짝 방향 | 미정(한 방향만 그림) | B→A 복사 한 번으로 A→B·B→A 둘 다 | 복사 한 번에 한 방향(A 가 B 에 묻기만) |
| R3 반대 방향을 여는 법 | — | A 가 짝 통로(짝 키 봉인)로 자기 방 자격을 B 에 넘김 | 반대로 한 번 더 복사(A→B) |
| R3 CP 자격 발급 | — | 2개(양쪽 방) | 짝짓기마다 1개 |
| R3 받는 세션 허용 | 세션마다 기본 거부 | 같음(양쪽 다 기본 거부) | 같음 |
| R1 처리 방식 | 승인(D4) | 같음 | 같음 |
| R2 짝짓기 방식 | 승인(D5 초대 문자열 복사) | 같음 | 같음 |
| S4 연결 유지 | 대기 | 대기 | 대기 |
Question D6:
D6 — PC 를 한 번 짝지으면 양쪽이 서로 물을 수 있게 할까요, 방향마다 따로 짝지을까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 범위 점검 S3.
ELI10: 초대 문자열을 B 에서 A 로 한 번 옮기면 A 가 B 에게 물을 수 있습니다. B 도 A 에게 물으려면 반대 방향 출입증이 하나 더 필요합니다. A 는 짝이 맺어진 순간 이미 B 와 둘만의 암호 통로가 있으니, 그 통로로 자기 출입증을 B 에게 건네면 복사 한 번으로 양쪽이 열립니다. 어느 쪽이든 실제로 답할 세션은 각 PC 에서 따로 골라야 열립니다.
Stakes if we pick wrong: 한 방향만이면 운영 세션끼리 서로 묻기(쓰임새 ②)에 복사를 두 번 해야 하고, 양쪽이면 필요 없는 방향의 출입증도 하나 생깁니다.
Recommendation: A 한 번에 양쪽 because 쓰임새 ②가 양쪽 묻기이고, 받는 세션은 어차피 각자 기본 거부라 출입증만으로는 열리는 것이 없습니다.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 한 번에 양쪽 (recommended)
  ✅ 문자열 복사 한 번으로 두 PC 가 서로 물을 수 있어 운영 세션끼리 연락이 바로 됩니다
  ✅ 반대 방향 출입증은 이미 맺어진 짝 통로(짝 키 봉인)로 건너가 사람 손을 다시 타지 않습니다
  ❌ 쓰지 않을 방향의 출입증도 하나 생기고, CP 자격 발급·폐기가 두 개라 짝짓기 코드가 조금 늘어납니다(human +0.5일 / CC +20분)
B) 방향마다 따로
  ✅ 필요한 방향만 열어 출입증이 가장 적고 짝짓기 코드가 단순합니다
  ✅ 짝 하나 = 출입증 하나라 해제·폐기도 단순합니다
  ❌ 양쪽 묻기가 필요하면 반대로 복사를 한 번 더 해야 해 짝 키 문자열이 사람 손을 두 번 탑니다
Net: 복사 한 번에 양쪽을 열지, 필요한 방향만 열고 양쪽이 필요할 때 복사를 두 번 할지입니다.
Header: D6 짝 방향
Options:
A) 한 번에 양쪽 (recommended)
B→A 초대 문자열 복사 한 번으로 양쪽이 서로 물을 수 있음. 반대 방향 자격은 짝 키로 봉인한 짝 통로로 A→B 전달. CP 자격 2개. 받는 세션은 양쪽 다 기본 거부. human +0.5일 / CC +20분.
B) 방향마다 따로
복사 한 번 = 한 방향(A 가 B 에 묻기). 반대 방향이 필요하면 A→B 로 한 번 더 복사. CP 자격 짝짓기마다 1개. 짝짓기·해제 코드 가장 단순.

State: approved
Actual answer: A) 한 번에 양쪽 (D6, 2026-10-22 사장님 답)
Accepted scope: B→A 초대 문자열 복사 한 번으로 양쪽이 서로 물을 수 있다. 반대 방향 자격(A 방의 자격)은 짝이 맺어진 뒤 짝 키로 봉인한 짝 통로로 A→B 에 넘긴다. CP 자격 2개(양쪽 방) 발급·폐기, 짝 해제는 둘 다 폐기. 받는 세션은 양쪽 다 기본 거부. R1·R2 승인값 그대로, S4 대기
History: (없음)

### R4: 짝 연결 유지 — A 가 B 방에 물을 때만 붙을지, 계속 붙어 있을지 (S4)
Finding: S4, P2, confidence 7/10, 설계 6장 「짝 채널: 계속 유지 vs 물을 때만」, Claude
Plan baseline: 설계 4.3 「A 의 Host 가 B 의 방에 client 로 붙는다」 — 언제 붙고 끊는지는 미정
Runtime evidence: 릴레이는 cid 를 모르는 agent 프레임을 방 전체에 뿌린다(007 relay/relay.py:499-505, Explore). client JWT 수명 12시간(007 control_plane/tokens.py:114-133, Explore). R1 로 B 는 묻기를 접수 즉시 답하고 결과를 나중에 그 cid 로 보낸다 → A 는 결과가 올 때까지 연결을 쥐어야 한다
Comparison grid:
| 선택 | 지금 | A 물을 때만 | B 계속 붙어 있기 |
|---|---|---|---|
| R4 연결 시점 | 미정 | 묻기 때 붙고, 답을 받으면 끊음(같은 짝의 다음 묻기가 60초 안이면 다시 씀) | 짝이 맺어지면 늘 붙어 있고 끊기면 다시 붙음 |
| R4 첫 묻기 지연 | 미정 | CP 토큰·접속 약 1~3초 더 [estimated] | 없음 |
| R4 B 방에 A 가 떠 있는 시간 | 미정 | 묻는 동안만 | 늘 |
| R4 재접속·끊김 처리 코드 | 미정 | 없음(묻기마다 새로) | 재접속 루프·토큰 갱신·끊김 감지 |
| R4 상대 꺼짐을 아는 때 | 미정 | 물을 때(접속 실패 → 즉시 「상대 PC 꺼짐」) | 바로 |
| R1·R2·R3 | 승인(D4·D5·D6) | 같음 | 같음 |
Question D7:
D7 — A 가 B 의 방에 물을 때만 붙을까요, 늘 붙어 있을까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 범위 점검 S4.
ELI10: A 가 B 에게 물으려면 B 의 방(릴레이)에 손님으로 들어가야 합니다. 물을 때만 들어갔다가 답을 받으면 나오면 단순하고, B 의 방에 낯선 손님이 늘 앉아 있지 않아 안전합니다. 대신 물을 때마다 문을 여는 1~3초가 더 걸립니다. 늘 앉아 있으면 바로 묻지만, 끊기면 다시 들어오는 장치를 만들어야 하고 B 의 방에 오가는 다른 소식(방 전체에 뿌려지는 것)을 볼 시간이 길어집니다.
Stakes if we pick wrong: 늘 붙어 있기를 고르면 재접속 버그와 노출 시간이 늘고, 물을 때만을 고르면 묻기마다 몇 초씩 느립니다.
Recommendation: A 물을 때만 because 묻기는 수 초~수십 초 걸리는 일이라 1~3초 더하는 비용이 작고, 재접속 장치가 필요 없으며 B 방 노출이 묻는 동안으로 줄어듭니다.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 물을 때만 (recommended)
  ✅ 묻기마다 새로 붙고 끝나면 끊어 재접속·토큰 갱신·끊김 감지 코드가 필요 없습니다
  ✅ A 가 B 의 방에 머무는 시간이 묻는 동안뿐이라 방 전체에 뿌려지는 프레임을 볼 기회가 줄어듭니다
  ❌ 묻기마다 CP 토큰 받기와 접속으로 약 1~3초가 더 걸립니다(60초 안의 연달은 묻기는 연결 재사용)
B) 계속 붙어 있기
  ✅ 묻기가 바로 나가고 상대 PC 가 꺼진 것을 즉시 압니다
  ✅ 나중에 허브의 실시간 주고받기로 넓히기 쉬운 모양입니다
  ❌ 재접속 루프·12시간 토큰 갱신·끊김 감지가 필요하고, A 가 B 방에 늘 떠 있어 노출이 깁니다(human +1일 / CC +40분)
Net: 묻기마다 몇 초를 내고 단순·안전을 얻을지, 즉시성을 위해 늘 붙어 있는 장치를 만들지입니다.
Header: D7 연결 유지
Options:
A) 물을 때만 (recommended)
묻기 때 B 방에 붙고 답을 받으면 끊음(같은 짝 60초 안 연달은 묻기는 재사용). 재접속 코드 없음, 노출은 묻는 동안만. 묻기마다 약 1~3초 더.
B) 계속 붙어 있기
짝이 맺어지면 늘 붙어 있고 끊기면 다시 붙음. 지연 없음·상대 꺼짐 즉시 앎. 재접속·토큰 갱신·끊김 감지 필요, 노출 늘. human +1일 / CC +40분.

State: approved
Actual answer: A) 물을 때만 (D7, 2026-10-22 사장님 답)
Accepted scope: A 는 묻기 때 B 방에 client 로 붙고, 결과(R1 의 나중 답)를 받으면 끊는다. 같은 짝에 60초 안 연달은 묻기는 그 연결을 다시 쓴다. 재접속 루프·토큰 갱신·끊김 감지는 만들지 않는다. 접속 실패면 즉시 「상대 PC 꺼짐」으로 돌려준다. R1·R2·R3 승인값 그대로
History: (없음)

범위 점검 C 처리: S1→R1 승인(D4) · S2→R2 승인(D5) · S3→R3 승인(D6) · S4→R4 승인(D7) · S5·S6·S7 승인된 범위의 필수 구현(질문 없음) · S8 NOT in scope. 범위 점검 결과: scope accepted as-is(D3 은 범위를 줄이지 않은 작은 배치).

### 1절 아키텍처 — 발견
- A1 [P2] (confidence: 8/10) session_manager/approvals.py:29-30 `KINDS = ("team_create", … "session_send", "service_register")` — 첫 연락·짝짓기 폰 승인은 이 대기열에 종류 두 개(`pclink_pair`·`pclink_contact`)를 더해 재사용(폰 카드·사람 증명 그대로) [Layer 1]. 승인된 「폰 승인」의 구현 수단 — 질문 없음.
- A2 [P1] (confidence: 8/10) R2 accepted scope — 초대 문자열의 유효 기간·사용 횟수가 없음. 옮긴 통로에 남은 문자열로 나중에 누구든 짝을 맺을 수 있다. → R5(D8).
- A3 [P2] (confidence: 8/10) session_manager/connector.py:124-130 `_is_local_only_api` — 새 pclink API 를 덴리스트에 넣어야 함(지난 교훈 clewpath-connector-proxy-bypasses-local-only). 폰에서 허용할 범위는 정책 선택 → R6(D9).
- A4 [P2] (confidence: 7/10) 전제 P11 — 보낸 세션 확인: Windows `psutil.net_connections`, 리눅스 /proc, 맥은 권한 오류라 내 사용자 프로세스별 연결·lsof(맥 출시 뒤 맥 측 실험). 1-3 의 필수 구현 — 질문 없음.
- A5 [P2] (confidence: 7/10) session_manager/webapi.py:328 `argv = [exe, "-p", prompt, …]` — 프롬프트가 명령줄 인자라 Windows 한도(약 32K자) 아래로. 기본 한도(설정 파일 `[pclink]` 로 조정): 묻기 8,000자·답 20,000자·복제본 180초·짝마다 분당 6회/하루 200회·묻기당 비용 상한 `--max-budget-usd 0.5`. 승인된 「한도」의 기본값 — 질문 없음.
- A6 [P2] (confidence: 8/10) 설계 4.4 — 실패 모드별 오류 코드: `peer_offline`(접속 실패)·`peer_old`(hello 버전)·`cp_unavailable`·`relay_unavailable`·`not_allowed`(세션 허용 꺼짐)·`pending_approval`(첫 연락 대기)·`paused`(끄기 스위치)·`limit`·`ask_failed`·`timeout`. A 가 결과 전에 끊기면 B 는 결과를 버리고 장부에 남김(같은 id 재시도는 중복 장부가 막음). 필수 구현 — 질문 없음.

### R5: 초대 문자열의 수명 (A2)
Finding: A2, P1, confidence 8/10, R2 accepted scope(D5), Claude
Plan baseline: R2 approved — 초대 문자열에 짝 키·B 자격·두 PC 신원, 양쪽 폰 승인. 수명·횟수는 정하지 않음
Runtime evidence: 폰 페어링 QR 의 수명은 별도(이번 대상 아님). 초대 문자열은 사장님이 메신저·메일·클립보드 동기화로 옮길 수 있음(D5 감수 위험)
Comparison grid:
| 선택 | 지금 | A 1회용·10분 | B 1회용·24시간 | C 제한 없음 |
|---|---|---|---|---|
| R5 사용 횟수 | 미정 | 1회(쓰면 무효) | 1회 | 해제 전까지 여러 번 |
| R5 유효 기간 | 미정 | 만든 뒤 10분 | 24시간 | 없음 |
| R5 남은 문자열을 나중에 주운 사람 | 미정 | 10분 지나면 못 씀, 이미 쓴 것도 못 씀 | 24시간 안·안 쓴 것만 | 언제든 짝 맺기 시도 가능(폰 승인이 마지막 방어) |
| R5 다시 만들기 | — | B 화면에서 새로(옛 것 무효) | 같음 | 필요 없음 |
| R1~R4 | 승인(D4~D7) | 같음 | 같음 | 같음 |
Question D8:
D8 — PC 짝짓기 초대 문자열을 얼마나 오래, 몇 번 쓸 수 있게 할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 1절 아키텍처 A2.
ELI10: 초대 문자열에는 짝 키가 들어 있어서, 사장님이 옮기면서 메신저나 메일에 남은 사본을 나중에 누가 주우면 짝을 맺으려 할 수 있습니다. 한 번 쓰면 무효가 되고 짧은 시간만 유효하면, 남은 사본은 쓸모가 없어집니다. 마지막엔 폰 승인도 있지만, 문자열 자체가 빨리 죽는 것이 첫째 방어입니다.
Stakes if we pick wrong: 제한이 없으면 오래전 메신저에 남은 문자열 하나로 언제든 짝 맺기 시도가 들어오고, 너무 짧으면 옮기다 시간이 지나 다시 만들어야 합니다.
Recommendation: A 1회용·10분 because 문자열을 옮기는 데는 몇 분이면 충분하고, 남은 사본이 10분 뒤·한 번 쓴 뒤엔 무효가 되어 감수한 위험(D5)을 거의 없앱니다.
Completeness: A=10/10, B=8/10, C=4/10
Pros / cons:
A) 1회용·10분 (recommended)
  ✅ 한 번 쓰면 무효라 메신저에 남은 사본을 나중에 주워도 짝을 맺을 수 없습니다
  ✅ 10분이 지나면 안 쓴 사본도 무효라 D5 에서 감수한 「옮기는 통로에 남는 위험」이 거의 사라집니다
  ❌ 옮기다 10분을 넘기면 B 화면에서 새로 만들어야 합니다(만들기는 버튼 한 번)
B) 1회용·24시간
  ✅ 하루 안에만 옮기면 되어 서두를 필요가 없습니다
  ❌ 안 쓴 사본이 하루 동안 살아 있어 그 사이 남의 손에 들어가면 쓸 수 있습니다
C) 제한 없음
  ✅ 한 번 만든 문자열로 다시 짝지을 때도 그대로 쓸 수 있습니다
  ❌ 오래된 사본도 영원히 살아 있어 폰 승인 하나만 남은 방어가 됩니다
Net: 다시 만드는 약간의 번거로움으로, 남은 사본을 쓸모없게 만들지입니다.
Header: D8 초대 수명
Options:
A) 1회용·10분 (recommended)
한 번 쓰면 무효, 만든 뒤 10분이 지나면 무효. 다시 필요하면 B 화면에서 새로 만듦(옛 것 무효).
B) 1회용·24시간
한 번 쓰면 무효, 24시간 동안 유효. 안 쓴 사본이 하루 동안 살아 있음.
C) 제한 없음
짝 해제 전까지 같은 문자열로 여러 번 짝 맺기 시도 가능. 폰 승인이 유일한 마지막 방어.

State: approved
Actual answer: B) 1회용·24시간 (D8, 2026-10-22 사장님 답 — 권장안 A 대신 선택)
Accepted scope: 초대 문자열은 한 번 쓰면 무효, 만든 뒤 24시간 동안 유효. 다시 필요하면 B 화면에서 새로 만든다(옛 것 무효). 감수한 위험: 안 쓴 사본이 하루 동안 살아 있음(폰 승인이 다음 방어). R1~R4 승인값 그대로
History: (없음)

### R6: 폰(릴레이 경유)에서 허용할 pclink 동작 (A3)
Finding: A3, P2, confidence 8/10, session_manager/connector.py:124-130 `_is_local_only_api`, Claude
Plan baseline: 승인 범위 1-2 「원격 통신 끄기 스위치(설정 + 폰)」, R2 「B 화면이 초대 문자열을 보여 주고 A 화면에 붙여 넣는다」. 그 밖의 동작을 폰에서 허용할지는 미정
Runtime evidence: ClewPath 화면은 PC 로컬과 폰(릴레이)에서 같은 화면이다. 릴레이 경유 특권은 2FA 를 켜 두었으면 OTP(connector.py:497-500, 교훈 clewpath-2fa-conditional-docs). 노트북은 사장님이 원격(ssh·폰)으로만 다룬다
Comparison grid:
| 선택 | 지금 | A 폰에서도(특권) | B PC 화면에서만 |
|---|---|---|---|
| R6 묻기 보내기 API(세션이 부름) | 미정 | 로컬 전용 | 로컬 전용 |
| R6 초대 만들기·초대 받기 | 미정 | PC 화면 + 폰(릴레이 경유는 2FA 특권) | PC 화면에서만 |
| R6 세션별 「원격 묻기 받기」 켜기 | 미정 | PC 화면 + 폰(2FA 특권) | PC 화면에서만 |
| R6 짝 해제·세션 허용 끄기 | 미정 | PC 화면 + 폰 | PC 화면 + 폰 |
| R6 끄기 스위치 | 승인(설정 + 폰) | 같음 | 같음 |
| 공통: 새 pclink API 는 덴리스트·특권 목록에 등록, 서버도 X-ClewPath-Via 로 다시 거부(2중) | — | 포함 | 포함 |
| R1~R5 | 승인(D4~D8) | 같음 | 같음 |
Question D9:
D9 — 짝짓기와 세션 허용을 폰에서도 할 수 있게 할까요, PC 화면에서만 할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 1절 아키텍처 A3.
ELI10: ClewPath 화면은 PC 앞에서도, 폰에서도 똑같이 열립니다. 짝짓기(초대 만들기·받기)와 「이 세션은 원격 묻기를 받는다」 켜기를 폰에서도 하게 하면, 노트북처럼 사장님이 직접 앞에 앉지 않는 PC 도 폰으로 짝지을 수 있습니다. 대신 폰이 뚫리면 그 길로도 열 수 있으니, 폰에서 할 때는 2차 인증(켜 두셨다면 OTP)을 요구합니다. 끄는 쪽(해제·끄기 스위치)은 어느 쪽이든 폰에서 됩니다.
Stakes if we pick wrong: PC 화면에서만이면 노트북을 짝지으려고 원격 데스크톱을 열어야 하고, 폰에서도면 폰이 뚫렸을 때 여는 길이 하나 늘어납니다.
Recommendation: A 폰에서도(특권) because 노트북을 사장님이 원격으로만 다루셔서 PC 화면 전용이면 짝짓기가 사실상 어렵고, 폰 경유는 2차 인증·폰 승인 카드가 함께 막습니다.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) 폰에서도(특권) (recommended)
  ✅ 노트북처럼 앞에 앉지 않는 PC 도 폰에서 초대를 만들고 다른 PC 화면에 붙여 넣어 짝지을 수 있습니다
  ✅ 폰 경유는 2차 인증(켜 두셨다면 OTP)과 폰 승인 카드가 함께 있어야 열립니다
  ❌ 폰이 뚫리고 2차 인증이 꺼져 있으면 그 폰으로 짝짓기·세션 허용을 열 수 있는 길이 하나 늡니다
B) PC 화면에서만
  ✅ 짝짓기·세션 허용은 그 PC 앞(또는 원격 데스크톱)에서만 되어 여는 길이 가장 좁습니다
  ✅ 폰 경유 특권 처리 코드가 덜 듭니다(human -0.5일 / CC -15분)
  ❌ 노트북을 짝지으려면 원격 데스크톱 등으로 노트북 화면을 열어야 합니다
Net: 원격 PC 를 폰으로 짝짓는 편의와, 여는 길을 PC 앞으로만 좁히는 안전 사이의 선택입니다.
Header: D9 폰 허용
Options:
A) 폰에서도(특권) (recommended)
초대 만들기·받기·세션 허용 켜기를 PC 화면과 폰에서. 폰(릴레이 경유)은 2FA 특권(켜 두었으면 OTP). 해제·끄기는 폰 가능. 묻기 보내기 API 는 로컬 전용.
B) PC 화면에서만
초대 만들기·받기·세션 허용 켜기는 PC 화면에서만. 폰은 해제·세션 허용 끄기·끄기 스위치만. 묻기 보내기 API 는 로컬 전용.

State: approved
Actual answer: A) 폰에서도(특권) (D9, 2026-10-22 사장님 답)
Accepted scope: 초대 만들기·초대 받기·세션별 「원격 묻기 받기」 켜기는 PC 화면과 폰에서 된다. 폰(릴레이 경유)은 2FA 특권 경로(2FA 를 켜 두었으면 OTP)이고 폰 승인 카드도 함께. 짝 해제·세션 허용 끄기·끄기 스위치는 폰 가능. 묻기 보내기 API(세션이 부름)는 로컬 전용. 새 pclink API 는 커넥터 덴리스트·특권 목록에 등록하고 서버도 X-ClewPath-Via 로 다시 거부(2중). R1~R5 승인값 그대로
History: (없음)

1절 처리: A1·A4·A5·A6 승인된 범위의 필수 구현(질문 없음) · A2→R5 승인(D8) · A3→R6 승인(D9).

### 2절 코드 품질 — 발견(질문 없음, 모두 승인된 범위의 필수 구현)
- Q1 [P3] (confidence: 9/10) session_manager/webapi.py:366-367 `if proc.poll() is None:` / `proc.kill(); proc.wait(timeout=3)` — `proc` 가 정의되지 않은 죽은 코드(NameError 가 except 에 삼켜짐). D3 로 넓혀 쓰는 함수라 같은 수정에서 정리(동작 변화 없음).
- Q2 [P2] (confidence: 8/10) session_manager/webapi.py:370 `d = lifecycle.delete_fork(fork_id, origin=session_id)` — 기록 없는 복제본은 지울 파일이 없으니 그 경로에서 건너뜀.
- Q3 [P2] (confidence: 8/10) 설계 4.4 — fail-closed: 작업 예외는 `ask_failed`, 중복 장부 쓰기 실패는 거부, 정책 파일 깨지면 짝 없음(거부)+경고.
- Q4 [P3] (confidence: 7/10) session_manager/connector.py:380-479 — 커넥터 변경은 PC 등급 검사 한 곳 + `pclink.*` 분기 한 줄, 본체는 `pclink/routes.py`.
- Q5 [P2] (confidence: 7/10) session_manager/e2ee.py:58-86(방 키 DPAPI 보관) — 짝 키·상대 자격도 같은 보호(Windows DPAPI `osdep.protect`, POSIX 0600) [Layer 1].
- Q6 [P3] (confidence: 8/10) 설계 4.4 흐름 그림 — D4·D5·D7 미반영(낡음) → 보고서 「그림」에서 새로.

### R7: 커넥터 회귀 보호 계약 (3절 시험 — REGRESSION RULE)
Finding: 3절, P1, confidence 9/10, session_manager/connector.py(R1 별도 작업·Q4 PC 등급 검사·pclink 분기) — 기존 폰 동작을 바꿀 위험, Claude
Plan baseline: 승인된 R1(별도 작업)·R6(덴리스트)·S5(PC 등급) — 회귀 계약은 정한 적 없음
Runtime evidence: 커넥터 기존 시험 39건(tests/test_connector_*.py 8개 파일)·E2EE 10건(tests/test_e2ee.py). 그중 tests/test_connector_multidevice.py:260 `test_late_answer_to_departed_device_is_dropped_not_broadcast_in_plaintext` 는 되돌린 동시 처리 사고(fadb3b4)의 재현 시험. 실 CP e2e 시험 틀이 있음(tests/e2e, `-m e2e`, a71eb60)
Comparison grid:
| 선택 | 지금 | A 소켓 수준 회귀 | B A + 두 Host 실 e2e |
|---|---|---|---|
| R7 보존할 동작 | 미정 | 기존 커넥터 39건·E2EE 10건 그대로 통과(폰 인증·api 프록시·로컬 전용·특권·스트림·봉투 봉인·떠난 기기 늦은 답 버림) | 같음 |
| R7 의도한 변화 | 미정 | PC 등급 기기는 `pclink.*`·ping 만, 폰 기기는 무변 | 같음 |
| R7 새 회귀 시험 | 미정 | ① pclink 작업 중에도 폰 프레임이 처리됨(막힘 없음) ② 결과 전에 떠난 A 에게 늦은 결과는 버리고 방 전체에 안 뿌림 ③ pclink 작업과 동시에 폰의 봉투 요청 답이 봉인된 채 나감 ④ PC 등급 기기가 api·terminal·monitor·ask·export·import·grant 를 부르면 거부 | 같음 |
| R7 두 Host 실 e2e | 없음 | 없음 | 시험용 CP·릴레이 컨테이너(`*-test`)로 짝짓기→묻기→답→해제→거부를 끝까지 |
| R1~R6 | 승인(D4~D9) | 같음 | 같음 |
Question D10:
D10 — 커넥터를 고치면서 기존 폰 동작이 깨지지 않게 하는 회귀 시험을 어디까지 둘까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 3절 시험(회귀 규칙).
ELI10: 원격 묻기를 받으려면 폰이 쓰는 커넥터를 고쳐야 합니다. 예전에 커넥터를 동시 처리로 바꿨다가 암호 응답이 평문으로 나갈 뻔해서 되돌린 적이 있어, 이번에는 그 사고들을 시험으로 막아 두고 고칩니다. 더 나아가 실제 PC 두 대를 흉내 낸 시험(시험용 서버 컨테이너)으로 짝짓기부터 묻기·해제까지 끝까지 돌려 볼 수도 있습니다.
Stakes if we pick wrong: 회귀 시험이 얕으면 폰 터미널·인증이 조용히 깨진 채 출시되고, 실 e2e 가 없으면 짝짓기 흐름의 연결부 버그가 사장님 PC 에서 처음 드러납니다.
Recommendation: B A + 두 Host 실 e2e because 짝짓기·인증·암호는 단위 시험의 흉내로는 연결부 실패를 놓치기 쉽고, 실 CP e2e 틀이 이미 있어 추가 비용이 작습니다.
Completeness: A=8/10, B=10/10
Pros / cons:
A) 소켓 수준 회귀
  ✅ 기존 49건 그대로 + 새 회귀 4종(막힘 없음·늦은 답 버림·봉인 유지·PC 등급 거부)으로 되돌린 사고를 정확히 막습니다
  ✅ 컨테이너 없이 빠르게 돌아 매번 Windows·리눅스 전체 시험에 들어갑니다(human ~1일 / CC ~40분)
  ❌ 짝짓기 문자열 → 실제 릴레이 접속 → CP 토큰 → 답까지의 연결부는 흉내로만 확인됩니다
B) A + 두 Host 실 e2e (recommended)
  ✅ A 전부에 더해 시험용 CP·릴레이 컨테이너로 두 Host 의 짝짓기·묻기·해제·거부를 실제로 끝까지 돌립니다
  ✅ 기존 실 CP e2e 틀(tests/e2e, `-m e2e`)을 재사용해 출시 전 게이트로 씁니다(human +1.5일 / CC +1시간)
  ❌ 컨테이너가 필요해 평소 전체 시험과 따로(`-m e2e`) 돌리고, 시간이 더 걸립니다
Net: 출시 전 게이트 하나를 더 지고, 짝짓기 연결부를 실제로 확인할지입니다.
Header: D10 회귀 계약
Options:
A) 소켓 수준 회귀
기존 커넥터 39·E2EE 10건 그대로 통과 + 새 회귀 4종(막힘 없음·늦은 답 버림·봉인 유지·PC 등급 거부). PC 등급만 pclink·ping, 폰 무변. human ~1일 / CC ~40분.
B) A + 두 Host 실 e2e (recommended)
A 전부 + 시험용 CP·릴레이 컨테이너(*-test)로 두 Host 짝짓기→묻기→답→해제→거부 끝까지. 기존 tests/e2e 틀, 출시 전 게이트. human +1.5일 / CC +1시간.

State: approved
Actual answer: B) A + 두 Host 실 e2e (D10, 2026-10-22 사장님 답)
Accepted scope: 회귀 계약 — 보존: 기존 커넥터 시험 39건·E2EE 10건이 그대로 통과(폰 인증·api 프록시·로컬 전용·특권·스트림·봉투 봉인·떠난 기기 늦은 답 버림). 의도한 변화: PC 등급 기기는 `pclink.*`·ping 만, 폰 기기는 무변. 새 회귀 4종(① pclink 작업 중 폰 프레임 처리 ② 떠난 A 의 늦은 결과는 버림·방 전체에 안 뿌림 ③ 동시에 폰 봉투 요청 답이 봉인 유지 ④ PC 등급 기기의 api·terminal·monitor·ask·export·import·grant 거부). 더해 시험용 CP·릴레이 컨테이너(*-test)로 두 Host 의 짝짓기→묻기→답→해제→거부를 끝까지 도는 실 e2e(tests/e2e, `-m e2e`)를 출시 전 게이트로. R1~R6 승인값 그대로
History: (없음)

### 3절 시험 — 범위 그림(모두 계획, 새 코드라 GAP)
시험 틀: pytest(`tests/test_*.py`, 기본에서 e2e·posix_e2e 제외 — pyproject.toml:61-65) + node(`tests/pwa/*.test.mjs`, harness.mjs). 시험은 진짜 claude 를 띄우지 않는다(conftest 가드) → 복제본 묻기는 subprocess 흉내로 인자·흐름만, 실제 실행은 실 e2e 에서.
```
CODE PATHS                                               USER FLOWS
[+] pclink/protocol.py                                   [+] 짝짓기
  ├── seal/open(AEAD, 머리=AAD)                             ├── [GAP] [→E2E] B 초대(폰/PC)→폰 승인→복사→A 붙여넣기→폰 승인→양쪽 짝
  │   ├── [GAP] 정상 왕복                                   ├── [GAP] 같은 초대 두 번 → 두 번째 거부
  │   ├── [GAP] 머리(to·kind·만료) 바꾸면 개봉 실패          ├── [GAP] 24시간 지난 초대 거부
  │   └── [GAP] 다른 짝 키면 실패                            └── [GAP] 다른 PC 신원으로 쓴 초대 거부
  ├── 중복 장부                                          [+] 묻기
  │   ├── [GAP] 같은 id 두 번 → 한 번만                      ├── [GAP] [→E2E] A 세션 curl→B 첫 연락 pending→폰 승인→다시 묻기→답
  │   ├── [GAP] 만료 지난 봉투 거부                          ├── [GAP] 세션 허용 꺼짐 → not_allowed
  │   └── [GAP] 재시작 뒤에도 거부(파일 유지)·만료분 정리      ├── [GAP] 끄기 스위치 → paused
  └── [GAP] 모르는 v·kind·크기 초과 거부                     ├── [GAP] 한도 초과 → limit
[+] pclink/link.py (A→B 방 client)                          └── [GAP] 상대 꺼짐 → peer_offline(빠르게)
  ├── [GAP] [→E2E] CP 토큰→릴레이 접속→auth→묻기           [+] 해제
  ├── [GAP] 상대 꺼짐·CP 불통·auth 실패 오류 코드            ├── [GAP] [→E2E] 폰에서 해제 → 진행 중 연결 끊김 → 다음 묻기 거부
  ├── [GAP] 60초 안 연결 재사용                             └── [GAP] CP 폐기 실패 = 불명 → pending → 재시도
  └── [GAP] 결과 기다림 시간 초과                        [+] 화면(PWA, harness)
[+] pclink/policy.py                                        ├── [GAP] 초대 표시·붙여넣기·세션 허용 토글·끄기 스위치
  ├── [GAP] 초대: 1회용·24시간·두 신원 묶음                 └── [GAP] 승인 카드 문구(첫 연락·짝짓기)
  ├── [GAP] 반대 방향 자격을 짝 통로로 전달(D6)
  ├── [GAP] 세션 허용 기본 끔·첫 연락 승인 범위 묶음·넓어지면 다시
  ├── [GAP] 한도(분·일·크기·동시 2건)·끄기 스위치
  ├── [GAP] 해제 → 키·자격 폐기·연결 끊기
  └── [GAP] 감사 장부(본문 없음)·정책 파일 깨지면 거부
[+] pclink/routes.py
  ├── [GAP] 보낸 세션 확인(프로세스→claude 조상→레지스트리), 실패면 거부
  ├── [GAP] 묻기 API 로컬 전용·초대/허용은 릴레이 경유 시 2FA 특권(서버도 Via 로 2중 거부)
  └── [GAP] B: 접수 즉시 답 → 작업 스레드 → 결과를 짝 키로 봉인해 그 cid 로
[+] connector.py
  ├── [★★★ TESTED] 기존 39건(인증·프록시·로컬 전용·특권·스트림·늦은 답 버림) — tests/test_connector_*.py
  ├── [GAP] 회귀 ① pclink 작업 중 폰 프레임 처리
  ├── [GAP] 회귀 ② 떠난 A 의 늦은 결과 버림(방 전체에 안 뿌림)
  ├── [GAP] 회귀 ③ 동시에 폰 봉투 답 봉인 유지
  └── [GAP] 회귀 ④ PC 등급 기기의 api·terminal·monitor·ask·export·import·grant 거부
[+] webapi.ephemeral_ask (넓힘)
  ├── [GAP] 격리 인자(--tools "" · --strict-mcp-config · disableAllHooks · --no-session-persistence)
  └── [GAP] 기록 없는 복제본은 delete_fork 건너뜀(죽은 proc 코드 정리)
[+] devices.py kind=pc · approvals.py 종류 2개
  └── [GAP] PC 기기 등록·폐기, 승인 카드 생성·결정

COVERAGE: 1/45 경로 시험됨(기존 커넥터만)  |  GAPS: 44 (5 [→E2E])  |  LLM eval: 없음(프롬프트 템플릿 변경 없음)
```
- 시험 파일(제안): `tests/test_pclink_protocol.py`·`test_pclink_policy.py`·`test_pclink_routes.py`·`test_pclink_link.py`·`test_connector_pclink.py`(회귀 ①~④)·`tests/pwa/pclink.test.mjs`·`tests/e2e/test_pclink_two_hosts.py`(`-m e2e`, *-test CP·릴레이). `test_pwa_js.py` 의 통과 개수 갱신.
- 없앨 시험: 없음.
- 시험 계획 파일: `~/.gstack/projects/006_session_manager/slpy22-main-eng-review-test-plan-20261022-172641.md`

### 4절 성능 — 발견(질문 없음)
- P1 [P2] (confidence: 6/10, Medium — 측정 필요) 큰 세션 복제 — 원본 대화 전체를 읽으므로 시간·비용이 크다(규모 미측정 [estimated]). 묻기당 `--max-budget-usd 0.5`·180초(A5)로 묶고, 출시 전 이 PC 의 큰 세션 하나로 다시 잰다.
- P2 [P3] (confidence: 7/10) 묻기마다 CP 토큰+접속 약 1~3초 [estimated] — D7 의 60초 재사용으로 연달은 묻기는 없음.
- P3 [P3] (confidence: 7/10) 보낸 세션 확인의 소켓 목록 조회 — 묻기 한 번에 한 번, 수십 ms 수준 [estimated].
- P4 [P3] (confidence: 8/10) 중복 장부 — 만료(분 단위) 지난 줄은 쓸 때마다 정리, id 색인 → 크기 한정.
- P5 [P2] (confidence: 7/10) B 동시 복제본 — claude 프로세스가 동시에 여럿 뜨지 않게 동시 2건(A5 기본값에 더함, 설정 파일로 조정).

### 다른 AI 독립 검토(Codex gpt-6-astra, OUTSIDE_STATUS: completed) — 8건
코드 근거 확인(Claude): ①·⑤·⑥ 인용 줄 맞음 — server.py:1237-1249 `_human_proof` 는 커넥터 증명 헤더 + 활성 기기면 `phone:` 사람 증명(커넥터는 require 꺼지면 평문 프레임도 인증된 cid 로 받음 connector.py:776-784), policy.py:110-122 `check_quota` 는 토큰 없는 로컬을 무제한·사용량은 실행 뒤 `record_usage`, cp_client.py:183-192 `revoke_client_credential` 은 자기 Host 자격으로만 폐기. ② 관련: `cryptography>=42.0.0`(pyproject.toml:30) — X25519 키 합의를 새 의존성 없이.
- O1 High 승인 통로 우회(장악된 릴레이 + 평문 폰 프레임 + 2FA 꺼짐 → 폰 사칭으로 초대·허용·승인) → R8(D11)
- O2 High 초대에 운영 짝 키가 그대로 → 남은 사본이 나중에도 해독·위조 비밀 → R9(D12)
- O3 High 「승인 범위」가 데이터 경계가 아님(복제본은 세션 전체, 미래 내용도) → R10(D13)
- O4 High 프로세스 확인 과대평가 — `~/.claude/sessions/*.json` 은 같은 사용자 프로그램(자동 허용 세션 포함)이 고칠 수 있다(peers.py:33-63). **사실 정정(질문 없음):** 설계 4.5 T4 를 「다른 세션이 from 을 그냥 거짓으로 대는 것은 막는다. 같은 사용자 권한의 악성 프로그램이나 레지스트리 파일을 고치는 세션은 막지 못한다(그 권한이면 짝 키도 읽는다)」로 고치고, 한 세션 id 를 두 레지스트리 항목이 주장하면 거부하는 검사를 더한다(승인된 「보낸 세션 확인」을 더 정확히). 세션별 허용은 인증이 아니라 노출 줄이기로 설명
- O5 High 입장 제어 없음 → **필수 구현(질문 없음):** 승인된 「한도」를 실제로 지키려면 실행 전에 횟수·예산(묻기당 상한만큼)을 예약하고 끝나면 정산, Host 전체 동시 2건(P5) + 대기 4건 넘으면 `busy`, 시간 초과도 예약분으로 계산. pclink 는 `check_quota` 의 「로컬 무제한」 길을 쓰지 않는다
- O6 Medium 「양쪽 즉시 해제」 불가 → **사실 정정(질문 없음):** 해제 = 내가 발급한 상대 자격은 CP 에서 즉시 폐기(내 방에서 상대 즉시 차단) + 상대가 발급한 내 자격·짝 키는 내 쪽에서 즉시 지움 + 상대에게 짝 통로로 알림(상대가 꺼져 있으면 「상대 쪽 정리 대기」로 표시, 상대가 다음에 붙거나 사장님이 상대 화면에서 지우면 끝). CP 실패는 불명→pending(묶인 결정)
- O7 Medium 응답 유실 뒤 복구 없음 → R11(D14)
- O8 Medium 실행 안전성 계약 시험 없음 → **필수 증명(질문 없음):** 승인된 「도구·MCP·훅 없는 복제본」의 증명으로, 실 e2e(`-m e2e`)에 흔적을 남기는 프로젝트 훅(.claude/settings.json)·MCP(.mcp.json)·도구 사용 유도 프롬프트를 심고 정상·실패·시간 초과 모두에서 흔적이 없음을 단언. 지원 Claude Code 버전이 바뀌면 다시 돈다(출시 게이트)

### R8: 폰 경유 pclink 관리의 통로 조건 (O1, D9 다시 열기)
Finding: O1, High, confidence 8/10, session_manager/server.py:1237-1249·session_manager/connector.py:776-784, Codex(Claude 코드 확인)
Plan baseline: R6 approved(D9) — 초대 만들기·받기·세션 허용 켜기를 폰에서도, 릴레이 경유는 2FA 특권(2FA 를 켜 두었으면 OTP). 그리고 결정 「운영 서버 보강은 기능을 켜는 조건 아님」(2026-10-22)
Runtime evidence: 사람 증명은 커넥터가 인증된 기기 요청에 붙이는 메모리 비밀 + 활성 기기다. 커넥터는 E2EE 강제가 꺼져 있으면 평문 프레임도 받고, 기기 번호(cid)는 릴레이가 붙인다. 이 PC 는 E2EE 강제 꺼짐(P8). 2FA 특권은 2FA 가 꺼져 있으면 그냥 통과(교훈 clewpath-2fa-conditional-docs). → 장악된 릴레이가 인증된 폰 cid 로 평문 요청을 끼워 초대를 만들고(평문 답이라 릴레이가 초대를 읽음)·세션 허용·승인까지 할 수 있다. 짝 통로 자체는 짝 키라 안전하지만 「짝을 만드는 길」이 뚫린다. 다시 여는 이유: 새 위험(D9 때 이 길을 따지지 않음)
Comparison grid:
| 선택 | 지금(D9) | A 적용 | B 그대로 | C 조사 먼저 | D 이것만 미룸 |
|---|---|---|---|---|---|
| R8 폰 경유 pclink 관리(초대 만들기·받기·세션 허용 켜기·pclink 승인 결정)의 통로 | 2FA 특권(2FA 꺼지면 통과), 평문 프레임도 받음 | E2EE 봉투로 온 요청만(평문 거부) + 2FA 켜져 있어야(꺼져 있으면 폰에선 막고 PC 화면만) | D9 그대로 | 폰 전체 E2EE 강제가 옛 앱·PWA 와 맞는지 조사(코드 읽기·이 PC 폰 확인, 반나절) 뒤 다시 정함 — 그동안 D9 그대로 | D9 그대로, 위험은 TODO 로 |
| R8 끄는 동작(해제·허용 끄기·끄기 스위치) | 폰 가능 | 폰 가능(그대로 — 끄는 쪽은 막지 않음) | 같음 | 같음 | 같음 |
| R8 결정 「서버 보강은 조건 아님」 | 유효 | 유효(이 경로는 Host 쪽 E2EE 조건으로 막으므로) | 근거가 약해진 채 유효 | 조사 뒤 | 근거가 약해진 채 유효 |
| R1~R7 | 승인(D4~D10) | 같음 | 같음 | 같음 | 같음 |
Question D11:
D11 — 폰에서 PC 짝짓기·세션 허용을 할 때, 암호(E2EE)로 온 요청만 받고 2차 인증을 필수로 할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 다른 AI 검토 O1(D9 다시 열기).
ELI10: 짝 통로 자체는 짝 키로 잠겨 안전합니다. 그런데 짝을 「만드는」 일을 폰에서 하게 했고(D9), 지금 폰 연결은 평문도 받아 주며 2차 인증이 꺼져 있으면 그냥 통과합니다. 그래서 중계 서버가 뚫리면 사장님 폰인 척 평문 요청을 끼워 초대를 만들고 그 문자열까지 읽어, 남의 PC 를 짝으로 들일 수 있습니다. 폰에서 짝짓기 관련 요청은 암호로 온 것만 받고 2차 인증을 필수로 하면 이 길이 막힙니다.
Stakes if we pick wrong: 그대로 두면 중계 서버 하나가 뚫렸을 때 공격자 PC 가 사장님 PC 의 세션에 물을 수 있는 짝이 됩니다.
Recommendation: A 적용 because 중계 서버는 방 키가 없어 암호 요청을 만들 수 없으니, 「암호로 온 요청만」 조건 하나로 이 길이 닫히고 폰 앱은 이미 암호로 보냅니다.
Completeness: A=10/10, B=5/10, C=7/10, D=5/10
Pros / cons:
A) 적용 (recommended)
  ✅ 중계 서버는 방 키가 없어 암호(E2EE) 요청을 만들 수 없으므로, 폰 사칭으로 짝을 만드는 길이 닫힙니다
  ✅ 끄는 동작(해제·허용 끄기·끄기 스위치)은 그대로 폰에서 돼 사고 때 빨리 막을 수 있습니다
  ❌ 2차 인증을 안 켜 두셨다면 폰에서 짝짓기·세션 허용이 막혀 PC 화면에서 해야 합니다(human ~0.5일 / CC ~20분)
B) 그대로
  ✅ 2차 인증 없이도 폰에서 바로 짝지을 수 있습니다
  ❌ 중계 서버가 뚫리면 폰 사칭으로 짝·허용·승인까지 만들어질 수 있습니다
C) 조사 먼저
  ✅ 폰 연결 전체를 암호 필수로 바꿀 수 있는지(옛 앱·PWA 호환) 반나절 조사해 더 넓게 막을 수 있습니다
  ❌ 조사하는 동안은 D9 그대로라 이 길이 열려 있고, 결정이 한 번 더 필요합니다
D) 이것만 미룸
  ✅ 이번 작업량이 늘지 않습니다
  ❌ 위험이 TODO 로만 남고 출시본에는 그대로 열려 있습니다
Net: 2차 인증을 켜 둬야 폰에서 짝지을 수 있게 되는 불편으로, 중계 서버 장악에서 짝 만들기 길을 닫을지입니다.
Header: D11 폰 통로
Options:
A) 적용 (recommended)
폰 경유 초대 만들기·받기·세션 허용 켜기·pclink 승인 결정은 E2EE 봉투 요청만(평문 거부) + 2FA 켜져 있어야(꺼져 있으면 PC 화면만). 끄는 동작은 폰 그대로. human ~0.5일 / CC ~20분.
B) 그대로
D9 그대로: 2FA 특권(꺼져 있으면 통과), 평문 프레임도 받음.
C) 조사 먼저
폰 전체 E2EE 강제가 옛 앱·PWA 와 맞는지 반나절 조사 뒤 다시 정함. 그동안 D9 그대로.
D) 이것만 미룸
D9 그대로 출시, 위험은 TODO 로.

State: approved
Actual answer: A) 적용 (D11, 2026-10-22 사장님 답)
Accepted scope: 릴레이 경유(폰)로 오는 초대 만들기·초대 받기·세션 허용 켜기·pclink 승인 결정(승인 대기열의 pclink 종류)은 E2EE 봉투로 온 요청만 받는다(평문이면 거부), 그리고 2FA 가 켜져 있어야 한다(꺼져 있으면 폰에서는 거부하고 PC 화면에서만). 끄는 동작(해제·세션 허용 끄기·끄기 스위치)은 폰에서 그대로. R6(D9)은 이 조건을 더한 채 유효. 결정 「서버 보강은 조건 아님」은 이 경로를 Host 쪽 E2EE 조건으로 막으므로 유지
History: R6 accepted scope(D9) 「폰(릴레이 경유)은 2FA 특권(2FA 를 켜 두었으면 OTP)」 → O1(새 위험)로 다시 열어 조건 추가

### R9: 초대 비밀과 운영 짝 키 분리 (O2, D5 다시 열기)
Finding: O2, High, confidence 9/10, R2 accepted scope(D5 「초대 문자열에 짝 키·B 자격·두 PC 신원」)·R5(D8 1회용·24시간), Codex(Claude 확인: pyproject.toml:30 `cryptography>=42.0.0` → X25519·HKDF 사용 가능)
Plan baseline: R2 approved(D5) — 초대 문자열이 짝 키를 그대로 담음. R5 approved(D8) — 초대 1회용·24시간
Runtime evidence: 1회용·24시간은 초대의 「사용」만 막고 담긴 키는 그대로다. 짝이 맺어진 뒤에도 메신저에 남은 초대 사본에는 앞으로의 모든 짝 통신을 열고(해독) 꾸밀(위조) 수 있는 키가 그대로 있다. 다시 여는 이유: 새 위험(D5·D8 때 키 수명을 따로 따지지 않음)
Comparison grid:
| 선택 | 지금(D5·D8) | A 적용 | B 그대로 | C 조사 먼저 | D 이것만 미룸 |
|---|---|---|---|---|---|
| R9 초대에 담는 비밀 | 운영 짝 키 그대로 | 1회용 초대 비밀만 | 운영 짝 키 | 조사 뒤 | 운영 짝 키 |
| R9 운영 짝 키를 만드는 법 | 초대가 곧 키 | 짝 맺을 때 두 Host 가 일회용 공개키(X25519)를 주고받아 새로 만듦(HKDF), 그 교환은 초대 비밀로 인증 | 초대가 곧 키 | 조사 뒤 | 초대가 곧 키 |
| R9 쓰고 난 초대 사본이 새면 | 앞으로의 짝 통신 해독·위조 가능 | 쓸모 없음(운영 키를 모름) | 해독·위조 가능 | 그동안 가능 | 가능 |
| R9 안 쓴 초대(24시간 안)가 새면 | 짝 맺기 시도 가능(폰 승인이 다음 방어) | 같음(D8 감수 위험 그대로) | 같음 | 같음 | 같음 |
| R9 새 의존성 | 없음 | 없음(cryptography 이미 있음) | 없음 | — | 없음 |
| R1~R8 | 승인(D4~D11) | 같음 | 같음 | 같음 | 같음 |
Question D12:
D12 — 초대 문자열에는 1회용 비밀만 넣고, 실제 통신 키는 짝 맺을 때 두 PC 가 새로 만들게 할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 다른 AI 검토 O2(D5 다시 열기).
ELI10: 지금 설계는 초대 문자열에 두 PC 가 앞으로 계속 쓸 통신 키를 그대로 넣습니다. 초대를 한 번 쓰면 무효가 되지만, 메신저에 남은 그 문자열 안의 키는 계속 유효해서 그걸 주운 사람은 나중의 통신을 엿보거나 꾸밀 수 있습니다. 초대에는 「짝 맺기용 1회 비밀」만 넣고, 진짜 통신 키는 짝을 맺는 순간 두 PC 가 서로 주고받은 값으로 새로 만들면, 남은 초대 사본은 짝이 맺어진 뒤엔 쓸모가 없어집니다.
Stakes if we pick wrong: 그대로 두면 사장님이 초대를 옮긴 메신저·메일이 새는 순간 짝지은 두 PC 의 묻기·답이 그 사람에게 열립니다.
Recommendation: A 적용 because 이미 있는 암호 라이브러리로 반나절이면 되고, D5 에서 감수한 「옮기는 통로에 남는 위험」을 짝이 맺어진 뒤로는 없앱니다.
Completeness: A=10/10, B=5/10, C=7/10, D=5/10
Pros / cons:
A) 적용 (recommended)
  ✅ 짝이 맺어진 뒤에는 메신저에 남은 초대 사본으로 통신을 엿보거나 꾸밀 수 없습니다
  ✅ 이미 있는 cryptography 라이브러리(X25519·HKDF)로 되어 새 의존성이 없습니다
  ❌ 짝짓기 단계가 한 번 더 오가고 코드·시험이 늘어납니다(human ~1일 / CC ~40분)
B) 그대로
  ✅ 짝짓기가 가장 단순합니다(초대 = 키)
  ❌ 초대 사본이 새면 짝 해제 전까지 모든 통신이 열립니다
C) 조사 먼저
  ✅ 키 합의 방식을 따로 반나절 검토해 더 나은 안이 있는지 봅니다
  ❌ 그동안 D5 그대로이고 결정이 한 번 더 필요합니다
D) 이것만 미룸
  ✅ 이번 작업량이 늘지 않습니다
  ❌ 출시본에서 초대 사본 = 영구 키 위험이 그대로입니다
Net: 짝짓기 한 단계를 더하고, 옮긴 통로에 남은 초대가 앞으로의 통신 열쇠가 되는 것을 막을지입니다.
Header: D12 키 분리
Options:
A) 적용 (recommended)
초대에는 1회용 초대 비밀만. 짝 맺을 때 두 Host 가 일회용 공개키(X25519)를 교환해 운영 짝 키를 새로 만들고(HKDF), 교환은 초대 비밀로 인증. 쓰고 난 초대 사본은 운영 키를 모름. 새 의존성 없음. human ~1일 / CC ~40분.
B) 그대로
D5 그대로: 초대 문자열에 운영 짝 키.
C) 조사 먼저
키 합의 방식을 반나절 따로 검토한 뒤 다시 정함. 그동안 D5 그대로.
D) 이것만 미룸
D5 그대로 출시, 위험은 TODO 로.

State: approved
Actual answer: A) 적용 (D12, 2026-10-23 사장님 답)
Accepted scope: 초대 문자열에는 1회용 초대 비밀(+ B 자격·두 PC 신원)만 담는다. 짝을 맺을 때 두 Host 가 일회용 X25519 공개키를 주고받아 HKDF 로 운영 짝 키를 새로 만들고, 그 교환은 초대 비밀로 인증한다(교환에 두 PC 신원을 묶음). 쓰고 난 초대 사본은 운영 키를 모른다. 새 의존성 없음(cryptography). 안 쓴 초대(24시간 안)가 새는 위험은 D8 감수 그대로(폰 승인이 다음 방어). R2(D5)는 「초대에 짝 키」 대신 이 방식으로 유효, R5(D8) 그대로
History: R2 accepted scope(D5) 「초대 문자열(짝 키·B 자격·두 PC 신원)」 → O2(새 위험)로 다시 열어 키 분리
필수 구현(D12 취지, 질문 없음): 초대에 담긴 B 자격은 짝짓기 전용 — 짝이 맺어지면 B 가 짝 통로(새 운영 키 봉인)로 새 기기 자격을 주고 초대의 자격은 CP 에서 폐기한다. 그래야 쓰고 난 초대 사본이 B 방 출입증으로도 쓸모없다.

### R10: 첫 연락 승인이 여는 범위와 기간 (O3)
Finding: O3, High, confidence 8/10, 설계 4.4·4.5 T5 「승인 범위에 묶음」, webapi.py:328 복제본은 `--resume <원본>` 으로 원본 대화 전체를 읽음, Codex
Plan baseline: 승인된 범위(2026-10-22) 「첫 연락 폰 승인(보낸 PC·세션 / 받는 세션 / 범위에 묶음, 범위가 넓어지면 다시)」 — 「범위」가 무엇인지 정의 없음, 기간 없음
Runtime evidence: 복제본은 받는 세션의 대화 전체를 맥락으로 읽는다(실험 4: 원본의 「청포도」를 답함). 승인 뒤 그 세션에 생기는 대화도 같은 승인으로 열린다. 「주제별 허용」은 도구 제거·답 길이로 강제할 수 없다
Comparison grid:
| 선택 | 지금 | A 적용 | B 그대로 | C 조사 먼저 | D 이것만 미룸 |
|---|---|---|---|---|---|
| R10 승인이 여는 것 | 「범위」 미정 | 보낸 PC·세션 → 받는 세션의 **지금과 앞으로의 대화 전체**를 묻기로 열람(승인 카드·설정 화면에 그대로 적음) | 미정 그대로 | 승인 시점까지의 대화만 쓰는 방법(특정 메시지까지 이어 묻기) 조사 뒤 | 미정 그대로 |
| R10 승인 기간 | 없음 | 30일(지나면 다시 승인), 세션 허용을 끄면 그 세션의 승인 모두 폐기 | 없음 | 조사 뒤 | 없음 |
| R10 「범위가 넓어지면 다시」 | 미정 | 보낸 PC·보낸 세션·받는 세션 중 하나라도 다르면 새 승인 | 미정 | 조사 뒤 | 미정 |
| R1~R9 | 승인(D4~D12) | 같음 | 같음 | 같음 | 같음 |
Question D13:
D13 — 첫 연락을 승인하면 무엇을, 얼마 동안 여는 것으로 할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 다른 AI 검토 O3.
ELI10: 다른 PC 가 이 PC 의 세션에 물으면, 그 세션의 「복제본」이 대화 전체를 읽고 답합니다. 그래서 한 번 승인하면 그 세션에 나중에 생기는 대화까지 같은 승인으로 열립니다. 「이 주제만」처럼 좁히는 것은 기술적으로 강제할 수 없습니다. 그러니 승인 카드에 「이 세션의 지금과 앞으로의 대화 전체를 묻기로 볼 수 있음」이라고 그대로 적고, 승인에 30일 기간을 두어 오래된 승인이 계속 열려 있지 않게 하는 것이 정직하고 안전합니다.
Stakes if we pick wrong: 범위를 흐리게 두면 사장님은 「잠깐 물어보는 것」으로 승인했는데, 그 세션에 나중에 쓴 민감한 내용까지 계속 열립니다.
Recommendation: A 적용 because 강제할 수 없는 「주제 범위」를 약속하지 않고 실제로 열리는 것을 그대로 보여 주며, 30일 기간과 허용 끄기로 오래된 승인을 닫습니다.
Completeness: A=10/10, B=4/10, C=7/10, D=4/10
Pros / cons:
A) 적용 (recommended)
  ✅ 승인 카드에 실제로 열리는 것(지금과 앞으로의 대화 전체)을 그대로 적어 오해 없이 승인합니다
  ✅ 30일이 지나거나 세션 허용을 끄면 승인이 닫혀 오래된 승인이 계속 열려 있지 않습니다
  ❌ 같은 상대가 30일마다 다시 승인을 받아야 합니다(폰에서 한 번 누르기)
B) 그대로
  ✅ 한 번 승인하면 계속 쓸 수 있어 번거롭지 않습니다
  ❌ 승인할 때 무엇이 열리는지 불분명하고, 앞으로의 대화까지 기한 없이 열립니다
C) 조사 먼저
  ✅ 승인 시점까지의 대화만 쓰는 방법이 있으면 미래 내용 노출을 아예 막을 수 있습니다
  ❌ 조사 동안 범위가 미정이고, 결정이 한 번 더 필요합니다
D) 이것만 미룸
  ✅ 이번 작업량이 늘지 않습니다
  ❌ 출시본의 승인 의미가 흐린 채 남습니다
Net: 30일마다 한 번 더 누르는 번거로움으로, 승인이 무엇을 얼마나 여는지 분명히 할지입니다.
Header: D13 승인 범위
Options:
A) 적용 (recommended)
승인 = 보낸 PC·세션 → 받는 세션의 지금과 앞으로의 대화 전체를 묻기로 열람(카드·설정에 그대로 적음). 30일 기간, 세션 허용 끄면 그 세션 승인 모두 폐기. 셋 중 하나라도 다르면 새 승인.
B) 그대로
범위·기간 정의 없이 둠.
C) 조사 먼저
승인 시점까지의 대화만 쓰는 방법(특정 메시지까지 이어 묻기)을 조사한 뒤 다시 정함.
D) 이것만 미룸
범위 미정 그대로 출시, TODO 로.

State: approved
Actual answer: A) 적용 (D13, 2026-10-23 사장님 답)
Accepted scope: 첫 연락 승인 = 보낸 PC·보낸 세션 → 받는 세션의 지금과 앞으로의 대화 전체를 묻기로 열람(승인 카드·설정 화면에 그대로 적음 — 주제 범위는 약속하지 않음). 승인 기간 30일(지나면 다시 승인), 세션 허용을 끄면 그 세션의 승인 모두 폐기, 보낸 PC·보낸 세션·받는 세션 중 하나라도 다르면 새 승인. R1~R9 승인값 그대로
History: (없음)

### R11: 답을 잃었을 때 복구 — 결과 잠시 보관 (O7)
Finding: O7, Medium, confidence 8/10, R1(D4 별도 작업: 결과를 나중에 그 cid 로)·R7(D10 회귀 ②: 떠난 A 의 늦은 결과는 버림)·1-1(영속 중복 장부: 같은 id 재실행 거부), Codex
Plan baseline: R1·R7 approved — 결과는 그 cid 로 따로, A 가 떠났으면 버림. 중복 장부는 같은 id 를 거부. 결과 재전달은 정하지 않음
Runtime evidence: 묻기 한 건은 수 초~수 분·비용이 든다(실험 4: 작은 세션 7.3초·0.001달러, 큰 세션은 더 큼). A 의 연결이 그사이 끊기면(D7 물을 때만 붙음·노트북 절전 등) 답이 버려지고, 같은 id 로 다시 물으면 중복 장부가 거부, 새 id 로 물으면 다시 실행·다시 과금
Comparison grid:
| 선택 | 지금 | A 적용 | B 그대로 | C 조사 먼저 | D 이것만 미룸 |
|---|---|---|---|---|---|
| R11 늦은 결과 | 버림 | 버리되(방 전체에 안 뿌림 — R7 그대로) B 가 요청 id 로 10분 보관 | 버림 | 조사 뒤 | 버림 |
| R11 같은 id 로 다시 물음 | 거부 | 다시 실행하지 않고 보관된 답(또는 「아직 도는 중」)을 돌려줌 | 거부 | 조사 뒤 | 거부 |
| R11 A 로컬 API 의 시간 초과 응답 | timeout | timeout + 요청 id(세션이 같은 id 로 다시 물을 수 있게) | timeout | 조사 뒤 | timeout |
| R11 보관 데이터 | 없음 | 답 본문을 B 데이터 폴더에 10분(보호 저장, 만료 정리) | 없음 | — | 없음 |
| R1~R10 | 승인(D4~D13) | 같음 | 같음 | 같음 | 같음 |
Question D14:
D14 — 답이 오는 사이 연결이 끊겨 답을 잃으면, 같은 요청으로 다시 받을 수 있게 할까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — 다른 AI 검토 O7.
ELI10: 원격 묻기는 수 초에서 수 분 걸리고 돈도 듭니다. 그사이 묻는 쪽 연결이 끊기면(절전·네트워크) 지금 설계로는 답이 버려지고, 같은 요청으로 다시 물으면 「이미 받은 요청」이라 거절되며, 새로 물으면 처음부터 다시 돌고 다시 과금됩니다. 답을 10분만 보관해 두면 같은 요청으로 다시 물을 때 다시 돌리지 않고 그 답을 바로 줄 수 있습니다.
Stakes if we pick wrong: 그대로 두면 연결이 잠깐 끊길 때마다 이미 돈 낸 답을 잃고, 같은 질문을 처음부터 다시 돌려야 합니다.
Recommendation: A 적용 because 보관 10분과 같은 id 재조회만으로 잃은 답을 비용 없이 되찾고, 늦은 결과를 방 전체에 뿌리지 않는 R7 규칙은 그대로 지킵니다.
Completeness: A=10/10, B=6/10, C=7/10, D=6/10
Pros / cons:
A) 적용 (recommended)
  ✅ 연결이 끊겨도 같은 요청 id 로 다시 물으면 다시 돌리지 않고 보관된 답을 바로 받아 비용이 두 번 들지 않습니다
  ✅ 늦은 결과를 방 전체에 뿌리지 않는 규칙(R7)은 그대로라 보안 변화가 없습니다
  ❌ 답 본문이 B 데이터 폴더에 10분 남습니다(보호 저장·만료 정리, human ~0.5일 / CC ~20분)
B) 그대로
  ✅ 답을 어디에도 남기지 않아 가장 단순합니다
  ❌ 연결이 끊길 때마다 답을 잃고 새로 물으면 다시 실행·다시 과금됩니다
C) 조사 먼저
  ✅ 끊김이 실제로 얼마나 잦은지 써 본 뒤 정할 수 있습니다
  ❌ 그동안 답을 잃는 문제가 그대로이고 결정이 한 번 더 필요합니다
D) 이것만 미룸
  ✅ 이번 작업량이 늘지 않습니다
  ❌ 출시본에서 끊김 = 답 분실·재과금이 그대로입니다
Net: 답 본문을 10분 보관하는 대신, 끊김 때 잃은 답을 비용 없이 되찾을지입니다.
Header: D14 답 복구
Options:
A) 적용 (recommended)
B 가 결과를 요청 id 로 10분 보관(보호 저장·만료 정리). 같은 id 로 다시 물으면 다시 실행하지 않고 보관된 답(또는 「아직 도는 중」). A 로컬 API 의 시간 초과 응답에 요청 id. 늦은 결과는 여전히 방 전체에 안 뿌림. human ~0.5일 / CC ~20분.
B) 그대로
늦은 결과 버림, 같은 id 재시도 거부, 새 id 면 다시 실행·과금.
C) 조사 먼저
끊김 빈도를 써 본 뒤 다시 정함. 그동안 B 그대로.
D) 이것만 미룸
B 그대로 출시, TODO 로.

State: approved
Actual answer: A) 적용 (D14, 2026-10-23 사장님 답)
Accepted scope: B 는 묻기 결과를 요청 id 로 10분 보관한다(데이터 폴더, 짝 키 봉인 상태 또는 DPAPI/0600 보호, 만료 정리). 같은 id 로 다시 오면 다시 실행하지 않고 보관된 답(아직 도는 중이면 「진행 중」)을 돌려준다 — 영속 중복 장부와 한 몸. A 로컬 API 의 시간 초과 응답에 요청 id 를 실어 세션이 같은 id 로 다시 물을 수 있게. 늦은 결과를 방 전체에 뿌리지 않는 R7 규칙은 그대로. R1~R10 승인값 그대로
History: (없음)

### R12: TODO — 맥 pclink 경로 실험(맥 출시 뒤)
Finding: S7·A4·S8, P3, confidence 7/10, 전제 P11(맥 `psutil.net_connections` 권한 오류, 미실측)·TODOS.md:66(맥은 아직 출시 대상 아님), Claude
Plan baseline: 승인된 1-3 「보낸 세션 확인」은 OS 별 경로가 필요. 맥 경로는 「구현 때 맥 측 실험」으로만 적힘(TODO 없음)
Runtime evidence: Phase 1 은 Windows(이 PC·노트북) 사이에서 먼저 쓴다. 맥은 출시 대상이 아니라 이 PC ↔ 맥 짝은 맥 정식 출시 뒤
Comparison grid:
| 선택 | 지금 | A TODOS 에 추가 | B 넣지 않음 | C 지금 함께 만듦 |
|---|---|---|---|---|
| R12 맥 pclink 경로(보낸 세션 확인: 내 사용자 프로세스별 연결·lsof, 맥 실기 짝짓기·묻기) | 기록 없음 | TODOS 「PC 사이 세션 통신」에 항목(맥 출시 뒤, 맥 측 `docs/mac-dev-loop.md` 로 실험·수리) | 기록 안 함 | Phase 1 에 맥 경로까지 넣어 지금 구현(맥 측 실험 필요) |
| R1~R11 | 승인 | 같음 | 같음 | 같음 |
Question D15:
D15 — 맥에서 원격 묻기를 쓰기 위한 작업(맥 출시 뒤)을 TODO 로 남겨 둘까요?
Project/branch/task: 006 main, pclink Phase 1 기획 검토 — TODO 정리.
ELI10: 원격 묻기는 보낸 세션을 확인할 때 운영체제마다 다른 방법이 필요합니다. 맥에서는 Windows 에서 쓰는 방법이 권한 오류로 안 돌아서 다른 길(프로세스별 연결·lsof)을 맥에서 실험해야 합니다. 맥은 아직 정식 출시 전이라 지금은 Windows 끼리 먼저 쓰고, 맥 출시 뒤 맥 측 Claude 가 실험·수리하도록 TODO 로 남겨 두자는 것입니다.
Stakes if we pick wrong: 기록하지 않으면 맥 출시 때 원격 묻기가 맥에서 조용히 안 되는 것을 사장님이 처음 발견하게 됩니다.
Recommendation: A TODOS 에 추가 because 맥 출시는 아직이라 지금 만들 수 없고, 잊지 않게 맥 측 작업 방식까지 적어 두는 것이 가장 싸고 확실합니다.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) TODOS 에 추가 (recommended)
  ✅ What: 맥 보낸 세션 확인 경로·맥 실기 짝짓기·묻기 실험. Why: 맥 psutil 권한 오류(미실측). Context: 맥 출시 뒤 맥 측 Claude 가 mac-dev-loop 로
  ✅ Depends on: 맥 정식 출시(platforms 에 mac) — 그 전엔 할 수 없어 기록이 맞습니다(human ~10분 / CC ~2분)
  ❌ 맥 출시까지는 이 PC ↔ 맥 짝을 쓸 수 없다는 사실은 그대로입니다
B) 넣지 않음
  ✅ TODOS 가 짧게 유지됩니다
  ❌ 맥 출시 때 원격 묻기 맥 경로가 빠진 채 나갈 수 있습니다
C) 지금 함께 만듦
  ✅ 맥 출시 때 바로 쓸 수 있게 미리 만들어 둡니다
  ❌ 맥 설치본이 아직 출시 대상이 아니라 실기 검증 없이 짐작으로 만드는 꼴이 됩니다(human ~1일 / CC ~40분 + 맥 실험)
Net: 지금 할 수 없는 맥 경로를 잊지 않게 적어 둘지입니다.
Header: D15 맥 TODO
Options:
A) TODOS 에 추가 (recommended)
TODOS 「PC 사이 세션 통신」에 「맥 pclink 경로(보낸 세션 확인: 내 사용자 프로세스별 연결·lsof, 맥 실기 짝짓기·묻기) — 맥 출시 뒤, 맥 측 mac-dev-loop 로 실험·수리」 추가.
B) 넣지 않음
기록하지 않음.
C) 지금 함께 만듦
Phase 1 에 맥 경로까지 넣어 지금 구현(맥 측 실험 필요).

State: approved
Actual answer: A) TODOS 에 추가 (D15, 2026-10-23 사장님 답)
Accepted scope: TODOS 「PC 사이 세션 통신」에 「맥 pclink 경로(보낸 세션 확인: 내 사용자 프로세스별 연결·lsof, 맥 실기 짝짓기·묻기) — 맥 출시 뒤, 맥 측 mac-dev-loop 로 실험·수리」 항목 추가. Phase 1 범위는 그대로
History: (없음)

다른 AI 검토 처리: O1→R8(D11 적용) · O2→R9(D12 적용) · O3→R10(D13 적용) · O7→R11(D14 적용) · O4 사실 정정(T4 범위) + 중복 주장 거부 · O5 승인된 한도의 필수 구현(실행 전 예약·Host 동시 2·대기 4) · O6 사실 정정(해제 = 내 발급분 즉시 폐기·상대 발급분 내 쪽 삭제+알림·상대 정리 대기) · O8 필수 증명(실 e2e 계약 시험: 흔적 남기는 훅·MCP·도구 유도, 정상·실패·시간 초과). Codex 와 Claude 사이 남은 이견 없음.

Approval readiness: PASS — R1(D4)·R2(D5)·R3(D6)·R4(D7)·R5(D8)·R6(D9)·R7(D10)·R8(D11)·R9(D12)·R10(D13)·R11(D14)·R12(D15) 각자 실제 답. 질문 없이 반영한 것은 근거가 있다: S5·S6·S7·A1·A4·A5·A6·Q1~Q6·P1~P5 는 2026-10-22 사장님 범위 답(「조회 전용 묻기까지」, 1-0~1-5)과 D3 의 필수 구현, O4·O6 은 사실 정정, O5 는 승인된 「한도」의 필수 구현, O8 은 승인된 「도구·MCP·훅 없는 복제본」의 필수 증명. 범위 점검 결과: scope accepted as-is.

## 검토 보고 본문

### NOT in scope (이번에 하지 않음)
- 실시간 주고받기·우체국·원격 작업 지시·관제 연동·허브(다자간·다중 홉·오프라인 큐·주소 서비스) — 허브 프로젝트(`docs/spec-pclink-hub-handoff.md`).
- 폰 연결 전체의 E2EE 강제·방 키 교체·재전송 방지 — Deferred(옛 폰 앱·PWA 호환 확인 뒤). pclink 관리 동작만 D11 로 막았다.
- 원격 `/respond`(권한 창 키 입력) 2FA 특권화 — Deferred.
- CP 의 주인 단위 PC 묶기 — SaaS 때.
- 맥 pclink 경로 — 맥 정식 출시 뒤(D15 TODO). 리눅스 정식 지원 — 결정 전.

### What already exists (재사용)
- 기기 등록부·쓰기 잠금 `devices.py:33,68-74`, 기기 인증 `connector.py:397-403`, 로컬 전용 덴리스트 `connector.py:124-130`, 2FA 특권 `connector.py:497-500`, 사람 증명 `server.py:1237-1249`.
- 승인 대기열 `approvals.py:29-30`(종류 2개 추가로 짝짓기·첫 연락 승인).
- 방 키 E2EE·DPAPI 보관 `e2ee.py:58-97`(짝 키 보관 방식 재사용), `cryptography>=42`(X25519·HKDF·AES-GCM).
- CP 기기 자격 발급·폐기(`cp_client.py` `/client/credential`·`revoke`), 릴레이 `role=client` 접속(007).
- 복제본 묻기 `webapi.ephemeral_ask`(D3 로 넓힘), 감사 기록 공용 `jsonl_log`(잠금+회전).
- 시험 틀: 커넥터 시험 39건·실제 소켓 흉내(`test_connector_multidevice.py`), 실 CP e2e(`tests/e2e`), PWA 하니스(`tests/pwa/harness.mjs`).

### 그림
흐름은 위 4.4(짝짓기·묻기)와 4.3. 구현 때 `pclink/protocol.py`(봉투·봉인·장부 상태)와 `pclink/routes.py`(B 쪽 작업 상태: accepted → running → done/failed → 10분 보관 → 정리)에 같은 그림을 코드 주석으로 둔다.

### 실패 모드(새 경로마다)
| 경로 | 현실적 실패 | 시험 | 오류 처리 | 사용자에게 |
|---|---|---|---|---|
| 짝짓기 | CP 자격 발급 시간 초과 | 계획(O6·pending) | 불명→pending·재시도 | 「확인 중」 |
| 짝짓기 | X25519 교환 중 끊김 | 계획(e2e) | 짝 미성립, 초대는 교환이 끝나야 소모 | 「짝짓기 실패 — 다시 붙여넣기」 |
| 짝짓기 | 쓴 초대·만료 초대·다른 PC 신원 | 계획 | 거부 | 「이미 쓴/만료된 초대」 |
| 폰 경유 관리 | 평문 프레임·2FA 꺼짐 | 계획(D11) | 거부 | 「PC 화면에서 하거나 2FA 를 켜세요」 |
| 묻기 | 상대 꺼짐·CP/릴레이 불통 | 계획 | 빠른 오류 코드 | `peer_offline` 등 |
| 묻기 | 결과 전에 A 끊김 | 계획(D14) | 10분 보관·같은 id 재조회 | timeout + 요청 id |
| 묻기 | 동시 몰림 | 계획(O5) | 실행 전 예약·busy | `busy`·`limit` |
| 묻기 | 복제본 실패(cwd 없음·claude 오류) | 계획 | `ask_failed` + 이유 | 오류 문구 |
| 묻기 | 장부·정책 파일 깨짐 | 계획(Q3) | fail-closed 거부 + host.log | `not_allowed` |
| 해제 | 상대 꺼짐 | 계획(O6) | 내 쪽 즉시 차단·상대 정리 대기 | 「상대 쪽 정리 대기」 |

critical gaps(시험 없음 + 처리 없음 + 조용한 실패): 0.

### 병렬 작업 계획
| 단계 | 모듈 | 먼저 |
|---|---|---|
| 프로토콜 약속 v0 문서(1-0) | docs | — |
| 복제본 격리(T6) | webapi | — |
| 007 보강 의뢰(T11) | 007 저장소 | — |
| 봉투·봉인·장부·결과 보관(T1) | pclink/protocol | 1-0 |
| 커넥터 PC 등급·분기·별도 작업 + 회귀 4종(T3) | connector | T1 |
| 짝 연결(T5) | pclink/link | T1 |
| 짝짓기·정책(T2) | pclink/policy, devices, approvals | T1 |
| 로컬 API·보낸 세션 확인·한도·덴리스트(T4) | pclink/routes, server | T2 |
| 화면(T7) | pwa(작업 트리에서만) | T2·T4 |
| 두 Host 실 e2e + 계약 시험(T8) | tests/e2e | 전부 |

- Lane A: T1 → T2 → T4 (pclink/ 공유, 차례로) · Lane B: T6 (따로) · Lane C: T3 (T1 뒤) · Lane D: T5 (T1 뒤) · Lane E: T7 (T4 뒤) · Lane F: T11 (따로).
- 순서: 1-0 문서 + B + F 동시 → A 의 T1 → C·D 와 A 의 T2 동시 → T4 → E → T8 → 세션용 안내(1-4)·CLAUDE.md 예외.
- 충돌 주의: `connector.py`·`server.py` 는 폰 코드와 함께 쓰는 파일(회귀 계약 R7), `tests/conftest.py`·의존성 파일은 맥 측과 겹치므로 건드리지 않는다.

## Implementation Tasks
이 검토의 발견에서 나온 과제다. Claude Code 나 Codex 로 하고, 끝나면 체크한다.

- [ ] **T1 (P1, human: ~1.5일 / CC: ~40분)** — pclink/protocol — 봉투 v0·AEAD(머리 인증)·nonce·만료·영속 중복 장부·결과 10분 보관
  - Surfaced by: 범위 점검 S5·1-1, D14(R11), O5
  - Files: session_manager/pclink/protocol.py, tests/test_pclink_protocol.py
  - Verify: `python -m pytest -q tests/test_pclink_protocol.py`
- [ ] **T2 (P1, human: ~2일 / CC: ~1시간)** — pclink/policy — 초대(1회용·24시간·초대 비밀)·X25519 키 합의·양쪽 자격·해제 의미·세션 허용·첫 연락 승인(30일)·끄기·PC 등급 기기·승인 종류 2개·짝 키 보호 저장
  - Surfaced by: D5·D6·D8·D12·D13, O6, A1, Q5
  - Files: session_manager/pclink/policy.py, session_manager/devices.py, session_manager/approvals.py, tests/test_pclink_policy.py
  - Verify: `python -m pytest -q tests/test_pclink_policy.py tests/test_devices.py tests/test_approvals.py`
- [ ] **T3 (P1, human: ~1.5일 / CC: ~1시간)** — connector — PC 등급은 pclink·ping 만, `pclink.*` 분기 한 줄, 묻기는 접수 즉시 답·별도 작업, 회귀 ①~④
  - Surfaced by: S1(D4), S5, Q4, R7(D10)
  - Files: session_manager/connector.py, tests/test_connector_pclink.py
  - Verify: `python -m pytest -q tests/test_connector_*.py tests/test_e2ee.py`(기존 39·10건 그대로 + 새 회귀)
- [ ] **T4 (P1, human: ~2일 / CC: ~1시간)** — pclink/routes — 로컬 API(로컬 전용)·보낸 세션 확인(중복 주장 거부)·한도 실행 전 예약·Host 동시 2·대기 4·오류 코드·덴리스트·폰 경유 E2EE+2FA
  - Surfaced by: A3(D9)·D11·A4·O4·A5·O5·P5·A6
  - Files: session_manager/pclink/routes.py, session_manager/server.py, session_manager/connector.py(덴리스트), tests/test_pclink_routes.py
  - Verify: `python -m pytest -q tests/test_pclink_routes.py tests/test_connector_local_only.py`
- [ ] **T5 (P1, human: ~1일 / CC: ~40분)** — pclink/link — 물을 때만 B 방 client 접속·60초 재사용·CP 토큰·시간 초과
  - Surfaced by: S4(D7), A6
  - Files: session_manager/pclink/link.py, tests/test_pclink_link.py
  - Verify: `python -m pytest -q tests/test_pclink_link.py`
- [ ] **T6 (P1, human: ~0.5일 / CC: ~15분)** — webapi — ephemeral_ask 격리 인자·기록 없는 경로는 delete_fork 건너뜀·죽은 proc 코드 정리
  - Surfaced by: S6, Q1, Q2, D3
  - Files: session_manager/webapi.py, tests/test_webapi_*.py
  - Verify: `python -m pytest -q tests/test_webapi*.py`
- [ ] **T7 (P2, human: ~1.5일 / CC: ~1시간)** — pwa — 초대 보이기·붙여넣기·세션 「원격 묻기 받기」 토글·끄기 스위치·승인 카드 문구(지금·앞으로 전체, 30일)
  - Surfaced by: D5·D9·D13, 1-2
  - Files: pwa/index.html(작업 트리에서만), tests/pwa/pclink.test.mjs, tests/test_pwa_js.py(개수)
  - Verify: `node --test --test-reporter=tap tests/pwa/pclink.test.mjs` + `python -m pytest -q tests/test_pwa_js.py`
- [ ] **T8 (P1, human: ~1.5일 / CC: ~1시간)** — tests/e2e — 두 Host 실 e2e(시험용 CP·릴레이 *-test) + 실행 안전성 계약 시험(흔적 훅·MCP·도구 유도)
  - Surfaced by: R7(D10), O8
  - Files: tests/e2e/test_pclink_two_hosts.py
  - Verify: `python -m pytest -m e2e tests/e2e/test_pclink_two_hosts.py`(출시 전 게이트)
- [ ] **T9 (P2, human: ~0.5일 / CC: ~20분)** — docs — 프로토콜 약속 v0(3층·봉투·주소·상태·버전·거부·X25519 짝짓기)
  - Surfaced by: 1-0, D12
  - Files: docs/designs/pclink-protocol-v0.md
  - Verify: 검토(1~2쪽 + 예제)
- [ ] **T10 (P2, human: ~0.5일 / CC: ~15분)** — 세션용 안내·CLAUDE.md 승인 예외 — 원격 목록·묻기 쓰는 법, 「원격 묻기 = 허용된 세션의 도구 없는 복제본」 예외 기록
  - Surfaced by: 1-4, 불가침 원칙 2
  - Files: 동봉 스킬, CLAUDE.md
  - Verify: 문서 확인
- [ ] **T11 (P2, human: ~1일 / CC: ~40분)** — 007 — 릴레이 fail-closed·토큰 세대 대조, CP `/internal/*` 인증 → 서비스 모니터 배포 의뢰
  - Surfaced by: 1-5(병행·조건 아님)
  - Files: 007 relay/relay.py, control_plane/api.py
  - Verify: 007 시험 + 서비스 모니터 회신
- [x] **T12 (P3, human: ~1시간 / CC: ~10분)** — 2026-10-23 측정·반영(아래 「T12 측정 기록」) — 측정 — 이 PC 의 큰 세션 하나로 복제본 묻기 시간·비용 다시 재기(0.5달러·180초가 맞는지)
  - Surfaced by: P1 [re-measure before gate]
  - Files: docs/designs/cross-pc-messaging.md(측정 기록)
  - Verify: 실측 값 기록

### Unresolved decisions
없음.

### Completion summary
- Step 0: Scope Challenge — scope accepted as-is
- Reality rows: 7/7 applicable rows emitted (PLAN_REALITY complete), 1 unverified premise(맥 프로세스 조회)
- Architecture Review: 6 issues found
- Code Quality Review: 6 issues found
- Test Review: diagram produced, 44 gaps identified(새 코드, 5 [→E2E])
- Performance Review: 5 issues found
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 1 items proposed to user(D15 추가)
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: codex(gpt-6-astra) completed — 8건(4건은 결정 D11~D14, 4건은 정정·필수 구현·필수 증명으로 반영)
- Parallelization: 6 lanes, 4 parallel / 2 sequential
- Lake Score: 6/7 = 완전성 점수가 있는 답 7개 중 10/10 선택 6개(D8 은 8/10 선택)

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR(2026-09-29, 다른 기획 — 이 기획엔 없음, 7일 지남) | 7 proposals, 4 accepted, 3 deferred |
| Outside Review | codex(gpt-6-astra) via `/plan-eng-review` 독립 검토 | Independent 2nd opinion | 4 | completed(issues_found, 2026-10-22) | 8 findings(High 5·Medium 3), 모두 처리 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 4 | ISSUES OPEN(이번, 미해결 0 — 발견은 모두 결정·과제로 정리) | 61 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR(2026-09-30, 다른 기획, 7일 지남) | score: 4/10 → 9/10, 15 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex · plan-review · completed(2026-10-22, commit c4f9098) · 8건(O1~O8) — O1·O2·O3·O7 은 D11~D14 로 적용, O4·O6 사실 정정, O5 필수 구현, O8 필수 증명.
- **CROSS-MODEL:** Claude(claude-opus-5-5)와 Codex(gpt-6-astra)가 함께 짚은 것: 커넥터 동시성·늦은 결과(S1·R7 ↔ O7), 한도의 실제 강제(A5 ↔ O5). Codex 만 짚은 것: 승인 통로 사칭(O1), 초대 = 운영 키(O2), 승인 범위의 실제 의미(O3), 프로세스 확인 과대평가(O4), 해제 소유 구조(O6), 실행 안전성 계약 시험(O8). 남은 이견 없음.
- **VERDICT:** 이번 Eng 검토는 ISSUES OPEN(발견 61건 — 대부분 새 코드의 시험 과제 44건 — 을 결정 15개·과제 T1~T12 로 모두 정리, 미해결 0). 규칙상 CLEAR 가 아니므로 eng review required — 구현 뒤 `/review` 로 다시 본다.

NO UNRESOLVED DECISIONS

## T12 측정 기록 (2026-10-23, Claude Code 2.1.308, 이 PC)

| 세션 | 기록 크기 | 마지막 압축 뒤 | claude 보고 total_cost_usd | 실제 이번 묻기 |
|---|---|---|---|---|
| 1090cd5d(002) | 80MB | 250KB | 7.08달러 | 약 0.47달러(보고값 − 프로젝트 lastCost 6.62), 7.2초, 답 정상 |
| d56d9dca(홈) | 6.6MB | 4.9MB | 114.44달러 | 0(usage 전부 0, `error_max_budget_usd` — API 호출 없이 즉시 거절) |
| 561280be(KoreaPedia) | 367MB | 9.7MB | 176.68달러 | 거의 0 추정(10초, is_error) |

- **발견:** 이어받은 세션이 그 프로젝트의 마지막 세션이면 claude 가 과거 누적 비용(`~/.claude.json` lastCost·modelUsage)을 되살려
  `total_cost_usd` 와 `--max-budget-usd` 비교에 섞는다 → 「묻기당 0.5달러」 장치가 지금 쓰는 세션에는 늘 실패했다(기획의 가정이 틀림).
- **결정(사장님, 2026-10-23):** 묻기당 2달러·짝당 하루 20달러(API 기준 어림). 구현: `--max-budget-usd` 를 빼고 마지막 압축 지점 이후 바이트로
  실행 전에 어림(1.9달러/MB)해 2달러 넘으면 `too_big`, 하루 예산은 실행 전 예약. 답·감사의 비용 칸은 누적이 섞여 빼고 어림값(est_usd)만.
- 남는 것: 기존 `/api/v1/sessions/{id}/ask`(서비스 토큰 묻기)의 가드레일 `max_budget_usd` 도 같은 이유로 엉뚱하게 실패할 수 있다(TODOS).

