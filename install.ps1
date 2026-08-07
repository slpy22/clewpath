# =====================================================================
#  ClewPath Host — 설치 스크립트 (Windows / PowerShell 5.1+)
#  내 PC의 클로드 세션으로 돌아가는 길.
#
#  ⚠ 이 파일은 Windows 기본 셸(PowerShell 5.1)에서도 돌아야 한다:
#    - PS7 전용 문법 금지: ?.  ??  삼항(a ? b : c)  체이닝(&& ||)
#    - 파일 저장은 UTF-8 **BOM 포함** — 5.1 은 BOM 없는 UTF-8 을 ANSI 로 읽어
#      한글 주석·문자열이 전부 깨진다 (tests/test_ps51_compat.py 가 검사)
#
#  이 스크립트 하나로:
#    1) uv 확보 (없으면 고정 버전 내려받아 SHA-256 대조)
#    2) 파이썬 확보 (uv 가 전용 런타임을 받아온다 — 시스템 파이썬 불필요)
#    3) 클로드 데이터 폴더 점검
#    4) 의존성 설치 (전용 .venv)
#    5) 설정 파일 생성 + 컨트롤플레인 자격증명 발급 (가입 없음)
#    6) 기동 + 자동시작 등록 (작업 스케줄러)
#
#  왜 uv 인가:
#    예전 버전은 "시스템에 Python 3.11+ 를 먼저 깔아라"가 전제였다. 클로드 코드는
#    그런 걸 안 물어보는데 우리만 물어보면 첫 화면에서 사람이 떨어져 나간다.
#    uv 는 단일 실행파일이고 파이썬 런타임까지 자기가 받아온다 → 사전 준비 0.
#    (PyInstaller 로 묶는 길도 있었지만 백신 오탐 + 코드서명 비용 때문에 접었다)
#
#  ⚠ Smart App Control (Windows 11) 주의 — 실측으로 걸린 문제:
#    uv 가 받아오는 파이썬은 python-build-standalone 빌드이고 **서명이 없다**.
#    Win11 클린 설치 기본값인 Smart App Control 이 켜져 있으면 그 안의 _ssl.pyd 가
#    차단되고, 그러면 HTTPS 가 통째로 죽어서 컨트롤플레인·릴레이에 아예 못 붙는다.
#    증상이 "ImportError: DLL load failed while importing _ssl" 하나뿐이라 원인을
#    찾기가 매우 어렵다. 그래서 이 스크립트는:
#      1) 서명된 시스템 파이썬을 먼저 찾고(python.org 빌드는 PSF 서명이라 통과)
#      2) 없을 때만 uv 관리 런타임을 쓰고
#      3) 어느 쪽이든 ssl/ctypes/sqlite3 를 실제로 import 해보고(스모크 테스트)
#      4) 막히면 winget 으로 서명된 python.org 빌드를 깔아 자동 복구한다.
#
#  사용법:
#    pwsh -File install.ps1
#    pwsh -File install.ps1 -ClientToken "<운영자에게 받은 폰 접속 토큰>"
#
#  옵션:
#    -CpUrl        컨트롤플레인 주소 (기본 https://clewpath.pyongso.com/cp)
#    -RelayUrl     릴레이 WS 주소   (기본 wss://clewpath.pyongso.com/relay/ws)
#    -AppUrl       폰에서 열 앱 주소 (기본 https://clewpath.pyongso.com/relay/app)
#    -ClientToken  폰 접속용 공유 토큰(페어링 QR 생성용)
#    -AgentToken   (선택/레거시) 릴레이 공유 agent 토큰. 지금은 CP 발급 JWT 를 쓰므로 불필요
#    -Port         로컬 웹 포트 (기본 5100)
#    -Python       파이썬 버전 (기본 3.11)
#    -UvVersion    uv 버전 고정 (기본 아래 $UvDefault)
#    -SystemUv     PATH 의 uv 를 그대로 쓴다(버전·해시 검증 건너뜀)
#    -NoWinget     파이썬이 막혔을 때 winget 자동 설치를 시도하지 않는다
#    -Reinstall    기존 .venv 를 지우고 새로 만든다
#    -NoAutoStart  자동시작 등록 건너뜀
#    -DryRun       아무것도 바꾸지 않고 점검만
#
#  주의: -AgentToken / -ClientToken 은 서비스 운영자에게 받는 값입니다.
#        스크립트에 하드코딩하지 않습니다(유출 방지).
# =====================================================================
[CmdletBinding()]
param(
    [string]$AgentToken  = "",
    [string]$ClientToken = "",
    [string]$CpUrl       = "https://clewpath.pyongso.com/cp",
    [string]$RelayUrl    = "wss://clewpath.pyongso.com/relay/ws",
    [string]$AppUrl      = "https://clewpath.pyongso.com/relay/app",
    [int]   $Port        = 5100,
    [string]$Python      = "3.11",
    [string]$UvVersion   = "",
    [switch]$SystemUv,
    [switch]$NoWinget,
    [switch]$Reinstall,
    [switch]$NoAutoStart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

# PowerShell 5.1 의 .NET 은 기본 프로토콜에 TLS 1.2 가 빠져 있을 수 있다.
# 그 상태로 GitHub(uv 다운로드)에 붙으면 "요청이 중단되었습니다" 로만 죽어서
# 원인을 알 수 없다. PS7 에선 이 줄이 그냥 무해한 no-op 이다.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

# 회사 프록시(HTTPS 가로채기) 환경 대응. uv 는 기본으로 자체 번들 인증서만 믿어서
# 사내 루트 CA 로 재서명된 PyPI 응답을 'invalid peer certificate' 로 거부한다(실측).
# 이 env 를 켜면 uv 가 OS(Windows) 인증서 저장소를 쓴다 — 일반 PC 에도 무해.
if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = "1" }

function Say  ($m) { Write-Host $m }

$script:LastNativeOut = @()
function Invoke-Native([scriptblock]$sb) {
    # 네이티브 명령(uv/winget) 전용 래퍼. PS 5.1 은 EAP=Stop 상태에서 2>&1 로
    # 받은 stderr 를 ErrorRecord 로 승격시켜 **정상 진행 로그 한 줄에도 죽는다**
    # (uv 는 진행 상황을 stderr 로 쓴다 — 실측으로 uv sync 에서 터졌다).
    # 성공/실패 판단은 밖에서 $LASTEXITCODE 로만 한다.
    # 출력은 Verbose 로 숨기되 $script:LastNativeOut 에 담아둔다 — 실패 시
    # 호출부가 이 버퍼를 찍어 **진짜 uv 오류를 사용자에게 보여줄 수 있게** 한다.
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $script:LastNativeOut = @(& $sb 2>&1 | ForEach-Object { Write-Verbose ("{0}" -f $_); "$_" }) }
    finally { $ErrorActionPreference = $old }
}
function Show-LastNative($title) {
    if ($script:LastNativeOut -and $script:LastNativeOut.Count) {
        Say "  -- $title --"
        $script:LastNativeOut | Select-Object -Last 25 |
            ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
}
function Ok   ($m) { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Fail ($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }

# ── uv 고정 버전 + 배포본 해시 ────────────────────────────────
#  해시를 소스에 박아두는 이유: 다운로드 URL 과 해시 파일이 같은 서버에 있으면
#  그 서버가 뚫렸을 때 둘 다 바뀐다. 여기 박아두면 zip 이 바뀐 걸 우리가 안다.
#  버전 올릴 때: https://github.com/astral-sh/uv/releases/download/<ver>/<파일>.sha256
$UvDefault = "0.12.1"
$UvSha256 = @{
    "0.12.1|x86_64-pc-windows-msvc"  = "8fcb0cb46e1229065e344758980924e569bef5882ef45f46fada8fb24e06b74a"
    "0.12.1|aarch64-pc-windows-msvc" = "9bc7c18e616230fa2dc6fb24bc3afde18a95c2b5c9433de747e9502c66041568"
}
if (-not $UvVersion) { $UvVersion = $UvDefault }

# uv 는 설치 폴더 밖(LOCALAPPDATA)에 둔다 — 폴더를 옮기거나 재설치해도 살아남고,
# 관리자 권한이나 PATH 오염이 없다. 지우려면 폴더째 삭제.
$UvHome = Join-Path $env:LOCALAPPDATA "ClewPath\bin"
$UvExe  = Join-Path $UvHome "uv.exe"

function Get-UvTarget {
    switch ($env:PROCESSOR_ARCHITECTURE) {
        "ARM64" { return "aarch64-pc-windows-msvc" }
        default { return "x86_64-pc-windows-msvc" }
    }
}

function Install-Uv {
    # 고정 버전 uv 를 내려받아 해시를 대조하고 $UvHome 에 푼다. 경로를 돌려준다.
    $target = Get-UvTarget
    $zipUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$target.zip"
    $tmp    = Join-Path ([IO.Path]::GetTempPath()) "clewpath-uv-$UvVersion-$PID"
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp "uv.zip"

    Say "         내려받는 중: uv $UvVersion ($target)"
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing -TimeoutSec 180
    } catch {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        throw "uv 다운로드 실패: $($_.Exception.Message)"
    }

    $actual   = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    $expected = $UvSha256["$UvVersion|$target"]
    if ($expected) {
        if ($actual -ne $expected.ToLower()) {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
            Fail "uv 파일 해시가 다릅니다 — 설치를 중단합니다."
            Say  "         기대: $expected"
            Say  "         실제: $actual"
            throw "uv_checksum_mismatch"
        }
        Ok "uv 해시 검증 통과 (SHA-256, 스크립트에 고정된 값)"
    } else {
        # 소스에 없는 버전을 -UvVersion 으로 지정한 경우. 배포처 해시로라도 대조한다.
        # (같은 서버라 전송 오류는 잡지만 서버 침해는 못 잡는다 → 그래서 경고)
        Warn "이 스크립트에 $UvVersion 의 해시가 없습니다 — 배포처 해시로만 대조합니다."
        $pub = (Invoke-WebRequest "$zipUrl.sha256" -UseBasicParsing -TimeoutSec 60).Content
        if ($pub -is [byte[]]) { $pub = [Text.Encoding]::UTF8.GetString($pub) }
        $pubHash = ($pub.Trim() -split '\s+')[0].ToLower()
        if ($actual -ne $pubHash) {
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
            throw "uv 해시 불일치(배포처 기준)"
        }
        Ok "uv 해시 대조 통과 (배포처 기준)"
    }

    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $found = Get-ChildItem $tmp -Recurse -Filter "uv.exe" | Select-Object -First 1
    if (-not $found) {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        throw "압축 안에 uv.exe 가 없습니다"
    }
    New-Item -ItemType Directory -Force -Path $UvHome | Out-Null
    Copy-Item $found.FullName $UvExe -Force
    $uvx = Get-ChildItem $tmp -Recurse -Filter "uvx.exe" | Select-Object -First 1
    if ($uvx) { Copy-Item $uvx.FullName (Join-Path $UvHome "uvx.exe") -Force }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    return $UvExe
}

Say ""
Say "=== ClewPath Host 설치 ==="
Say "  대상 폴더 : $Root"
Say "  컨트롤플레인: $CpUrl"
Say "  릴레이      : $RelayUrl"
if ($DryRun) { Warn "DryRun — 점검만 하고 아무것도 변경하지 않습니다." }
Say ""

if (-not (Test-Path (Join-Path $Root "session_manager"))) {
    Fail "이 스크립트는 ClewPath Host 폴더 안에서 실행해야 합니다(session_manager 폴더가 안 보임)."
    exit 1
}

# 0.3.5 이하 zip 에 따라갔던 서버 전용 코드 사본을 걷어낸다 — 사용자 PC 에서 실행될
# 일이 없고, 방치하면 낡은 사본이 영영 남는다. 개발 저장소(ops/ 존재)는 진짜
# 서버 코드가 사는 곳이므로 절대 건드리지 않는다.
if (-not (Test-Path (Join-Path $Root "ops"))) {
    foreach ($legacy in @("control_plane", "relay")) {
        $d = Join-Path $Root $legacy
        if (Test-Path $d) {
            Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
            Ok "구버전이 남긴 서버 코드 사본 제거: $legacy\"
        }
    }
}

# ── 1) uv 확보 ────────────────────────────────────────────────
Say "[1/6] uv 확보"
$uv = $null
if ($SystemUv) {
    $sys = Get-Command uv -ErrorAction SilentlyContinue
    if ($sys) { $uv = $sys.Source; Ok "PATH 의 uv 사용: $uv (-SystemUv)" }
    else { Fail "-SystemUv 를 줬지만 PATH 에 uv 가 없습니다."; exit 1 }
} elseif (Test-Path $UvExe) {
    $uv = $UvExe
    $v = (& $uv --version 2>$null) -join " "
    Ok "설치된 uv 재사용: $v"
} elseif ($DryRun) {
    Warn "DryRun: uv 설치 건너뜀 (받을 위치: $UvExe)"
} else {
    try { $uv = Install-Uv; Ok "uv 설치 완료: $uv" }
    catch { Fail $_.Exception.Message; exit 1 }
}

# ── 2) 파이썬 확보 ────────────────────────────────────────────
Say ""
Say "[2/6] 파이썬 $Python 확보"

function Test-PythonUsable($exe) {
    # 진짜로 쓸 수 있는 파이썬인지 확인한다. 버전만 보면 안 된다 —
    # Smart App Control 이 _ssl.pyd 를 막으면 파이썬은 멀쩡히 뜨는데 HTTPS 만 죽는다.
    #   ssl     : 컨트롤플레인(HTTPS) · 릴레이(WSS) 필수
    #   ctypes  : pywinpty(웹터미널) 가 의존
    #   sqlite3 : 표준 라이브러리 경로 온전성 확인
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    & $exe -c "import ssl, ctypes, sqlite3" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Find-Python($uvExe, $pref, $spec) {
    $old = $env:UV_PYTHON_PREFERENCE
    $env:UV_PYTHON_PREFERENCE = $pref
    try { $p = (& $uvExe python find $spec 2>$null | Select-Object -First 1) }
    catch { $p = $null }
    finally {
        if ($null -eq $old) { Remove-Item Env:\UV_PYTHON_PREFERENCE -ErrorAction SilentlyContinue }
        else { $env:UV_PYTHON_PREFERENCE = $old }
    }
    if ($p) { return $p.Trim() }
    return $null
}

function Test-SmartAppControl {
    try {
        $s = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" `
                -ErrorAction Stop).VerifiedAndReputablePolicyState
        return ($s -eq 1)   # 1=적용중, 2=평가모드, 0=꺼짐
    } catch { return $false }
}

$PyExe = $null
if ($DryRun -or -not $uv) {
    Warn "DryRun: 파이썬 확보 건너뜀"
} else {
    # (1) 서명된 시스템 파이썬 우선 — Smart App Control 아래서도 확실히 동작한다
    $cand = Find-Python $uv "only-system" ">=$Python"
    if ($cand -and (Test-PythonUsable $cand)) {
        $PyExe = $cand
        Ok "시스템 파이썬 사용: $PyExe"
    } else {
        if ($cand) { Warn "시스템 파이썬이 있으나 사용 불가(표준 모듈 차단): $cand" }

        # (2) 없으면 uv 관리 런타임 — 사전 준비 없이 여기서 끝나는 게 정상 경로
        Say "         uv 관리 런타임을 받습니다 (시스템에 설치되지 않음)"
        Invoke-Native { & $uv python install $Python }
        $cand = Find-Python $uv "only-managed" $Python
        if ($cand -and (Test-PythonUsable $cand)) {
            $PyExe = $cand
            Ok "uv 관리 파이썬 사용: $PyExe (시스템 설치 아님)"
        } else {
            # (3) 여기 오면 십중팔구 Smart App Control 이 서명 없는 _ssl.pyd 를 막은 것
            $sac = Test-SmartAppControl
            Warn "uv 가 받은 파이썬이 이 PC 에서 동작하지 않습니다(ssl 등 표준 모듈 차단)."
            if ($sac) { Warn "원인: Smart App Control 이 켜져 있어 서명 없는 파이썬이 차단됩니다." }

            # (4) 자동 복구 — winget 의 python.org 빌드는 PSF 서명이라 통과한다
            $wg = Get-Command winget -ErrorAction SilentlyContinue
            if ($wg -and -not $NoWinget) {
                Say "         winget 으로 서명된 파이썬 $Python 을 설치합니다 (python.org 공식 빌드)"
                Say "         원치 않으면 Ctrl+C 후 -NoWinget 으로 다시 실행하세요."
                Invoke-Native { & winget install --id "Python.Python.$Python" --exact --silent `
                    --accept-package-agreements --accept-source-agreements }
                # winget 이 PATH 를 갱신해도 현재 세션엔 반영되지 않는다 → 레지스트리에서 다시 읽는다
                $env:PATH = [Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                            [Environment]::GetEnvironmentVariable("PATH","User")
                $cand = Find-Python $uv "only-system" ">=$Python"
                if ($cand -and (Test-PythonUsable $cand)) {
                    $PyExe = $cand
                    Ok "서명된 파이썬 설치 완료: $PyExe"
                }
            }

            if (-not $PyExe) {
                Fail "쓸 수 있는 파이썬을 준비하지 못했습니다."
                Say  ""
                Say  "  다음 중 하나를 하신 뒤 install.ps1 을 다시 실행하세요:"
                Say  "    A) 서명된 파이썬 설치 (권장)"
                Say  "         winget install --id Python.Python.$Python"
                Say  "       또는 https://www.python.org/downloads/ 에서 $Python 설치"
                if ($sac) {
                    Say  "    B) Smart App Control 끄기"
                    Say  "         설정 > 개인 정보 및 보안 > Windows 보안 > 앱 및 브라우저 컨트롤"
                    Say  "         > 스마트 앱 제어 > 끄기"
                    Say  "       (⚠ 한 번 끄면 Windows 재설치 전까지 다시 켤 수 없습니다. A 를 권합니다)"
                }
                Say  ""
                exit 1
            }
        }
    }
}

# ── 3) 클로드 데이터 폴더 ─────────────────────────────────────
Say ""
Say "[3/6] 클로드 데이터 폴더 점검"
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Ok "claude CLI 발견"
} else {
    Warn "claude CLI 를 못 찾았습니다. 세션 재개 기능은 claude 가 있어야 동작합니다(설치 후 이용 가능)."
}

# 공식 env(CLAUDE_CONFIG_DIR) 를 먼저 본다 — 폴더를 옮겨 쓰는 사용자 대응
$claudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR }
             else { Join-Path $env:USERPROFILE ".claude" }
$projects = Join-Path $claudeDir "projects"
if (Test-Path $projects) {
    $n = (Get-ChildItem $projects -Directory -ErrorAction SilentlyContinue).Count
    if ($n -gt 0) { Ok "클로드 세션 폴더 발견: $projects (프로젝트 $n 개)" }
    else { Warn "폴더는 있으나 프로젝트가 0개입니다: $projects" }
} else {
    Fail "클로드 데이터 폴더를 찾지 못했습니다: $projects"
    Say  "  - 이 PC 에서 클로드 코드를 한 번 이상 사용했나요?"
    Say  "  - 폴더를 옮겨 쓴다면 CLAUDE_CONFIG_DIR 을 설정한 뒤 다시 실행하세요."
    Say  "    예:  `$env:CLAUDE_CONFIG_DIR = 'D:\claude'  ;  powershell -File install.ps1"
    if (-not $DryRun) { exit 1 }
}

# AgentToken 은 이제 선택이다 — 커넥터는 컨트롤플레인이 서명한 단기 JWT 로 릴레이에 들어간다.
# (레거시 릴레이에 붙을 때만 필요. 공유 토큰을 안 나눠주면 유출·회수 문제가 사라진다)
if ($AgentToken) { Ok "공유 agent 토큰 사용(레거시 호환 모드)" }
else { Ok "공유 agent 토큰 없음 - 컨트롤플레인 JWT 로 접속(권장)" }

# ── 4) 의존성 ────────────────────────────────────────────────
Say ""
Say "[4/6] 의존성 설치 (전용 .venv)"
$venv   = Join-Path $Root ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
if ($DryRun -or -not $uv) {
    Warn "DryRun: venv 생성/설치 건너뜀"
} else {
    if ($Reinstall -and (Test-Path $venv)) {
        Remove-Item -Recurse -Force $venv
        Ok "기존 .venv 삭제 (-Reinstall)"
    }
    # [2/6] 에서 스모크 테스트를 통과한 인터프리터로만 venv 를 만든다.
    # (버전 문자열로 넘기면 uv 가 다시 고르면서 차단된 런타임을 집을 수 있다)
    if (-not (Test-Path $venvPy)) {
        Invoke-Native { & $uv venv --python $PyExe $venv }
        if (-not (Test-Path $venvPy)) { Fail "venv 생성 실패"; exit 1 }
        Ok "venv 생성"
    } else { Ok "기존 venv 재사용" }

    # venv 는 베이스 파이썬의 DLL 을 그대로 쓴다 → 여기서 한 번 더 확인한다.
    # 기존 venv 재사용 경로에서 예전에 만든 불량 venv 를 걸러내는 역할도 한다.
    if (-not (Test-PythonUsable $venvPy)) {
        Fail "만들어진 venv 의 파이썬이 표준 모듈(ssl 등)을 못 씁니다."
        Say  "  -Reinstall 로 .venv 를 지우고 다시 실행하세요:"
        Say  "     powershell -File install.ps1 -Reinstall"
        exit 1
    }

    # uv.lock 이 함께 배포되면 그대로(고정 버전) 설치한다 — 사람마다 다른 버전이
    # 깔려서 "제 PC 에선 되는데" 가 생기는 걸 막는다. 없으면 pyproject 로 푼다.
    $lock = Join-Path $Root "uv.lock"
    $installed = $false
    if (Test-Path $lock) {
        $env:UV_PROJECT_ENVIRONMENT = $venv
        Invoke-Native { & $uv sync --frozen --no-dev }
        $rc = $LASTEXITCODE
        Remove-Item Env:\UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
        if ($rc -eq 0) { Ok "의존성 설치 완료 (uv.lock 고정 버전)"; $installed = $true }
        else { Warn "잠금 파일로 설치 실패 — pyproject 기준으로 재시도합니다." }
    }
    if (-not $installed) {
        Invoke-Native { & $uv pip install --python $venvPy -e . }
        if ($LASTEXITCODE -ne 0) {
            Fail "의존성 설치 실패"
            Show-LastNative "uv 오류(마지막 25줄)"
            Say "  대개 원인:"
            Say "   1) 이전 설치의 .venv 손상 → 아래로 지우고 다시 설치하세요:"
            Say "      Remove-Item -Recurse -Force `"`$env:LOCALAPPDATA\ClewPath\app\.venv`"; irm https://clewpath.pyongso.com/get | iex"
            Say "   2) PyPI(pypi.org) 연결 차단(사내 프록시/방화벽) → 네트워크·프록시 확인."
            exit 1
        }
        Ok "의존성 설치 완료"
    }
}

# ── 5) 설정 파일 + 자격증명 ───────────────────────────────────
Say ""
Say "[5/6] 설정 파일 생성 + 자격증명 발급"

# 앱과 동일한 규칙: SESSION_MANAGER_CLAUDE_HOME 이 있으면 그쪽(테스트/격리용)
$claudeHome = if ($env:SESSION_MANAGER_CLAUDE_HOME) { $env:SESSION_MANAGER_CLAUDE_HOME }
              else { Join-Path $env:USERPROFILE ".claude" }
$dataDir  = Join-Path $claudeHome "session_manager"
$credFile = Join-Path $dataDir "cp_credentials.json"
$confFile = Join-Path $dataDir "clewpath.toml"

# 설정은 데이터 폴더에 둔다 — 재설치·업데이트가 덮어쓰지 않아야 사용자가 고친 값이 산다.
# 이미 있으면 절대 건드리지 않는다(사용자가 포트를 바꿔놨을 수 있다).
if (Test-Path $confFile) {
    Ok "기존 설정 파일 유지: $confFile"
} elseif ($DryRun) {
    Warn "DryRun: 설정 파일 생성 건너뜀 ($confFile)"
} else {
    $clientLine = if ($ClientToken) { "client_token = `"$ClientToken`"" }
                  else { "# client_token = `"...`"   # 운영자에게 받은 값 (폰 페어링 QR 용)" }
    $agentLine  = if ($AgentToken)  { "agent_token = `"$AgentToken`"" } else { "" }
    $claudeLine = if ($env:CLAUDE_CONFIG_DIR) { "config_dir = `"$($env:CLAUDE_CONFIG_DIR -replace '\\','\\')`"" }
                  else { "# config_dir = `"D:\\claude`"   # 폴더를 옮겨 쓸 때만" }
    $toml = @"
# ClewPath Host 설정 파일
#
#   위치: 데이터 폴더 — 재설치나 업데이트를 해도 지워지지 않습니다.
#   반영: 값을 고친 뒤 ClewPath 를 재시작하세요.
#   우선순위: 환경변수 > 이 파일 > 기본값

[server]
# 로컬 웹 주소. host 는 127.0.0.1 을 권장합니다 —
# 0.0.0.0 으로 열면 같은 공유기의 다른 기기가 인증 없이 들어올 수 있습니다.
host = "127.0.0.1"
port = $Port

# 포트가 이미 사용 중이면 다음 포트를 차례로 시도합니다(0 이면 시도하지 않고 종료).
# 실제로 열린 주소는 기동 로그와 같은 폴더의 runtime.json 에 적힙니다.
port_fallback = 10

# 비워두면 비밀번호 없이 로컬 전용으로 동작합니다(루프백은 무인증).
# password = "..."

[cloud]
cp_url    = "$CpUrl"
relay_url = "$RelayUrl"
app_url   = "$AppUrl"
$clientLine
$agentLine

[claude]
$claudeLine

[update]
# 새 버전이 있는지 주기적으로 확인합니다(설치는 하지 않음).
auto_check = true
check_interval_hours = 24

# 서비스를 재기동할 때 새 버전이 있으면 자동 적용(서명·해시 검증분, 실패 시 롤백).
# 재기동은 명시적 행동이라 주기 자동설치보다 안전해서 기본 ON.
apply_on_restart = true

# true 로 두면 재기동을 기다리지 않고 확인 즉시 설치·재시작합니다.
auto_apply = false
"@
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    Set-Content -Path $confFile -Value $toml -Encoding UTF8
    Ok "설정 파일 생성: $confFile"
    if (-not $ClientToken) {
        Warn "ClientToken 미지정 — 폰 페어링 QR 은 설정 파일의 client_token 을 채우면 됩니다."
    }
}

# 기동 스크립트는 이제 '실행'만 한다. 설정은 전부 위 toml 로 갔다 —
# 재설치할 때마다 이 파일을 덮어써도 사용자 설정이 날아가지 않는다.
# 신규 설치는 room 을 지정하지 않는다 → 커넥터가 컨트롤플레인이 발급한 room 을 그대로 채택.
$startPs = Join-Path $Root "start-connector.ps1"
$cfg = @"
# 자동 생성됨 (install.ps1) — 재설치하면 덮어써집니다.
# 설정을 바꾸려면 이 파일이 아니라 아래 파일을 고치세요:
#   $confFile
#
# 이 스크립트는 서버를 '창 없이' 띄우고 곧바로 끝납니다.
# 예전처럼 & python 으로 직접 실행하면 더블클릭·수동 실행 때 콘솔창이
# 서버와 함께 계속 떠 있게 됩니다(실사용자 보고). 분리 실행이 정답.
# 로그: $dataDir\host.log (.err.log)
`$ErrorActionPreference = "Stop"
Set-Location -Path `$PSScriptRoot
`$env:PYTHONUNBUFFERED = "1"     # 리다이렉트 시 버퍼링으로 로그가 안 찍히는 것 방지
`$env:PYTHONIOENCODING = "utf-8" # 로그 인코딩 고정 - 없으면 로케일(cp949)이라 특수문자에 취약
`$log = "$dataDir\host.log"
`$err = "$dataDir\host.err.log"
# 무한 성장 방지 — 5MB 넘으면 한 세대 밀어둔다(돌고 있는 서버가 잡고 있으면 건너뜀)
foreach (`$f in @(`$log, `$err)) {
    try { if ((Test-Path `$f) -and ((Get-Item `$f).Length -gt 5MB)) { Move-Item `$f "`$f.1" -Force } } catch {}
}
Start-Process -FilePath "`$PSScriptRoot\.venv\Scripts\python.exe" ``
    -ArgumentList "-m","session_manager.server" ``
    -WindowStyle Hidden -RedirectStandardOutput `$log -RedirectStandardError `$err
"@
if ($DryRun) {
    Warn "DryRun: $startPs 생성 건너뜀"
} else {
    Set-Content -Path $startPs -Value $cfg -Encoding UTF8
    Ok "start-connector.ps1 생성"
}
if (Test-Path $credFile) {
    Ok "이미 발급된 자격증명이 있습니다(재사용)"
} elseif ($DryRun) {
    Warn "DryRun: 발급 건너뜀"
    try {
        $h = Invoke-RestMethod -Uri "$CpUrl/healthz" -TimeoutSec 10
        if ($h.ok) { Ok "컨트롤플레인 도달 확인" } else { Warn "예상치 못한 응답: $h" }
    } catch { Warn "컨트롤플레인에 닿지 못했습니다: $($_.Exception.Message)" }
} else {
    # 서버 기동 시 커넥터가 자동 발급하지만, 여기서 미리 받아 실패를 조기에 알린다.
    try {
        $body = @{ display_name = $env:COMPUTERNAME } | ConvertTo-Json
        $r = Invoke-RestMethod -Uri "$CpUrl/provision/anonymous" -Method Post `
                -ContentType "application/json" -Body $body -TimeoutSec 15
        New-Item -ItemType Directory -Force -Path (Split-Path $credFile) | Out-Null
        @{
            cp_url               = $CpUrl
            connector_id         = $r.connector_id
            room_key             = $r.room_key
            credential_public_id = $r.credential_public_id
            secret               = $r.secret
            claim_secret         = $r.claim_secret
            created_at           = [int][double]::Parse((Get-Date -UFormat %s))
        } | ConvertTo-Json | Set-Content -Path $credFile -Encoding UTF8
        Ok "자격증명 발급 완료 (connector $($r.connector_id))"
        Say "     room: $($r.room_key)"
    } catch {
        $msg = $_.Exception.Message
        if ($msg -match "429") {
            Warn "발급 한도 초과(429). 잠시 후 다시 시도하세요 — 서비스는 공유토큰으로도 동작합니다."
        } else {
            Warn "발급 실패: $msg"
            Warn "괜찮습니다 — 서버가 뜬 뒤 자동 재시도하며, 그동안 공유토큰으로 동작합니다(fail-open)."
        }
    }
}

# ── 6) 기동 + 자동시작 ───────────────────────────────────────
Say ""
Say "[6/6] 기동 및 자동시작 등록"
$taskName = "ClewPathHost"
if ($DryRun) {
    Warn "DryRun: 기동/등록 건너뜀"
} else {
    # ?. 는 PS7 전용 — 5.1 호환을 위해 평범한 분기로 쓴다
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCmd) { $pwsh = $pwshCmd.Source }
    else { $pwsh = (Get-Command powershell).Source }

    if (-not $NoAutoStart) {
        # 1차: 작업 스케줄러.
        # ⚠ 트리거는 반드시 "이 사용자" 로그온으로 한정한다 — -AtLogOn 을 사용자 지정
        #   없이 만들면 '모든 사용자 로그온' 트리거가 되고, 그건 관리자 권한을 요구해서
        #   일반 계정 설치가 전부 "Access is denied" 로 죽는다(실사용자 PC 에서 발생).
        #   principal 도 현재 사용자·Interactive·Limited 로 명시해 비관리자 등록을 보장.
        $registered = $false
        $me = "$env:USERDOMAIN\$env:USERNAME"
        try {
            # -ExecutionPolicy Bypass 필수 — Windows 클라이언트의 기본 정책은
            # Restricted(스크립트 파일 실행 금지)라, 없으면 로그온 자동시작이
            # **조용히** 실패한다. 설치 자체는 irm|iex(문자열 실행)라 정책을 안 타서
            # 개발 PC(Bypass 정책)에선 재현이 안 됐다 — 실사용자 PC 에서 발견.
            $action    = New-ScheduledTaskAction -Execute $pwsh `
                          -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startPs`""
            $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $me
            $principal = New-ScheduledTaskPrincipal -UserId $me `
                          -LogonType Interactive -RunLevel Limited
            $set       = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                          -DontStopIfGoingOnBatteries -StartWhenAvailable
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
                        -Principal $principal -Settings $set -Force | Out-Null
            Ok "자동시작 등록 ($taskName, 이 사용자 로그온 시)"
            $registered = $true
        } catch {
            Warn "작업 스케줄러 등록 실패: $($_.Exception.Message)"
        }

        # 2차 폴백: HKCU Run 키(시작 프로그램). 현재 사용자 하이브라 권한이 필요 없어서
        # 회사 PC 처럼 작업 스케줄러가 정책으로 막힌 환경에서도 항상 된다.
        if (-not $registered) {
            try {
                $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
                $cmd = "`"$pwsh`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startPs`""
                Set-ItemProperty -Path $runKey -Name $taskName -Value $cmd
                Ok "자동시작 등록 (시작 프로그램 — 관리자 권한 불필요)"
                Warn "로그온 때 창이 잠깐 보였다 사라질 수 있습니다(정상)."
                $registered = $true
            } catch {
                Warn "시작 프로그램 등록도 실패: $($_.Exception.Message)"
            }
        }
        if (-not $registered) {
            Warn "자동시작을 등록하지 못했습니다. 부팅 후 수동 실행: $pwsh -File `"$startPs`""
        }
    } else { Warn "자동시작 등록 건너뜀(-NoAutoStart)" }

    # 지금 기동
    try {
        Start-Process -FilePath $pwsh `
            -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-WindowStyle","Hidden","-File","`"$startPs`"" | Out-Null
        # 느린 PC 대비 폴링(최대 20초). 고정 6초 단일 확인은 콜드 스타트에서 오판한다.
        # 매 회 runtime.json 을 다시 본다 — 포트 폴백이 일어났으면 그쪽이 실제 주소.
        $health = $null
        $rtFile = Join-Path $dataDir "runtime.json"
        foreach ($i in 1..20) {
            Start-Sleep -Seconds 1
            if (Test-Path $rtFile) {
                try {
                    $rt = Get-Content $rtFile -Raw | ConvertFrom-Json
                    if ($rt.port) {
                        if ([int]$rt.port -ne $Port) { Warn "$Port 가 사용 중이라 $($rt.port) 번 포트로 열렸습니다." }
                        $Port = [int]$rt.port
                    }
                } catch {}
            }
            try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/owner/connection" -TimeoutSec 3; break } catch {}
        }
        $diag = $null
        try { $diag = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/owner/diagnostics" -TimeoutSec 5 } catch {}
        if ($diag) {
            if ($diag.ok) { Ok "세션 인식됨: $($diag.session_count) 개 (폴더: $($diag.claude_home))" }
            else { Warn "세션을 못 찾았습니다 (폴더: $($diag.claude_home), 개수: $($diag.session_count))" }
            if (-not $diag.claude_exe_found) { Warn "claude 실행파일을 못 찾았습니다 — 세션 재개가 안 됩니다." }
        }
        if ($health) {
            Ok "서비스 기동 확인 (연결 모드: $($health.mode))"
            if ($health.mode -eq "fallback") {
                Warn "컨트롤플레인에 닿지 못해 공유토큰으로 접속 중입니다(기능은 정상)."
            }
        } else {
            Warn "기동 확인 실패 — http://127.0.0.1:$Port/ 가 열리지 않습니다."
            # 원인 진단을 그 자리에서 — 사용자가 "안 돼요"만 보낼 수밖에 없던 문제 해소
            $hlog = Join-Path $dataDir "host.log"
            $herr = Join-Path $dataDir "host.err.log"
            if ((Test-Path $herr) -and ((Get-Item $herr).Length -gt 0)) {
                Warn "오류 로그 끝부분 ($herr):"
                Get-Content $herr -Tail 8 | ForEach-Object { Say "    ! $_" }
            } elseif (Test-Path $hlog) {
                Warn "로그 끝부분 ($hlog):"
                Get-Content $hlog -Tail 8 | ForEach-Object { Say "    | $_" }
            } else {
                Warn "로그 파일이 아예 없습니다 = start-connector.ps1 자체가 실행되지 못했습니다."
                Warn "회사 정책(GPO)이 PowerShell 스크립트를 막고 있을 가능성이 큽니다:"
                Say  "      Get-ExecutionPolicy -List   로 MachinePolicy 가 걸려있는지 확인하세요."
            }
        }
    } catch { Warn "기동 실패: $($_.Exception.Message)" }
}

# ── 안내 ────────────────────────────────────────────────────
# 안내문에 pwsh 를 박아두면 5.1 만 있는 PC 사용자가 그대로 복붙하다 막힌다
$shellName = "powershell"
if (Get-Command pwsh -ErrorAction SilentlyContinue) { $shellName = "pwsh" }
Say ""
Say "=== 설치 완료 ==="
Say ""
Say "  1) 이 PC 에서 열기 :  http://127.0.0.1:$Port/"
Say "  2) 폰에서 쓰려면   :  로컬 웹 우측 상단 [📱] -> '새 기기 추가' -> QR 스캔"
Say "  3) 서비스 관리     :"
Say "       시작 : Start-ScheduledTask $taskName"
Say "       중지 : Get-Process python | Where-Object { `$_.Path -like '*$([IO.Path]::GetFileName($Root))*' } | Stop-Process"
Say "       수동 : $shellName -File `"$startPs`""
Say ""
Say "  설정 파일: $confFile"
Say "             (포트·주소는 여기서 바꾸고 재시작하세요. 재설치해도 유지됩니다)"
Say "  자격증명 : $credFile"
Say "  실제 주소: $(Join-Path $dataDir 'runtime.json')"
Say "  uv       : $UvExe  (지우려면 $UvHome 폴더 삭제)"
Say ""
