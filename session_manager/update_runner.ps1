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
#  시험: tests/test_update_runner_ps1.py — 임시 폴더에서 5.1·7 로 끝까지 돌린다(파일을 쥐고 복사 실패 흉내 포함).
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
             "pyproject.toml","uv.lock","install.ps1","install.sh","uninstall.sh","README.md","LICENSE")

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
}

# 폴더 내용을 그 폴더 안으로 하나씩 덮어 복사하고 오류(ErrorRecord)들을 돌려준다 — 대상 폴더를 다 못 지웠을 때(Copy-Payload).
# 하나씩인 이유: 5.1 의 Copy-Item 은 잠긴 대상 파일을 덮다 -ErrorAction 과 무관하게 종료 오류를 던져 그 뒤 파일을 건너뛴다(7 은 안 그런다).
function Copy-Merge($src, $dst) {
    $ge = $null
    foreach ($rel in @(Get-ChildItem -LiteralPath $src -Recurse -Force -Name -ErrorAction SilentlyContinue -ErrorVariable ge)) {
        $s = Join-Path $src $rel
        $t = Join-Path $dst $rel
        try {
            if (Test-Path -LiteralPath $s -PathType Container) {
                if (-not (Test-Path -LiteralPath $t)) { New-Item -ItemType Directory -Force -Path $t -ErrorAction Stop | Out-Null }
            } else {
                $pd = Split-Path $t
                if (-not (Test-Path -LiteralPath $pd)) { New-Item -ItemType Directory -Force -Path $pd -ErrorAction Stop | Out-Null }
                Copy-Item -LiteralPath $s -Destination $t -Force -ErrorAction Stop
            }
        } catch { $_ }
    }
    @($ge) | Where-Object { $_ }
}

# 교체 대상을 $from -> $to 로. 오류는 모아서 끝에 던진다 — 예전엔 -ErrorAction SilentlyContinue 뿐이라 복사가 실패해도 예외가
# 없어 백업·교체 실패를 잡는 try/catch 가 사실상 돌지 않았다(반쪽 백업 위에 교체하면 롤백도 반쪽, 결과는 '성공').
#  - 이름마다 한 번 더(1초 뒤) 해 본다 — 백신 검사 같은 잠깐의 잠금으로 업데이트를 실패시키지 않게.
#  - 대상 폴더를 다 못 지웠으면(잠긴 파일) 폴더째 복사하지 않고 그 안으로 내용을 덮어 복사한다(Copy-Merge) — 폴더째 복사하면
#    남은 폴더 안에 한 겹 더 생겼다(session_manager\session_manager). 롤백은 이 덕에 잠긴 파일 말고는 되돌린다. 어느 쪽이든 오류로 센다.
#  - 이름 하나의 종료 오류(5.1 의 Copy-Item 은 잠긴 대상을 덮다 던진다)가 나머지 이름의 복사를 막지 않게 이름마다 try.
function Copy-Payload($from, $to) {
    New-Item -ItemType Directory -Force -Path $to -ErrorAction Stop | Out-Null
    $errs = New-Object System.Collections.Generic.List[string]
    foreach ($name in $Payload) {
        $src = Join-Path $from $name
        if (-not (Test-Path $src)) { continue }
        $dst = Join-Path $to $name
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            $e1 = $null; $e2 = $null; $e3 = @()
            try {
                if (Test-Path $dst) { Remove-Item -Recurse -Force $dst -ErrorAction SilentlyContinue -ErrorVariable e1 }
                if ((Test-Path $dst) -and (Test-Path $src -PathType Container)) {
                    $e3 = @(Copy-Merge $src $dst)
                } else {
                    Copy-Item $src $dst -Recurse -Force -ErrorAction SilentlyContinue -ErrorVariable e2
                }
            } catch { $e3 += $_ }
            $bad = @(@($e1) + @($e2) + @($e3) | Where-Object { $_ })
            if ($bad.Count -eq 0) { break }
            if ($attempt -lt 2) { Start-Sleep -Seconds 1; continue }
            foreach ($e in $bad) { $errs.Add(("{0}: {1}" -f $name, ($e.Exception.Message -replace '\s+', ' '))) }
        }
    }
    if ($errs.Count -gt 0) {
        throw ("복사 오류 {0}건 - {1}" -f $errs.Count, ((@($errs) | Select-Object -First 3) -join " | "))
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
        # 회사 프록시(HTTPS 가로채기) PC 대응 — install.ps1 과 동일. 이게 없으면
        # uv 가 사내 루트 CA 를 거부해 업데이트의 의존성 재설치가 실패하고,
        # dist-info 가 안 갱신돼 "업데이트가 안 먹은 것처럼" 옛 버전에 머문다(실측).
        if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = "1" }
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

function Get-RuntimePort {
    # 새로 뜬 본체가 runtime.json 에 적은 '실제' 포트. 옛 프로세스가 포트를 아직 안 풀어
    # 폴백 포트로 떴을 때 -Port 만 보면 멀쩡한 새 버전을 롤백한다. 파일은 데이터 폴더
    # (= 로그 폴더의 부모)에 있다. 없거나 깨지면 $null.
    try {
        $p = Join-Path (Split-Path (Split-Path $LogFile)) "runtime.json"
        if (Test-Path $p) {
            $j = Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($j.port) { return [int]$j.port }
        }
    } catch {}
    return $null
}

function Test-Healthy($port, $seconds, $expectVersion) {
    # -Port 와, 새 본체가 runtime.json 에 적은 포트(다르면)를 함께 살핀다.
    # $expectVersion 이 있으면 '그 버전이 응답'해야 통과 — 안 죽은 옛 프로세스가 포트를
    # 쥐고 응답하는 것을 성공으로 오판하지 않는다(버전 필드가 없는 응답은 그냥 통과).
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        $ports = @($port)
        $rp = Get-RuntimePort
        if ($rp -and ($rp -ne $port)) { $ports += $rp }
        foreach ($p in $ports) {
            try {
                $r = Invoke-RestMethod "http://127.0.0.1:$p/api/owner/diagnostics" -TimeoutSec 3
                if ($r.claude_home) {
                    if ((-not $expectVersion) -or (-not $r.version) -or ($r.version -eq $expectVersion)) { return $true }
                }
            } catch {}
        }
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
    # 창을 아예 만들지 않고 띄운다(CreateNoWindow, 2026-10-15 사장님: 업데이트 때마다 콘솔이 번쩍였다).
    # Start-Process 는 새 콘솔 창을 먼저 만들고 pwsh 가 뜬 뒤에야 -WindowStyle Hidden 으로 숨겨서 잠깐 보였고,
    # 기본 터미널이 Windows Terminal 인 PC 에선 숨김 지시가 무시되기도 한다. (PS 5.1 호환 문법)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pwsh
    $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $RestartScript + '"'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    # 못 띄워도 러너는 끝까지 간다 — 예외로 끝나면 롤백·결과 기록·잠금 해제를 건너뛴다
    try { [System.Diagnostics.Process]::Start($psi) | Out-Null } catch { Log "재기동 실패: $($_.Exception.Message)" }
}

# 결과를 state.json 옆에 남기고(다음 기동 때 UI 가 읽는다. ok=false 인 버전은 재기동 자동 적용이 다시 받지 않는다 —
# updater.recently_failed) 동시 실행 방지 잠금을 푼다(updater.py 가 apply 시작 때 건다. 못 지워도 5분 뒤 만료).
# 끝·실패·중단 어느 길이든 여기를 거친다 — 예전엔 백업 실패의 exit 1 이 둘 다 건너뛰어, 다음 적용이 5분 동안 거부되고
# 재기동 자동 적용이 같은 버전을 또 받았다.
function Write-ApplyResult($ok) {
    try {
        $res = @{
            version   = $Version
            ok        = [bool]$ok
            finished_at = [int][double]::Parse((Get-Date -UFormat %s))
            log       = $LogFile
        } | ConvertTo-Json
        Set-Content -Path (Join-Path (Split-Path $LogFile) "last_apply.json") -Value $res -Encoding UTF8
    } catch {}
    try { Remove-Item -Force (Join-Path (Split-Path $LogFile) "apply.lock") -ErrorAction SilentlyContinue } catch {}
    Log "=== 종료 (성공: $([bool]$ok)) ==="
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
    Log "[FAIL] 백업 실패 - 되돌릴 수 없으므로 교체하지 않고 멈춥니다(설치 폴더는 그대로): $_"
    # 결과·잠금 정리를 옛 Host 를 다시 띄우기 전에 — 뜨자마자 재기동 자동 적용이 같은 버전을 또 받지 않게
    Write-ApplyResult $false
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
    Log "[FAIL] 교체 중 오류 - 백업으로 되돌립니다: $_"
}

# ── 4) 의존성 ───────────────────────────────────────────────
if ($swapped) { Sync-Deps "새 버전 $Version" }

# ── 5) 재기동 + 헬스체크 ────────────────────────────────────
$healthy = $false
if ($swapped) {
    Start-Host2
    $healthy = Test-Healthy $Port 45 $Version
    Log $(if ($healthy) { "헬스체크 통과 - 업데이트 성공 ($Version)" } else { "헬스체크 실패 (port=$Port, runtime.json port=$(Get-RuntimePort))" })
}

# ── 6) 롤백 ─────────────────────────────────────────────────
if (-not $healthy) {
    Log "롤백합니다."
    # 되돌리기 전에 실패한 버전을 남겨둔다 — 원인을 봐야 하니까
    try {
        $failed = "$BackupDir-failed-$Version"
        if (Test-Path $failed) { Remove-Item -Recurse -Force $failed -ErrorAction SilentlyContinue }
        Copy-Payload $InstallRoot $failed
    } catch { Log "  실패한 버전 보관 중 오류(계속): $_" }

    try {
        # 터미널 관리 프로세스(session_manager.ptyd)는 제외 — 열린 터미널을 들고 있고, 롤백과 무관하게 계속 산다
        $keep = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like '*session_manager.ptyd*' } |
            ForEach-Object { [int]$_.ProcessId })
        Get-Process python -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -and ($keep -notcontains $_.Id)
        } | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        # 복원이 일부 실패해도(잠긴 파일) 옛 Host 를 다시 띄우는 데까지는 간다 — 복사 오류가 예외로 보이게 된 뒤에도 예전처럼.
        # 되돌릴 수 있는 것은 Copy-Payload 가 되돌렸다(못 지운 폴더엔 내용을 덮어 복사).
        try {
            Copy-Payload $BackupDir $InstallRoot
            Log "이전 버전 복원 완료"
        } catch {
            Log "[FAIL] 이전 버전 복원 중 오류 - 되돌릴 수 있는 것만 되돌렸습니다, 수동 확인이 필요합니다: $_"
        }
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

# 결과 기록 + 잠금 해제(Write-ApplyResult — 백업 실패로 멈추는 길도 같은 함수를 거친다)
Write-ApplyResult $healthy
