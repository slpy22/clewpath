# =====================================================================
#  ClewPath Host 릴리스 한 방: 사전점검 → 패키징 → 서명 → 검증 → (게시) → (이 PC 적용·헬스 확인)
#
#    pwsh -File ops/release.ps1 -Version 0.9.4 -Notes "무엇이 바뀌었나"            # 패키지+서명+검증까지
#    pwsh -File ops/release.ps1 -Version 0.9.4 -Notes "..." -Publish              # + CP 게시
#    pwsh -File ops/release.ps1 -Version 0.9.4 -Notes "..." -Publish -Apply       # + 이 PC 적용·헬스·조용한 재기동 확인
#    -Platforms win,mac                                                           # 받는 OS(기본 win). mac·linux 는 같은 HEAD 의
#                                                                                 #   POSIX e2e 기록(ops/logs/posix-e2e-<ver>.json)이 있어야
#
#  왜: 손으로 7단계(package → rename → sign → verify → publish → apply → poll)를 11번 반복하며
#  실수 표면이 컸다(retro 2026-09-27). 순서·이름 규칙·토큰 취급을 한 곳에 고정한다.
#  토큰: SM_CP_ADMIN_TOKEN 은 -EnvFile 에서 읽어 python 인자로만 넘기고 절대 출력하지 않는다.
#  로그: ops/logs/release-<ver>.log (토큰 없음)
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [switch]$PrepareChangelog,
    [switch]$Publish,
    [switch]$Apply,
    [switch]$SkipTests,
    [switch]$AllowDirty,
    [string]$OutDir = "",
    [string]$CpUrl = "http://127.0.0.1:5200",
    [string]$HostUrl = "http://127.0.0.1:5100",
    [string]$DownloadBase = "https://clewpath.pyongso.com/cp/updates/download",
    [string]$EnvFile = "E:/018_whisper/util/.env",
    [string]$Python = "python",
    [string[]]$Platforms = @("win")
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

# 받는 OS(매니페스트 platforms 클레임, docs/designs/posix-install.md 4.6) — 값은 osdep.NAME, 정해진 순서로 "win,mac" 꼴.
# win 은 늘 들어간다: CP 는 최신 한 건만 주므로 win 이 빠진 릴리스가 최신인 동안 Windows 사용자 전체가 업데이트를
# 못 받고, get.ps1(Windows 원라인 설치)은 platforms 를 보지 않아 그 zip 을 그대로 깐다(8절 R6).
function Resolve-Platforms([string[]]$Names) {
    $known = @("win", "mac", "linux")
    $want = @($Names | ForEach-Object { "$_" -split '[,\s]+' } | Where-Object { $_ } | ForEach-Object { $_.ToLowerInvariant() })
    $bad = @($want | Where-Object { $known -notcontains $_ })
    if ($bad.Count) { throw "모르는 플랫폼: $($bad -join ', ') (쓸 수 있는 값: win, mac, linux)" }
    if ($want -notcontains "win") { throw "-Platforms 에 win 이 없습니다 - CP 는 최신 한 건만 주므로 그동안 Windows 사용자가 업데이트를 못 받습니다" }
    return (@($known | Where-Object { $want -contains $_ }) -join ",")
}

# updater.py 의 RELEASE_KEYS kid 목록(정렬). 주석 줄의 옛 키는 세지 않는다. 못 읽으면 빈 결과
function Get-ReleaseKids([string]$Text) {
    $m = [regex]::Match($Text, '(?ms)^RELEASE_KEYS\b[^\n=]*=\s*\{(.*?)^\}')
    if (-not $m.Success) { return }
    $body = [regex]::Replace($m.Groups[1].Value, '(?m)#.*$', '')
    [regex]::Matches($body, '"([0-9a-f]{16})"\s*:') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
}

# zip 내용 게이트(posix-install.md 4.5) — 맥·리눅스 설치에 필요한 파일, 항목 이름 구분자(맥·리눅스에서 '\' 는 이름의
# 일부가 된다 — Windows PowerShell 5.1 의 Compress-Archive 가 그렇게 만든다), 배포본이 믿는 서명 키(시험 키를 심은 시험
# 빌드는 여기서 걸린다 — sign 은 시험 하네스도 쓰므로 이 검사는 게시 흐름인 여기에만 둔다). 돌려주는 값: 문제 목록
function Test-ReleaseZip([string]$ZipPath, [string]$RepoRoot) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $problems = New-Object System.Collections.Generic.List[string]
    $a = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $names = @($a.Entries | ForEach-Object { $_.FullName })
        foreach ($need in @("install.sh", "uninstall.sh", "session_manager/update_runner.sh",
                            "session_manager/start-connector.template.sh", "session_manager/hostctl.py")) {
            if ($names -cnotcontains $need) { $problems.Add("zip 에 $need 이(가) 없습니다") }
        }
        $bs = @($names | Where-Object { $_.Contains('\') })
        if ($bs.Count) { $problems.Add("항목 이름에 역슬래시가 있습니다(맥·리눅스에서는 이름의 일부가 됩니다): $($bs[0]) 외 $($bs.Count - 1)개") }
        $upd = @($a.Entries | Where-Object { $_.FullName -ceq "session_manager/updater.py" })
        if (-not $upd.Count) { $problems.Add("zip 에 session_manager/updater.py 가 없습니다") }
        else {
            $sr = New-Object System.IO.StreamReader($upd[0].Open())
            try { $zipText = $sr.ReadToEnd() } finally { $sr.Dispose() }
            $zipKids = @(Get-ReleaseKids $zipText) -join ","
            $repoKids = @(Get-ReleaseKids ([System.IO.File]::ReadAllText((Join-Path $RepoRoot "session_manager\updater.py")))) -join ","
            if (-not $zipKids -or -not $repoKids) { $problems.Add("RELEASE_KEYS 를 읽지 못했습니다(zip: '$zipKids', 저장소: '$repoKids')") }
            elseif ($zipKids -cne $repoKids) { $problems.Add("zip 의 RELEASE_KEYS(kid $zipKids)가 저장소(kid $repoKids)와 다릅니다 - 시험 빌드는 게시하지 않습니다") }
        }
    } finally { $a.Dispose() }
    return $problems.ToArray()
}

Log "=== release $Version 시작 (publish=$Publish apply=$Apply prepare=$PrepareChangelog) ==="

# ── 0) 사전점검 ─────────────────────────────────────────────
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "버전 형식은 X.Y.Z 여야 합니다: $Version" }
try { $plats = Resolve-Platforms $Platforms } catch { Fail $_.Exception.Message }
Log "받는 OS(platforms): $plats"
$Changelog = Join-Path $Root "CHANGELOG.md"
$clText = if (Test-Path $Changelog) { Get-Content $Changelog -Raw } else { "" }
$hasEntry = $clText -match "(?m)^## $([regex]::Escape($Version)) \("
if ($PrepareChangelog) {
    # 항목만 써 주고 끝난다 — bump 와 함께 커밋한 뒤 실제 릴리스를 돌린다(트리 깨끗 게이트와 순서 맞춤)
    if (-not $Notes) { Fail "-PrepareChangelog 에는 -Notes 가 필요합니다" }
    if ($hasEntry) { Fail "CHANGELOG.md 에 이미 ## $Version 항목이 있습니다" }
    $entry = "## $Version ($(Get-Date -Format 'yyyy-MM-dd'))`n$Notes`n`n"
    $idx = $clText.IndexOf("`n## ")
    $new = if ($idx -ge 0) { $clText.Substring(0, $idx + 1) + $entry + $clText.Substring($idx + 1) } else { $clText.TrimEnd() + "`n`n" + $entry }
    Set-Content -Path $Changelog -Value $new -Encoding UTF8 -NoNewline
    Log "CHANGELOG.md 에 ## $Version 항목 추가 — pyproject bump 와 함께 커밋한 뒤 -Publish 로 릴리스하세요"
    exit 0
}
if (-not $hasEntry) { Fail "CHANGELOG.md 에 ## $Version 항목이 없습니다 — 먼저 `pwsh -File ops/release.ps1 -Version $Version -Notes `"…`" -PrepareChangelog` 후 커밋" }
if (-not $Notes) {
    # -Notes 생략 시 CHANGELOG 항목 본문을 매니페스트 notes 로 쓴다(한 문장 정본)
    $m = [regex]::Match($clText, "(?ms)^## $([regex]::Escape($Version)) \([^)]*\)\r?\n(.*?)(?=\r?\n## |\z)")
    $Notes = $m.Groups[1].Value.Trim()
    if (-not $Notes) { Fail "CHANGELOG.md 의 ## $Version 항목이 비어 있습니다" }
}
$pyver = (Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if ($pyver -ne $Version) { Fail "pyproject.toml 버전($pyver) 과 -Version($Version) 이 다릅니다 — 먼저 bump 하고 커밋하세요" }
$dirty = (git status --porcelain 2>$null | Where-Object { $_ -notmatch '^\?\? (003_crosscheck|\.context)' })
if ($dirty -and -not $AllowDirty) { Fail "작업 트리가 깨끗하지 않습니다(커밋 먼저, 또는 -AllowDirty):`n$($dirty -join "`n")" }
if (-not (Test-Path (Join-Path $Root "ops\release-keys\private.pem"))) { Fail "서명 키가 없습니다: ops/release-keys/private.pem" }
if ($plats -ne "win") {
    # 맥·리눅스가 받는 릴리스는 같은 HEAD 에서 통과한 컨테이너 왕복(pytest -m posix_e2e) 기록이 있어야 한다(4.5·Q17).
    # 기록 모양: {"version": "<ver>", "head": "<git rev-parse HEAD>", "ok": true} — .gitignore 라 트리 깨끗 게이트에 안 걸린다
    $e2eRec = Join-Path $PSScriptRoot "logs\posix-e2e-$Version.json"
    $head = (git rev-parse HEAD 2>$null | Out-String).Trim()
    $rec = $null
    if (Test-Path $e2eRec) { try { $rec = Get-Content $e2eRec -Raw | ConvertFrom-Json } catch { $rec = $null } }
    if (-not $rec -or $rec.ok -ne $true -or "$($rec.version)" -ne $Version -or "$($rec.head)" -ne $head) {
        Fail "맥·리눅스($plats)가 받는 릴리스는 같은 HEAD($head)에서 통과한 POSIX e2e 기록이 필요합니다: $e2eRec"
    }
    Log "POSIX e2e 기록 확인: $e2eRec (head $head)"
}
# 의존성 잠금 검사(0.10.6): pyproject 에 새 의존성을 넣고 uv.lock 을 안 갱신하면 설치본은 `uv sync --frozen` 이라
# 그 의존성이 영원히 설치되지 않는다(0.10.5 psutil 실사고). lock 이 어긋나면 릴리스를 막는다.
$uvExe = Join-Path $env:LOCALAPPDATA "ClewPath\bin\uv.exe"
if (-not (Test-Path $uvExe)) { $uvExe = (Get-Command uv -ErrorAction SilentlyContinue).Source }
if ($uvExe) {
    # lock 검사는 의존성 해석만 한다 — 프로젝트 .venv 를 건드리지 않게 임시 환경 경로 + 시스템 파이썬을 쓴다
    # (2026-10-01: Windows 응용 프로그램 제어가 .venv\Scripts\python.exe 실행을 막아 uv lock 이 실패한 사례).
    $prevUvEnv = $env:UV_PROJECT_ENVIRONMENT
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $env:TEMP 'clewpath-uv-lock-env'
    $pyExe = (Get-Command $Python -ErrorAction SilentlyContinue).Source
    if ($pyExe) { & $uvExe lock --check --python $pyExe 2>$null | Out-Null } else { & $uvExe lock --check 2>$null | Out-Null }
    $env:UV_PROJECT_ENVIRONMENT = $prevUvEnv
    if ($LASTEXITCODE -ne 0) { Fail "uv.lock 이 pyproject.toml 과 어긋납니다 - uv lock 으로 갱신하고 커밋하세요(설치본 의존성 누락 방지)" }
} else { Log "uv 를 못 찾아 lock 검사를 건너뜁니다" }
if (-not $SkipTests) {
    Log "테스트 실행(pytest -q)…"
    $t = & $Python -m pytest -q 2>&1 | Select-Object -Last 1
    Log "  $t"
    if ($LASTEXITCODE -ne 0 -or $t -notmatch 'passed' -or $t -match 'failed') { Fail "테스트 실패 — 릴리스 중단" }
    # 페어링 e2e 게이트(설계 6-1): 실 CP 를 상대로 tests/e2e 를 돈다. SM_E2E_CP_URL 이 없으면 건너뛰고 알린다.
    if ($env:SM_E2E_CP_URL) {
        Log "e2e 실행(pytest -m e2e, CP=$($env:SM_E2E_CP_URL))…"
        $e = & $Python -m pytest -q -m e2e -o addopts="" tests/e2e 2>&1 | Select-Object -Last 1
        Log "  $e"
        if ($LASTEXITCODE -ne 0 -or $e -match 'failed' -or $e -match 'error') { Fail "e2e 실패 — 릴리스 중단" }
    } else {
        Log "e2e 건너뜀(SM_E2E_CP_URL 없음) — 0.11.0 이후 릴리스는 설정 권장"
    }
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
$zipProblems = @(Test-ReleaseZip $zip $Root)
if ($zipProblems.Count) { Fail ("zip 내용 게이트:`n  " + ($zipProblems -join "`n  ")) }
Log "zip 내용 게이트 통과(맥·리눅스 스크립트·이름 구분자 /·RELEASE_KEYS 일치)"

# ── 2) 서명 + 검증 ──────────────────────────────────────────
$manifest = Join-Path $OutDir "manifest-$Version.jwt"
$sign = & $Python (Join-Path $PSScriptRoot "tools\sign_release.py") sign --version $Version --artifact $zip `
          --url "$DownloadBase/$Version" --out $manifest --notes $Notes --platforms $plats 2>&1
if ($LASTEXITCODE -ne 0) { Fail "서명 실패:`n$sign" }
Log ("서명: " + (($sign | Select-String "ver=") -join ""))
$ver = & $Python (Join-Path $PSScriptRoot "tools\sign_release.py") verify --manifest $manifest 2>&1
if ($LASTEXITCODE -ne 0 -or ($ver -join "`n") -notmatch "OK") { Fail "검증 실패:`n$ver" }
if (($ver -join "`n") -notmatch "(?m)^platforms=$([regex]::Escape($plats))\s*$") { Fail "매니페스트의 platforms 가 -Platforms($plats) 와 다릅니다:`n$ver" }
Log "검증: OK (platforms=$plats)"
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
    # 300초: apply 는 받기·스테이징·작업 폴더 정리(prune_work_dir)까지 한 요청 안에서 한다 — 처음 정리는 쌓인 버전이 많으면 오래 걸려
    #   30초로는 0.32.4 출시 때 요청만 끊기고 Host 는 혼자 적용을 마쳤다(2026-10-26).
    $ap = Invoke-RestMethod -Uri "$HostUrl/api/owner/update/apply" -Method Post -TimeoutSec 300
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
