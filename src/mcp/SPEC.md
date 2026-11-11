# `src/mcp/` & `src/main.py` Specification (`src/mcp/SPEC.md`)

본 문서는 `Phase 5` 구현 시 참조하는 MCP 서버/도구 레이어 및 CLI 엔드투엔드 파이프라인 진입점 명세입니다.

## 1. `src/mcp/tools/` 및 `GEMINI_MCP_FUNCTION_DECLARATIONS`

`google.genai.types.FunctionDeclaration` 및 `FastMCP` 도구로 등록되는 4개 핵심 도구 규격입니다.

1. `upload_media(file_path: str, alt_text: str)`

   * `enabled_channels`에 `"instagram"` 또는 `"x_twitter"`가 포함된 경우에만 1회 선행 호출.

2. `create_draft_post(channel: Literal["instagram", "x_twitter"], content: str, media_ids: list[str], scheduled_at: str | None = None)`

   * 활성화된 `"instagram"`, `"x_twitter"` 채널에 대해 각각 호출하여 Postiz `DRAFT` 생성.

3. `publish_naver_cafe_article(club_id: str, menu_id: int, head_tag: str, subject: str, html_body: str, search_tags: list[str], image_path: str, approval_mode: str)`

   * `"naver_cafe"` 채널이 활성화된 경우에만 호출.

4. `dispatch_dcinside_pack(gallery_id: str, title: str, body: str, image_path: str)`

   * `"dcinside"` 채널이 활성화된 경우에만 호출.

## 2. `src/main.py` — CLI 인자 및 비동기 오케스트레이터 규격

`argparse`를 통해 아래 CLI 플래그를 지원합니다:

* `--image` (필수): 홍보할 원본 이미지 경로

* `--prompt` (필수): 간단한 제품/상황 소개 프롬프트

* `--channels` / `-c` (선택): 콤마로 구분된 발행 플랫폼 오버라이드 (예: `instagram,naver_cafe`)

* `--gallery` (선택): 디시인사이드 타겟 갤러리 ID 오버라이드

* `--cafe-club-id`, `--cafe-menu-id`, `--cafe-head-tag` (선택): 네이버 카페 타겟 게시판 및 말머리 오버라이드

* `--dry-run` (선택): 외부 MCP 도구(Postiz/네이버 카페/텔레그램)를 실제 호출하지 않고 생성·검증된 `SocialPostOutput` JSON만 콘솔에 출력
