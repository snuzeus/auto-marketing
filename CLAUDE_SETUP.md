# CLAUDE_SETUP.md — 새 PC 설치 실행서 (Claude Code용)

> 사용법: 새 PC에서 Claude Code를 열고 **「CLAUDE_SETUP.md 보고 이 PC에 세팅해줘」** 라고 입력하세요.
> 이 문서는 Claude가 위에서부터 순서대로 실행하도록 쓴 것입니다. 사람이 읽을 안내서는 `README.md` 입니다.

---

## 0. Claude가 지킬 규칙

1. **키·토큰을 채팅으로 받지 않는다.** 사용자는 웹 화면의 **설정 탭**에 직접 입력한다. `.env` 내용을 출력하지 않는다(값 확인이 필요하면 "설정됨/비어 있음"만).
2. **게시는 사용자 확인 후에만.** 네이버 카페 테스트 글, 텔레그램 메시지, Postiz 초안 생성은 매번 먼저 물어본다.
3. **결제·약관 동의·로그인·CAPTCHA는 사용자가 한다.** Claude는 어디서 무엇을 누를지 안내만 한다.
4. 진행 상황은 아래 **체크리스트 번호**로 보고한다. 막히면 해당 단계의 「막히면」을 먼저 본다.
5. 셸은 PowerShell 기준. 모든 명령은 **프로젝트 루트**에서 실행한다.

---

## 1. 사전 점검 (Claude 실행)

```powershell
[Environment]::OSVersion.Version; $env:PROCESSOR_ARCHITECTURE
Get-Command git, uv, docker, winget -ErrorAction SilentlyContinue | Select-Object Name, Source
```

- `PROCESSOR_ARCHITECTURE` 가 `AMD64` 인지 기록한다 (Docker 설치 파일 선택용).
- 없는 도구를 사용자에게 알린다. git이 없으면: `winget install --id Git.Git -e`

## 2. 코드 받기

프로젝트 폴더가 아직 없을 때만:

```powershell
git clone https://github.com/snuzeus/auto-marketing.git "$HOME\auto-marketing"
Set-Location "$HOME\auto-marketing"
```

- 비공개 저장소라 실패하면: `gh auth login` 을 사용자에게 요청하거나, 사용자가 ZIP을 받아 푼 폴더로 이동한다.

## 3. Docker Desktop (X·인스타그램용)

```powershell
docker info --format '{{.ServerVersion}}'
```

- 버전이 나오면 다음 단계로.
- `docker` 가 없으면 사용자에게 승인받고: `winget install --id Docker.DockerDesktop -e` → **재부팅 필요**를 알린다.
  - 재부팅 후 사용자가 Docker Desktop을 한 번 실행해 약관에 동의하고, 왼쪽 아래 **Engine running** 이 뜨면 계속.
- 설치 파일을 직접 받는 경우 `AMD64` 용을 고르게 한다.

**막히면**
- `cannot find registry key "SOFTWARE\Docker Inc.\Docker Desktop"` → 설치가 깨진 상태. 앱 제거 → `C:\Program Files\Docker` 삭제 → 재설치.
- WSL 오류 → 관리자 PowerShell에서 `wsl --update` 후 재부팅.

## 4. 설치 스크립트 실행 (Claude 실행)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\install.ps1
```

이 스크립트가 하는 일: uv 설치(없으면) → `uv sync --no-dev` → `.env` 생성 + `JWT_SECRET` 무작위 생성 → `docker compose up -d`(Postiz) → 바탕화면 바로가기.

확인:

```powershell
Test-Path .env; Test-Path .venv
docker compose ps --format '{{.Name}}\t{{.Status}}'
```

- 컨테이너 8개가 `Up` 이고 `postiz` 가 `healthy` 가 될 때까지 1~3분 기다린다.
  `Invoke-WebRequest -UseBasicParsing http://localhost:4007 -MaximumRedirection 0` 가 응답(302/307)하면 준비 완료.

**막히면**
- 첫 실행은 이미지 수 GB를 받는다. 시간 초과면 `docker compose up -d` 를 백그라운드로 다시 실행.
- 컨테이너가 `Exited (137)` → 메모리 부족. Docker Desktop의 메모리 할당을 늘리거나 다른 프로그램을 닫는다.

## 5. 앱 실행 (Claude 실행, 백그라운드)

```powershell
$env:PYTHONIOENCODING = "utf-8"; uv run --no-dev python -m src.main --serve --host 127.0.0.1 --port 8000
```

- 백그라운드로 실행하고 `http://127.0.0.1:8000/api/health` 가 200이 될 때까지 기다린다.
- 사용자에게 `http://localhost:8000` 을 열라고 안내한다. Gemini 키가 없으면 **설정 탭**이 자동으로 열린다.
- 평소 실행은 바탕화면 「카드샵 공지 스튜디오」(= `start.bat`) 로 한다고 알려 준다.

---

## 6. 계정 연결 (사용자 작업 + Claude 확인)

각 항목: 사용자가 외부 사이트에서 발급 → **설정 탭에 입력 → 저장** → Claude가 확인 API를 호출한다.

확인 API (부작용 없음. 단, telegram은 확인 메시지 1건을 보낸다 → 먼저 알린다):

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/settings/test/gemini
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/settings/test/postiz
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/settings/test/telegram
Invoke-RestMethod http://127.0.0.1:8000/api/health | Select-Object -ExpandProperty integrations_configured
```

Windows PowerShell 5.1에서는 응답의 한글 `message` 가 깨져 보일 수 있다. 판단은 `ok` 값으로 한다.

### 6-1. 매장 이름 · Gemini (필수)
- https://aistudio.google.com → API 키 만들기 → 설정 탭 「Gemini API 키」.
- 비용 안내: 공지 1건 ≈ 100~250원. **지출 → 월 지출 한도** 설정을 권한다.
- 선불(Prepay) 결제 계정은 크레딧 잔액이 0이면 호출이 실패한다. 받은 GCP 프로모션 크레딧이 선불 잔액으로 자동 쓰이지 않을 수 있다 → 결제 미연결 프로젝트의 무료 키로 먼저 시험하는 것도 방법.
- 확인: `test/gemini` → `ok: true`.

### 6-2. 텔레그램 (네이버 승인용)
- `@BotFather` → `/newbot` → 토큰. **만든 봇에게 먼저 아무 메시지나 보내게 한다.**
- `@userinfobot` → `Id` 숫자 = chat ID.
- 확인: 사용자에게 알린 뒤 `test/telegram` → 텔레그램에 「✅ 공지 스튜디오 연결 확인」 도착.

### 6-3. X (Postiz 경유)
X 개발자 콘솔(https://console.x.com):
1. 앱 생성. use case 설명은 "내 계정 하나에 매장 공지를 게시. 다른 사용자 데이터 수집·분석·DM·자동 팔로우 없음" 취지로 영어 작성.
2. **User authentication settings**: App permissions **Read and write** / Type of App **Native App** (Web App 선택 시 error code 32) / Callback URI `http://localhost:4007/integrations/social/x`
3. **Keys and tokens → OAuth 1.0a → Consumer Key → Regenerate** → API Key, API Key Secret → 설정 탭에 입력·저장.
   (Bearer Token, Access Token, OAuth 2.0 Client ID/Secret 은 쓰지 않는다)
4. 설정 탭 **Postiz 다시 시작** (또는 Claude가 `docker compose up -d`).

Postiz(http://localhost:4007):
5. 계정 생성 → 채널 추가 → **X** 로그인·승인. ("에이전트 연결" 안내는 건너뛴다)
6. **Settings → Agents / Dev → API Key** 복사 → 설정 탭 「Postiz API 키」 저장.
7. 확인: `test/postiz` → 메시지에 `x` 포함.
   (채널 ID는 비워 두면 `/integrations` 에서 자동으로 찾는다)

### 6-4. 네이버 카페
https://developers.naver.com → 애플리케이션 등록:
- 사용 API: **네이버 로그인** + **카페**(글쓰기 권한 체크)
- 로그인 제공 정보: 필수 최소화 (회원이름만 남겨도 됨)
- 환경: PC 웹 / 서비스 URL `http://localhost:8000` / Callback URL `http://localhost:8000/naver/callback`
- 멤버 관리: 글을 올릴 계정이 앱 소유 계정이 아니면 테스터로 추가.

그다음:
1. Client ID / Secret → 설정 탭 저장.
2. 설정 탭 **네이버 로그인** → 글 올릴 계정으로 로그인·동의 → 「네이버 카페 연결 완료」.
3. 카페 ID / 게시판 ID: 게시판 주소의 `clubid=`, `menuid=` 숫자 → 설정 탭 저장.
4. 확인: `integrations_configured.naver_cafe_api == true`.
5. (사용자 승인 후) 실제 게시 테스트는 6-6에서.

**막히면** HTTP 401 `errorCode 024` (Scope Status Invalid) → 카페 글쓰기 권한 누락. 개발자센터에서 권한 확인 → 네이버 내정보 → 연결된 서비스에서 앱 연결 해제 → 다시 **네이버 로그인**.

### 6-5. 인스타그램 (선택, 가장 번거로움)
- 인스타 계정을 **프로페셔널**로 전환.
- https://developers.facebook.com/apps/creation → 유형 Other → **Instagram** 제품 → Instagram 비즈니스 로그인 → Redirect URI `http://localhost:4007/integrations/social/instagram-standalone`
- Instagram App ID / Secret → 설정 탭 저장 → Postiz 다시 시작.
- 앱 역할 → Instagram Tester 추가 → https://www.instagram.com/accounts/manage_access/ 에서 수락.
- Postiz → 채널 추가 → **Instagram (Standalone)**.
- **막히면** Meta 가입 중 「평소에 사용하지 않는 기기」 오류 → 평소 쓰던 기기/브라우저에서 진행하거나 며칠 사용 후 재시도. 반복 시도 금지.
- 주의: 이미지가 localhost에만 있어 **실제 게시**는 추가 설정(외부 공개 저장소 등)이 필요할 수 있다.

### 6-6. 디시인사이드
- 설정 탭 「디시 기본 갤러리」: 갤러리 ID, 마이너·미니는 주소 전체.
- 자동 게시 기능은 없다(만들지 않는다). 앱은 제목/본문 복사와 글쓰기 화면 열기까지만 한다.

---

## 7. 전체 흐름 확인 (사용자와 함께)

1. 사용자가 사진 + 메모로 공지 1건 생성 (Gemini 비용 발생 → 먼저 알린다).
2. **X**: 승인 → 전송 → Postiz Calendar에 초안(이미지 2장: 원본+카드) 확인.
3. **네이버 카페**: 승인 → 전송 → 텔레그램 승인 요청의 카페 ID·게시판이 맞는지 확인 → 테스트면 **취소**, 게시하면 글 확인 후 삭제 안내.
4. **디시**: 글쓰기 열기 → 제목/본문 복사 동작만 확인.

---

## 8. 완료 체크리스트 (Claude가 마지막에 보고)

| # | 항목 | 확인 방법 |
|---|---|---|
| 1 | Docker · Postiz 실행 | `docker compose ps` 전부 Up |
| 2 | 앱 실행 · 바로가기 | `/api/health` 200, 바탕화면 바로가기 존재 |
| 3 | 매장 이름 | `/api/health` 의 `channel_defaults.store_brand` |
| 4 | Gemini | `test/gemini` ok |
| 5 | 텔레그램 | `test/telegram` ok |
| 6 | X | `test/postiz` 에 x |
| 7 | 네이버 카페 | `naver_cafe_api` true + 카페/게시판 ID 설정 |
| 8 | 인스타 | (선택) `test/postiz` 에 instagram |
| 9 | 디시 갤러리 | 설정 탭 값 |
| 10 | 전체 흐름 | 7번 결과 |

미완료 항목은 남은 사용자 작업과 함께 보고한다.

---

## 9. 운영 메모

- 실행: 바탕화면 바로가기. 검은 창을 닫으면 앱 종료. `stop.bat` 은 Postiz까지 정지(메모리 확보).
- X 전송 시 `All connection attempts failed` → Postiz가 꺼진 것: Docker Desktop 켜고 `docker compose up -d`.
- 백업: `.env`, `config\campaigns_history.db`, `config\style_profiles.json`.
- 업데이트: `git pull` → `uv sync --no-dev` → `docker compose up -d` → 앱 재실행.
- 개발/테스트가 필요하면 `uv sync` (dev 포함) 후 `uv run pytest -q`.
