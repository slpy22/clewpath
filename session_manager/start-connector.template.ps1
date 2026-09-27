# clewpath-launcher v2 — 자동 생성됨 (install.ps1 / ensure_task.ps1). 재설치·업데이트 때 덮어써집니다.
# 설정을 바꾸려면 이 파일이 아니라 아래 파일을 고치세요:
#   {{CONF_FILE}}
#
# 멱등 런처(v0.9.2 자기 회복): 5분 반복 트리거로 계속 불리지만, 서버가 이미 떠 있으면
# 아무것도 하지 않는다. 죽어 있을 때만(health 없음 + pid 없음) 새로 띄운다.
#  - health 실패만으로 기존 프로세스를 죽이거나 겹쳐 띄우지 않는다(기동 중일 수 있음)
#  - maintenance.flag 가 있으면 의도적 중지로 보고 손대지 않는다
#  - launcher.lock 으로 동시 실행을 막는다(2분 넘은 잠금은 시체)
#  - 기동 사유는 launcher.log 에 남긴다(무음 종료 원인 추적 실마리)
# 로그: {{DATA_DIR}}\host.log (.err.log)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$dataDir = "{{DATA_DIR}}"
$log  = Join-Path $dataDir "host.log"
$err  = Join-Path $dataDir "host.err.log"
$llog = Join-Path $dataDir "launcher.log"
function LLog($m) { try { Add-Content -Path $llog -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 } catch {} }

if (Test-Path (Join-Path $dataDir "maintenance.flag")) { exit 0 }

# 살아 있는가 — ① health(짧은 타임아웃) ② runtime.json 의 pid 가 python 으로 살아 있음(기동 중)
$port = {{PORT}}
$rt = Join-Path $dataDir "runtime.json"
$j = $null
try { if (Test-Path $rt) { $j = Get-Content $rt -Raw | ConvertFrom-Json; if ($j.port) { $port = [int]$j.port } } } catch {}
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
    if ($h.status -eq "ok") { exit 0 }
} catch {}
try {
    if ($j -and $j.pid) {
        $p = Get-Process -Id ([int]$j.pid) -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -match "python") { LLog "skip: pid $($j.pid) 살아 있음(health 없음 — 기동 중이거나 응답 불능)"; exit 0 }
    }
} catch {}

# 기동 잠금(동시 실행 방지)
$lock = Join-Path $dataDir "launcher.lock"
try { if ((Test-Path $lock) -and (((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds -lt 120)) { exit 0 } } catch {}
try { New-Item -ItemType File -Path $lock -Force | Out-Null } catch {}
try {
    $env:PYTHONUNBUFFERED = "1"     # 리다이렉트 시 버퍼링으로 로그가 안 찍히는 것 방지
    $env:PYTHONIOENCODING = "utf-8" # 로그 인코딩 고정 - 없으면 로케일(cp949)이라 특수문자에 취약
    # 무한 성장 방지 — 5MB 넘으면 한 세대 밀어둔다(돌고 있는 서버가 잡고 있으면 건너뜀)
    foreach ($f in @($log, $err)) {
        try { if ((Test-Path $f) -and ((Get-Item $f).Length -gt 5MB)) { Move-Item $f "$f.1" -Force } } catch {}
    }
    $why = if ($j -and $j.pid) { "이전 pid $($j.pid) 없음(health 없음)" } else { "runtime.json 없음" }
    LLog "start: $why → 기동"
    Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" `
        -ArgumentList "-m","session_manager.server" `
        -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err
} finally {
    try { Remove-Item $lock -Force -ErrorAction SilentlyContinue } catch {}
}
