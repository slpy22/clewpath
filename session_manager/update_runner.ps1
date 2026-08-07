# =====================================================================
#  ClewPath Host — 업데이트 적용기 (분리 실행)
#
#  왜 별도 프로세스인가:
#    돌고 있는 파이썬은 자기 코드를 덮어쓸 수 없다(윈도우는 실행 중 파일이 잠긴다).
#    그래서 이 스크립트는 설치 폴더 **밖**(데이터 폴더)에 복사돼 실행되고,
#    본체가 죽기를 기다렸다가 파일을 갈아끼운다.
#
#  순서:
#    1) 본체 PID 가 사라질 때까지 대기 (안 죽으면 강제 종료)
#    2) 교체 대상만 백업       <- 실패해도 되돌릴 수 있어야 한다
#    3) 스테이지 -> 설치 폴더
#    4) uv sync (의존성이 바뀌었을 수 있다)
#    5) 재기동 + 헬스체크
#    6) 실패하면 백업으로 롤백 후 다시 기동
#
#  이 스크립트는 절대 스스로 죽지 않는다(모든 단계 try/catch) — 중간에 죽으면
#  사용자 PC 에 반쯤 교체된 설치 폴더가 남는다. 그게 최악이다.
#
#  ⚠ PowerShell 5.1 에서도 돌아야 한다 — pwsh 가 없는 PC 에선 updater.py 가
#    powershell.exe 로 이 파일을 띄운다. PS7 전용 문법(?. ?? 삼항 && ||) 금지,
#    저장은 UTF-8 BOM 포함 (tests/test_ps51_compat.py 가 검사).
# =====================================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory)][string]$StageDir,
    [Parameter(Mandatory)][string]$BackupDir,
    [Parameter(Mandatory)][int]$WaitPid,
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$LogFile,
    [int]$Port = 5100,
    [string]$RestartScript = "",
    [int]$WaitSeconds = 60
)

$ErrorActionPreference = "Continue"

# 교체 대상. updater.py 의 PAYLOAD 와 반드시 같아야 한다.
$Payload = @("session_manager","pwa",
             "pyproject.toml","uv.lock","install.ps1","README.md","LICENSE")

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}

function Copy-Payload($from, $to) {
    New-Item -ItemType Directory -Force -Path $to | Out-Null
    foreach ($name in $Payload) {
        $src = Join-Path $from $name
        if (-not (Test-Path $src)) { continue }
        $dst = Join-Path $to $name
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst -ErrorAction SilentlyContinue }
        Copy-Item $src $dst -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Sync-Deps($reason) {
    # 파일을 갈아끼운 뒤에는 반드시 부른다. 의존성 때문만이 아니라 **설치 메타데이터**
    # 때문이다 — 이걸 빼먹으면 롤백 후에도 dist-info 가 새 버전으로 남아서,
    # 코드는 구버전인데 자기를 신버전이라고 보고하고 다시는 업데이트를 안 받는다.
    $uv = Join-Path $env:LOCALAPPDATA "ClewPath\bin\uv.exe"
    if (-not (Test-Path $uv)) {
        $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($uvCmd) { $uv = $uvCmd.Source } else { $uv = $null }
    }
    $venvPy = Join-Path $InstallRoot ".venv\Scripts\python.exe"
    if (-not $uv -or -not (Test-Path $venvPy)) {
        Log "uv 나 venv 를 못 찾아 의존성 동기화를 건너뜁니다 ($reason)"
        return
    }
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $InstallRoot ".venv"
        Push-Location $InstallRoot
        & $uv sync --frozen --no-dev 2>&1 | ForEach-Object { Log "  uv: $_" }
        if ($LASTEXITCODE -ne 0) {
            & $uv pip install --python $venvPy -e . 2>&1 | ForEach-Object { Log "  pip: $_" }
        }
        Pop-Location
        Remove-Item Env:\UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
        Log "의존성 동기화 완료 ($reason)"
    } catch { Log "의존성 동기화 실패($reason, 계속 진행): $_" }
}

function Test-Healthy($port, $seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try {
            $r = Invoke-RestMethod "http://127.0.0.1:$port/api/owner/diagnostics" -TimeoutSec 3
            if ($r.claude_home) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 700
    }
    return $false
}

function Start-Host2 {
    if (-not $RestartScript -or -not (Test-Path $RestartScript)) {
        Log "재기동 스크립트가 없어 건너뜁니다: $RestartScript"
        return
    }
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCmd) { $pwsh = $pwshCmd.Source }
    else { $pwsh = (Get-Command powershell).Source }
    # Bypass 필수 — 기본 정책 Restricted 인 PC 에서 업데이트 후 재기동이 조용히 실패한다
    Start-Process -FilePath $pwsh `
        -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-WindowStyle","Hidden","-File","`"$RestartScript`"" | Out-Null
}

Log "=== 업데이트 적용 시작 -> $Version ==="
Log "  설치폴더 : $InstallRoot"
Log "  스테이지 : $StageDir"

# ── 1) 본체 종료 대기 ────────────────────────────────────────
Log "본체(PID $WaitPid) 종료 대기..."
$end = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $end) {
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
if (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {
    Log "시간 내에 안 죽어서 강제 종료합니다."
    try { Stop-Process -Id $WaitPid -Force -ErrorAction Stop } catch { Log "  강제 종료 실패: $_" }
    Start-Sleep -Seconds 2
}
# 파일 핸들이 풀릴 시간을 준다 — 바로 덮어쓰면 간헐적으로 '사용 중' 오류가 난다
Start-Sleep -Seconds 2

# ── 2) 백업 ─────────────────────────────────────────────────
try {
    if (Test-Path $BackupDir) { Remove-Item -Recurse -Force $BackupDir -ErrorAction SilentlyContinue }
    Copy-Payload $InstallRoot $BackupDir
    Log "백업 완료: $BackupDir"
} catch {
    Log "[FAIL] 백업 실패 - 되돌릴 수 없으므로 여기서 멈춥니다: $_"
    Start-Host2
    exit 1
}

# ── 3) 교체 ─────────────────────────────────────────────────
$swapped = $false
try {
    Copy-Payload $StageDir $InstallRoot
    $swapped = $true
    Log "파일 교체 완료"
} catch {
    Log "[FAIL] 교체 중 오류: $_"
}

# ── 4) 의존성 ───────────────────────────────────────────────
if ($swapped) { Sync-Deps "새 버전 $Version" }

# ── 5) 재기동 + 헬스체크 ────────────────────────────────────
$healthy = $false
if ($swapped) {
    Start-Host2
    $healthy = Test-Healthy $Port 45
    Log $(if ($healthy) { "헬스체크 통과 - 업데이트 성공 ($Version)" } else { "헬스체크 실패" })
}

# ── 6) 롤백 ─────────────────────────────────────────────────
if (-not $healthy) {
    Log "롤백합니다."
    # 되돌리기 전에 실패한 버전을 남겨둔다 — 원인을 봐야 하니까
    try {
        $failed = "$BackupDir-failed-$Version"
        if (Test-Path $failed) { Remove-Item -Recurse -Force $failed -ErrorAction SilentlyContinue }
        Copy-Payload $InstallRoot $failed
    } catch {}

    try {
        Get-Process python -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($InstallRoot, [StringComparison]::OrdinalIgnoreCase)
        } | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Copy-Payload $BackupDir $InstallRoot
        Log "이전 버전 복원 완료"
        # 파일만 되돌리면 dist-info 가 실패한 버전으로 남는다 → 자기를 신버전이라
        # 착각하고 다시는 업데이트를 안 받는다. 메타데이터까지 되돌려야 한다.
        Sync-Deps "롤백"
        Start-Host2
        if (Test-Healthy $Port 45) { Log "롤백 후 정상 동작 확인" }
        else { Log "[FAIL] 롤백 후에도 응답이 없습니다 - 수동 확인이 필요합니다." }
    } catch {
        Log "[FAIL] 롤백 실패: $_"
    }
}

# 결과를 state.json 옆에 남긴다 — 다음 기동 때 UI 가 읽어서 보여준다
try {
    $res = @{
        version   = $Version
        ok        = $healthy
        finished_at = [int][double]::Parse((Get-Date -UFormat %s))
        log       = $LogFile
    } | ConvertTo-Json
    Set-Content -Path (Join-Path (Split-Path $LogFile) "last_apply.json") -Value $res -Encoding UTF8
} catch {}

Log "=== 종료 (성공: $healthy) ==="
