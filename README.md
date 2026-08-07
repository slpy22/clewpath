# ClewPath Host

로컬 PC의 모든 Claude Code 세션을 관리하는 GUI 도구. 뷰어를 넘어 **세션 생명주기 관리**(export/import/폴더변경/삭제)에 집중한다.

## 기능

| 기능 | 설명 |
|---|---|
| 목록 조회 | `~/.claude/projects/` 전체 세션을 프로젝트별로 그룹핑 |
| 뷰어 | 세션 대화 내용 렌더 (모르는 메시지 타입도 방어적 표시) |
| 검색 | 키워드(파일 내용) 검색 |
| 통계 | 크기/메시지 수/토큰 사용량 집계 |
| 삭제 | 연관 파일 포함 안전 삭제 (dry-run → 확인 → 실행) |
| 작업폴더 변경 | jsonl 내 cwd 일괄 치환 (원본 .bak 백업) |
| Export | 세션 + manifest + (선택)작업폴더를 .zip으로 (`.gitignore` 존중) |
| Import | 다른 PC의 export를 복원, cwd 새 경로로 재지정 |
| 재개 | `claude --resume` 명령 제공 / 새 터미널에서 실행 |

---

# 처음 시작하기 (사용자 매뉴얼)

## 이게 뭐 하는 물건인가

내 PC에서 하던 클로드 코드 작업을, **PC만 켜져 있으면** 밖에서 폰이나 다른 브라우저로
그대로 이어서 하게 해주는 도구다. 대화 내용은 원래대로 내 PC에만 있고, 밖으로는
암호화된 중계선 하나만 나간다.

## 1. 준비물

| 필요한 것 | 확인 방법 |
|---|---|
| Windows 10/11 PC | — (기본 내장 PowerShell 5.1 로 충분) |
| 클로드 코드를 이 PC에서 써본 적 | `~/.claude/projects/` 폴더가 있으면 됨 (설치 때 자동 확인) |

파이썬도, PowerShell 7 도, 내려받을 파일도 **없어도 된다** — 설치 명령 한 줄이
전부 알아서 챙긴다.

## 2. 설치 (5분)

**터미널을 열고**(⊞ Win 키 → "powershell" 입력 → 엔터) 아래 한 줄을 붙여넣는다:

```powershell
irm https://clewpath.pyongso.com/get | iex
```

이게 전부다. 내려받을 파일도, 풀 폴더도 고를 필요 없다 —
최신 버전을 받아 무결성(SHA-256)을 확인하고 `%LOCALAPPDATA%\ClewPath\app` 에 설치한다.
같은 명령을 다시 치면 재설치/업데이트가 된다(설정은 유지).

포트 등 옵션을 주고 싶으면 이 형태로:

```powershell
& ([scriptblock]::Create((irm https://clewpath.pyongso.com/get))) -Port 5123
```

<details>
<summary>zip 으로 설치 (오프라인·수동 방식)</summary>

운영자에게 `clewpath-host-<날짜>.zip` 을 받아 원하는 폴더에 풀고
(`Program Files` 같은 관리자 권한 폴더는 피한다), 그 폴더에서:

```powershell
powershell -File install.ps1
```
</details>

설치 스크립트가 순서대로:
파이썬 확보 → 클로드 폴더 확인 → 의존성 설치 → 설정 파일 생성 →
클라우드에서 내 PC 전용 접속 자격 자동 발급(**가입·로그인 없음**) → 서비스 기동 →
로그온 시 자동시작 등록.

끝나면 `http://127.0.0.1:5100/` 이 열리는지 확인한다. 이게 내 PC 전용 관리 화면이다.

> **설치가 중간에 멈추면** — 화면의 [FAIL] 메시지가 원인과 해법을 알려준다.
> 대표적인 경우: Windows 11 의 Smart App Control 이 서명 없는 파이썬을 차단하는
> 환경인데, 이때는 스크립트가 감지해서 서명된 파이썬(winget)으로 자동 복구한다.
> 자동 설치가 싫으면 `-NoWinget` 을 주고 직접 깔면 된다.

주요 옵션: `-Port 5100` · `-Reinstall`(venv 재생성) · `-NoAutoStart` · `-DryRun`(점검만)

## 3. 폰 연결 (1분)

1. **PC에서**: `http://127.0.0.1:5100/` → 우측 상단 **📱 (외부 접속 기기 관리)** → **＋ 새 기기 추가** → 기기 이름 입력
2. **폰에서**: 화면에 뜬 **QR 코드를 스캔**한다 → 브라우저가 열리며 자동으로 연결된다
3. 폰 브라우저에서 "홈 화면에 추가"를 하면 앱처럼 쓸 수 있다

QR 을 못 쓰는 상황(다른 PC 브라우저 등)이면 QR 아래 **페어링 링크 복사** 버튼으로
링크를 보내서 열어도 같다.

- 기기 토큰은 **이때 한 번만** 표시된다. 놓쳤으면 그 기기의 **🔄 (QR 재발급)** 을 누르면 된다 — 재발급하면 이전 QR/연결은 즉시 무효.
- 기기를 **삭제**하면 그 폰은 그 자리에서 접근이 끊긴다(이미 열려 있던 화면 포함).

## 4. 평소 사용

**폰에서**: 페어링해둔 주소를 열면 → 내 PC의 세션 목록이 보인다 → 세션을 고르면
대화 내용을 보고, **이어서 대화(재개)** 할 수 있다. 상단 **🖧 (PC 전환)** 으로 여러 PC를
페어링해뒀다면 골라가며 쓴다.

**PC에서**: `http://127.0.0.1:5100/` 은 비밀번호 없이 열린다(이 PC 안에서만 열리는
주소라서). 세션 조회·검색·내보내기/가져오기·삭제·기기 관리 전부 여기서.

**보안을 더 조이려면**: 🔒 (2차 인증) 에서 TOTP 를 켜면, 폰에서 민감한 조작을 할 때
인증앱(Google Authenticator 등) 6자리를 요구한다.

## 5. 서비스 관리

| 하고 싶은 것 | 방법 |
|---|---|
| 지금 켜기 | `Start-ScheduledTask ClewPathHost` |
| 지금 끄기 | `Get-Process python \| Where-Object { $_.Path -like "*<설치폴더>*" } \| Stop-Process` |
| 자동시작 끄기 | `Disable-ScheduledTask ClewPathHost` |
| 포트 등 설정 바꾸기 | `~/.claude/session_manager/clewpath.toml` 수정 후 재시작 (아래 "설정 파일" 절) |
| 실제 열린 주소 확인 | `~/.claude/session_manager/runtime.json` |
| 로그 보기 | `~/.claude/session_manager/host.log` (오류는 `host.err.log`) |

서비스는 항상 **창 없이(백그라운드)** 돈다. `start-connector.ps1` 을 직접 실행해도
창 없이 띄우고 스크립트는 바로 끝난다 — 콘솔창이 계속 떠 있다면 0.3.3 이하 버전이니
설치 한 줄을 다시 실행하면 된다.

## 6. 업데이트

하루에 한 번 새 버전이 있는지 자동으로 확인한다. 새 버전이 있으면 로컬 웹 헤더의
**버전 배지가 `v0.3.8 → 0.3.9` 로 바뀌고**, 클릭하면 **"지금 업데이트"** 버튼이 뜬다.

- **재기동 시 자동 적용**(기본 ON): 서비스를 껐다 켜면 새 버전이 있을 때 알아서 적용된다
  (`[update] apply_on_restart`). 재기동은 명시적 행동이라 안전하다고 보고 켜뒀다.
- **즉시 자동 적용**(기본 OFF): `[update] auto_apply = true` 로 두면 재기동을 기다리지 않고
  확인 즉시 설치·재시작한다.
- 어느 쪽이든 **검증(서명·해시) 통과분만** 적용되고, 실패하면 이전 버전으로 자동 롤백된다
  (방금 실패한 버전은 재기동 때 다시 시도하지 않는다). 설정·기기·자격증명은 그대로 유지.

> PowerShell 에서 설치 명령을 다시 쳐도 업데이트가 된다: `irm https://clewpath.pyongso.com/get | iex`

## 7. 문제가 생기면

| 증상 | 원인/해법 |
|---|---|
| 세션이 0개로 보임 | 클로드 폴더를 옮겨 쓰는 경우. `$env:CLAUDE_CONFIG_DIR` 설정 후 재설치, 또는 설정 파일 `[claude] config_dir` |
| 127.0.0.1:5100 이 안 열림 | 다른 프로그램이 5100을 쓰면 자동으로 5101~5110 으로 옮겨 뜬다 → `runtime.json` 에서 실제 주소 확인 |
| 폰이 "인증 필요"라며 끊김 | PC에서 그 기기가 삭제/재발급된 것. 📱 → 🔄 로 새 QR 을 만들어 다시 스캔 |
| 폰은 되는데 재개가 안 됨 | PC에 `claude` CLI 가 없거나 경로 문제. 로컬 웹의 진단(diagnostics)에서 `claude_exe_found` 확인 |
| 설치 직후 HTTPS 오류 | Smart App Control 환경 → 위 설치 절 참고 |
| 설치 중 "자동시작 등록 실패: Access is denied" | 0.3.1 이하의 버그(관리자 권한 없이 설치한 경우). 설치 한 줄을 그대로 다시 치면 고쳐진 버전이 설치되며 자동시작까지 등록된다 |

## 8. 제거

```powershell
# 자동시작 해제 (설치 환경에 따라 둘 중 하나만 실제로 존재 — 둘 다 실행해도 무해)
Unregister-ScheduledTask ClewPathHost -Confirm:$false -ErrorAction SilentlyContinue
Remove-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name ClewPathHost -ErrorAction SilentlyContinue
# 설치 폴더 삭제 (기본 %LOCALAPPDATA%\ClewPath\app)
# 부속 데이터까지 지우려면:
#   %LOCALAPPDATA%\ClewPath\        (uv 실행파일)
#   ~/.claude/session_manager\      (설정·기기·라벨 — 지우면 페어링 전부 무효)
```

클로드 세션 원본(`~/.claude/projects/`)은 이 도구가 소유하지 않으므로 어떤 경우에도 남는다.

## 9. 라이선스

**MIT** — 오픈소스다. 자유롭게 쓰고, 고치고, 재배포할 수 있다. 전문은 `LICENSE`.
(내 데이터(세션·설정)는 애초에 내 PC 로컬 파일이라 라이선스와 무관하게 내 것이다.)

---

# 상세 레퍼런스

## 폴더 구조

```
006_session_manager/               ← PUBLIC 저장소 (오픈소스·Host)
├─ session_manager/   제품 코드 — Host (로컬 웹 + 커넥터 + 업데이터)     [사용자 배포]
├─ pwa/               제품 코드 — 폰·웹 앱 (단일 html)                   [사용자 배포]
├─ install.ps1        사용자 설치 스크립트                                [사용자 배포]
├─ pyproject.toml · uv.lock · README.md · LICENSE                        [사용자 배포]
├─ tests/             테스트 (배포 안 됨, Host 전용)
├─ docs/              Host 설계·API·MCP 문서 (배포 안 됨)
└─ ops/               ★운영자 전용 — 배포 zip 에 절대 안 들어감
   ├─ start-public.ps1     운영자 PC 의 006 기동 (라이브 비밀 포함)
   ├─ package-for-user.ps1 배포 zip 생성 (비밀·키 스캔 게이트)
   ├─ tools/sign_release.py  릴리스 서명·게시
   ├─ release-keys/        ★릴리스 서명 개인키 (유출=전 사용자 장악)
   ├─ dist/                패키징 산출물 (zip·manifest)
   └─ logs/                운영자 006 로그 (server.log)

../007_clewpath_server/            ← PRIVATE 저장소 (서버 인프라)
├─ control_plane/  relay/          Cloud CP + blind 릴레이
├─ tests/  docs/  ops/deploy/      서버 테스트·설계문서·사업기획서·배포 스크립트
└─ pwa/ → 006/pwa 정션             릴레이가 서빙하는 PWA(단일 소스는 006)
```

경계 규칙: **Host 제품 폴더는 위치를 바꾸지 않는다** — 배포 zip 화이트리스트,
업데이터 PAYLOAD, `uv sync` 가 이 경로를 그대로 참조한다. 서버(control_plane·relay)는
별도 private 저장소 `clewpath-server`(`../007_clewpath_server`)로 분리됐고, 운영 도커는
그쪽을 bind-mount 한다. `pwa` 만 006 이 단일 소스(릴레이가 정션으로 참조).
운영 자산은 전부 `ops/` 아래로 — 새 운영 스크립트·비밀·산출물은 여기에 추가한다.

## 설정 파일 (자세히)

포트·주소는 **데이터 폴더의 설정 파일**에서 바꾼다. 재설치·업데이트해도 지워지지 않는다.

```
~/.claude/session_manager/clewpath.toml
```

```toml
[server]
host = "127.0.0.1"
port = 5100
port_fallback = 10   # 포트가 이미 쓰이면 5101..5110 을 차례로 시도 (0 이면 시도 안 함)

[cloud]
cp_url    = "https://clewpath.pyongso.com/cp"
relay_url = "wss://clewpath.pyongso.com/relay/ws"
app_url   = "https://clewpath.pyongso.com/relay/app"
```

- 우선순위는 **환경변수 > 설정 파일 > 기본값**. 개발용 `start-public.ps1` 과 도커는 env 로 돌기 때문에 그쪽이 항상 이긴다.
- 파일이 깨져 있으면 경고만 찍고 기본값으로 뜬다 — 배경 프로세스라 파싱 에러로 죽으면 원인을 알 수가 없다.
- 폴백이 일어나 다른 포트로 열렸다면 실제 주소가 같은 폴더 `runtime.json` 에 적힌다.
- 이미 ClewPath 가 떠 있는 포트면 **옆 포트로 비키지 않고 기동을 중단**한다(두 인스턴스가 같은 세션 폴더를 만지는 사고 방지).

## 자동 업데이트 (내부 동작)

기본은 **하루 한 번 확인만** 하고 설치는 사용자가 누를 때 한다(`[update] auto_apply = true` 로 바꾸면 자동 설치).

```
확인 → 서명 검증 → 다운로드 → SHA-256 대조 → 스테이징
     → (별도 프로세스) 본체 종료 대기 → 백업 → 교체 → uv sync
     → 재기동 → 헬스체크 → 실패하면 롤백
```

- **매니페스트는 오프라인 릴리스 키로 서명**된 EdDSA JWT다. 공개키는 `session_manager/updater.py` 의 `RELEASE_KEYS` 에 **박혀 있다**(네트워크로 받지 않는다).
- 릴리스 키는 **CP 의 JWKS 키가 아니다.** CP 개인키는 CP 자신의 DB에 살기 때문에, 그 키로 서명하면 "CP가 뚫려도 코드는 못 민다"가 성립하지 않는다. 릴리스 개인키는 서버에 올라가지 않는다.
- 교체는 설치 폴더 **밖**에서 도는 별도 프로세스가 한다(실행 중인 파이썬은 자기 코드를 못 덮어쓴다).
- 갈아끼우는 것: `session_manager/ pwa/ pyproject.toml uv.lock install.ps1 README.md` (서버 전용 코드는 배포되지 않음)
  **남는 것**: `.venv`, 그리고 `~/.claude/session_manager/` 전체(설정·기기·자격증명).
- 실패하면 백업으로 되돌리고 **파일뿐 아니라 설치 메타데이터까지** 복원한다. 안 그러면 코드는 구버전인데 자기를 신버전이라 보고하며 다시는 업데이트를 안 받는다.
- 적용은 **로컬 전용**이다. 폰에서 원격으로 코드 교체를 시킬 수 있으면 기기 토큰 하나가 새는 순간 임의 코드 실행이 된다.

## 릴리스 내보내기 (운영자)

```powershell
# 최초 1회 — 키는 ops/release-keys/ 에 만들어진다. 서버에 절대 올리지 않는다.
python ops/tools/sign_release.py keygen
#   출력된 kid/공개키 줄을 updater.py 의 RELEASE_KEYS 에 추가

pwsh -File ops/package-for-user.ps1                   # ops/dist/clewpath-host-<날짜>.zip
python ops/tools/sign_release.py sign `
    --version 0.4.0 --artifact ops/dist/clewpath-host-20260804.zip `
    --url https://clewpath.pyongso.com/cp/updates/download/0.4.0 --out ops/dist/manifest-0.4.0.jwt
python ops/tools/sign_release.py verify --manifest ops/dist/manifest-0.4.0.jwt   # 배포본이 받아들이는지
python ops/tools/sign_release.py publish --cp https://clewpath.pyongso.com/cp `
    --admin-token <SM_CP_ADMIN_TOKEN> --manifest ops/dist/manifest-0.4.0.jwt `
    --artifact ops/dist/clewpath-host-20260804.zip
```

나쁜 릴리스를 내릴 때: `POST /admin/updates/<버전>/withdraw` (아직 안 받은 사람은 더 이상 못 받는다).

게시(publish)하면 세 경로가 한꺼번에 갱신된다 — **원라인 설치**(`/cp/get` 이 최신
릴리스를 받음), **자동 업데이트**(`/updates/manifest`), 수동 zip 전달. 별도 작업 없음.

## 서명 개인키 (운영자)

`ops/release-keys/private.pem` — **이게 새면 모든 사용자 PC 에서 임의 코드를 실행시킬 수 있고,
잃어버리면 기존 사용자에게 업데이트를 밀 수 없습니다.** 오프라인 백업(USB)을 하나 만들어 두세요.
클라우드 동기화 폴더에는 두지 마세요.

저장소 안에 두는 대신 새어나갈 두 경로를 하드 게이트로 막아뒀습니다:

| 경로 | 장치 |
|---|---|
| 사용자 배포 zip | 화이트리스트에 없음 + `excludePatterns` + 스테이지에서 키 파일 발견 시 **패키징 중단** |
| 소스 저장소 | `.gitignore` 의 `release-keys/`, `*.pem` |
| 회귀 | `tests/test_release_key_safety.py` — 위 장치들과 "공개키가 개인키와 짝인지"까지 검사 |

자세한 건 `ops/release-keys/README.md`.

## 실행 (개발)

```bash
cd 006_session_manager
uv sync --extra dev        # 서버·테스트 의존성까지 (uv.lock 고정)
python -m session_manager.server
```

## 설계 원칙

1. **원본 보존**: 원본 jsonl을 손상시키지 않는다. 파괴적 작업은 dry-run 먼저, cwd 변경은 .bak 백업.
2. **방어적 파싱**: JSONL 스키마를 가정하지 않는다. 알려진 핵심 필드만 읽고, 모르는 필드/타입은 그대로 보존.
3. **포터빌리티**: export/import로 세션을 다른 PC로 완전 이전 (작업폴더 번들 포함, cwd 자동 재지정).

## 아키텍처

```
session_manager/
├── config.py       경로 설정 (~/.claude, 환경변수 재정의 가능)
├── pathenc.py      경로 ↔ 폴더명 인코딩
├── scanner.py      세션 스캔 + 메타 추출 (스트리밍, 방어적)
├── viewer.py       jsonl → 메시지 렌더
├── search.py       키워드/날짜 검색
├── stats.py        토큰/크기 통계
├── lifecycle.py    삭제 / cwd 변경
├── exporter.py     .zip export (작업폴더 .gitignore 존중)
├── importer.py     .zip import (cwd 재지정)
├── resume.py       claude --resume 연동
├── server.py       FastAPI + 정적 UI 서빙
└── static/index.html   단일 페이지 UI
```

## 테스트

```bash
python -m pytest tests/ -v
```

fixture 기반(실제 `~/.claude` 안 건드림). scanner/lifecycle/export-import 17개 테스트.

## 주의

- Claude Code의 JSONL 포맷은 비공식이며 버전마다 바뀔 수 있다. 방어적으로 작성했으나, 포맷 변경 시 일부 메타가 누락될 수 있다(원본은 안전).
- import 시 작업폴더 경로는 새 PC 기준으로 재지정된다. `claude --resume`은 해당 경로에서 실행해야 한다.
