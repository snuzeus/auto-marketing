from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from src.core.config_loader import ALL_SUPPORTED_CHANNELS, load_app_settings
from src.mcp.tools import MCPToolDispatcher

# FastMCP was renamed to MCPServer in mcp 2.x; alias kept for backward compatibility
FastMCP = MCPServer
mcp_server = FastMCP("auto-marketing-mcp")


def _default_dispatcher() -> MCPToolDispatcher:
    settings = load_app_settings()
    return MCPToolDispatcher(
        settings=settings,
        enabled_channels=list(ALL_SUPPORTED_CHANNELS),
    )


@mcp_server.tool()
async def upload_media(file_path: str, alt_text: str) -> dict[str, Any]:
    """Upload media file to Postiz for Instagram or X drafts."""
    dispatcher = _default_dispatcher()
    return await dispatcher.execute_tool(
        "upload_media",
        {"file_path": file_path, "alt_text": alt_text},
    )


@mcp_server.tool()
async def create_draft_post(
    channel: Literal["instagram", "x_twitter"],
    content: str,
    media_ids: list[str],
    scheduled_at: str | None = None,
) -> dict[str, Any]:
    """Create a DRAFT post in Postiz for Instagram or X."""
    dispatcher = _default_dispatcher()
    return await dispatcher.execute_tool(
        "create_draft_post",
        {
            "channel": channel,
            "content": content,
            "media_ids": media_ids,
            "scheduled_at": scheduled_at,
        },
    )


@mcp_server.tool()
async def publish_naver_cafe_article(
    club_id: str,
    menu_id: int,
    head_tag: str,
    subject: str,
    html_body: str,
    search_tags: list[str],
    image_path: str,
    approval_mode: str = "telegram_confirm_api",
) -> dict[str, Any]:
    """Publish or package a Naver Cafe article."""
    dispatcher = _default_dispatcher()
    return await dispatcher.execute_tool(
        "publish_naver_cafe_article",
        {
            "club_id": club_id,
            "menu_id": menu_id,
            "head_tag": head_tag,
            "subject": subject,
            "html_body": html_body,
            "search_tags": search_tags,
            "image_path": image_path,
            "approval_mode": approval_mode,
        },
    )


@mcp_server.tool()
async def dispatch_dcinside_pack(
    gallery_id: str,
    title: str,
    body: str,
    image_path: str,
) -> dict[str, Any]:
    """Copy DCInside draft to clipboard and send package via Telegram."""
    dispatcher = _default_dispatcher()
    return await dispatcher.execute_tool(
        "dispatch_dcinside_pack",
        {
            "gallery_id": gallery_id,
            "title": title,
            "body": body,
            "image_path": image_path,
        },
    )
