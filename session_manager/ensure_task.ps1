# 자기 회복 보장(v0.9.2) — 기존 설치본을 새 런처·반복 트리거로 맞춘다. Host 가 기동 시 한 번 부른다.
#  1) start-connector.ps1 이 v2(멱등) 가 아니면 패키지 템플릿으로 재생성
#  2) ClewPathHost 작업에 5분 반복 트리거가 없으면 추가(로그온 트리거 유지, 중복 인스턴스 무시)
# ⚠ 순서가 안전의 전부: 런처가 멱등이 아닌데 반복 트리거를 달면 5분마다 서버가 겹쳐 뜬다.
#    그래서 1) 이 실패하면 2) 는 하지 않는다.
param(
    [string]$Root,
    [string]$DataDir,
    [string]$ConfFile,
    [int]$Port = 5100,
    [string]$TaskName = "ClewPathHost"
)
$ErrorActionPreference = "Continue"

$tpl = Join-Path $Root "session_manager\start-connector.template.ps1"
$startPs = Join-Path $Root "start-connector.ps1"
$isV2 = $false
if (Test-Path $startPs) {
    $head = Get-Content $startPs -TotalCount 1
    if ($head -match "clewpath-launcher v2") { $isV2 = $true }
}
if (-not $isV2) {
    if (Test-Path $tpl) {
        try {
            $c = (Get-Content $tpl -Raw).Replace("{{DATA_DIR}}", $DataDir).Replace("{{CONF_FILE}}", $ConfFile).Replace("{{PORT}}", "$Port")
            Set-Content -Path $startPs -Value $c -Encoding UTF8
            $isV2 = $true
            "launcher: regenerated (v2)"
        } catch { "launcher: regenerate failed: $($_.Exception.Message)" }
    } else { "launcher: template missing — skip" }
} else { "launcher: ok (v2)" }

if (-not $isV2) { "task: skip (launcher not idempotent)"; exit 0 }

try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch { "task: not registered — skip"; exit 0 }
$hasRep = $false
foreach ($tr in $t.Triggers) { if ($tr.Repetition -and $tr.Repetition.Interval) { $hasRep = $true } }
if ($hasRep) { "task: ok (repetition present)"; exit 0 }
try {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $me
    if ($PSVersionTable.PSVersion.Major -ge 7) {
        $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    } else {
        $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
    }
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    Set-ScheduledTask -TaskName $TaskName -Trigger @($logon, $rep) -Settings $set | Out-Null
    "task: repetition trigger added (5m)"
} catch { "task: update failed: $($_.Exception.Message)" }
