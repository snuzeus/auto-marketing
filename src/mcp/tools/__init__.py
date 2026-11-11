from __future__ import annotations

from typing import Any, Literal

from google.genai import types

from src.core.config_loader import AppSettings
from src.core.schema import ChannelType
from src.integrations.dcinside_router import DCInsideRouter
from src.integrations.naver_cafe_client import NaverCafeClient
from src.integrations.postiz_client import PostizClient

UPLOAD_MEDIA_DECLARATION = types.FunctionDeclaration(
    name="upload_media",
    description=(
        "Postiz 서버에 원본 이미지를 업로드하고 media_id를 반환합니다. "
        "enabled_channels에 instagram 또는 x_twitter가 포함된 경우에만 1회 선행 호출합니다."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "file_path": types.Schema(type=types.Type.STRING, description="원본 이미지 경로"),
            "alt_text": types.Schema(type=types.Type.STRING, description="대체 텍스트"),
        },
        required=["file_path", "alt_text"],
    ),
)

CREATE_DRAFT_POST_DECLARATION = types.FunctionDeclaration(
    name="create_draft_post",
    description=(
        "Postiz에 Instagram 또는 X(트위터) 게시물을 DRAFT(임시저장) 상태로 생성합니다. "
        "절대 즉시 공개 발행하지 않습니다."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "channel": types.Schema(
                type=types.Type.STRING,
                enum=["instagram", "x_twitter"],
                description="타겟 채널 식별자",
            ),
            "content": types.Schema(type=types.Type.STRING, description="최종 캡션 또는 트윗 전문"),
            "media_ids": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.STRING),
                description="업로드된 미디어 ID 리스트",
            ),
            "scheduled_at": types.Schema(
                type=types.Type.STRING,
                description="예약 시각 (ISO 8601, 선택)",
            ),
        },
        required=["channel", "content", "media_ids"],
    ),
)

PUBLISH_NAVER_CAFE_DECLARATION = types.FunctionDeclaration(
    name="publish_naver_cafe_article",
    description="네이버 카페 게시글을 승인 모드에 따라 등록하거나 원고 패키지를 전달합니다.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "club_id": types.Schema(type=types.Type.STRING, description="네이버 카페 club_id"),
            "menu_id": types.Schema(type=types.Type.INTEGER, description="게시판 menu_id"),
            "head_tag": types.Schema(type=types.Type.STRING, description="말머리 태그"),
            "subject": types.Schema(type=types.Type.STRING, description="게시글 제목"),
            "html_body": types.Schema(type=types.Type.STRING, description="HTML 본문"),
            "search_tags": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(type=types.Type.STRING),
                description="'#' 제외한 검색 태그 리스트",
            ),
            "image_path": types.Schema(type=types.Type.STRING, description="첨부 이미지 경로"),
            "approval_mode": types.Schema(
                type=types.Type.STRING,
                description="telegram_confirm_api 또는 clipboard_and_telegram",
            ),
        },
        required=[
            "club_id",
            "menu_id",
            "head_tag",
            "subject",
            "html_body",
            "search_tags",
            "image_path",
            "approval_mode",
        ],
    ),
)

DISPATCH_DCINSIDE_DECLARATION = types.FunctionDeclaration(
    name="dispatch_dcinside_pack",
    description="디시인사이드 원고를 클립보드에 복사하고 텔레그램으로 원고 패키지를 전송합니다.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "gallery_id": types.Schema(type=types.Type.STRING, description="갤러리 ID"),
            "title": types.Schema(type=types.Type.STRING, description="게시글 제목"),
            "body": types.Schema(type=types.Type.STRING, description="음슴체 본문"),
            "image_path": types.Schema(type=types.Type.STRING, description="원본 이미지 경로"),
        },
        required=["gallery_id", "title", "body", "image_path"],
    ),
)

GEMINI_MCP_FUNCTION_DECLARATIONS: list[types.FunctionDeclaration] = [
    UPLOAD_MEDIA_DECLARATION,
    CREATE_DRAFT_POST_DECLARATION,
    PUBLISH_NAVER_CAFE_DECLARATION,
    DISPATCH_DCINSIDE_DECLARATION,
]


def build_tools_for_enabled_channels(enabled_channels: list[ChannelType]) -> list[types.Tool]:
    """Return only the Gemini FunctionDeclarations permitted for `enabled_channels`."""
    declarations: list[types.FunctionDeclaration] = []
    enabled_set = set(enabled_channels)

    if "instagram" in enabled_set or "x_twitter" in enabled_set:
        declarations.append(UPLOAD_MEDIA_DECLARATION)
        declarations.append(CREATE_DRAFT_POST_DECLARATION)
    if "naver_cafe" in enabled_set:
        declarations.append(PUBLISH_NAVER_CAFE_DECLARATION)
    if "dcinside" in enabled_set:
        declarations.append(DISPATCH_DCINSIDE_DECLARATION)

    if not declarations:
        return []
    return [types.Tool(function_declarations=declarations)]


class MCPToolDispatcher:
    """Executes MCP tool calls while strictly enforcing `enabled_channels` isolation."""

    def __init__(
        self,
        settings: AppSettings,
        enabled_channels: list[ChannelType],
        postiz_client: PostizClient | None = None,
        naver_client: NaverCafeClient | None = None,
        dcinside_router: DCInsideRouter | None = None,
    ) -> None:
        self.settings = settings
        self.enabled_channels = set(enabled_channels)
        self.postiz = postiz_client or PostizClient(
            base_url=settings.postiz_base_url,
            api_key=settings.postiz_api_key,
            instagram_channel_id=settings.postiz_instagram_channel_id,
            x_channel_id=settings.postiz_x_channel_id,
        )
        self.naver = naver_client or NaverCafeClient(
            client_id=settings.naver_client_id,
            client_secret=settings.naver_client_secret,
            access_token=settings.naver_access_token,
            refresh_token=settings.naver_refresh_token,
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            enable_local_clipboard=settings.enable_local_clipboard,
        )
        self.dcinside = dcinside_router or DCInsideRouter(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            enable_local_clipboard=settings.enable_local_clipboard,
        )

    async def execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "upload_media":
            if not ({"instagram", "x_twitter"} & self.enabled_channels):
                raise PermissionError(
                    "instagram 또는 x_twitter 채널이 비활성화되어 upload_media를 실행할 수 없습니다."
                )
            return await self.postiz.upload_media(
                file_path=str(args["file_path"]),
                alt_text=str(args["alt_text"]),
            )

        if name == "create_draft_post":
            channel_arg = str(args["channel"])
            if channel_arg not in ("instagram", "x_twitter"):
                raise ValueError(f"지원하지 않는 Postiz 채널입니다: {channel_arg}")
            target_channel: Literal["instagram", "x_twitter"] = channel_arg  # type: ignore[assignment]
            if target_channel not in self.enabled_channels:
                raise PermissionError(
                    f"비활성화된 채널('{target_channel}')에 대해 create_draft_post를 호출할 수 없습니다."
                )
            raw_media = args.get("media_ids") or []
            media_ids = [str(m) for m in raw_media]
            scheduled = args.get("scheduled_at")
            return await self.postiz.create_draft_post(
                channel=target_channel,
                content=str(args["content"]),
                media_ids=media_ids,
                scheduled_at=str(scheduled) if scheduled else None,
            )

        if name == "publish_naver_cafe_article":
            if "naver_cafe" not in self.enabled_channels:
                raise PermissionError(
                    "비활성화된 채널('naver_cafe')에 대해 publish_naver_cafe_article을 호출할 수 없습니다."
                )
            raw_tags = args.get("search_tags") or []
            return await self.naver.publish_article(
                club_id=str(args["club_id"]),
                menu_id=int(args["menu_id"]),
                head_tag=str(args["head_tag"]),
                subject=str(args["subject"]),
                html_body=str(args["html_body"]),
                search_tags=[str(t) for t in raw_tags],
                image_path=str(args["image_path"]),
                approval_mode=str(args.get("approval_mode", "telegram_confirm_api")),
            )

        if name == "dispatch_dcinside_pack":
            if "dcinside" not in self.enabled_channels:
                raise PermissionError(
                    "비활성화된 채널('dcinside')에 대해 dispatch_dcinside_pack을 호출할 수 없습니다."
                )
            return await self.dcinside.dispatch_pack(
                gallery_id=str(args["gallery_id"]),
                title=str(args["title"]),
                body=str(args["body"]),
                image_path=str(args["image_path"]),
            )

        raise ValueError(f"알 수 없는 MCP 도구입니다: {name}")
