# =====================================================================
#  ClewPath Host 배포용 zip 을 만든다 (비밀 제외).
#
#  왜 필요한가:
#    이 폴더를 그냥 zip 으로 주면 start-public.ps1 / start-relay.ps1 에 들어있는
#    운영자의 라이브 비밀(릴레이 agent/client 토큰, SM_PASSWORD, SM_API_TOKEN)이
#    그대로 새어나간다. 이 스크립트는 그 파일들을 제외하고 패키징한다.
#
#  사용법:
#    pwsh -File package-for-user.ps1                  # 기본: .\dist\clewpath-host-<날짜>.zip
#    pwsh -File package-for-user.ps1 -OutDir D:\share
#
#  사용자에게 함께 전달할 것(zip 에는 넣지 않는다 — 채널을 분리):
#    - ClientToken (폰 페어링 QR 생성용 공유 client 토큰)
#    - AgentToken  (선택/레거시. 지금은 CP 발급 JWT 를 쓰므로 보통 불필요)
#  사용자 실행:
#    pwsh -File install.ps1 -ClientToken "..."
#    → uv 를 알아서 받아오므로 사용자 PC 에 파이썬이 미리 없어도 된다
# =====================================================================
[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path $PSScriptRoot "dist"),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
# ops/ 로 이사(2026-08-04): 배포 대상(제품)은 저장소 루트에 있다
$Root = Split-Path -Parent $PSScriptRoot

# 배포에 포함할 것만 화이트리스트로 (블랙리스트는 새 파일이 생기면 새어나간다)
# control_plane/ relay/ 는 서버(도커) 전용이라 넣지 않는다 — 사용자 PC 에서
# 실행될 일이 없고, 서버 소스를 말단에 뿌릴 이유도 없다. (updater.PAYLOAD 와 짝)
$include = @(
    "session_manager",
    "pwa",
    "pyproject.toml",
    "uv.lock",          # 의존성 고정 — 없으면 사용자마다 버전이 갈린다
    "install.ps1",
    "README.md",
    "LICENSE"
)

# 포함 목록 안에서도 제외할 패턴(비밀·캐시·가상환경·로컬산출물)
$excludePatterns = @(
    "__pycache__", "*.pyc", ".venv", "dist",
    "start-public.ps1", "start-relay.ps1", "start-connector.ps1",
    "release-keys", "*.pem",          # 릴리스 서명 개인키 — 최우선 차단
    "*.db", "*.env", ".env", "*.log"
)

function Test-Excluded($relPath) {
    foreach ($p in $excludePatterns) {
        if ($relPath -like "*$p*") { return $true }
    }
    return $false
}

$stamp   = Get-Date -Format "yyyyMMdd"
$stage   = Join-Path $env:TEMP "sm-pkg-$stamp-$PID"
$zipPath = Join-Path $OutDir "clewpath-host-$stamp.zip"

Write-Host ""
Write-Host "=== 배포 패키지 생성 ===" -ForegroundColor Cyan
Write-Host "  대상 : $zipPath"

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ── 복사 ─────────────────────────────────────────────────────
$copied = 0
foreach ($item in $include) {
    $src = Join-Path $Root $item
    if (-not (Test-Path $src)) { Write-Host "  [skip] $item (없음)" -ForegroundColor DarkGray; continue }
    if (Test-Path $src -PathType Container) {
        Get-ChildItem $src -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($Root.Length).TrimStart('\')
            if (Test-Excluded $rel) { return }
            $dst = Join-Path $stage $rel
            New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
            Copy-Item $_.FullName $dst
            $script:copied++
        }
    } else {
        if (Test-Excluded $item) { return }
        Copy-Item $src (Join-Path $stage $item)
        $script:copied++
    }
}
Write-Host "  파일 $copied 개 준비"

# ── 비밀 유출 검사 (게이트) ──────────────────────────────────
# 실제 토큰 형태를 훑는다. 하나라도 걸리면 zip 을 만들지 않는다.
$secretPatterns = @(
    'rly-agt-[A-Za-z0-9_\-]{16,}',
    'rly-cli-[A-Za-z0-9_\-]{16,}',
    'sk-sm-[A-Za-z0-9]{16,}',
    'cxs_[A-Za-z0-9_\-]{16,}',
    'clm_[A-Za-z0-9_\-]{16,}',
    'adm-[A-Za-z0-9_\-]{16,}',
    'cp-[A-Za-z0-9_\-]{20,}',
    # 릴리스 서명 개인키가 실수로 섞이면 그 순간 서명 체계 전체가 무의미해진다.
    # 공개키(updater.py 의 RELEASE_KEYS)는 base64 라 이 패턴에 안 걸린다.
    '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)
# 스테이지에 키 파일이 통째로 섞였는지 먼저 본다.
# 아래 문자열 스캔은 텍스트 확장자만 훑기 때문에 .pem 같은 파일은 그냥 지나친다 —
# 화이트리스트/excludePatterns 가 이미 막고 있지만, 릴리스 키가 새면 사용자 PC 전체에
# 코드를 밀어넣을 수 있는 사안이라 독립된 장치를 하나 더 둔다.
$keyFiles = Get-ChildItem $stage -Recurse -File | Where-Object {
    $_.Extension -in @(".pem", ".key", ".pfx", ".p12") -or
    $_.FullName -like "*\release-keys\*"
}
if ($keyFiles.Count -gt 0) {
    Write-Host ""
    Write-Host "  [FAIL] 키 파일이 패키지에 섞였습니다 - 패키징 중단" -ForegroundColor Red
    $keyFiles | ForEach-Object { Write-Host "     $($_.FullName.Substring($stage.Length).TrimStart('\'))" -ForegroundColor Red }
    Remove-Item -Recurse -Force $stage
    exit 1
}

$leaks = @()
Get-ChildItem $stage -Recurse -File | Where-Object {
    $_.Extension -in @(".py", ".ps1", ".toml", ".md", ".html", ".json", ".txt", ".yml", ".yaml")
} | ForEach-Object {
    $text = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { return }
    foreach ($pat in $secretPatterns) {
        foreach ($m in [regex]::Matches($text, $pat)) {
            $leaks += [pscustomobject]@{
                File  = $_.FullName.Substring($stage.Length).TrimStart('\')
                Match = $m.Value.Substring(0, [Math]::Min(18, $m.Value.Length)) + "..."
            }
        }
    }
}

if ($leaks.Count -gt 0) {
    Write-Host ""
    Write-Host "  [FAIL] 비밀로 보이는 문자열이 발견됐습니다 — 패키징 중단" -ForegroundColor Red
    $leaks | Group-Object File | ForEach-Object {
        Write-Host "     $($_.Name)" -ForegroundColor Red
        $_.Group | Select-Object -First 3 | ForEach-Object { Write-Host "        $($_.Match)" -ForegroundColor DarkRed }
    }
    Write-Host ""
    Write-Host "  해당 파일을 excludePatterns 에 추가하거나 값을 제거한 뒤 다시 실행하세요." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $stage
    exit 1
}
Write-Host "  [OK]   비밀 스캔 통과" -ForegroundColor Green

# ── zip ──────────────────────────────────────────────────────
if (Test-Path $zipPath) {
    if ($Force) { Remove-Item -Force $zipPath }
    else { Write-Host "  [FAIL] 이미 존재합니다: $zipPath (-Force 로 덮어쓰기)" -ForegroundColor Red
           Remove-Item -Recurse -Force $stage; exit 1 }
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath
Remove-Item -Recurse -Force $stage

$size = [Math]::Round((Get-Item $zipPath).Length / 1KB, 1)
Write-Host "  [OK]   생성 완료 ($size KB)" -ForegroundColor Green
Write-Host ""
Write-Host "다음 단계:" -ForegroundColor Cyan
Write-Host "  1) 사용자에게 zip 전달"
Write-Host "  2) 토큰은 '별도 채널'로 전달 (zip 에 없음):"
Write-Host "       ClientToken : util/.env 의 RELAY_CLIENT_TOKEN"
Write-Host "       AgentToken  : util/.env 의 RELAY_AGENT_TOKEN (선택/레거시)"
Write-Host "  3) 사용자 실행:"
Write-Host "       pwsh -File install.ps1 -ClientToken '<...>'"
Write-Host ""
