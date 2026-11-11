from __future__ import annotations

from typing import Literal

from google.genai import types

from src.core.llm_client import GeminiLLMClient, _map_thinking_level
from src.core.schema import (
    ChannelType,
    MultilingualTagSet,
    SocialPostOutput,
    ValidationFailureDetail,
    VisionAnalysisResult,
)


def build_copywriter_system_instruction(personas_yaml_raw: str) -> str:
    return (
        "<INSTRUCTIONS>\n"
        "당신은 글로벌 소셜 미디어 알고리즘(Instagram, X)과 한국 대표 커뮤니티(네이버 카페, 디시인사이드) "
        "문화를 완벽히 꿰뚫고 있는 수석 크리에이티브 카피라이터입니다.\n"
        "제공된 이미지 분석 결과와 SEO 해시태그를 바탕으로, 사용자가 활성화한 타겟 채널(enabled_channels)에 "
        "대해서만 맞춤형 홍보 게시물을 작성하십시오.\n"
        "각 채널의 페르소나 규칙, 글자 수 한도, 말머리, 금지어 제약을 절대적으로 준수해야 합니다.\n"
        "</INSTRUCTIONS>\n\n"
        "<CONTEXT>\n"
        "다음은 사용자가 정의한 채널별 페르소나 및 글로벌/네이버 카페 SEO 규칙(config/personas.yaml) 전문입니다:\n"
        f"```yaml\n{personas_yaml_raw}\n```\n"
        "</CONTEXT>"
    )


def build_gemini_copywriter_request(
    personas_yaml_raw: str,
    vision_result: VisionAnalysisResult,
    tag_set: MultilingualTagSet,
    user_prompt: str,
    enabled_channels: list[ChannelType],
    feedback_list: list[ValidationFailureDetail] | None = None,
    thinking_level: Literal["low", "medium", "high"] = "low",
    cached_content_name: str | None = None,
) -> tuple[str, types.GenerateContentConfig]:
    system_instruction = build_copywriter_system_instruction(personas_yaml_raw)

    feedback_section = ""
    if feedback_list:
        formatted_errors = "\n".join(
            f"- [{f.channel}] 위반 규칙({f.rule_violated}): {f.reason} -> 수정 지침: {f.fix_instruction}"
            for f in feedback_list
        )
        feedback_section = (
            "<SELF_CORRECTION_FEEDBACK>\n"
            "이전 생성물에서 아래의 규격 위반이 발생했습니다. 이번 생성 시 반드시 모두 교정하십시오:\n"
            f"{formatted_errors}\n"
            "</SELF_CORRECTION_FEEDBACK>\n\n"
        )

    active_channels_str = ", ".join(enabled_channels)

    user_prompt_content = (
        f"{feedback_section}"
        "<TASK>\n"
        f"1. 이번 실행에서 활성화된 타겟 채널(enabled_channels): [{active_channels_str}]\n"
        f"2. 사용자 원본 요청: {user_prompt}\n"
        f"3. Vision 에이전트 분석 결과(JSON): {vision_result.model_dump_json(indent=2)}\n"
        f"4. 다국어 및 네이버 카페 SEO 태그 세트(JSON): {tag_set.model_dump_json(indent=2)}\n\n"
        f"위 데이터를 결합하여 활성화된 채널([{active_channels_str}])의 맞춤형 최종 원고만 작성하십시오. "
        "활성화되지 않은 나머지 채널 필드는 반드시 `null`로 출력하십시오.\n"
        "</TASK>\n\n"
        "<OUTPUT_FORMAT>\n"
        "응답은 제공된 `SocialPostOutput` response_schema 규격과 100% 일치하는 JSON 객체로만 반환하십시오.\n"
        "- `enabled_channels` 배열에는 활성화된 채널 식별자를 그대로 기입하십시오.\n"
        "- `x_twitter` 활성화 시: 본문과 해시태그를 합친 전체 글자 수(`total_char_count`)는 "
        "공백 및 줄바꿈 포함 280자 이하여야 합니다.\n"
        "- `dcinside` 활성화 시: 본문에 이모지와 존댓말, 광고성 금지어를 절대 포함하지 마십시오.\n"
        "- `naver_cafe` 활성화 시: 정중하고 친근한 카페 회원 후기/정보 공유 톤(`~해요`, `~더라고요`)을 유지하고, "
        "`html_body`와 `plain_body_preview`는 최소 300자 이상으로 작성하며 `search_tags`에는 `#`을 뺀 "
        "키워드 5~10개를 넣으십시오.\n"
        "</OUTPUT_FORMAT>"
    )

    config = types.GenerateContentConfig(
        system_instruction=None if cached_content_name else system_instruction,
        cached_content=cached_content_name,
        response_mime_type="application/json",
        response_schema=SocialPostOutput,
        thinking_config=types.ThinkingConfig(
            thinking_level=_map_thinking_level(thinking_level)
        ),
    )

    return user_prompt_content, config


class CopyAgent:
    """Multi-channel creative copywriter agent using `gemini-2.5-flash` (or main model)."""

    def __init__(self, llm_client: GeminiLLMClient, personas_yaml_raw: str) -> None:
        self.llm = llm_client
        self.personas_yaml_raw = personas_yaml_raw

    async def generate_drafts(
        self,
        vision_result: VisionAnalysisResult,
        tag_set: MultilingualTagSet,
        user_prompt: str,
        enabled_channels: list[ChannelType],
        feedback_list: list[ValidationFailureDetail] | None = None,
        thinking_level: Literal["low", "medium", "high"] = "low",
    ) -> SocialPostOutput:
        system_instruction = build_copywriter_system_instruction(self.personas_yaml_raw)
        cached_name = await self.llm.create_or_get_persona_cache(
            model=self.llm.main_model,
            system_instruction=system_instruction,
            personas_yaml_raw=self.personas_yaml_raw,
        )

        user_prompt_content, _ = build_gemini_copywriter_request(
            personas_yaml_raw=self.personas_yaml_raw,
            vision_result=vision_result,
            tag_set=tag_set,
            user_prompt=user_prompt,
            enabled_channels=enabled_channels,
            feedback_list=feedback_list,
            thinking_level=thinking_level,
            cached_content_name=cached_name,
        )

        return await self.llm.generate_structured(
            model=self.llm.main_model,
            system_instruction=system_instruction,
            user_content=user_prompt_content,
            response_model=SocialPostOutput,
            thinking_level=thinking_level,
            cached_content_name=cached_name,
        )
