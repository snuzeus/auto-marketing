# `src/integrations/` Specification (`src/integrations/SPEC.md`)

본 문서는 `Phase 4` 구현 시 참조하는 외부 플랫폼 연동 클라이언트(`PostizClient`, `NaverCafeClient`, `DCInsideRouter`) 명세입니다.

## 1. `src/integrations/postiz_client.py` — Postiz 비동기 REST 클라이언트

* `httpx.AsyncClient`를 사용하여 셀프 호스팅 Postiz API(`POSTIZ_BASE_URL`)와 통신합니다.

* **주요 메서드:**

  * `async def upload_media(self, file_path: str, alt_text: str) -> dict`: `POST /media` 엔드포인트에 멀티파트로 이미지를 업로드하고 `{"media_id": ..., "media_url": ..., "aspect_ratio": ...}` 반환.

  * `async def create_draft_post(self, channel: Literal["instagram", "x_twitter"], content: str, media_ids: list[str], scheduled_at: str | None = None) -> dict`: `POST /posts` 엔드포인트에 `status="DRAFT"` 상태로 게시물을 생성하고 `{"post_id": ..., "status": "DRAFT", "dashboard_url": ...}` 반환.

## 2. `src/integrations/naver_cafe_client.py` — 네이버 카페 Open API & 승인 라우터

* **Naver Open API 엔드포인트:** `POST https://openapi.naver.com/v1/cafe/{club_id}/menu/{menu_id}/articles`

* **OAuth 2.0 토큰 관리:** `NAVER_ACCESS_TOKEN` 만료(401 응답) 시 `NAVER_REFRESH_TOKEN`으로 `https://nid.naver.com/oauth2.0/token?grant_type=refresh_token...`을 호출해 자동 갱신.

* **Multipart & URL 인코딩 처리:**

  * 네이버 카페 API 특유의 MS949/UTF-8 URL 인코딩 규격에 맞춰 `subject`(말머리+제목)와 `content`(HTML 본문 + 하단 검색 태그)를 인코딩하고 `image` 바이너리를 `multipart/form-data`로 전송.

* **Human-in-the-Loop (`approval_mode`):**

  * `"telegram_confirm_api"`: 텔레그램으로 미리보기 및 `[네이버 카페 게시 승인]` 인라인 버튼을 먼저 발송하고, 승인 확인 후 Open API를 호출해 실제 게시글 등록.

  * `"clipboard_and_telegram"`: 네이버 카페 에디터 호환 HTML/텍스트를 로컬 클립보드에 복사하고 텔레그램으로 원고 패키지 전송.

## 3. `src/integrations/dcinside_router.py` — 디시인사이드 클립보드 & 텔레그램 라우터

* `pyperclip`을 통해 `[제목]\n{title}\n\n[본문]\n{body}`를 시스템 클립보드에 1초 복사하고, `rich.panel.Panel`로 터미널에 프리뷰를 출력합니다.

* Telegram Bot API (`sendPhoto`)로 원본 이미지와 원클릭 복사 코드 블록, 타겟 갤러리(`https://gall.dcinside.com/board/lists?id={gallery_id}`) 바로가기 인라인 버튼을 발송합니다.
