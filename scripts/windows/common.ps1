# PS 5.1에서 docker 같은 프로그램의 stderr 출력이 오류로 처리되지 않도록 Continue 로 둔다.
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$AppUrl = "http://localhost:8000"

function Write-Step($msg) { Write-Host ""; Write-Host "▶ $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  ✔ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function Add-UvToPath {
    $candidates = @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin", "$env:LOCALAPPDATA\Programs\uv")
    foreach ($c in $candidates) {
        if ((Test-Path $c) -and -not ($env:Path -split ";" -contains $c)) { $env:Path = "$c;$env:Path" }
    }
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return "missing" }
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { return "running" }
    return "stopped"
}

function Start-DockerDesktop {
    $exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $exe) { Start-Process $exe | Out-Null }
    Write-Host "  Docker Desktop이 켜지기를 기다리는 중 (최대 3분)..."
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    return $false
}

function Start-Postiz {
    $state = Test-Docker
    if ($state -eq "missing") {
        Write-Warn "Docker Desktop이 없습니다. X·인스타그램 전송은 Docker Desktop 설치 후에 쓸 수 있습니다."
        return
    }
    if ($state -eq "stopped" -and -not (Start-DockerDesktop)) {
        Write-Warn "Docker Desktop이 켜지지 않았습니다. 직접 켠 뒤 다시 실행하세요. (X·인스타그램만 영향)"
        return
    }
    docker compose up -d
    if ($LASTEXITCODE -eq 0) { Write-Ok "Postiz 실행 중 (http://localhost:4007)" }
    else { Write-Warn "Postiz를 시작하지 못했습니다. 위 오류를 확인하세요." }
}
