# pclink 프로토콜 약속 v0

> PC 사이 세션 통신(pclink)의 **선 위 약속**이다. 이 문서가 정본이고 코드(`session_manager/pclink/`)가 따른다.
> v0 의 범위는 **짝지은 두 Host 사이의 조회 전용 원격 묻기**다. 실시간 전달·작업 지시·다자간·다중 홉은 허브 프로젝트(별도)가 이 약속을 넓혀 만든다.

## 1. 세 층

| 층 | 맡는 것 | 모르는 것 |
|---|---|---|
| 전송·암호 (`pclink/protocol.py`, `pclink/link.py`) | 짝 채널(지금은 상대 Host 의 릴레이 방에 client 로 붙는 연결), 봉투 봉인·개봉, 만료, 중복 id 장부, 짝 키 | 봉투 본문의 뜻 |
| 정책 (`pclink/policy.py`) | 짝 목록, PC 등급 기기, 세션별 허용, 첫 연락 승인, 한도, 끄기 스위치, 감사 장부, 짝짓기 상태 | 어떻게 보내는지, Claude 를 어떻게 부르는지 |
| 실행 어댑터 (`webapi.ephemeral_ask(isolated=True)`) | 도구·MCP·훅이 없고 기록도 남기지 않는 복제본에 묻기 | 암호·짝 |

주소·권한·재시도는 Host 코드가 정한다. 모델은 정하지 않는다.

## 2. 주소

- **Host:** `host_id` = 16자 16진수. Host 데이터 폴더에 한 번 만들어 둔다(CP·릴레이 방과 무관하게 고정).
- **세션:** Claude 세션 UUID. 주소는 `host_id/session_id` 하나뿐이다. 이름·방·파이프 경로는 표시·연결용 매핑이지 주소가 아니다.
- **봉투 id:** UUID4. 같은 id 는 두 번 실행되지 않는다(4절 장부).

## 3. 봉투

```json
{"h": {"v": 0, "id": "8f0c…", "reply_to": null, "kind": "ask",
       "from": {"host": "a1b2c3d4e5f60718", "session": "5d1e…"},
       "to":   {"host": "0f1e2d3c4b5a6978", "session": "9401…"},
       "expires_at": 1792650120},
 "n": "<b64url 12바이트 nonce>",
 "c": "<b64url 암호문+태그>"}
```

- **머리 `h`** 는 평문이다(받는 쪽이 짝을 찾으려면 `from.host` 가 보여야 한다). 대신 머리 전체를 AEAD 의 인증 데이터로 묶어, 릴레이가 주소·종류·만료를 바꾸면 개봉이 실패한다.
- **암호:** AES-256-GCM, 키 = 짝 키(32바이트), nonce = 매번 새 12바이트 난수.
  인증 데이터 = `b"clewpath-pclink/0\n" + canonical(h)`, 평문 = `canonical(body)`.
  `canonical(x)` = `json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.
- 한 짝 키를 두 방향이 같이 쓴다. 방향은 머리의 `from`·`to` 가 묶는다(받는 쪽은 `from.host` = 그 짝 상대, `to.host` = 자기인지 확인 — 반사 공격 차단).
- `session` 은 Host 단위 글(`hello`·`list`·`unpair`·`pair_*`)과 그 답, `poll` 에서 `null` 일 수 있다. `ask` 의 `to.session` 만은 반드시 있다.

### 종류(kind)

| kind | 방향 | 본문 | 비고 |
|---|---|---|---|
| `hello` | 양쪽 | `{"pv": 0, "min_pv": 0, "features": ["ask","poll","list"], "host_ver": "…"}` | 짝 채널을 열 때 첫 글 |
| `list` | 묻는 쪽 → 받는 쪽 | `{}` | 받는 쪽은 「원격 묻기 받기」가 켜진 세션만 돌려준다 |
| `list_result` | 받는 쪽 → | `{"sessions": [{"id", "title", "updated_at"}]}` | |
| `ask` | 묻는 쪽 → | `{"prompt": "…"}` | 8,000자 상한 |
| `accepted` | 받는 쪽 → | `{"ask_id"}` | 접수만 뜻한다(답이 아님) |
| `poll` | 묻는 쪽 → | `{"ask_id"}` | 시간 초과 뒤 같은 묻기의 결과 다시 받기 |
| `answer` | 받는 쪽 → | `{"ask_id", "answer", "truncated", "duration_s"}` | 20,000자 상한. 묻는 쪽은 **믿지 않는 외부 데이터**로 표시 |
| `pending` | 받는 쪽 → | `{"ask_id", "state": "queued"\|"running"}` | `poll` 의 답 |
| `refused` | 받는 쪽 → | `{"ask_id"?, "code", "retry_after"?}` | 정책 거절(5절) |
| `error` | 받는 쪽 → | `{"ask_id"?, "code", "reason"}` | 실행 실패 |
| `pair_cred` | 양쪽(짝짓기 중) | `{"room", "relay", "cp", "cred": {"id", "secret"}}` | 6절 |
| `pair_done` | 초대한 쪽 → | `{}` | 6절 |
| `unpair` | 양쪽 | `{"reason"}` | 해제 알림 |

모르는 `v`·`kind` 는 거부한다. 새 종류는 `hello.features` 로 알리고, 상대가 모르면 보내지 않는다.

## 4. 받는 쪽 검사 순서 (모두 통과해야 처리)

1. `h.v == 0` — 아니면 `bad_version`.
2. `kind` 를 안다 — 아니면 `bad_kind`.
3. `from.host` 가 살아 있는 짝이고 그 짝의 기기 행이 활성, `to.host` 가 나 — 아니면 `not_paired`.
4. 만료: `now - 60 ≤ expires_at ≤ now + 600 + 60`(초, 두 PC 시계 차 60초 허용) — 아니면 `expired`. 보내는 쪽은 보통 `now + 120`.
5. 개봉(AEAD) — 실패면 `bad_seal`(이유를 더 말하지 않는다).
6. **중복 id 장부:** 처음 본 id 면 `expires_at + 60` 까지 기록(디스크, 재시작 뒤에도 남음)하고 처리. 본 적 있으면 다시 실행하지 않는다 — `ask` 면 그 묻기의 지금 상태(보관된 답·진행 중)를 돌려주고, 나머지는 버린다. **장부를 쓰지 못하면 거부**(fail-closed).

1~5 의 실패는 봉인하지 않은 짧은 오류(`pclink:<code>`)로만 답한다 — 아직 상대를 믿을 수 없으니 정책 정보를 싣지 않는다. 6 이후의 정책 결과는 봉인한 `refused` 로 답한다.

## 5. 묻기의 상태와 거절 코드

받는 쪽: `받음 → (거절 | 접수) → 대기 → 실행 → (답 | 실패) → 10분 보관 → 지움`.
묻는 쪽: `보냄 → 접수 → (답 | 시간 초과 + 요청 id → poll)`.

| 코드 | 뜻 |
|---|---|
| `not_allowed` | 그 세션은 「원격 묻기 받기」가 꺼져 있다(기본) |
| `pending_approval` | 첫 연락 승인 대기 — 받는 쪽 사장님이 폰에서 승인한 뒤 다시 묻는다 |
| `paused` | 받는 쪽 끄기 스위치가 켜져 있다 |
| `limit` | 짝당 분 6·일 200 또는 하루 어림 예산(20달러)을 넘었다(`retry_after` 초) |
| `too_big` | 그 세션의 마지막 압축 이후 분량이 묻기당 어림 상한(2달러 ≈ 1MB)을 넘는다 — 세션이 압축된 뒤 다시 |
| `busy` | 받는 쪽 Host 의 동시 실행 2·대기 4 가 찼다 |
| `too_long` | 묻기 8,000자 초과 |
| `ask_failed` | 복제본 실행 실패(세션 폴더 없음·claude 오류·180초 초과·도구 사용 감지) |
| `unknown_ask` | `poll` 의 묻기를 모른다(10분 지남·받는 쪽 재시작) |

묻는 쪽 Host 가 세션에게 돌려주는 코드는 위에 더해 `peer_offline`(상대 방에 Host 없음·CP/릴레이 불통), `timeout`(+요청 id), `peer_outdated`(상대가 pclink 를 모름·pv 낮음), `not_paired`, `bad_sender`(보낸 세션을 확인하지 못함·같은 세션 id 를 둘이 주장).

## 6. 짝짓기 (X25519, 한 번에 양쪽)

**초대 문자열**(초대하는 Host B 가 만든다, 1회용·24시간): `clewpath-pair:0:<b64url(JSON)>`

```json
{"v": 0, "inv": "<16자 16진수>", "k": "<b64url 32바이트 초대 비밀>",
 "host": "<B host_id>", "name": "<B 이름>",
 "relay": "wss://…/relay", "cp": "https://…", "room": "<B 방>",
 "cred": {"id": "<CP client 자격 id>", "secret": "<비밀>"},
 "exp": 1792736400}
```

`cred` 는 **짝짓기에만 쓰는** B 방 client 자격이다. 짝짓기가 끝나면 B 가 CP 에서 폐기한다. 초대 문자열에는 운영 키가 없다.

**키:**
- `K_mac = HKDF-SHA256(ikm=k, salt="", info="clewpath-pclink/0 invite-mac", 32)`
- `shared = X25519(내 일회용 비밀키, 상대 일회용 공개키)`
- `짝 키 = HKDF-SHA256(ikm=shared, salt=k, info="clewpath-pclink/0 pair-key\n" + host_B + "\n" + host_A + "\n" + epk_A + "\n" + epk_B, 32)` (epk 는 b64url 문자열)

**주고받기**(A 가 초대 자격으로 B 방에 client 로 붙은 뒤, 바깥 메서드 `pclink.pair`):

```
1. A→B  {"step":"init","inv","from":host_A,"name","epk":epk_A,"ts",
         "mac": HMAC(K_mac, "init|inv|host_B|host_A|epk_A|ts")}
2. B→A  {"step":"resp","epk":epk_B,"mac": HMAC(K_mac, "resp|inv|host_B|host_A|epk_A|epk_B"),
         "env": 봉투(kind=pair_cred, A 가 앞으로 B 방에 붙을 새 client 자격)}
3. A→B  봉투(kind=pair_cred, B 가 앞으로 A 방에 붙을 client 자격)      ← 바깥 메서드 pclink
4. B→A  봉투(kind=pair_done) — B 는 초대를 '씀'으로 적고 초대 자격을 CP 에서 폐기
```

- B 는 1 의 `ts` 가 ±300초 안이고 mac 이 맞고 초대가 살아 있을 때만 2 로 간다. 한 초대에 진행 중인 짝짓기는 하나(둘째 `init` 은 `busy`).
- A 는 2 의 mac 으로 상대가 초대 비밀을 아는 B 임을 확인한다(릴레이는 초대 비밀을 모른다).
- 3 이 120초 안에 오지 않으면 B 는 되돌린다(2 에서 준 자격 폐기, 짝 지움). 초대는 4 를 보내야 소모된다. A 도 4 를 60초 안에 못 받으면 자기 쪽을 되돌린다.
- 승인: B 는 초대를 만들 때, A 는 붙여 넣을 때 각자 폰 승인(승인 대기열 `pclink_pair`). 끝나면 양쪽 PC·폰에 알림.
- 각 Host 는 상대를 기기 목록에 **PC 등급**(`kind=pc`, 접속 토큰 없음)으로 적는다. PC 등급 기기는 바깥 `auth` 로 인증될 수 없고, 커넥터는 PC 에 `pclink`·`pclink.pair`·`ping` 만 처리한다. PC 에는 방 E2EE 키를 주지 않는다.

## 7. 바깥 프레임 (지금 릴레이 위에서)

```json
A→B  {"v":1,"type":"req","id":"k7","method":"pclink","params":{"env":{…봉투…}}}
B→A  {"v":1,"type":"res","id":"k7","cid":"…","ok":true,"data":{"env":{…accepted…}}}
B→A  {"v":1,"type":"res","id":"k7","cid":"…","ok":true,"data":{"env":{…answer…}}}
```

- 한 `req` 에 `res` 가 여러 번 올 수 있다(접수, 그다음 답). 묻는 쪽은 바깥 id 가 아니라 봉투의 `reply_to`·`ask_id` 로 맞춘다.
- 받는 쪽은 결과를 **그 cid 로만** 보낸다. 그 cid 가 떠났으면 버리고(방 전체에 뿌리지 않는다), 결과는 10분 보관해 `poll` 을 기다린다.
- 받는 쪽 커넥터는 `pclink` 요청을 받자마자 접수를 답하고 실행은 별도 작업으로 돌린다 — 폰 요청을 막지 않는다.
- 상대가 pclink 를 모르면 바깥 응답이 `unknown_method:pclink` 다 → `peer_outdated`.

## 8. 버전

- `pv` 는 pclink 봉투 규약 번호(이 문서 = 0). `hello` 로 서로의 `pv`·`min_pv`·`features` 를 알린다. 공통 `pv` 가 없으면 `peer_outdated`.
- 바뀌면 안 되는 것: 머리 필드 이름, 인증 데이터 앞머리 `clewpath-pclink/<pv>\n`, canonical 규칙. 새 필드는 본문에 더하고, 모르는 본문 필드는 무시한다.

## 9. 예제

묻기 머리와 본문(봉인 전):

```json
h    = {"v":0,"id":"3b1f0b0e-…","reply_to":null,"kind":"ask",
        "from":{"host":"a1b2c3d4e5f60718","session":"5d1e…"},
        "to":{"host":"0f1e2d3c4b5a6978","session":"9401…"},"expires_at":1792650120}
body = {"prompt":"이 세션에서 정한 배포 순서를 한 줄로"}
```

받는 쪽 답(봉인 전):

```json
h    = {"v":0,"id":"c2aa…","reply_to":"3b1f0b0e-…","kind":"answer",
        "from":{"host":"0f1e2d3c4b5a6978","session":"9401…"},
        "to":{"host":"a1b2c3d4e5f60718","session":"5d1e…"},"expires_at":1792650300}
body = {"ask_id":"3b1f0b0e-…","answer":"CP 재시작 → 릴레이 재시작 → healthz 확인",
        "truncated":false,"duration_s":7.3}
```

고정 시험 벡터(키·nonce 고정)는 `tests/test_pclink_protocol.py` 에 있다 — 다른 언어로 구현할 때 같은 바이트가 나와야 한다.
