. "$PSScriptRoot\common.ps1"
Add-UvToPath
if (-not (Get-Command uv -ErrorAction SilentlyContinue) -or -not (Test-Path ".env") -or -not (Test-Path ".venv")) {
    throw "아직 설치되지 않았습니다. install.bat 을 먼저 실행하세요."
}

$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Ok "이미 실행 중입니다. 브라우저를 엽니다."
    Start-Process $AppUrl
    return
}

Write-Step "Postiz 시작"
Start-Postiz

Write-Step "공지 스튜디오 시작 — 이 창을 닫으면 프로그램이 종료됩니다"
$env:PYTHONIOENCODING = "utf-8"
Start-Job -ScriptBlock {
    param($url)
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $url | Out-Null; Start-Process $url; return } catch { Start-Sleep -Seconds 1 }
    }
} -ArgumentList $AppUrl | Out-Null
uv run --no-dev python -m src.main --serve --host 127.0.0.1 --port 8000
