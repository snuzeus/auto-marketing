from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.copy_agent import CopyAgent
from src.core.clock import today_kst_label
from src.core.llm_client import GeminiLLMClient, LLMGenerationError
from src.core.schema import (
    ALL_CHANNELS_LIST,
    INSTAGRAM_MAX_HASHTAGS,
    X_WEIGHTED_CHAR_LIMIT,
    AccountStyleProfile,
    ChannelDraftSpec,
    ChannelType,
    DCInsidePostSpec,
    InstagramPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
    NaverPlacePostSpec,
    NoticeCategoryType,
    SocialPostOutput,
    ValidationFailureDetail,
    ValidationReport,
    VisionAnalysisResult,
    XTwitterPostSpec,
    x_weighted_length,
)

_HANGUL_REGEX = re.compile(r"[\uac00-\ud7a3]")
_KANA_REGEX = re.compile(r"[\u3040-\u30ff]")
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
    score: int = Field(ge=0, le=100, description="입력 자료 대비 사실 일치 점수 (0~100, 위반 없으면 90 이상)")
    failures: list[ValidationFailureDetail] = Field(
        default_factory=list,
        description="사실 관계 위반이 있는 경우 채널별 구체적인 위반 문구와 수정 지침",
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
        tag_min = int(ch_cfg.get("hashtag_count", {}).get("min", 0))
        # 인스타그램 플랫폼 상한(게시물당 5개)은 설정값과 무관하게 넘을 수 없다.
        tag_max = min(
            int(ch_cfg.get("hashtag_count", {}).get("max", INSTAGRAM_MAX_HASHTAGS)),
            INSTAGRAM_MAX_HASHTAGS,
        )
        tag_min = min(tag_min, tag_max)
        if not (tag_min <= len(spec.hashtags) <= tag_max):
            failures.append(
                ValidationFailureDetail(
                    channel="instagram",
                    rule_violated="hashtag_count",
                    reason=f"인스타그램 해시태그 개수가 {len(spec.hashtags)}개입니다.",
                    fix_instruction=(
                        f"해시태그를 {tag_min}~{tag_max}개로 맞추십시오 "
                        f"(인스타그램은 게시물당 최대 {INSTAGRAM_MAX_HASHTAGS}개만 허용). "
                        "가장 관련성 높은 태그만 남기십시오."
                    ),
                )
            )
        _check_garbled_hashtags("instagram", spec.hashtags, failures)
        _check_forbidden_words(
            channel="instagram",
            text=spec.full_caption,
            forbidden_words=forbidden_words,
            failures=failures,
        )

    elif channel == "x_twitter" and isinstance(spec, XTwitterPostSpec):
        limit = min(int(ch_cfg.get("strict_char_limit", X_WEIGHTED_CHAR_LIMIT)), X_WEIGHTED_CHAR_LIMIT)
        tweet_len = x_weighted_length(spec.full_tweet)
        if tweet_len > limit:
            over_hangul = (tweet_len - limit + 1) // 2
            failures.append(
                ValidationFailureDetail(
                    channel="x_twitter",
                    rule_violated="strict_char_limit",
                    reason=(
                        f"X(트위터) 가중치 글자 수가 {limit}을 초과했습니다 ({tweet_len}). "
                        "X는 한글 1자를 2로 계산합니다."
                    ),
                    fix_instruction=(
                        f"본문과 해시태그를 합쳐 한글 기준 약 {over_hangul}자 이상 줄이십시오. "
                        "덜 중요한 수식어와 해시태그부터 빼고 핵심 사실(일시·가격·제한)만 남기십시오."
                    ),
                )
            )
        x_tag_min = int(ch_cfg.get("hashtag_count", {}).get("min", 2))
        x_tag_max = int(ch_cfg.get("hashtag_count", {}).get("max", 3))
        if not (x_tag_min <= len(spec.hashtags) <= x_tag_max):
            failures.append(
                ValidationFailureDetail(
                    channel="x_twitter",
                    rule_violated="hashtag_count",
                    reason=f"X(트위터) 해시태그 개수가 {len(spec.hashtags)}개입니다.",
                    fix_instruction=f"해시태그를 {x_tag_min}~{x_tag_max}개로 맞추십시오.",
                )
            )
        _check_garbled_hashtags("x_twitter", spec.hashtags, failures)
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
                    fix_instruction=(
                        f"확정 팩트를 항목별로 풀어 쓰고 방문·문의 안내를 보강하여 순수 텍스트 기준 {min_body}자 이상으로 "
                        "작성하십시오. 분량을 채우려고 새로운 사실을 지어내지는 마십시오."
                    ),
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

    elif channel == "naver_place" and isinstance(spec, NaverPlacePostSpec):
        max_body = int(ch_cfg.get("max_body_length", 500))
        if len(spec.body) > max_body:
            failures.append(
                ValidationFailureDetail(
                    channel="naver_place",
                    rule_violated="max_body_length",
                    reason=f"네이버 플레이스 본문이 {len(spec.body)}자로 최대 기준({max_body}자)을 초과했습니다.",
                    fix_instruction=f"본문을 {max_body}자 이하로 줄이십시오.",
                )
            )
        _check_forbidden_words(
            channel="naver_place",
            text=f"{spec.title}\n{spec.body}",
            forbidden_words=forbidden_words,
            failures=failures,
        )

    return failures


def channels_config_for(channels_cfg: dict[str, Any], channel: ChannelType) -> dict[str, Any]:
    val = channels_cfg.get(channel, {})
    return val if isinstance(val, dict) else {}


def _check_garbled_hashtags(
    channel: ChannelType,
    hashtags: list[str],
    failures: list[ValidationFailureDetail],
) -> None:
    """한 태그 안에 한글과 일본어 가나가 섞인 경우(예: '#리프트바ウンド')는 모델의 표기 오류로 본다."""
    garbled = [t for t in hashtags if _HANGUL_REGEX.search(t) and _KANA_REGEX.search(t)]
    if garbled:
        failures.append(
            ValidationFailureDetail(
                channel=channel,
                rule_violated="garbled_hashtag",
                reason=f"한글과 일본어 문자가 한 태그에 섞여 있습니다: {garbled}",
                fix_instruction="해당 태그를 삭제하거나 한 가지 문자 체계로 올바르게 다시 쓰십시오.",
            )
        )


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
    """Stage 1 결정적 규격 검사 + Stage 2 팩트 일치 검사, 그리고 채널 단위 자기교정 루프."""

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
                    failures.append(_missing_channel_failure(ch))
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
        vision_result: VisionAnalysisResult | None = None,
        user_prompt: str = "",
        style_profiles: dict[ChannelType, AccountStyleProfile] | None = None,
    ) -> ValidationReport:
        """Stage 2: 원고가 입력(사용자 메모·확정 팩트)과 사실 관계에서 일치하는지 lite 모델로 검사한다."""
        if not self.enable_semantic_stage:
            return ValidationReport(is_valid=True, failures=[])

        system_instruction = (
            "당신은 매장 공지문의 사실 관계를 검수하는 교정 담당자입니다. 말투나 문체는 평가하지 않습니다.\n"
            "제출된 채널별 원고를 '입력 자료'(사용자 메모, 확정 팩트, 이미지 OCR)와 대조하여 아래만 검사하십시오:\n"
            "1. 입력에 없는 사실의 추가: 일시, 장소, 가격, 수량, 구매 제한, 혜택, 접수 방법 등 "
            "(예: 입력에 없는데 '한정 수량', '조기 품절', '선착순'이라고 쓴 경우)\n"
            "2. 입력과 다른 값: 날짜·시간·가격·수량·상품명이 입력과 다르게 적힌 경우\n"
            "3. 상대 날짜: 확정 팩트에 절대 날짜가 있는데 원고가 '내일', '모레' 등으로만 쓴 경우\n"
            "4. 핵심 팩트 누락: 일시·가격·구매 제한처럼 독자가 꼭 알아야 할 확정 팩트가 빠진 경우 "
            "(X처럼 글자 수가 매우 제한된 채널은 가장 중요한 1~2개만 있어도 통과)\n"
            "5. 깨진 표기: 한 단어 안에 한글과 일본어 가나가 섞이는 등 오타로 보이는 글자\n"
            "허용되는 것: 인사말, 마무리 문구, 방문 안내 같은 일반적 표현, 그리고 '허용된 계정 고정 정보'에 "
            "적힌 매장명·주소 등은 위반이 아닙니다.\n"
            "위반이 없으면 score를 90 이상으로, failures를 빈 배열로 두십시오. 위반이 있으면 채널별로 "
            "ValidationFailureDetail을 작성하되 rule_violated는 'unsupported_fact', 'fact_mismatch', "
            "'relative_date', 'missing_key_fact', 'garbled_text' 중 하나로, reason에는 문제가 된 문구를 그대로 인용하고, "
            "fix_instruction에는 무엇을 지우거나 어떤 값으로 고칠지 구체적으로 적으십시오."
        )

        facts_lines = "(없음)"
        ocr_lines = "(없음)"
        if vision_result is not None:
            if vision_result.notice_facts:
                facts_lines = "\n".join(f"- {f.label}: {f.value}" for f in vision_result.notice_facts)
            if vision_result.extracted_text:
                ocr_lines = " | ".join(vision_result.extracted_text)

        style_lines = "(없음)"
        if style_profiles:
            style_chunks = [
                f"[{ch}] {prof.account_handle}\n" + "\n---\n".join(prof.sample_posts[:2])
                for ch, prof in style_profiles.items()
            ]
            style_lines = "\n\n".join(style_chunks)

        draft_payload = {
            ch: getattr(draft, ch).model_dump()
            for ch in enabled_channels
            if getattr(draft, ch) is not None
        }
        prompt = (
            f"오늘 날짜: {today_kst_label()}\n"
            f"검사 대상 채널: {enabled_channels}\n\n"
            f"### 입력 자료 1: 사용자 메모\n{user_prompt or '(없음)'}\n\n"
            f"### 입력 자료 2: 확정 팩트\n{facts_lines}\n\n"
            f"### 입력 자료 3: 이미지 OCR 텍스트\n{ocr_lines}\n\n"
            f"### 허용된 계정 고정 정보 (스타일 프로필 예시 글)\n{style_lines}\n\n"
            f"### 검사할 원고(JSON)\n{json.dumps(draft_payload, ensure_ascii=False, indent=2)}\n"
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

        failures = list(score_result.failures)
        if not failures:
            failures = [
                ValidationFailureDetail(
                    channel=ch,
                    rule_violated="fact_check_score",
                    reason=f"팩트 일치 점수가 기준치(80점) 미달입니다 ({score_result.score}점).",
                    fix_instruction="확정 팩트와 사용자 메모에 있는 사실만 남기고, 값이 정확히 일치하는지 다시 확인하십시오.",
                )
                for ch in enabled_channels
            ]
        return ValidationReport(is_valid=False, failures=failures)

    async def validate(
        self,
        draft: SocialPostOutput,
        enabled_channels: list[ChannelType],
        vision_result: VisionAnalysisResult | None = None,
        user_prompt: str = "",
        style_profiles: dict[ChannelType, AccountStyleProfile] | None = None,
    ) -> ValidationReport:
        stage1 = self.validate_stage1_deterministic(draft, enabled_channels)
        if not stage1.is_valid:
            return stage1
        return await self.validate_stage2_semantic(
            draft,
            enabled_channels,
            vision_result=vision_result,
            user_prompt=user_prompt,
            style_profiles=style_profiles,
        )

    async def _generate_channels(
        self,
        copy_agent: CopyAgent,
        channels: list[ChannelType],
        **gen_kwargs: Any,
    ) -> tuple[dict[ChannelType, ChannelDraftSpec], dict[ChannelType, ValidationFailureDetail], str]:
        """
        `channels`를 한 번에 생성한다. 스키마 검증 실패(한 채널의 형식 오류가 응답 전체를 무효화)가 나면
        채널별 단독 호출로 나눠 다시 시도해, 한 채널의 실패가 다른 채널을 막지 않게 한다.
        Returns (채널별 원고, 채널별 생성 오류, generation_reasoning).
        """
        try:
            out = await copy_agent.generate_drafts(enabled_channels=channels, **gen_kwargs)
        except (ValueError, LLMGenerationError) as exc:
            if len(channels) == 1:
                return {}, {channels[0]: _schema_failure(channels[0], exc)}, ""
            results = await asyncio.gather(
                *(self._generate_channels(copy_agent, [ch], **gen_kwargs) for ch in channels)
            )
            merged_specs: dict[ChannelType, ChannelDraftSpec] = {}
            merged_errors: dict[ChannelType, ValidationFailureDetail] = {}
            reasons: list[str] = []
            for specs_part, errors_part, reason_part in results:
                merged_specs.update(specs_part)
                merged_errors.update(errors_part)
                if reason_part:
                    reasons.append(reason_part)
            return merged_specs, merged_errors, " / ".join(reasons)

        specs: dict[ChannelType, ChannelDraftSpec] = {}
        for ch in channels:
            spec = getattr(out, ch, None)
            if spec is not None:
                specs[ch] = spec
        return specs, {}, str(getattr(out, "generation_reasoning", "") or "")

    async def run_with_self_correction(
        self,
        copy_agent: CopyAgent,
        vision_result: VisionAnalysisResult,
        tag_set: MultilingualTagSet,
        user_prompt: str,
        enabled_channels: list[ChannelType],
        max_attempts: int = 3,
        notice_category: NoticeCategoryType = "general",
        style_profiles: dict[ChannelType, AccountStyleProfile] | None = None,
        approved_examples: dict[ChannelType, list[str]] | None = None,
    ) -> tuple[SocialPostOutput, ValidationReport, int]:
        """
        최대 `max_attempts`회 자기교정한다. 1회차는 thinking_level="low", 재시도는 "medium".
        - 통과한 채널은 확정하고, 실패한 채널만 해당 피드백과 함께 다시 생성한다.
        - 스키마 오류도 예외로 끝내지 않고 피드백으로 되돌린다.
        - 마지막까지 남은 Stage 1 위반은 report.failures(승인 차단), Stage 2(팩트) 지적은
          report.warnings(사람 확인용)로 반환한다.
        Returns (final_draft, final_validation_report, attempts_used).
        """
        specs: dict[ChannelType, ChannelDraftSpec] = {}
        hard: dict[ChannelType, list[ValidationFailureDetail]] = {}
        soft: dict[ChannelType, list[ValidationFailureDetail]] = {}
        feedback: dict[ChannelType, list[ValidationFailureDetail]] = {}
        reasoning_parts: list[str] = []
        pending: list[ChannelType] = list(enabled_channels)
        attempts_used = 0

        for attempt in range(1, max_attempts + 1):
            if not pending:
                break
            attempts_used = attempt
            thinking_level: Literal["low", "medium", "high"] = "low" if attempt == 1 else "medium"
            feedback_list = [f for ch in pending for f in feedback.get(ch, [])] or None

            produced, gen_errors, reasoning = await self._generate_channels(
                copy_agent,
                pending,
                vision_result=vision_result,
                tag_set=tag_set,
                user_prompt=user_prompt,
                feedback_list=feedback_list,
                thinking_level=thinking_level,
                notice_category=notice_category,
                style_profiles=style_profiles,
                approved_examples=approved_examples,
            )
            if reasoning:
                reasoning_parts.append(reasoning)

            still_failing: set[ChannelType] = set()
            stage1_passed: list[ChannelType] = []
            for ch in pending:
                spec = produced.get(ch)
                if spec is None:
                    err = gen_errors.get(ch) or _missing_channel_failure(ch)
                    feedback[ch] = [err]
                    if ch not in specs:
                        hard[ch] = [err]
                    still_failing.add(ch)
                    continue
                specs[ch] = spec
                soft[ch] = []
                det = validate_single_channel_deterministic(ch, spec, self.personas_config)
                hard[ch] = det
                if det:
                    feedback[ch] = det
                    still_failing.add(ch)
                else:
                    stage1_passed.append(ch)

            if stage1_passed and self.enable_semantic_stage:
                sem_by_channel = await self._fact_check_channels(
                    {ch: specs[ch] for ch in stage1_passed},
                    vision_result=vision_result,
                    user_prompt=user_prompt,
                    style_profiles=style_profiles,
                )
                for ch, sem_failures in sem_by_channel.items():
                    if sem_failures:
                        soft[ch] = sem_failures
                        feedback[ch] = sem_failures
                        still_failing.add(ch)

            pending = [ch for ch in enabled_channels if ch in still_failing]

        if not specs:
            details = "; ".join(f.reason for fs in hard.values() for f in fs) or "원인 불명"
            raise LLMGenerationError(f"모든 채널의 원고 생성에 실패했습니다: {details}")

        produced_channels: list[ChannelType] = [ch for ch in enabled_channels if ch in specs]
        final_kwargs: dict[str, Any] = {ch: specs[ch] for ch in produced_channels}
        final_draft = SocialPostOutput(
            enabled_channels=produced_channels,
            generation_reasoning=reasoning_parts[-1] if reasoning_parts else "",
            **final_kwargs,
        )
        all_failures = [f for ch in enabled_channels for f in hard.get(ch, [])]
        all_warnings = [f for ch in enabled_channels for f in soft.get(ch, [])]
        report = ValidationReport(
            is_valid=len(all_failures) == 0,
            failures=all_failures,
            warnings=all_warnings,
        )
        return final_draft, report, max(1, attempts_used)

    async def _fact_check_channels(
        self,
        channel_specs: dict[ChannelType, ChannelDraftSpec],
        vision_result: VisionAnalysisResult,
        user_prompt: str,
        style_profiles: dict[ChannelType, AccountStyleProfile] | None,
    ) -> dict[ChannelType, list[ValidationFailureDetail]]:
        """Stage 2를 주어진 채널들에만 실행하고 지적 사항을 채널별로 묶어 돌려준다."""
        channels: list[ChannelType] = list(channel_specs)
        result: dict[ChannelType, list[ValidationFailureDetail]] = {ch: [] for ch in channels}
        spec_kwargs: dict[str, Any] = {ch: channel_specs.get(ch) for ch in ALL_CHANNELS_LIST}
        partial = SocialPostOutput.model_construct(
            enabled_channels=channels,
            generation_reasoning="",
            **spec_kwargs,
        )
        try:
            report = await self.validate_stage2_semantic(
                partial,
                channels,
                vision_result=vision_result,
                user_prompt=user_prompt,
                style_profiles=style_profiles,
            )
        except (ValueError, LLMGenerationError):
            # 검수 모델의 응답 형식 오류로 이미 규격을 통과한 원고까지 버리지는 않는다.
            return result
        for failure in report.failures:
            if failure.channel in result:
                result[failure.channel].append(failure)
            else:
                for ch in channels:
                    result[ch].append(failure.model_copy(update={"channel": ch}))
        return result


def _missing_channel_failure(channel: ChannelType) -> ValidationFailureDetail:
    return ValidationFailureDetail(
        channel=channel,
        rule_violated="missing_enabled_channel",
        reason=f"활성화된 채널 '{channel}'의 원고가 생성되지 않았습니다(null).",
        fix_instruction=f"'{channel}' 채널 필드에 규격에 맞는 원고 객체를 반드시 생성하십시오.",
    )


def _schema_failure(channel: ChannelType, exc: Exception) -> ValidationFailureDetail:
    return ValidationFailureDetail(
        channel=channel,
        rule_violated="schema_validation",
        reason=f"'{channel}' 원고가 출력 스키마를 통과하지 못했습니다: {str(exc)[:400]}",
        fix_instruction=(
            "필드 타입, 필수 필드, 글자 수·개수 제한을 정확히 지켜 SocialPostOutput 스키마에 맞는 JSON으로 다시 작성하고, "
            "활성화되지 않은 채널 필드는 null로 두십시오."
        ),
    )
