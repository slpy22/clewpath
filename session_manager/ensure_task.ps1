# 자기 회복 보장(v0.9.2) — 기존 설치본을 새 런처·반복 트리거로 맞춘다. Host 가 기동 때 버전당 한 번 부른다
# (liveness.ensure_launcher → osdep._win.ensure_launcher). exit 0 이면 Host 가 출력을 launcher-ensured-<버전> 표식에 적고,
# exit 1 이면 표식을 쓰지 않는다 → 다음 기동에 다시 한다.
#  1) start-connector.ps1 이 'v2(멱등) + 구문 정상' 이 아니면 패키지 템플릿으로 다시 만든다 — BOM 붙여 쓰고 구문 검사,
#     못 넘으면 원래 런처(바이트 그대로)로 되돌린다
#  2) ClewPathHost 작업에 5분 반복 트리거가 없으면 단다(로그온 트리거 유지, 중복 인스턴스 무시). 작업 동작이 우리 기본 모양이면
#     conhost --headless 로 감싼다(5분마다 콘솔 창이 깜박이지 않게) — 반복 트리거를 다는 그 Set-ScheduledTask 에서, 또는
#     반복 트리거가 이미 있으면 동작만 바꾼다(2026-10-23 사장님 결정: 이미 5분 확인이 도는 PC 도 깜박임을 없앤다).
#     사람이 바꾼 모양(이미 conhost·다른 실행 파일·다른 인자)은 어느 경우든 건드리지 않는다.
# ⚠ 순서가 안전의 전부: 런처가 멱등이 아닌데 반복 트리거를 달면 5분마다 서버가 겹쳐 뜬다.
#    그래서 1) 이 실패하면 2) 는 하지 않는다. 순서는 런처 v2 → 동작 → 트리거(동작과 트리거는 한 번의 Set-ScheduledTask).
# ⚠ 인코딩(2026-10-22 노트북 사고): 영어 Windows(ANSI cp1252)의 powershell.exe 5.1 은 BOM 없는 파일을 cp1252 로 읽는다.
#    이 파일에 BOM 이 없던 동안 「—」(E2 80 94)의 0x94 가 ” 로 읽혀 파스 에러 → 0.10.5~0.31.1 내내 한 줄도 안 돌았다.
#    그래서 이 파일은 UTF-8 BOM + 코드 줄의 문자열은 ASCII 만(한글은 주석에만 — tests/test_ps51_compat.py 가 검사),
#    템플릿은 -Encoding UTF8 로 읽고, 만든 런처는 BOM 을 붙여 쓴다.
# 시험: tests/test_ensure_task.py — 가짜 폴더·없는 작업 이름으로 끝까지 돌리고, -LibOnly 로 함수만 불러 작업 조회 결과를 흉내 낸다.
param(
    [string]$Root,
    [string]$DataDir,
    [string]$ConfFile,
    [int]$Port = 5100,
    [string]$TaskName = "ClewPathHost",
    [switch]$LibOnly        # 함수만 정의하고 끝(점 소싱용 — 시험·install.ps1). 파일·작업은 건드리지 않는다
)

# ---------------------------------------------------------------- 런처

function Test-Utf8Bom([string]$Path) {
    try {
        $b = [IO.File]::ReadAllBytes($Path)
        return ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
    } catch { return $false }
}

# 구문 검사 — 정상이면 $null, 아니면 첫 오류 한 줄. 이 셸의 파서로 보고, 이 셸이 7 이면 5.1(powershell.exe)에게도 묻는다:
# 작업 동작이 powershell.exe 일 수 있고, BOM 없는 파일을 ANSI 로 읽는 건 5.1 뿐이다.
function Get-Ps1SyntaxError([string]$Path) {
    try {
        $errs = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errs)
        if ($errs -and $errs.Count -gt 0) { return ("line {0}: {1}" -f $errs[0].Extent.StartLineNumber, $errs[0].Message) }
    } catch { return "syntax check failed: $($_.Exception.Message)" }
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        $ps51 = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        if (Test-Path -LiteralPath $ps51) {
            $code = "`$e = `$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + $Path.Replace("'", "''") +
                    "', [ref]`$null, [ref]`$e); if (`$e.Count) { 'line ' + `$e[0].Extent.StartLineNumber + ': ' + `$e[0].Message; exit 1 }"
            $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
            $o = & $ps51 -NoProfile -NonInteractive -EncodedCommand $enc 2>&1
            if ($LASTEXITCODE -ne 0) { return ("ps5.1 " + (($o | Out-String).Trim())) }
        }
    }
    return $null
}

# 런처를 그대로 둬도 되면 $null, 아니면 이유. v2 표식만 보지 않는다 — 고치기 전 install.ps1 이 영어 Windows 5.1 에서 만든
# v2 런처는 템플릿을 cp1252 로 읽어 구문이 깨져 있는데 표식은 있다. 표식만 보면 그런 PC 는 영영 안 고쳐진다.
function Get-LauncherProblem([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "missing" }
    $head = Get-Content -LiteralPath $Path -TotalCount 1 -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not ("$head" -match "clewpath-launcher v2")) { return "not v2" }
    $e = Get-Ps1SyntaxError $Path
    if ($e) { return "v2 but syntax error ($e)" }
    return $null
}

# 템플릿으로 런처를 다시 쓴다. 성공이면 $null, 실패면 이유 — 실패하면 원래 런처(바이트 그대로)로 되돌린다.
# 쓰기는 BOM 붙은 UTF-8 로 직접(Set-Content -Encoding UTF8 은 5.1 이면 BOM, 7 이면 BOM 없음이라 셸마다 다르다).
function Write-Launcher([string]$Template, [string]$Target, [string]$DataDir, [string]$ConfFile, [int]$Port) {
    $orig = $null
    try { if (Test-Path -LiteralPath $Target) { $orig = [IO.File]::ReadAllBytes($Target) } }
    catch { return "cannot read current launcher: $($_.Exception.Message)" }
    try {
        $c = Get-Content -LiteralPath $Template -Raw -Encoding UTF8 -ErrorAction Stop
        $c = $c.TrimStart([char]0xFEFF).Replace("{{DATA_DIR}}", $DataDir).Replace("{{CONF_FILE}}", $ConfFile).Replace("{{PORT}}", "$Port")
        if (-not $c.StartsWith("# clewpath-launcher v2", [StringComparison]::Ordinal)) { throw "template is not a v2 launcher" }
        if ($c -match "\{\{[A-Z_]+\}\}") { throw "template placeholder left: $($Matches[0])" }
        [IO.File]::WriteAllText($Target, $c, (New-Object System.Text.UTF8Encoding($true)))
        if (-not (Test-Utf8Bom $Target)) { throw "written launcher has no BOM" }
        $e = Get-Ps1SyntaxError $Target
        if ($e) { throw "written launcher fails syntax check: $e" }
        return $null
    } catch {
        $why = $_.Exception.Message
        try {
            if ($null -ne $orig) { [IO.File]::WriteAllBytes($Target, $orig) }
            elseif (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Force -ErrorAction Stop }
            return "$why (original restored)"
        } catch { return "$why (restore failed: $($_.Exception.Message))" }
    }
}

# ---------------------------------------------------------------- 예약 작업

function Test-HasRepetition($Triggers) {
    foreach ($tr in @($Triggers)) { if ($tr -and $tr.Repetition -and $tr.Repetition.Interval) { return $true } }
    return $false
}

function Get-OsBuild {
    try { return [int](Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -Name CurrentBuildNumber -ErrorAction Stop).CurrentBuildNumber } catch {}
    try { return [Environment]::OSVersion.Version.Build } catch { return 0 }
}

# 작업 동작을 conhost --headless 로 감쌀지. 우리 기본 모양(install.ps1 이 등록한 그대로)일 때만 —
#   동작 1개, Execute 가 powershell.exe·pwsh.exe, 인자가 -File "<Root>\start-connector.ps1" 로 끝남.
# 이미 conhost 이거나 다른 모양(사람이 바꾼 것)이면 손대지 않는다. --headless 는 Windows 10 1809(빌드 17763)부터.
# 결과: @{ Change = $true; Execute; Argument; WorkingDirectory; Exe } 또는 @{ Change = $false; Reason }
function Get-HeadlessAction($Actions, [string]$StartPs, [int]$Build, [string]$Conhost) {
    $acts = @($Actions | Where-Object { $_ })
    if ($acts.Count -ne 1) { return @{ Change = $false; Reason = "$($acts.Count) actions" } }
    $a = $acts[0]
    $exe = "$($a.Execute)".Trim().Trim('"')
    $leaf = ""
    try { $leaf = [IO.Path]::GetFileName($exe).ToLowerInvariant() } catch {}
    if ($leaf -eq "conhost.exe") { return @{ Change = $false; Reason = "already conhost" } }
    if (@("powershell.exe", "pwsh.exe") -notcontains $leaf) { return @{ Change = $false; Reason = "not our default shape (execute: $leaf)" } }
    $arg = "$($a.Arguments)".Trim()
    $m = [regex]::Match($arg, '-File\s+"([^"]+)"$', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    $same = $false
    if ($m.Success) {
        try { $same = [string]::Equals([IO.Path]::GetFullPath($m.Groups[1].Value), [IO.Path]::GetFullPath($StartPs), [StringComparison]::OrdinalIgnoreCase) } catch {}
    }
    if (-not $same) { return @{ Change = $false; Reason = "not our default shape (arguments)" } }
    if ($Build -lt 17763) { return @{ Change = $false; Reason = "build $Build < 17763 (no conhost --headless)" } }
    if (-not $Conhost -or -not (Test-Path -LiteralPath $Conhost)) { return @{ Change = $false; Reason = "conhost.exe not found" } }
    return @{ Change = $true; Execute = $Conhost; Argument = ('--headless "' + $exe + '" ' + $arg); WorkingDirectory = "$($a.WorkingDirectory)"; Exe = $exe; Reason = "" }
}

# conhost --headless 로 그 셸을 실제로 한 번 띄워 본다(임시 파일 하나 쓰고 끝나는 명령). 동작을 바꾸기 전 마지막 확인 —
# 이게 안 되는 PC 에서 바꾸면 로그온·5분 트리거가 런처를 못 띄워 자동 시작이 통째로 죽는다.
function Test-HeadlessWorks([string]$Conhost, [string]$Exe) {
    if (-not $Conhost -or -not $Exe -or -not (Test-Path -LiteralPath $Conhost) -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    $f = Join-Path ([IO.Path]::GetTempPath()) ("clewpath-headless-probe-{0}-{1}.txt" -f $PID, [Guid]::NewGuid().ToString("N").Substring(0, 8))
    try {
        $code = "[IO.File]::WriteAllText('" + $f.Replace("'", "''") + "', 'ok')"
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
        $p = Start-Process -FilePath $Conhost -ArgumentList @("--headless", ('"' + $Exe + '"'), "-NoProfile", "-NonInteractive", "-EncodedCommand", $enc) `
                -WindowStyle Hidden -PassThru -ErrorAction Stop
        if (-not $p.WaitForExit(30000)) { try { $p.Kill() } catch {}; return $false }
        for ($i = 0; $i -lt 20 -and -not (Test-Path -LiteralPath $f); $i++) { Start-Sleep -Milliseconds 100 }
        return [bool](Test-Path -LiteralPath $f)
    } catch { return $false }
    finally { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
}

# 작업에 무엇을 할지 — @{ NeedRepetition; Action(Get-HeadlessAction 결과) }. 동작 판단은 반복 트리거가 있든 없든 같다:
# 우리 기본 모양이면 conhost 로 감싸고, 사람이 바꾼 모양(이 PC 처럼 이미 conhost 등)은 그대로 둔다.
function Get-TaskPlan($Task, [string]$StartPs, [int]$Build, [string]$Conhost) {
    return @{ NeedRepetition = (-not (Test-HasRepetition $Task.Triggers))
              Action = (Get-HeadlessAction -Actions $Task.Actions -StartPs $StartPs -Build $Build -Conhost $Conhost) }
}

# 5분 반복 트리거 — 지속 시간을 주지 않는다(= 무기한). 예전 5.1 분기가 붙이던 [TimeSpan]::MaxValue 는 작업 XML 에서
# P99999999DT23H59M59S 가 되어 Windows 11 작업 스케줄러가 "incorrectly formatted or out of range" 로 거부했다(2026-10-23 노트북,
# 0.32.0 — 트리거와 함께 conhost 동작 변경까지 실패). 작업 스케줄러 cmdlet 은 PowerShell 버전과 무관하게 OS 모듈이라 버전 분기가
# 필요 없다(pwsh 7 분기는 처음부터 지속 시간 없이 통과). -Bounded 는 무기한을 거부하는 옛 Windows 를 위한 재시도(10년).
function New-RepeatTrigger([switch]$Bounded) {
    if ($Bounded) {
        return New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650) -ErrorAction Stop
    }
    return New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -ErrorAction Stop
}

if ($LibOnly) { return }

# ================================================================ 본체
$ErrorActionPreference = "Continue"
if (-not $Root) { "error: -Root missing"; exit 1 }
$tpl = Join-Path $Root "session_manager\start-connector.template.ps1"
$startPs = Join-Path $Root "start-connector.ps1"

# 1) 런처
$problem = Get-LauncherProblem $startPs
if (-not $problem) {
    "launcher: ok (v2)"
} elseif (-not (Test-Path -LiteralPath $tpl)) {
    "launcher: template missing - skip (launcher $problem)"
    "task: skip (launcher not idempotent)"
    exit 1
} else {
    $why = Write-Launcher -Template $tpl -Target $startPs -DataDir $DataDir -ConfFile $ConfFile -Port $Port
    if ($why) {
        "launcher: regenerate failed (launcher $problem): $why"
        "task: skip (launcher not idempotent)"
        exit 1
    }
    "launcher: regenerated (v2) - was: $problem"
}

# 2) 예약 작업 — 여기까지 왔으면 런처는 v2(멱등)·구문 정상
$gerr = $null
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue -ErrorVariable gerr
if (-not $t) {
    if ($gerr -and $gerr.Count -and "$($gerr[0].FullyQualifiedErrorId)" -like "CmdletizationQuery_NotFound*") { "task: not registered - skip"; exit 0 }
    $msg = "no result"
    if ($gerr -and $gerr.Count) { $msg = $gerr[0].Exception.Message }
    "task: query failed: $msg"
    exit 1
}
$plan = Get-TaskPlan -Task $t -StartPs $startPs -Build (Get-OsBuild) -Conhost (Join-Path $env:SystemRoot "System32\conhost.exe")
$act = $plan.Action
if (-not $plan.NeedRepetition -and -not $act.Change) { "task: ok (repetition present)"; exit 0 }

$newAction = $null
if ($act.Change) {
    if (Test-HeadlessWorks -Conhost $act.Execute -Exe $act.Exe) {
        try {
            if ($act.WorkingDirectory) {
                $newAction = New-ScheduledTaskAction -Execute $act.Execute -Argument $act.Argument -WorkingDirectory $act.WorkingDirectory -ErrorAction Stop
            } else {
                $newAction = New-ScheduledTaskAction -Execute $act.Execute -Argument $act.Argument -ErrorAction Stop
            }
        } catch { $act = @{ Change = $false; Reason = "action build failed: $($_.Exception.Message)" } }
    } else { $act = @{ Change = $false; Reason = "conhost --headless probe failed" } }
}

# 반복 트리거가 이미 있으면 동작만 바꾼다(트리거·설정은 그대로). 시험 실행이 안 되면 그대로 두고 성공으로 끝낸다(깜박임만 남음).
if (-not $plan.NeedRepetition) {
    if (-not $newAction) { "action: unchanged ($($act.Reason))"; "task: ok (repetition present)"; exit 0 }
    try {
        Set-ScheduledTask -TaskName $TaskName -Action $newAction -ErrorAction Stop | Out-Null
        $t2 = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ("$(@($t2.Actions)[0].Execute)" -ne $act.Execute) { throw "action not changed after update" }
        if (-not (Test-HasRepetition $t2.Triggers)) { throw "repetition trigger lost after action update" }
    } catch {
        "task: action update failed: $($_.Exception.Message)"
        exit 1
    }
    "action: conhost --headless ($([IO.Path]::GetFileName($act.Exe)))"
    "task: ok (repetition present)"
    exit 0
}

# 동작과 트리거는 따로 바꾼다 — 한쪽이 거부돼도 다른 쪽은 들어가게(노트북 0.32.0: 트리거 거부에 conhost 동작 변경까지 묶여 실패).
$actionDone = $false
try {
    if ($newAction) {
        Set-ScheduledTask -TaskName $TaskName -Action $newAction -ErrorAction Stop | Out-Null
        $t1 = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ("$(@($t1.Actions)[0].Execute)" -ne $act.Execute) { throw "action not changed after update" }
        $actionDone = $true
    }
    $me = "$env:USERDOMAIN\$env:USERNAME"
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $me -ErrorAction Stop
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ErrorAction Stop
    $bounded = $false
    try {
        Set-ScheduledTask -TaskName $TaskName -Trigger @($logon, (New-RepeatTrigger)) -Settings $set -ErrorAction Stop | Out-Null
    } catch {
        $first = $_.Exception.Message
        try {
            Set-ScheduledTask -TaskName $TaskName -Trigger @($logon, (New-RepeatTrigger -Bounded)) -Settings $set -ErrorAction Stop | Out-Null
            $bounded = $true
        } catch { throw "$first | bounded retry: $($_.Exception.Message)" }
    }
    # 바뀌었는지 다시 읽어 확인 — 예전엔 Set 의 비종료 오류가 catch 를 지나쳐 실패해도 'added' 라고 적었다
    $t2 = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if (-not (Test-HasRepetition $t2.Triggers)) { throw "repetition trigger not present after update" }
} catch {
    if ($actionDone) { "action: conhost --headless ($([IO.Path]::GetFileName($act.Exe)))" }
    "task: update failed: $($_.Exception.Message)"
    exit 1
}
if ($newAction) { "action: conhost --headless ($([IO.Path]::GetFileName($act.Exe)))" } else { "action: unchanged ($($act.Reason))" }
if ($bounded) { "task: repetition trigger added (5m, 10 years - indefinite was refused)" } else { "task: repetition trigger added (5m)" }
exit 0
