# 5분 반복 트리거가 이 PC 의 작업 스케줄러에 실제로 받아들여지는지 확인한다(2026-10-23 노트북 0.32.0 회귀).
# 임시 작업 하나를 이 사용자 이름으로 등록(사용 안 함 상태) → ensure_task.ps1 과 같은 함수(New-RepeatTrigger)로 트리거를 다시 달고
# 동작만 따로 바꿔 본다(ensure_task.ps1 과 같은 순서) → 읽어서 확인 → 반드시 지운다(finally). ClewPathHost 작업은 건드리지 않는다.
# 실행: powershell.exe -NoProfile -ExecutionPolicy Bypass -File ops\tools\verify-task-trigger.ps1   (5.1)
#       pwsh -NoProfile -File ops\tools\verify-task-trigger.ps1                                     (7)
# 출력은 ASCII 만(영어 Windows 5.1 콘솔). 성공이면 마지막 줄 "RESULT: OK", exit 0.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $root "session_manager\ensure_task.ps1") -LibOnly
$name = "ClewPathVerify-trigger-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$me = "$env:USERDOMAIN\$env:USERNAME"
"engine: PowerShell $($PSVersionTable.PSVersion) / build $([Environment]::OSVersion.Version.Build)"
"task: $name (temporary, disabled, removed at the end)"
$ok = $true
try {
    $act = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument "/c exit 0"
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $me
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    # 1) install.ps1 과 같은 첫 등록(로그온 + 무기한 5분 반복)
    Register-ScheduledTask -TaskName $name -Action $act -Trigger @($logon, (New-RepeatTrigger)) -Principal $principal -Settings $set -Force | Out-Null
    Disable-ScheduledTask -TaskName $name | Out-Null
    "register (indefinite): ok"
    # 2) ensure_task.ps1 과 같은 순서: 동작만 따로 바꾸고, 트리거를 따로 다시 단다
    $act2 = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument "/c exit 1"
    Set-ScheduledTask -TaskName $name -Action $act2 | Out-Null
    Set-ScheduledTask -TaskName $name -Trigger @($logon, (New-RepeatTrigger)) -Settings $set | Out-Null
    "set action, then set triggers (indefinite): ok"
    # 3) 10년 재시도 모양도 받아들여지는지
    Set-ScheduledTask -TaskName $name -Trigger @($logon, (New-RepeatTrigger -Bounded)) -Settings $set | Out-Null
    "set triggers (10 years): ok"
    Set-ScheduledTask -TaskName $name -Trigger @($logon, (New-RepeatTrigger)) -Settings $set | Out-Null
    $t = Get-ScheduledTask -TaskName $name
    $rep = @($t.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval })
    "read back: triggers=$(@($t.Triggers).Count) repetition interval=$($rep[0].Repetition.Interval) duration='$($rep[0].Repetition.Duration)' action=$(@($t.Actions)[0].Arguments)"
    if ($rep.Count -ne 1 -or "$($rep[0].Repetition.Interval)" -ne "PT5M" -or "$($rep[0].Repetition.Duration)" -ne "") { $ok = $false; "check: repetition is not indefinite 5 minutes" }
    if ("$(@($t.Actions)[0].Arguments)" -ne "/c exit 1") { $ok = $false; "check: action not changed" }
    if (-not (Test-HasRepetition $t.Triggers)) { $ok = $false; "check: Test-HasRepetition false" }
} catch {
    $ok = $false
    "FAILED: $($_.Exception.Message)"
} finally {
    try { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop; "cleanup: removed $name" }
    catch { "cleanup: not found or failed - $($_.Exception.Message)" }
}
if ($ok) { "RESULT: OK"; exit 0 } else { "RESULT: FAIL"; exit 1 }
