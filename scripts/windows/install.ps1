. "$PSScriptRoot\common.ps1"
Write-Host "카드샵 공지 스튜디오 설치를 시작합니다." -ForegroundColor White

Write-Step "1/4 uv(파이썬 실행 도구) 확인"
Add-UvToPath
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  uv를 설치합니다 (공식 설치 스크립트: astral.sh)..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    Add-UvToPath
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv를 설치하지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요." }
}
Write-Ok "uv 준비됨"

Write-Step "2/4 프로그램 구성 요소 설치 (처음에는 몇 분 걸립니다)"
uv sync --no-dev
if ($LASTEXITCODE -ne 0) { throw "구성 요소를 설치하지 못했습니다. 위 오류를 확인하세요." }
Write-Ok "설치 완료"

Write-Step "3/4 설정 파일 준비"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Ok ".env 를 만들었습니다"
} else {
    Write-Ok "기존 .env 를 그대로 씁니다"
}
$envText = Get-Content ".env" -Raw -Encoding UTF8
if ($envText -match '(?m)^JWT_SECRET=""\s*$' -or $envText -notmatch '(?m)^JWT_SECRET=') {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    if ($envText -match '(?m)^JWT_SECRET=') { $envText = $envText -replace '(?m)^JWT_SECRET=.*$', "JWT_SECRET=""$secret""" }
    else { $envText += "`nJWT_SECRET=""$secret""`n" }
    [System.IO.File]::WriteAllText((Join-Path $Root ".env"), $envText, (New-Object System.Text.UTF8Encoding $false))
    Write-Ok "Postiz 보안 키를 만들었습니다"
}

Write-Step "4/4 Postiz(X·인스타그램 연동) 준비 — 처음에는 수 GB를 내려받습니다"
if ($env:STUDIO_SKIP_POSTIZ) { Write-Warn "건너뜀 (STUDIO_SKIP_POSTIZ)" } else { Start-Postiz }

if (-not $env:STUDIO_SKIP_SHORTCUT) {
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "카드샵 공지 스튜디오.lnk"))
$shortcut.TargetPath = Join-Path $Root "start.bat"
$shortcut.WorkingDirectory = $Root
$shortcut.Save()
Write-Ok "바탕화면에 「카드샵 공지 스튜디오」 바로가기를 만들었습니다"
}

Write-Host ""
Write-Host "설치가 끝났습니다. 바탕화면 바로가기(또는 start.bat)로 실행하세요." -ForegroundColor Green
