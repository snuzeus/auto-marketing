. "$PSScriptRoot\common.ps1"
Write-Step "Postiz 종료 (메모리 확보)"
if ((Test-Docker) -eq "running") { docker compose stop; Write-Ok "Postiz를 멈췄습니다" }
else { Write-Ok "Postiz가 실행 중이 아닙니다" }
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    Write-Ok "공지 스튜디오를 종료했습니다"
}
