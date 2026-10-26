# CP `/get.sh` 공개 절차 (맥·리눅스 한 줄 설치)

`curl -fsSL https://clewpath.pyongso.com/get.sh | sh` 를 여는 절차다. 근거는 `docs/designs/posix-install.md` 7절,
TODOS 의 「CP `/get.sh` 공개」 줄. **공개 시점은 맥 정식 출시 때, 사장님 확인 뒤다.** 운영 반영(CP 재시작·nginx)은
「서비스 모니터」가 한다(`/deploy-request`). 작업 세션은 합치기와 확인만 한다.

2026-10-26 기준 운영 상태: `https://clewpath.pyongso.com/get.sh` 404(nginx), `/cp/get.sh` 404(CP), `127.0.0.1:5200/get.sh` 404.

## 1. 준비된 것 (가지 `w0327/getsh`)

| 저장소 | 커밋 | 내용 | 언제 합치나 |
|---|---|---|---|
| 007 | `feat(get.sh 공개 준비): CP 에 /get.sh 라우트와 맥·리눅스 부트스트랩 사본` | `control_plane/get.sh`(006 `ops/get.sh` 바이트 사본), `/get.sh` 라우트, `.gitattributes`(`*.sh text eol=lf`), `tests/test_get_sh_route.py` | 공개 직전. 라우트는 CP 재시작 전에는 안 열린다 |
| 007 | `feat(get.sh 공개 준비): 랜딩에 OS 별 한 줄 설치(맥·리눅스 시험용) — 공개 때 합칠 것` | `landing.html` 맥·리눅스 줄(리눅스 '시험용'), 복사 버튼 | **`/get.sh` 확인 뒤**. 랜딩은 요청마다 디스크에서 읽혀 합치는 순간 보인다 |
| 006 | `feat(get.sh 공개 준비): 정본 ops/get.sh 와 007 사본 대조 시험·공개 절차 문서` | `tests/test_get_sh_copy.py`, 이 문서, README 폴더 구조 한 줄 | 언제든(007 에 get.sh 가 없으면 시험은 건너뛴다) |
| 006 | `feat(get.sh 공개 준비): README 맥·리눅스 한 줄 설치 문장과 문서 시험 — 공개 때 합칠 것` | README 한 줄 설치 문장, `tests/test_docs_multi_os.py` 의 '공개 주소 없음' 시험을 '공개 주소 있음'으로 | `/get.sh` 확인 뒤 |

정본은 006 `ops/get.sh` 다. 007 `control_plane/get.sh` 는 바이트 그대로의 사본이고, 006 `tests/test_get_sh_copy.py` 가
007 트리(`SM_E2E_007_DIR` → 006 옆 `007_clewpath_server`)를 찾아 대조한다. get.sh 를 고칠 때는 006 에서 고치고 007 로 복사한다.

## 2. 공개 순서

1. **정본·사본 대조** — 006 main 의 `ops/get.sh` 와 007 가지의 사본이 같은지(다르면 006 쪽을 복사해 007 가지에 커밋).
   ```sh
   sha256sum "F:/021_3_AX 기술스택/006_session_manager/ops/get.sh"
   git -C "F:/021_3_AX 기술스택/007_clewpath_server" show w0327/getsh:control_plane/get.sh | sha256sum   # 가지의 사본
   ```
2. **007 라우트 커밋 합치기** — 007 main(운영 CP 가 bind-mount 로 서빙하는 트리)의 `git status` 가 깨끗한지 보고 첫 커밋만
   합친다. 합친 뒤 007 main 에서 `python -m pytest -q -p no:cacheprovider tests/test_get_sh_route.py tests/test_get_bootstrap.py`.
   라우트는 CP 재시작 전에는 열리지 않으므로 이 단계는 운영에 보이지 않는다.
3. **서비스 모니터 의뢰**(아래 4절 문장) — `docker restart util-session-cp` + nginx `location = /get.sh` 추가·`nginx -t`·reload.
4. **확인**(아래 3절) — 받은 본문의 sha256 = 006 `ops/get.sh`.
5. **007 랜딩 커밋 합치기** — 재시작 없이 바로 보인다. `https://clewpath.pyongso.com/` 에서 세 줄·복사 버튼 확인.
6. **006 README 커밋 합치기** — 006 에서 `python -m pytest -q -p no:cacheprovider tests/test_get_sh.py tests/test_get_sh_copy.py tests/test_docs_multi_os.py`.
   README 는 다음 릴리스 zip 에 실린다. TODOS 의 「CP `/get.sh` 공개」 줄 체크는 그때 사장님 확인으로.

되돌리기: nginx 의 `location = /get.sh` 블록을 지우고 reload. 라우트는 남아도 무해하다(`/cp/get.sh` 는 CP 를 거쳐 계속 열려 있으니
완전히 닫으려면 007 라우트 커밋을 되돌리고 CP 재시작). 랜딩 커밋을 먼저 되돌린다.

### nginx 조각

`D:\docker\infra\nginx\default.conf` 의 `server_name clewpath.pyongso.com;` 블록, 기존 원라인 설치 블록
`location ~ ^/(get|install\.ps1)$ { … proxy_pass http://$cp_get:5200/get; … }` **바로 아래**에 더한다. 기존 블록은 고정 URI
`/get` 으로 넘기므로 손대지 않는다(정규식에 `get\.sh` 를 넣으면 get.ps1 이 나간다).

```nginx
    # 원라인 설치(맥·리눅스):  curl -fsSL https://clewpath.pyongso.com/get.sh | sh
    location = /get.sh {
        set $cp_getsh session-cp;
        proxy_pass http://$cp_getsh:5200/get.sh;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 15;
    }
```

`/cp/get.sh` 는 nginx 를 바꾸지 않아도 기존 `location /cp/` catch-all 이 `rewrite ^/cp/(.*)$ /$1` 로 CP 의 `/get.sh` 에 보낸다
(CP 재시작만으로 열린다).

## 3. 확인 명령

기대 해시는 그때의 006 정본에서 다시 계산한다(2026-10-26 가지 기준 `7f88d22ad12b5926e317fe59bf475eb8e4125d3d16964353dc65c0786adc3ff9`).

```sh
WANT=$(sha256sum "F:/021_3_AX 기술스택/006_session_manager/ops/get.sh" | cut -c1-64)

# CP 직접(재시작 뒤, nginx 전에도 된다)
curl -fsS http://127.0.0.1:5200/get.sh | sha256sum | cut -c1-64        # = $WANT

# 공개 주소 — 둘 다 같은 해시
for u in https://clewpath.pyongso.com/get.sh https://clewpath.pyongso.com/cp/get.sh; do
  printf '%s ' "$u"; curl -fsS "$u" | sha256sum | cut -c1-64
done                                                                     # = $WANT

curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' https://clewpath.pyongso.com/get.sh
                                                                         # 200 text/plain; charset=utf-8
curl -fsS https://clewpath.pyongso.com/get.sh | head -n 1               # #!/bin/sh
curl -fsS https://clewpath.pyongso.com/get.sh | od -c | head -n 2       # \r 이나 BOM(357 273 277) 없음
curl -fsS https://clewpath.pyongso.com/get.sh | tail -n 1               # main "$@"
curl -fsS https://clewpath.pyongso.com/get.sh | sh -n && echo 구문OK     # (sh 가 있는 곳에서)

# 기존 Windows 한 줄 설치 회귀 — 둘 다 200, get.ps1 본문(첫 줄이 #!/bin/sh 가 아님)
for u in https://clewpath.pyongso.com/get https://clewpath.pyongso.com/install.ps1; do
  printf '%s ' "$u"; curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "$u"
done
```

PowerShell 에서 해시만 볼 때:

```powershell
curl.exe -fsS https://clewpath.pyongso.com/get.sh -o "$env:TEMP\get.sh"
(Get-FileHash "$env:TEMP\get.sh" -Algorithm SHA256).Hash.ToLower()
(Get-FileHash "F:\021_3_AX 기술스택\006_session_manager\ops\get.sh" -Algorithm SHA256).Hash.ToLower()
```

실제 설치 확인은 맥 실기에서(`docs/mac-dev-loop.md`). 릴리스가 `platforms` 에 `mac` 을 담기 전에는 get.sh 가 '이 OS 용 릴리스가
없다'며 설치를 거부하는 것이 정상이다.

## 4. 서비스 모니터 의뢰 문장 (초안)

> ClewPath CP 에 맥·리눅스 한 줄 설치 주소 `/get.sh` 를 엽니다(사장님 확인 완료, 맥 정식 출시).
> 007 main 에 `/get.sh` 라우트·`control_plane/get.sh`·`.gitattributes` 를 합쳐 두었습니다(커밋 `<해시>`).
> 1) `docker restart util-session-cp` — 소스 bind-mount 라 restart 만으로 반영됩니다. recreate·rebuild 는 하지 마세요(DB 유실).
> 2) `D:\docker\infra\nginx\default.conf` 의 clewpath.pyongso.com 블록, `location ~ ^/(get|install\.ps1)$` 바로 아래에
>    006 `docs/deploy/get-sh-publish.md` 2절의 `location = /get.sh { … }` 조각을 더하고 `docker exec nginx nginx -t` 뒤 reload.
> 확인: `https://clewpath.pyongso.com/get.sh` 와 `/cp/get.sh` 의 sha256 이 006 `ops/get.sh` 와 같음(`<해시>`),
> content-type `text/plain; charset=utf-8`, 첫 줄 `#!/bin/sh`, CR·BOM 없음, 기존 `/get`·`/install.ps1` 200(3절 명령).
> 되돌리기: location 블록을 지우고 reload(라우트는 남아도 무해). 운영 대장에 기록 부탁드립니다.
