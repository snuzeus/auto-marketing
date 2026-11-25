# `src/integrations/` Specification (`src/integrations/SPEC.md`)

본 문서는 `Phase 4` 구현 시 참조하는 외부 플랫폼 연동 클라이언트(`PostizClient`, `NaverCafeClient`, `DCInsideRouter`) 명세입니다.

## 1. `src/integrations/postiz_client.py` — Postiz 비동기 REST 클라이언트

* `httpx.AsyncClient`를 사용하여 셀프 호스팅 Postiz Public API(`POSTIZ_BASE_URL`, 예: `http://localhost:4007/api/public/v1`)와 통신합니다.

* **인증:** `Authorization: <POSTIZ_API_KEY>` (Bearer 접두사 없음). 키는 Postiz `Settings > Agents / Dev`에서 발급.

* **채널 ID:** `POSTIZ_INSTAGRAM_CHANNEL_ID` / `POSTIZ_X_CHANNEL_ID`가 비어 있으면 `GET /integrations`에서 식별자(`x`, `instagram`)로 자동 조회합니다. 연결된 채널이 없으면 `PostizIntegrationError`.

* **주요 메서드:**

  * `async def list_integrations(self, refresh: bool = False) -> list[dict]`: `GET /integrations` (연결된 채널 목록, 캐시).

  * `async def upload_media(self, file_path: str, alt_text: str) -> dict`: `POST /upload` 에 멀티파트(`file`)로 이미지를 업로드하고 `{"media_id": ..., "media_url": ..., "aspect_ratio": ...}` 반환. 게시물 생성에 `id`와 `path`가 모두 필요하므로 클라이언트가 media_id → path/alt를 기억합니다.

  * `async def create_draft_post(self, channel: Literal["instagram", "x_twitter"], content: str, media_ids: list[str], scheduled_at: str | None = None) -> dict`: `POST /posts` 에 `type="draft"`, `posts[].integration.id`, `posts[].value[].{content, image[{id,path,alt}]}`, `posts[].settings`(X: `__type="x"`, `who_can_reply_post`; 인스타: `__type="instagram"`, `post_type="post"`)를 담아 초안을 생성하고 `{"post_id": ..., "status": "DRAFT", "dashboard_url": ...}` 반환.

* **검증 상태:** X 채널 초안 생성(이미지 1장 포함)은 실제 서버에서 확인됨. 인스타는 미연결(Meta 앱 필요)이라 미검증.

## 2. `src/integrations/naver_cafe_client.py` — 네이버 카페 Open API & 승인 라우터

* **Naver Open API 엔드포인트:** `POST https://openapi.naver.com/v1/cafe/{club_id}/menu/{menu_id}/articles`

* **OAuth 2.0 토큰 관리:** `NAVER_ACCESS_TOKEN` 만료(401 응답) 시 `NAVER_REFRESH_TOKEN`으로 `https://nid.naver.com/oauth2.0/token?grant_type=refresh_token...`을 호출해 자동 갱신.

* **Multipart & URL 인코딩 처리:**

  * 네이버 카페 API 특유의 MS949/UTF-8 URL 인코딩 규격에 맞춰 `subject`(말머리+제목)와 `content`(HTML 본문 + 하단 검색 태그)를 인코딩하고 `image` 바이너리를 `multipart/form-data`로 전송.

* **토큰 발급:** `uv run python scripts/naver_token.py` (Callback `http://localhost:8000/naver/callback`, `.env`에 저장). 갱신된 토큰도 `.env`에 자동 저장됩니다. 앱의 사용 API에 카페 글쓰기가 없거나 미동의 토큰이면 HTTP 401 `errorCode 024`.

* **검증 상태:** 실제 카페에 이미지 1장을 첨부한 게시(텔레그램 승인 경유)와 한글·특수문자 표시가 정상임을 확인.

* **Human-in-the-Loop (`approval_mode`):**

  * `"telegram_confirm_api"`: 텔레그램으로 미리보기 및 `[네이버 카페 게시 승인]` / `[취소]` 인라인 버튼을 발송하고 `getUpdates`로 대기(기본 5분). 설정된 `TELEGRAM_CHAT_ID`의 클릭만 인정하며, 승인 시에만 게시합니다. 취소/시간 초과는 `status: "rejected" | "timeout"`, 텔레그램 미설정 시 오류.

  * `"direct_api"`: 별도 승인 없이 바로 게시.

  * `"clipboard_and_telegram"`: 네이버 카페 에디터 호환 HTML/텍스트를 로컬 클립보드에 복사하고 텔레그램으로 원고 패키지 전송.

## 3. `src/integrations/dcinside_router.py` — 디시인사이드 클립보드 & 텔레그램 라우터

* `pyperclip`을 통해 `[제목]\n{title}\n\n[본문]\n{body}`를 시스템 클립보드에 1초 복사하고, `rich.panel.Panel`로 터미널에 프리뷰를 출력합니다.

* Telegram Bot API (`sendPhoto`)로 원본 이미지와 원클릭 복사 코드 블록, 타겟 갤러리(`https://gall.dcinside.com/board/lists?id={gallery_id}`) 바로가기 인라인 버튼을 발송합니다.
