from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from rich.console import Console

from src.agents.copy_agent import CopyAgent
from src.agents.tag_engine import TagEngine
from src.agents.validator_agent import ValidatorAgent
from src.agents.vision_agent import VisionAgent
from src.core.card_renderer import render_notice_card_png
from src.core.config_loader import (
    AppSettings,
    apply_dcinside_gallery,
    apply_naver_cafe_target,
    load_app_settings,
    load_personas_config,
    load_personas_raw,
    resolve_enabled_channels,
)
from src.core.llm_client import GeminiLLMClient, LLMConfigurationError
from src.core.schema import ChannelType, SocialPostOutput, ValidationReport
from src.mcp.tools import MCPToolDispatcher, build_tools_for_enabled_channels

console = Console()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto-Marketing AI Pipeline (CLI & Web Server)"
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="FastAPI 백엔드 및 웹 스튜디오 UI 서버를 실행합니다.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="웹 서버 바인딩 호스트 (기본값: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="웹 서버 포트 (기본값: 8000)",
    )
    parser.add_argument(
        "--image",
        type=str,
        default=None,
        help="홍보할 원본 이미지 경로 (--serve 미사용 시 필수)",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="간단한 제품/상황 소개 프롬프트 (--serve 미사용 시 필수)",
    )
    parser.add_argument(
        "--channels",
        "-c",
        type=str,
        default=None,
        help="콤마로 구분된 발행 플랫폼 오버라이드 (예: instagram,naver_cafe)",
    )
    parser.add_argument(
        "--gallery",
        type=str,
        default=None,
        help="디시인사이드 타겟 갤러리 ID 오버라이드",
    )
    parser.add_argument(
        "--cafe-club-id",
        type=str,
        default=None,
        help="네이버 카페 club_id 오버라이드",
    )
    parser.add_argument(
        "--cafe-menu-id",
        type=int,
        default=None,
        help="네이버 카페 게시판 menu_id 오버라이드",
    )
    parser.add_argument(
        "--cafe-head-tag",
        type=str,
        default=None,
        help="네이버 카페 말머리 오버라이드 (예: [후기])",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="외부 MCP 도구를 호출하지 않고 생성·검증된 SocialPostOutput JSON만 출력",
    )
    return parser


def apply_cli_overrides_to_output(
    post_output: SocialPostOutput,
    gallery: str | None = None,
    cafe_club_id: str | None = None,
    cafe_menu_id: int | None = None,
    cafe_head_tag: str | None = None,
) -> SocialPostOutput:
    if post_output.dcinside is not None and gallery:
        post_output.dcinside.gallery_id = gallery
    if post_output.naver_cafe is not None:
        if cafe_club_id:
            post_output.naver_cafe.club_id = cafe_club_id
        if cafe_menu_id is not None:
            post_output.naver_cafe.menu_id = cafe_menu_id
        if cafe_head_tag:
            post_output.naver_cafe.head_tag = cafe_head_tag
    return post_output


async def run_pipeline(
    image_path: str,
    user_prompt: str,
    channels_arg: str | None = None,
    gallery: str | None = None,
    cafe_club_id: str | None = None,
    cafe_menu_id: int | None = None,
    cafe_head_tag: str | None = None,
    dry_run: bool = False,
    settings: AppSettings | None = None,
    llm_client: GeminiLLMClient | None = None,
    dispatcher: MCPToolDispatcher | None = None,
) -> tuple[SocialPostOutput, ValidationReport, list[dict[str, Any]]]:
    """End-to-end async pipeline: Config -> Vision -> Tag -> Copy -> Validator -> MCP Dispatch."""
    app_settings = settings or load_app_settings()
    personas_cfg = load_personas_config()
    personas_raw = load_personas_raw()

    enabled_channels: list[ChannelType] = resolve_enabled_channels(personas_cfg, channels_arg)

    if not Path(image_path).exists():
        raise FileNotFoundError(f"입력 이미지 파일을 찾을 수 없습니다: {image_path}")

    llm = llm_client or GeminiLLMClient(
        api_key=app_settings.gemini_api_key,
        main_model=app_settings.gemini_main_model,
        lite_model=app_settings.gemini_lite_model,
        default_thinking_level=app_settings.gemini_default_thinking_level,
    )

    vision_agent = VisionAgent(llm)
    tag_engine = TagEngine(llm)
    copy_agent = CopyAgent(llm, personas_raw)
    validator = ValidatorAgent(llm, personas_cfg, enable_semantic_stage=True)

    vision_result = await vision_agent.analyze(image_path, user_prompt)
    tag_set = await tag_engine.generate_tags(
        vision_result=vision_result,
        user_prompt=user_prompt,
        enabled_channels=enabled_channels,
        personas_config=personas_cfg,
    )

    post_output, report, _ = await validator.run_with_self_correction(
        copy_agent=copy_agent,
        vision_result=vision_result,
        tag_set=tag_set,
        user_prompt=user_prompt,
        enabled_channels=enabled_channels,
        max_attempts=3,
    )

    apply_naver_cafe_target(post_output.naver_cafe, app_settings)
    apply_dcinside_gallery(post_output.dcinside, app_settings)
    post_output = apply_cli_overrides_to_output(
        post_output,
        gallery=gallery,
        cafe_club_id=cafe_club_id,
        cafe_menu_id=cafe_menu_id,
        cafe_head_tag=cafe_head_tag,
    )

    if dry_run:
        return post_output, report, []

    if not report.is_valid:
        raise RuntimeError(
            f"검증에 통과하지 못한 원고는 외부 발행할 수 없습니다: {report.model_dump_json(indent=2)}"
        )

    tool_dispatcher = dispatcher or MCPToolDispatcher(
        settings=app_settings,
        enabled_channels=enabled_channels,
    )
    tools = build_tools_for_enabled_channels(enabled_channels)

    card_path_line = ""
    if "instagram" in enabled_channels or "x_twitter" in enabled_channels:
        card_headline = vision_result.primary_subject
        card_body = ""
        if post_output.instagram is not None:
            card_headline = post_output.instagram.hook_headline
            card_body = post_output.instagram.body
        elif post_output.x_twitter is not None:
            x_lines = post_output.x_twitter.body.splitlines()
            card_headline = x_lines[0] if x_lines else vision_result.primary_subject
            card_body = "\n".join(x_lines[1:]) if len(x_lines) > 1 else post_output.x_twitter.body
        cli_card_path = Path(".runtime_uploads/cli_notice_card.png")
        cli_card_path.parent.mkdir(parents=True, exist_ok=True)
        cli_card_path.write_bytes(
            render_notice_card_png(
                headline=card_headline,
                body=card_body,
                source_image_path=image_path,
                user_prompt=user_prompt,
                include_source_thumbnail=False,
            )
        )
        card_path_line = f"공지 카드뉴스 이미지 경로 (2번째 슬라이드): {cli_card_path}\n"

    dispatch_system_instruction = (
        "당신은 SNS 발행 오케스트레이터입니다. 제공된 검증 완료 원고(SocialPostOutput)와 "
        "활성 채널(enabled_channels)에 해당하는 도구만 정확히 호출하여 초안을 등록하거나 전달하십시오.\n"
        "- instagram 또는 x_twitter가 활성화된 경우 upload_media를 호출하여 원본 이미지(1번째 슬라이드)와 "
        "공지 카드뉴스 이미지(2번째 슬라이드)를 각각 업로드하고, 얻은 두 media_id를 [원본_media_id, 카드뉴스_media_id] "
        "순서대로 담아 각 활성 채널에 대해 create_draft_post를 호출하십시오.\n"
        "- naver_cafe가 활성화된 경우 publish_naver_cafe_article을 호출하십시오.\n"
        "- dcinside가 활성화된 경우 dispatch_dcinside_pack을 호출하십시오.\n"
        "- 비활성 채널 도구는 절대 호출하지 마십시오."
    )
    approval_mode = (
        personas_cfg.get("channels", {})
        .get("naver_cafe", {})
        .get("approval_mode", "telegram_confirm_api")
    )
    dispatch_prompt = (
        f"원본 이미지 경로 (1번째 슬라이드): {image_path}\n"
        f"{card_path_line}"
        f"대체 텍스트(alt_text): {vision_result.suggested_alt_text}\n"
        f"네이버 카페 approval_mode: {approval_mode}\n"
        f"검증 완료 원고 JSON:\n{post_output.model_dump_json(indent=2)}"
    )

    executed_calls = await llm.run_mcp_tool_loop(
        model=llm.main_model,
        system_instruction=dispatch_system_instruction,
        prompt=dispatch_prompt,
        tools=tools,
        tool_executor=tool_dispatcher.execute_tool,
    )
    return post_output, report, executed_calls


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    # 설정·이력·업로드 경로가 모두 프로젝트 폴더 기준이므로, 어디서 실행해도 같은 파일을 쓰게 한다.
    os.chdir(PROJECT_ROOT)
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.serve:
        from src.web.app import create_app

        app = create_app()
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if not args.image or not args.prompt:
        parser.error("--serve 모드가 아닐 때는 --image와 --prompt 인자가 필수입니다.")

    try:
        output, report, calls = asyncio.run(
            run_pipeline(
                image_path=args.image,
                user_prompt=args.prompt,
                channels_arg=args.channels,
                gallery=args.gallery,
                cafe_club_id=args.cafe_club_id,
                cafe_menu_id=args.cafe_menu_id,
                cafe_head_tag=args.cafe_head_tag,
                dry_run=bool(args.dry_run),
            )
        )
    except LLMConfigurationError as exc:
        console.print(f"[bold red][설정 오류][/bold red] {exc}")
        return 2
    except Exception as exc:
        console.print(f"[bold red][실행 오류][/bold red] {exc}")
        return 1

    console.print_json(output.model_dump_json(indent=2))
    if not args.dry_run and calls:
        console.print(f"[bold green]실행된 MCP 도구 수: {len(calls)}[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
