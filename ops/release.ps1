# =====================================================================
#  ClewPath Host 릴리스 한 방: 사전점검 → 패키징 → 서명 → 검증 → (게시) → (이 PC 적용·헬스 확인)
#
#    pwsh -File ops/release.ps1 -Version 0.9.4 -Notes "무엇이 바뀌었나"            # 패키지+서명+검증까지
#    pwsh -File ops/release.ps1 -Version 0.9.4 -Notes "..." -Publish              # + CP 게시
#    pwsh -File ops/release.ps1 -Version 0.9.4 -Notes "..." -Publish -Apply       # + 이 PC 적용·헬스·조용한 재기동 확인
#
#  왜: 손으로 7단계(package → rename → sign → verify → publish → apply → poll)를 11번 반복하며
#  실수 표면이 컸다(retro 2026-09-27). 순서·이름 규칙·토큰 취급을 한 곳에 고정한다.
#  토큰: SM_CP_ADMIN_TOKEN 은 -EnvFile 에서 읽어 python 인자로만 넘기고 절대 출력하지 않는다.
#  로그: ops/logs/release-<ver>.log (토큰 없음)
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [switch]$Publish,
    [switch]$Apply,
    [switch]$SkipTests,
    [switch]$AllowDirty,
    [string]$OutDir = "",
    [string]$CpUrl = "http://127.0.0.1:5200",
    [string]$HostUrl = "http://127.0.0.1:5100",
    [string]$DownloadBase = "https://clewpath.pyongso.com/cp/updates/download",
    [string]$EnvFile = "E:/018_whisper/util/.env",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot "dist" }
New-Item -ItemType Directory -Force -Path $OutDir, (Join-Path $PSScriptRoot "logs") | Out-Null
$LogFile = Join-Path $PSScriptRoot "logs\release-$Version.log"
function Log($m) { $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m; Write-Host $line; Add-Content -Path $LogFile -Value $line -Encoding UTF8 }
function Fail($m) { Log "FAIL: $m"; exit 1 }

Log "=== release $Version 시작 (publish=$Publish apply=$Apply) ==="

# ── 0) 사전점검 ─────────────────────────────────────────────
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "버전 형식은 X.Y.Z 여야 합니다: $Version" }
$pyver = (Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if ($pyver -ne $Version) { Fail "pyproject.toml 버전($pyver) 과 -Version($Version) 이 다릅니다 — 먼저 bump 하고 커밋하세요" }
$dirty = (git status --porcelain 2>$null | Where-Object { $_ -notmatch '^\?\? (003_crosscheck|\.context)' })
if ($dirty -and -not $AllowDirty) { Fail "작업 트리가 깨끗하지 않습니다(커밋 먼저, 또는 -AllowDirty):`n$($dirty -join "`n")" }
if (-not (Test-Path (Join-Path $Root "ops\release-keys\private.pem"))) { Fail "서명 키가 없습니다: ops/release-keys/private.pem" }
if (-not $SkipTests) {
    Log "테스트 실행(pytest -q)…"
    $t = & $Python -m pytest -q 2>&1 | Select-Object -Last 1
    Log "  $t"
    if ($LASTEXITCODE -ne 0 -or $t -notmatch 'passed' -or $t -match 'failed') { Fail "테스트 실패 — 릴리스 중단" }
}

# ── 1) 패키징 ───────────────────────────────────────────────
$stamp = Get-Date -Format "yyyyMMdd"
$rawZip = Join-Path $OutDir "clewpath-host-$stamp.zip"
$zip    = Join-Path $OutDir "clewpath-host-$stamp-$Version.zip"
if (Test-Path $zip) { Fail "이미 존재: $zip (같은 날 같은 버전을 두 번 패키징하지 않습니다)" }
& pwsh -NoProfile -File (Join-Path $PSScriptRoot "package-for-user.ps1") -OutDir $OutDir -Force | Out-Null
if (-not (Test-Path $rawZip)) { Fail "패키징 결과가 없습니다: $rawZip" }
Move-Item -Force $rawZip $zip
Log "패키지: $zip ($([math]::Round((Get-Item $zip).Length/1KB)) KB)"
# 패키지 안 버전 확인(pyproject 이 들어갔는지)
Add-Type -AssemblyName System.IO.Compression.FileSystem
$a = [System.IO.Compression.ZipFile]::OpenRead($zip)
try {
    $e = $a.Entries | Where-Object { $_.FullName -eq "pyproject.toml" }
    if (-not $e) { Fail "zip 에 pyproject.toml 이 없습니다" }
    $sr = New-Object System.IO.StreamReader($e.Open()); $txt = $sr.ReadToEnd(); $sr.Dispose()
    if ($txt -notmatch "version\s*=\s*`"$([regex]::Escape($Version))`"") { Fail "zip 안 pyproject 버전이 $Version 이 아닙니다" }
} finally { $a.Dispose() }

# ── 2) 서명 + 검증 ──────────────────────────────────────────
$manifest = Join-Path $OutDir "manifest-$Version.jwt"
$sign = & $Python (Join-Path $PSScriptRoot "tools\sign_release.py") sign --version $Version --artifact $zip `
          --url "$DownloadBase/$Version" --out $manifest --notes $Notes 2>&1
if ($LASTEXITCODE -ne 0) { Fail "서명 실패:`n$sign" }
Log ("서명: " + (($sign | Select-String "ver=") -join ""))
$ver = & $Python (Join-Path $PSScriptRoot "tools\sign_release.py") verify --manifest $manifest 2>&1
if ($LASTEXITCODE -ne 0 -or ($ver -join "`n") -notmatch "OK") { Fail "검증 실패:`n$ver" }
Log "검증: OK"
$sha = ((Get-FileHash $zip -Algorithm SHA256).Hash).ToLower()

# ── 3) 게시 ─────────────────────────────────────────────────
if ($Publish) {
    if (-not (Test-Path $EnvFile)) { Fail "토큰 파일이 없습니다: $EnvFile" }
    $tok = (Get-Content $EnvFile | Where-Object { $_ -match '^SM_CP_ADMIN_TOKEN=' } | Select-Object -First 1) -replace '^SM_CP_ADMIN_TOKEN=', '' -replace '"', ''
    if (-not $tok) { Fail "SM_CP_ADMIN_TOKEN 이 $EnvFile 에 없습니다" }
    $pub = & $Python (Join-Path $PSScriptRoot "tools\sign_release.py") publish --cp $CpUrl --admin-token $tok `
             --manifest $manifest --artifact $zip 2>&1
    $pubText = ($pub -join "`n") -replace [regex]::Escape($tok), "<token>"
    if ($LASTEXITCODE -ne 0 -or $pubText -notmatch '"published"\s*:\s*"' + [regex]::Escape($Version)) { Fail "게시 실패:`n$pubText" }
    if ($pubText -notmatch $sha) { Fail "게시된 sha256 이 로컬 zip 과 다릅니다:`n$pubText" }
    Log "게시: $Version → $CpUrl (sha 일치)"
    $tok = $null
} else { Log "게시 건너뜀(-Publish 없음)" }

# ── 4) 이 PC 적용 + 헬스 + 조용한 재기동 확인 ────────────────
if ($Apply) {
    if (-not $Publish) { Fail "-Apply 는 -Publish 와 함께 써야 합니다(Host 는 CP 에서 받습니다)" }
    $dataDir = Join-Path $env:USERPROFILE ".claude\session_manager"
    $incFile = Join-Path $dataDir "incidents.jsonl"
    $incBefore = if (Test-Path $incFile) { (Get-Content $incFile | Measure-Object -Line).Lines } else { 0 }
    $st = Invoke-RestMethod -Uri "$HostUrl/api/owner/update/status?refresh=1" -TimeoutSec 30
    if ($st.latest -ne $Version) { Fail "Host 가 보는 최신 버전이 $($st.latest) 입니다(기대 $Version) — CP 게시를 확인하세요" }
    Log "Host: current=$($st.current) latest=$($st.latest) → apply"
    $ap = Invoke-RestMethod -Uri "$HostUrl/api/owner/update/apply" -Method Post -TimeoutSec 30
    if (-not $ap.applied) { Fail "apply 거부: $($ap | ConvertTo-Json -Compress)" }
    $deadline = (Get-Date).AddSeconds(150); $up = $false; $sawDown = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        try {
            $d = Invoke-RestMethod -Uri "$HostUrl/api/owner/diagnostics" -TimeoutSec 3
            if ($d.version -eq $Version) { $up = $true; break }
        } catch { $sawDown = $true }
    }
    if (-not $up) { Fail "Host 가 $Version 으로 올라오지 않았습니다(150초). 로그: $dataDir\updates\apply-$Version.log" }
    $applyLog = Join-Path $dataDir "updates\apply-$Version.log"
    if (Test-Path $applyLog) { Log ("apply 로그: " + ((Get-Content $applyLog -Tail 2) -join " | ")) }
    Start-Sleep -Seconds 5
    $incAfter = if (Test-Path $incFile) { (Get-Content $incFile | Measure-Object -Line).Lines } else { 0 }
    if ($incAfter -ne $incBefore) { Log "⚠ 업데이트 재기동이 '비정상 종료 의심'으로 기록됨(incidents $incBefore → $incAfter) — 표식 경로 확인" }
    else { Log "재기동 조용함(incidents $incBefore 유지)" }
    Log "적용 완료: Host $Version 가동 중"
}

Log "=== release $Version 끝 ==="
Write-Host ""
Write-Host "다음: git tag v$Version (선택) · TODOS/문서 갱신 · 병합·push"
