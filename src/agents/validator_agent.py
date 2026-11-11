from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.copy_agent import CopyAgent
from src.core.llm_client import GeminiLLMClient
from src.core.schema import (
    ALL_CHANNELS_LIST,
    ChannelDraftSpec,
    ChannelType,
    DCInsidePostSpec,
    InstagramPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
    SocialPostOutput,
    ValidationFailureDetail,
    ValidationReport,
    VisionAnalysisResult,
    XTwitterPostSpec,
)

HONORIFIC_REGEX = re.compile(r"(습니다|합니다|해요|네요|세요|드립니다|어요|아요)(?=[.\s!?\n,~]|$)")
EMOJI_REGEX = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002702-\U000027b0"
    "\U0001f900-\U0001f9ff"
    "\U0001fa70-\U0001faff"
    "]+",
    flags=re.UNICODE,
)


class _SemanticCheckScore(BaseModel):
    score: int = Field(ge=0, le=100, description="채널 페르소나 및 커뮤니티 자연스러움 점수 (0~100)")
    failures: list[ValidationFailureDetail] = Field(
        default_factory=list,
        description="80점 미만인 경우 구체적인 위반 사유와 수정 지침",
    )


def validate_single_channel_deterministic(
    channel: ChannelType,
    spec: ChannelDraftSpec,
    personas_config: dict[str, Any],
) -> list[ValidationFailureDetail]:
    """Run deterministic Stage-1 rules on a single channel's draft (zero LLM cost)."""
    failures: list[ValidationFailureDetail] = []
    channels_cfg = personas_config.get("channels", {})
    ch_cfg = channels_config_for(channels_cfg, channel)
    forbidden_words: list[str] = ch_cfg.get("forbidden_words", [])

    if channel == "instagram" and isinstance(spec, InstagramPostSpec):
        max_len = int(ch_cfg.get("max_caption_length", 1200))
        if len(spec.full_caption) > max_len:
            failures.append(
                ValidationFailureDetail(
                    channel="instagram",
                    rule_violated="max_caption_length",
                    reason=f"인스타그램 전체 캡션 길이가 {max_len}자를 초과했습니다 ({len(spec.full_caption)}자).",
                    fix_instruction=f"전체 본문과 해시태그 합산 길이를 {max_len}자 이하로 줄이십시오.",
                )
            )
        tag_min = int(ch_cfg.get("hashtag_count", {}).get("min", 10))
        tag_max = int(ch_cfg.get("hashtag_count", {}).get("max", 15))
        if not (tag_min <= len(spec.hashtags) <= tag_max):
            failures.append(
                ValidationFailureDetail(
                    channel="instagram",
                    rule_violated="hashtag_count",
                    reason=f"인스타그램 해시태그 개수가 {len(spec.hashtags)}개입니다.",
                    fix_instruction=f"해시태그 개수를 {tag_min}~{tag_max}개 사이로 맞추십시오.",
                )
            )
        _check_forbidden_words(
            channel="instagram",
            text=spec.full_caption,
            forbidden_words=forbidden_words,
            failures=failures,
        )

    elif channel == "x_twitter" and isinstance(spec, XTwitterPostSpec):
        limit = int(ch_cfg.get("strict_char_limit", 280))
        tweet_len = len(spec.full_tweet)
        if tweet_len > limit:
            failures.append(
                ValidationFailureDetail(
                    channel="x_twitter",
                    rule_violated="strict_char_limit",
                    reason=f"X(트위터) 전체 길이가 {limit}자를 초과했습니다 ({tweet_len}자).",
                    fix_instruction=f"본문과 해시태그를 합쳐 공백 포함 {limit}자 이하로 축약하십시오.",
                )
            )
        _check_forbidden_words(
            channel="x_twitter",
            text=spec.full_tweet,
            forbidden_words=forbidden_words,
            failures=failures,
        )

    elif channel == "dcinside" and isinstance(spec, DCInsidePostSpec):
        max_title = int(ch_cfg.get("max_title_length", 38))
        if len(spec.title) > max_title:
            failures.append(
                ValidationFailureDetail(
                    channel="dcinside",
                    rule_violated="max_title_length",
                    reason=f"디시인사이드 제목이 {max_title}자를 초과했습니다 ({len(spec.title)}자).",
                    fix_instruction=f"제목을 {max_title}자 이하로 짧고 자연스럽게 줄이십시오.",
                )
            )
        combined_dc = f"{spec.title}\n{spec.body}"
        honorific_match = HONORIFIC_REGEX.search(combined_dc)
        if honorific_match:
            failures.append(
                ValidationFailureDetail(
                    channel="dcinside",
                    rule_violated="no_honorifics",
                    reason=f"디시인사이드 원고에 존댓말 표현('{honorific_match.group(0)}')이 감지되었습니다.",
                    fix_instruction="존댓말을 모두 제거하고 100% 음슴체(~함, ~임, ~듯) 및 반말 단문으로 재작성하십시오.",
                )
            )
        if not ch_cfg.get("use_emojis", False) and EMOJI_REGEX.search(combined_dc):
            failures.append(
                ValidationFailureDetail(
                    channel="dcinside",
                    rule_violated="no_emojis",
                    reason="디시인사이드 원고에 유니코드 이모지가 포함되어 있습니다.",
                    fix_instruction="모든 이모지를 제거하고 텍스트(필요시 ㅋㅋ, ㄹㅇ 등 자음 표현)만 사용하십시오.",
                )
            )
        _check_forbidden_words(
            channel="dcinside",
            text=combined_dc,
            forbidden_words=forbidden_words,
            failures=failures,
        )

    elif channel == "naver_cafe" and isinstance(spec, NaverCafePostSpec):
        min_body = int(ch_cfg.get("min_body_length", 300))
        plain_len = len(spec.plain_body_preview.strip())
        if plain_len < min_body:
            failures.append(
                ValidationFailureDetail(
                    channel="naver_cafe",
                    rule_violated="min_body_length",
                    reason=f"네이버 카페 본문 길이가 {plain_len}자로 최소 기준({min_body}자) 미달입니다.",
                    fix_instruction=f"도입부, 핵심 특징 3가지, 총평을 보강하여 순수 텍스트 기준 {min_body}자 이상으로 작성하십시오.",
                )
            )
        allowed_heads: list[str] = ch_cfg.get(
            "allowed_head_tags", ["[후기]", "[정보공유]", "[내돈내산]", "[일상/소통]"]
        )
        if allowed_heads and spec.head_tag not in allowed_heads:
            failures.append(
                ValidationFailureDetail(
                    channel="naver_cafe",
                    rule_violated="allowed_head_tags",
                    reason=f"허용되지 않은 말머리('{spec.head_tag}')가 사용되었습니다.",
                    fix_instruction=f"말머리를 허용 목록({allowed_heads}) 중 하나로 설정하십시오.",
                )
            )
        tags_with_hash = [t for t in spec.search_tags if "#" in t]
        if tags_with_hash:
            failures.append(
                ValidationFailureDetail(
                    channel="naver_cafe",
                    rule_violated="search_tags_no_hash",
                    reason=f"네이버 카페 검색 태그에 '#' 기호가 포함되어 있습니다: {tags_with_hash}",
                    fix_instruction="search_tags에서 '#' 기호를 모두 제거하고 순수 키워드만 남기십시오.",
                )
            )
        _check_forbidden_words(
            channel="naver_cafe",
            text=f"{spec.subject}\n{spec.plain_body_preview}\n{spec.html_body}",
            forbidden_words=forbidden_words,
            failures=failures,
        )

    return failures


def channels_config_for(channels_cfg: dict[str, Any], channel: ChannelType) -> dict[str, Any]:
    val = channels_cfg.get(channel, {})
    return val if isinstance(val, dict) else {}


def _check_forbidden_words(
    channel: ChannelType,
    text: str,
    forbidden_words: list[str],
    failures: list[ValidationFailureDetail],
) -> None:
    found = [w for w in forbidden_words if w and w in text]
    if found:
        failures.append(
            ValidationFailureDetail(
                channel=channel,
                rule_violated="forbidden_words",
                reason=f"금지어가 포함되어 있습니다: {found}",
                fix_instruction=f"해당 금지어({found})를 완전히 삭제하거나 자연스러운 일상 표현으로 대체하십시오.",
            )
        )


class ValidatorAgent:
    """2-Stage Validator (Stage 1 deterministic rules + Stage 2 LLM semantic check) & Self-Correction."""

    def __init__(
        self,
        llm_client: GeminiLLMClient,
        personas_config: dict[str, Any],
        enable_semantic_stage: bool = True,
    ) -> None:
        self.llm = llm_client
        self.personas_config = personas_config
        self.enable_semantic_stage = enable_semantic_stage

    def validate_stage1_deterministic(
        self,
        draft: SocialPostOutput,
        enabled_channels: list[ChannelType],
    ) -> ValidationReport:
        failures: list[ValidationFailureDetail] = []
        enabled_set = set(enabled_channels)

        for ch in ALL_CHANNELS_LIST:
            spec = getattr(draft, ch)
            if ch in enabled_set:
                if spec is None:
                    failures.append(
                        ValidationFailureDetail(
                            channel=ch,
                            rule_violated="missing_enabled_channel",
                            reason=f"활성화된 채널 '{ch}'의 원고가 생성되지 않았습니다(null).",
                            fix_instruction=f"'{ch}' 채널 필드에 규격에 맞는 원고 객체를 반드시 생성하십시오.",
                        )
                    )
                else:
                    failures.extend(
                        validate_single_channel_deterministic(ch, spec, self.personas_config)
                    )
            else:
                if spec is not None:
                    failures.append(
                        ValidationFailureDetail(
                            channel=ch,
                            rule_violated="disabled_channel_not_null",
                            reason=f"비활성화된 채널 '{ch}'에 원고가 포함되어 있습니다.",
                            fix_instruction=f"비활성 채널 '{ch}' 필드는 반드시 null로 설정하십시오.",
                        )
                    )

        return ValidationReport(is_valid=len(failures) == 0, failures=failures)

    async def validate_stage2_semantic(
        self,
        draft: SocialPostOutput,
        enabled_channels: list[ChannelType],
    ) -> ValidationReport:
        """Call `gemini-2.5-flash-lite` to verify community tone naturalness when Stage 1 passes."""
        if not self.enable_semantic_stage:
            return ValidationReport(is_valid=True, failures=[])

        system_instruction = (
            "당신은 한국 온라인 커뮤니티(디시인사이드, 네이버 카페)와 SNS(인스타그램, X)의 "
            "진성 유저이자 콘텐츠 품질 감사관입니다.\n"
            "제출된 원고가 각 플랫폼의 고유 문맥에 자연스럽게 녹아드는지 0~100점으로 채점하십시오.\n"
            "80점 이상이면 failures를 빈 배열로 두고, 80점 미만이면 어색한 채널에 대해 구체적인 "
            "ValidationFailureDetail을 작성하십시오."
        )

        prompt = (
            f"활성 채널: {enabled_channels}\n"
            f"생성된 원고 JSON:\n{draft.model_dump_json(indent=2)}\n"
            "각 채널의 톤앤매너 자연스러움을 평가하세요."
        )

        score_result = await self.llm.generate_structured(
            model=self.llm.lite_model,
            system_instruction=system_instruction,
            user_content=prompt,
            response_model=_SemanticCheckScore,
            thinking_level="low",
        )
        if score_result.score >= 80 and not score_result.failures:
            return ValidationReport(is_valid=True, failures=[])

        failures = score_result.failures
        if not failures:
            failures = [
                ValidationFailureDetail(
                    channel=enabled_channels[0],
                    rule_violated="semantic_tone_score",
                    reason=f"시맨틱 자연스러움 점수가 기준치(80점) 미달입니다 ({score_result.score}점).",
                    fix_instruction="광고 티를 줄이고 실제 유저 후기 톤앤매너로 자연스럽게 다듬으십시오.",
                )
            ]
        return ValidationReport(is_valid=False, failures=failures)

    async def validate(
        self,
        draft: SocialPostOutput,
        enabled_channels: list[ChannelType],
    ) -> ValidationReport:
        stage1 = self.validate_stage1_deterministic(draft, enabled_channels)
        if not stage1.is_valid:
            return stage1
        return await self.validate_stage2_semantic(draft, enabled_channels)

    async def run_with_self_correction(
        self,
        copy_agent: CopyAgent,
        vision_result: VisionAnalysisResult,
        tag_set: MultilingualTagSet,
        user_prompt: str,
        enabled_channels: list[ChannelType],
        max_attempts: int = 3,
    ) -> tuple[SocialPostOutput, ValidationReport, int]:
        """
        Execute up to `max_attempts` (default 3) self-correction iterations.
        Attempt 1 uses thinking_level="low"; retries (2..3) escalate to thinking_level="medium".
        Returns (final_draft, final_validation_report, attempts_used).
        """
        feedback: list[ValidationFailureDetail] | None = None
        last_draft: SocialPostOutput | None = None
        last_report = ValidationReport(is_valid=False, failures=[])

        for attempt in range(1, max_attempts + 1):
            thinking_level: Literal["low", "medium", "high"] = (
                "low" if attempt == 1 else "medium"
            )
            draft = await copy_agent.generate_drafts(
                vision_result=vision_result,
                tag_set=tag_set,
                user_prompt=user_prompt,
                enabled_channels=enabled_channels,
                feedback_list=feedback,
                thinking_level=thinking_level,
            )
            report = await self.validate(draft, enabled_channels)
            last_draft = draft
            last_report = report
            if report.is_valid:
                return draft, report, attempt
            feedback = report.failures

        assert last_draft is not None
        return last_draft, last_report, max_attempts
