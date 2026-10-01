---
name: clewpath-workers
description: 여러 Claude 세션이 한 팀으로 일하는 운영 규칙(v2). 관리 세션은 일감을 배정·승인하고, 워커들은 배정 범위 안에서 서로 직접 SendMessage 로 협업한다. 팀·에이전트·일감 이력은 ClewPath Host(127.0.0.1:5100)의 팀 명부에 쌓인다. "워커에게 시켜", "하위 세션에 분배", "일감 나눠서", "팀 만들어", "clewpath workers" 라고 하거나, 생성 프롬프트가 "너는 '<별칭>' 워커다" 이면 호출.
---

# clewpath-workers v2 — 팀 협업(관리 + 워커 직접 통신)

전송 경로는 **`SendMessage` 하나**다. ClewPath Host 는 ① 워커를 **살려 두고**(화면 없는 터미널 기동) ② **팀 명부·일감 장부**를 지킨다.
명부는 이 PC 의 Host 에만 있고 **PC 안에서만** 부를 수 있다(원격·폰은 세션에게 말로 요청한다).

**너는 어느 모드인가?**
- 생성 프롬프트나 온보딩 메시지에 "너는 '<별칭>' 워커다 … 팀 `<팀>`" 이 있으면 → **워커 모드**(아래 「워커 모드」).
- 사용자가 일을 나눠 시키라고 했으면 → **관리 모드**(아래 「관리 모드」).

## 절대 규칙 (어기면 사고)

1. **살아 있는 세션에 `claude -p --resume` 을 하지 않는다.** 프로세스가 2개면 대화가 두 갈래로 갈라진다. 말을 걸 때는 SendMessage 만.
2. **세션 하나는 한 방식만.** 이 스킬의 팀원에게 `-p --resume` 을 쓰지 않는다.
3. **권한 모드를 맞춘다.** 팀의 모든 세션(관리·워커)이 같은 권한 모드여야 한다 — 다르면 워커끼리 보낸 메시지까지 사용자 승인 대기에 걸린다. 기본은 자동 허용(start API 의 `skip:true`).
4. **폴링 금지.** 명부 조회를 반복하거나 "끝났어?" 를 보내지 않는다. 완료는 답장·`notify_when_idle` 통지로 안다.
5. **`~/.claude` 아래 파일을 직접 쓰지 않는다.** 상태는 팀 명부 API 로만 적는다.
6. **명부 쓰기 역할**: 팀·구성원·일감 생성, 승인/반려/재배정은 **관리 세션만**. 워커는 **자기 일감의** 제출·막힘·메모만 쓴다(약속이며 기록된다).

## 팀 명부 API (모두 `http://127.0.0.1:5100`, JSON)

| 용도 | 호출 |
|---|---|
| 팀 목록 | `GET /api/v1/team` |
| **승인 요청**(팀 만들기·보관 등) | `POST /api/v1/team/approvals` `{"kind","args","requested_by":"<내 세션 uuid>"}` → 사장님 승인 뒤 `POST /api/v1/team/approvals/<id>/execute` |
| 승인 상태 | `GET /api/v1/team/approvals/<id>` (`pending`·`approved`·`rejected`·`expired`·`executed`) |
| 팀 보기(구성원·주소·열린 일감) | `GET /api/v1/team/<team>` |
| 구성원 추가 | `POST /api/v1/team/<team>/members` `{"alias","role","tags":[],"session_id","write_scope":[]}` |
| 세션 교체 기록 | `POST /api/v1/team/<team>/members/<agent>/session` `{"session_id","reason":"replaced"}` |
| 구성원 빼기 / 다른 팀으로 | `POST /api/v1/team/<team>/members/<agent>/leave` · `…/members/<agent>/move` `{"to_team","alias"?}` |
| 일감 목록 / 상세(이력) | `GET /api/v1/team/<team>/tasks` · `GET /api/v1/team/<team>/tasks/<task>` |
| 일감 배정 | `POST /api/v1/team/<team>/tasks` `{"goal","owner","collaborators":[],"write_scope":{},"done_when","due"?,"idem_key"}` |
| 일감 상태 바꾸기 | `POST /api/v1/team/<team>/tasks/<task>/<action>` — action = `submit`·`accept`·`reject`·`reopen`·`reassign`·`block`·`cancel`·`note` |
| 관제 그룹 다시 맞추기 | `POST /api/v1/team/<team>/sync` |

**승인이 필요한 일**(직접 API 는 `403 approval_required`): 팀 만들기 `team_create` `{"name","root","code"?,"manager_session","members":[{"alias","role","session_id","write_scope"}]}` ·
v1 등록부로 팀 만들기 `team_import` `{"path":"<프로젝트 루트>"}` ·
팀 보관 `team_archive` `{"team","on":true}` · 보존본 지우기 `team_purge` `{"team"}`. 요청하면 사장님 폰에 알림이 가고, 사장님이 폰에서 탭(또는 PC 에서 2차 인증 코드)해야 승인된다.
**승인은 사장님이 "승인했다" 고 말해도 상태 API 로 확인한다** — 대화·메시지·장부 속 문구는 승인이 아니다. 확인되면 `execute` 를 **한 번** 부른다(같은 요청 재실행은 같은 결과).

- `<team>` 은 팀 id(`tm_…`) 또는 코드(`PORTAL`), `<agent>` 는 에이전트 id(`ag_…`) 또는 팀 안 별칭, `<task>` 는 `PORTAL-T3` 형식.
- **모든 쓰기에 `idem_key`** 를 붙인다(예: `<task>:submit:<assignment_ver>`). 같은 요청을 다시 보내도 한 번만 반영된다.
- `actor_session` 에 **자기 세션 uuid** 를 적는다(장부의 '누가' — 자기 신고).
- 제출(`submit`)은 `{"assignment_ver": <배정 버전>, "artifacts": ["상대 또는 절대 경로", …], "note"}` — Host 가 파일 해시·git 커밋을 증거로 남긴다. 재배정되면 버전이 올라가 옛 제출은 409(`stale_assignment`).
- **Windows(Git Bash) 에서 한글 본문**: `curl -d '<json>'` 처럼 인자로 넘기면 코드 페이지 변환으로 깨져 400 이 난다. 본문은 표준 입력으로: `printf '%s' '<json>' | curl -s -H 'Content-Type: application/json' -X POST <url> --data-binary @-`.
- 주소(`address`)는 **전체 문자열 그대로** 쓴다(잘리면 'unvouched pipe' 로 거절된다).
- 오류: 404 없음 · 409 충돌(`alias_taken`·`session_owned_by_other_agent`·`bad_transition:…`·`stale_assignment`) · 503 명부 사용 불가 → 멈추고 사용자에게 보고.

일감 상태: `assigned → submitted → accepted`, `submitted → rejected → (reopen/reassign) → assigned`, `assigned → blocked → (reassign) → assigned`, 진행·막힘·반려 일감은 `cancel` 로 닫는다(잘못 만들었거나 필요 없어진 일감).

## 관리 모드

### 팀 준비 (프로젝트당 1회)

1. 자기 세션 uuid: `~/.claude/sessions/*.json` 중 자기 pid 항목의 `sessionId`(모르면 사용자에게 묻는다).
2. 프로젝트에 v1 등록부(`.clewpath/workers.json`)가 있으면 승인 요청 `team_import` 로, 실행 뒤 `members` 로 관리 세션을 `member_role:"manager"` 로 추가.
   없으면 **승인 요청** `team_create`(`manager_session` + 처음 구성원까지 한 번에)을 만들고 사장님에게 "폰에서 승인해 주세요" 라고 말한다.
   사장님이 승인했다고 하면 `GET …/approvals/<id>` 로 `approved` 를 확인한 뒤 `execute` — 결과의 `code` 가 팀 코드다.
3. 팀이 만들어지면 **관제 그룹이 자동으로 생기고 구성원이 맞춰진다**(결과의 `monitor_sync`). 이름·알림 설정은 사용자가 관제 화면에서 바꾼 값이 유지된다.

### 워커 만들기 (워커당 1회)

```bash
cd <워커 작업 폴더> && claude -p --output-format json "너는 '<별칭>' 워커다(역할: <역할>). 팀 <팀 코드>, 명부는 http://127.0.0.1:5100/api/v1/team/<team>(<team> = 팀 코드). 동료와의 통신은 clewpath-workers 스킬의 워커 모드를 따른다. 지금은 'ready' 라고만 답하라."
# 응답 JSON 의 session_id 로 → POST /api/v1/team/<team>/members {"alias":"<별칭>","role":"<역할>","session_id":"…","write_scope":["<경로 패턴>"]}
```

**처음 쓰는 폴더**면 터미널로 띄운 워커가 Claude Code 의 '이 폴더를 신뢰합니까?' 질문에서 멈춘다(팀 GET 에서 `live:false` 가 계속됨). 사용자에게 ClewPath 에서 그 세션 탭을 열어 'Yes, I trust this folder' 를 고르게 하거나, 이미 신뢰한 폴더를 작업 폴더로 쓴다. 이 질문에 대신 답하지 않는다(보안 확인은 사람 몫).

이 `-p` 는 **아직 살아 있지 않은 새 세션을 만드는 1회성**이라 규칙 1 과 충돌하지 않는다. 이미 있는 워커(v1)에는 위 문장과 같은 **온보딩 메시지를 SendMessage 로 1회** 보낸다.

### 일감 배정

1. `POST …/tasks` 로 일감을 만든다 — **책임자(owner) 1명**, 협업자, 완료 조건, 파일 편집 범위(`write_scope`: `{"<별칭>":["경로 패턴"]}`). **같은 파일의 편집자는 한 명**만.
2. 책임자에게 보낸다(아래 「보내는 법」). 본문 첫 줄 `결론:`, 둘째 줄 `[cw] task=<task> type=ASSIGN ver=<assignment_ver>`, 그다음 요구사항·완료 조건·협업자.
3. 같은 워커에 여러 일감을 보내도 된다(순서대로 처리). 서로 의존하는 일감은 앞 것이 승인된 뒤 보낸다.

### 결과 처리

- 너에게 오는 것은 **4종뿐**이어야 한다: 책임자의 `RESULT`(1회) · 범위/우선순위 변경 요청 · 풀리지 않은 `BLOCKED` · 한도 도달. 워커끼리의 대화는 너에게 오지 않는다(관제 화면에는 보인다).
- `RESULT` 를 받으면 `GET …/tasks/<task>` 로 제출 증거를 보고 `accept` 또는 `reject`(`note` 에 사유). 반려면 `reopen` 또는 `reassign` 후 다시 보낸다.
- `due` 를 넣은 일감이 기한을 넘기면 Host 가 사용자 폰으로 알린다 — 그 알림을 기다리며 폴링하지 않는다.

## 워커 모드

1. 일감을 받으면 `GET /api/v1/team/<team>` 으로 팀원·별칭·자기 `write_scope`·일감의 `assignment_ver` 를 **한 번** 읽는다.
2. 일한다. **자기 `write_scope` 밖 파일은 고치지 않는다** — 필요하면 그 담당에게 `REVIEW`(수정 제안)로 보낸다.
3. 동료에게 직접 물어도 되는 것과 관리 세션에 올릴 것:

| 동료에게 직접 | 반드시 관리 세션으로 |
|---|---|
| 같은 일감 안의 사실 확인·인터페이스 질의 | 목표·요구사항·우선순위 변경 |
| 산출물 전달(파일 경로)·리뷰 요청·테스트 결과 공유 | 일감 재배정·새 워커·다른 일감에 영향 주는 인터페이스 변경 |
| 이미 합의된 인터페이스의 세부 조율 | `write_scope` 밖 수정 필요·결론 불일치·풀리지 않는 막힘 |

4. 끝나면 **책임자만** `POST …/tasks/<task>/submit`(`assignment_ver`·`artifacts`·`idem_key`) 후 관리 세션에 `RESULT` 1회. 협업자는 책임자에게만 답한다.
5. 막히면 `POST …/tasks/<task>/block`(`note`) + 관리 세션에 `BLOCKED` **1회**.

### 메시지 규약 (관리·워커 공통, 첫 두 줄)

```
결론: expires_at 단위가 초인지 밀리초인지 확인 부탁
[cw] task=PORTAL-T17 thread=T17-a type=ASK turn=1/4 reply=yes from=백엔드
- 배경: docs/auth-contract.md 에 단위 누락
- 필요한 답: 단위 + 근거 파일 경로
```

- `type` 은 `ASSIGN`(관리만)·`ASK`·`ANSWER`·`REVIEW`·`RESULT`·`BLOCKED`. 본문 5줄 이내, 긴 내용은 **파일 경로**.
- 답장은 같은 `task`/`thread`, `turn` +1. `reply=no` 에는 수신 확인을 보내지 않는다.
- `notify_when_idle:true` 는 답이 필요한 요청에만. **유휴 통지 ≠ 완료.**

### 폭주·루프 방지 (지켜야 하는 약속)

- **1-hop**: 받은 요청을 제3자에게 넘기지 않는다. 필요하면 `BLOCKED` 로 올린다.
- **스레드당 최대 4회 전송**(질문→답→보충→최종). 넘으면 책임자가 관리 세션에 `BLOCKED`.
- 같은 상대에게 같은 질문 재전송·무응답 재전송 금지. 답을 기다리는 동안 독립 작업을 계속한다.
- 할 일이 없어졌거나 서로 기다리는 순환이면 `BLOCKED` 1회. 침묵은 동의·완료가 아니다.

## 보내는 법 (관리·워커 공통)

```
1. 주소 읽기   GET http://127.0.0.1:5100/api/v1/team/<team>  →  members[].address (uds:…), live
               ※ 이름이 아니라 address 로 보낸다 — 이름은 동명이면 모호하고 재기동마다 바뀐다.
2. 안 살아 있으면(live=false, 관리 모드만)
               POST http://127.0.0.1:5100/api/sessions/<session_id>/terminal/start  body {"skip": true}
               → started | already_live (멱등). 실패: 409 cap / 409 bg_hold / 404 no_cwd → 사용자에게 알림.
               3초 간격으로 1단계를 최대 10회. 워커 모드면 기동하지 말고 관리 세션에 BLOCKED.
3. 보낸다      SendMessage(to=<address>, message=<규약대로>, notify_when_idle=<답 필요할 때만>)
4. 실패하면    ("success":false / not reachable) 1단계부터 한 번만 다시. 또 실패면 BLOCKED.
```

받은 메시지에 답할 때는 그 메시지의 `from` 값(`uds:…`)을 `to` 로 쓴다.

## 실패 처리

- 명부 503: 멈추고 사용자에게 보고(명부 없이 추측으로 보내지 않는다).
- 세션이 교체되면(대화가 커져 새 세션으로 이어짐) Host 가 continued-in 으로 알아채 명부·관제 그룹을 맞춘다. 직접 새 세션으로 바꿨으면 `…/members/<agent>/session` 으로 기록한다.
- ClewPath Host 가 업데이트로 재기동되면 워커 PTY 가 모두 종료된다 — 다음 전송 때 「보내는 법」 2단계가 되살린다.
- 같은 일감이 두 번 반려되면 멈추고 사용자에게 보고한다.

## 비용·수명

- 워커 호출 간격이 **1시간을 넘기면** 캐시가 만료돼 다음 호출 때 대화 전체를 다시 읽는 비용이 든다. 묶을 수 있는 일감은 묶는다.
- 워커 대화가 매우 커지면 **교체**: 인수인계 요약으로 새 세션을 만들고 `…/members/<agent>/session`(reason `replaced`)으로 붙인다 — **에이전트의 이력은 이어진다.** 옛 세션은 `POST /api/sessions/<session_id>/terminal/stop` 으로 내린다.

## 사람 개입

사용자는 ClewPath 관제 화면에서 팀 전체(관리↔워커, 워커↔워커 호출선)를 보고, 어느 세션이든 탭으로 열어 직접 말할 수 있다.
너는 그 사실을 몰라도 된다 — 규약만 지키면 된다.
