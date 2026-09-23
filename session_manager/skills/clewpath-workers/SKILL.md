---
name: clewpath-workers
description: 상위(관리) 세션이 하위 워커 세션들에 일감을 오래 지속적으로 나눠 주고 결과를 회수하는 운영 규칙. ClewPath Host(127.0.0.1:5100)로 워커 세션을 '살려 두고', 통신은 claude 표준 SendMessage 만 쓴다. "워커에게 시켜", "하위 세션에 분배", "일감 나눠서", "clewpath workers" 라고 하면 호출.
---

# clewpath-workers — 하위 워커 세션 분배·관리

너는 **관리 세션**이다. 워커 세션들에 일감을 주고 결과를 받아 다음 일감을 정한다.
전송 경로는 **`SendMessage` 하나**다. ClewPath 는 워커를 **살려 두는 일**(화면 없는 터미널 기동)만 한다.

## 절대 규칙 (어기면 사고)

1. **살아 있는 세션에 `claude -p --resume` 을 하지 않는다.** 프로세스가 2개면 대화가 두 갈래로 갈라진다.
   워커에게 말을 걸 때는 항상 아래 "배분 절차"를 따른다.
2. **세션 하나는 한 방식만.** 이 스킬로 다루는 워커에는 `-p --resume` 을 쓰지 않고, 기존에 `-p --resume` 으로
   부리던 하위 세션은 이 스킬의 워커로 등록하지 않는다.
3. **권한 모드를 맞춘다.** 워커와 관리 세션의 권한 모드가 다르면 메시지가 사용자 승인 대기에 걸린다.
   기본은 둘 다 자동 허용(`--dangerously-skip-permissions`; start API 의 `skip:true`).
4. **폴링 금지.** `ListAgents` 를 반복 호출하거나 "끝났어?" 를 보내지 않는다. 완료는 워커의 답장 또는
   `notify_when_idle` 통지로 안다.
5. 워커 jsonl·`~/.claude` 아래 파일을 직접 쓰지 않는다. 상태는 프로젝트의 등록부 파일에만 적는다.

## 워커 등록부

프로젝트 루트 `.clewpath/workers.json` (없으면 만든다):

```json
{
  "workers": {
    "포털개발": {"session_id": "<uuid>", "role": "포털 프론트 개발", "cwd": "F:/proj/portal",
                "last_dispatch": "2026-09-23T10:00:00+09:00", "state": "idle"}
  },
  "monitor_group_id": "<ClewPath 관제 그룹 id 또는 null>"
}
```

`state` 는 관리 세션이 기록하는 참고값(`idle|working|failed`)이고, 실제 생존 여부는 매번 아래 1단계로 확인한다.

## 새 워커 만들기 (워커당 1회)

작업 폴더에서 헤드리스로 세션을 하나 만들어 UUID 를 얻고 등록부에 적는다:

```bash
cd <워커 작업 폴더> && claude -p --output-format json "너는 '<역할>' 워커다. 관리 세션이 보내는 일감을 처리하고, 결과는 요약 5줄과 산출물 경로로 답한다. 지금은 'ready' 라고만 답하라."
# 응답 JSON 의 session_id 를 등록부에 기록
```

이 `-p` 는 **아직 살아 있지 않은 새 세션을 만드는 1회성**이라 규칙 1 과 충돌하지 않는다. 이후 그 세션에는 `-p` 를 다시 쓰지 않는다.

## 배분 절차 (일감마다)

```
1. 살아 있나?   GET http://127.0.0.1:5100/api/v1/sessions  →  해당 session_id 의 peer 필드
                (peer 가 있으면 살아 있음. name 이 SendMessage 의 주소다. status idle|busy)
                또는 ListAgents 에 그 이름이 있으면 살아 있음
2. 아니면 기동   POST http://127.0.0.1:5100/api/sessions/<session_id>/terminal/start  body {"skip": true}
                → {"status":"started"|"already_live","pid":…}   (멱등 — 두 번 불러도 프로세스 1개)
                실패: 409 cap(터미널 상한) / 409 bg_hold(백그라운드 에이전트 점유) / 404 no_cwd
3. 뜰 때까지     3초 간격으로 1단계를 최대 10회 (레지스트리에 이름이 나타나면 됨)
4. 보낸다        SendMessage(to=<peer.name>, message=<일감>, notify_when_idle=true)
                ※ 이름은 **매번 1단계에서 새로 읽는다** — 제목이 없는 세션의 파생 이름(예: worker2-7f)은
                  프로세스를 다시 띄울 때마다 바뀐다(실측). 등록부에는 UUID 만 믿는다.
                첫 줄에 일감 제목, 본문에 요구사항·산출물 위치·완료 시 답장 형식을 쓴다.
5. 기록          등록부의 last_dispatch, state=working
```

**같은 워커에 여러 일감**을 보내도 된다 — 워커의 턴 루프가 순서대로 처리한다(큐잉 실측). 단 서로 의존하는 일감은
앞 것의 답장을 받은 뒤 보낸다.

## 결과 회수

- 워커의 답장은 `<cross-session-message from-name="<워커 이름>">` 로 관리 세션 대화에 자동 도착한다.
  워커에게 답장 형식을 강제한다: **첫 줄 결론, 요약 5줄 이내, 산출물은 파일 경로**. 긴 결과를 메시지에 넣게 하지 않는다
  (관리 세션 컨텍스트가 그만큼 찬다). 필요하면 경로의 파일을 읽는다.
- `notify_when_idle:true` 로 보냈으면 워커가 유휴가 될 때 `[Cross-session idle notice]` 가 한 번 온다.
- 답장이 없이 유휴 통지만 왔으면 워커에게 "결과를 형식대로 답하라" 고 한 번 더 보낸다.

## 실패 처리

- `SendMessage` 결과가 `"success":false`(예: `No agent named … is reachable`) → 워커가 죽은 것. 2단계(기동)부터 다시.
  ClewPath 관제 그룹에 등록돼 있으면 이 실패는 폰 알림("호출 실패")으로도 온다.
- 기동이 409 `cap` 이면 다른 워커를 하나 종료(`POST …/terminal/stop`)하거나 사용자에게 알린다. `bg_hold` 면 사용자에게 알린다.
- ClewPath Host 가 업데이트로 재기동되면 워커 PTY 가 모두 종료된다 — 다음 배분 때 1·2단계가 자연히 되살린다.
- 같은 일감이 두 번 실패하면 멈추고 사용자에게 보고한다.

## 비용·수명

- 워커 호출 간격이 **1시간을 넘기면** 그 워커의 캐시가 만료돼 다음 호출 때 대화 전체를 다시 읽는 비용이 든다.
  묶어서 보낼 수 있는 일감은 묶고, 오래 쉴 워커는 그냥 두되 비용을 알고 있어라.
- 워커 대화가 매우 커지면(수천 메시지) **교체**한다: 인수인계 요약을 프롬프트로 새 워커를 만들고 등록부의 UUID 를 바꾼다.
  옛 워커는 `POST …/terminal/stop` 으로 내린다.

## ClewPath 관제 연동 (권장)

등록부의 워커들을 관제 그룹으로 저장하면 사용자가 폰에서 타임라인·응답 완료·호출 실패 알림을 받는다:

```
POST http://127.0.0.1:5100/api/owner/monitor/groups
{"name":"<프로젝트명>","manager":"<관리 세션 uuid>","subs":["<워커 uuid>",…],"labels":{"<uuid>":"<이름>"}}
→ 응답 id 를 등록부 monitor_group_id 에 기록. 워커 교체 시 같은 body 에 "id" 를 넣어 갱신
```

관리 세션 자신의 UUID 는 `~/.claude/sessions/*.json` 중 자기 pid 항목의 `sessionId`, 또는 사용자에게 묻는다.

## 사람 개입

사용자가 워커에 직접 말하고 싶으면 ClewPath 에서 그 세션을 탭으로 열어 타이핑한다(살아 있는 PTY 에 화면이 붙는다).
너는 그 사실을 몰라도 된다 — 답장 형식만 지키게 하면 된다.
