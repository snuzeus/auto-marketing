from __future__ import annotations

from typing import Literal

from google.genai import types

from src.core.clock import today_kst_label
from src.core.llm_client import GeminiLLMClient, _map_thinking_level
from src.core.schema import (
    INSTAGRAM_MAX_HASHTAGS,
    X_WEIGHTED_CHAR_LIMIT,
    AccountStyleProfile,
    ChannelType,
    MultilingualTagSet,
    NoticeCategoryType,
    SocialPostOutput,
    ValidationFailureDetail,
    VisionAnalysisResult,
)

NOTICE_CATEGORY_GUIDES: dict[NoticeCategoryType, str] = {
    "tcg_tournament": (
        "[TCG 대회/이벤트 공지] 유희왕, 포켓몬카드, 원피스카드, 디지몬 등 종목명을 명시하고, "
        "대회 일시, 진행 포맷/룰(스위스/토너먼트, 금제 규정), 참가비, 모집 정원, 사전/현장 접수 방식, "
        "참가상 및 상위 입상 상품(부스터팩, 프로모 카드, 매장 포인트 등)을 항목별로 빠짐없이 작성하십시오."
    ),
    "tcg_weekly_schedule": (
        "[주간 대회 일정표 일괄 공지] 월요일부터 일요일까지 요일별 진행되는 TCG 공인대회/비공인 이벤트의 "
        "요일, 시작 시간, 종목명, 포맷, 참가비를 한눈에 보기 쉬운 요일별 타임테이블(월~일) 형태로 깔끔하게 정리하여 안내하십시오."
    ),
    "tcg_tournament_result": (
        "[대회 결과 및 입상 덱 레시피 공지] 대회 참가자 전원에게 감사 인사를 전하고, "
        "우승(1위) 및 상위 입상자(2~4위)의 닉네임과 사용 리더/덱 아키타입(예: 흑루피, 리자몽ex, 스네이크아이 등), "
        "간략한 대회 분위기 및 다음 대회 일정을 축하 톤으로 작성하십시오."
    ),
    "tcg_product_release": (
        "[신규 부스터팩/스타터덱 입고 및 발매 공지] 입고 상품명, 정식 발매일 및 판매 개시 일시, "
        "판매 가격(소비자가/할인가), 1인당 구매 제한 수량(예: 1인 1박스, 1인 10팩 등 대기열 및 되팔이 방지 안내), "
        "결제 수단 안내(카드/현금/매장포인트)를 명확히 고지하십시오."
    ),
    "tcg_store_schedule": (
        "[TCG 매장 영업/휴무/듀얼스페이스 이용 공지] 오픈 및 마감 시간, 듀얼테이블/자유대전존 이용 요금 및 규칙 "
        "(외부음식 반입 여부, 쓰레기 분리수거, 트레이드 매너 등), 공휴일/임시휴무 일정을 직관적으로 안내하십시오."
    ),
    "general": (
        "[일반 공지/안내] 공지 목적과 핵심 내용을 명확하게 전달하고, "
        "일시·대상·세부사항을 항목별로 가독성 높게 정리하십시오."
    ),
    "schedule_change": (
        "[일정/휴무/운영시간 변경 공지] 변경 전후 일정이나 휴무 기간, 재오픈 일시를 "
        "가장 눈에 띄게 배치하고 방문/이용에 착오가 없도록 정중히 안내하십시오."
    ),
    "urgent": (
        "[긴급/점검/지연/사과 공지] 첫 줄에 긴급 안내임을 명확히 표기하고, "
        "발생 원인·현재 조치 현황·예상 복구/재개 시점·보상 또는 문의처를 신뢰감 있게 전달하십시오."
    ),
    "event_open": (
        "[이벤트/예약/모집 오픈 공지] 오픈 일시, 참여/예약 방법, 대상 및 유의사항을 "
        "순서대로 명료하게 정리하여 혼선을 방지하십시오."
    ),
}


def build_copywriter_system_instruction(personas_yaml_raw: str) -> str:
    return (
        "<INSTRUCTIONS>\n"
        "당신은 기업·브랜드·커뮤니티의 공식 SNS 채널(Instagram, X, 네이버 카페, 디시인사이드)을 총괄하는 "
        "수석 커뮤니케이션 매니저이자 공지 자동작성 전문 봇입니다.\n"
        "제공된 이미지 분석(OCR 추출 텍스트 포함)과 사용자의 공지 메모를 바탕으로, "
        "활성화된 타겟 채널(enabled_channels)에 대해서만 정확하고 가독성 높은 공지문을 작성하십시오.\n"
        "- 과장된 광고성 문구나 허위 사실을 절대 지어내지 마십시오.\n"
        "- 사실 정보(일시, 장소, 가격, 수량, 구매 제한, 혜택, 접수 방법 등)는 사용자 메모와 '확정 팩트' 목록에 "
        "있는 것만 쓰십시오. 거기에 없는 '한정 수량', '조기 품절', '선착순', '특전' 같은 내용을 덧붙이지 마십시오.\n"
        "- '내일', '모레' 같은 상대 날짜는 쓰지 말고 확정 팩트에 적힌 절대 날짜(예: 11월 17일(화))로 쓰십시오.\n"
        "- 스타일 프로필의 예시 글은 말투와 서식만 참고하고, 예시에 나온 일정·가격·상품·링크는 가져오지 마십시오. "
        "(매장명, 주소, 고정 인사말처럼 계정에 고정된 정보는 사용해도 됩니다.)\n"
        "- 일시, 장소, 대상, 변경사항, 유의사항 등 핵심 팩트를 누락 없이 구조화하여 전달하십시오.\n"
        "- 특정 채널에 대해 '계정 맞춤 스타일 프로필(AccountStyleProfile)'이 제공된 경우, 기본 페르소나보다 "
        "해당 계정의 고유 말투·도입부 패턴·글머리기호·줄바꿈·마무리 서명을 최우선으로 완벽히 복제하여 작성하십시오.\n"
        "</INSTRUCTIONS>\n\n"
        "<CONTEXT>\n"
        "다음은 기본 채널별 페르소나 및 규격 규칙(config/personas.yaml) 전문입니다:\n"
        f"```yaml\n{personas_yaml_raw}\n```\n"
        "</CONTEXT>"
    )


def _format_style_profiles_section(
    style_profiles: dict[ChannelType, AccountStyleProfile] | None,
) -> str:
    if not style_profiles:
        return ""
    blocks: list[str] = []
    for ch, prof in style_profiles.items():
        rules_str = "\n".join(f"  * {r}" for r in prof.bullet_and_formatting_rules)
        samples_str = "\n---\n".join(prof.sample_posts[:2])
        blocks.append(
            f"### [{ch}] 계정 맞춤 스타일 프로필: {prof.account_handle} (ID: {prof.profile_id})\n"
            f"- 전반적 말투/톤 요약: {prof.tone_summary}\n"
            f"- 첫 줄/도입부 패턴: {prof.opening_pattern}\n"
            f"- 줄바꿈 및 항목 나열 규칙:\n{rules_str}\n"
            f"- 이모지 사용 규칙: {prof.emoji_policy}\n"
            f"- 마무리 인사 및 고정 서명: {prof.closing_signature}\n"
            f"- 실제 작성글 참고 예시:\n{samples_str}"
        )
    joined = "\n\n".join(blocks)
    return (
        "<ACCOUNT_CUSTOM_STYLE_PROFILES>\n"
        "중요: 아래 채널은 사용자가 학습시킨 고유 계정 스타일이 지정되었습니다. "
        "해당 채널의 원고를 작성할 때는 위 기본 문체 대신 아래 계정의 인사말, 종결어미, 글머리기호, 줄바꿈 호흡, "
        "마무리 서명을 100% 그대로 따라 작성하십시오:\n\n"
        f"{joined}\n"
        "</ACCOUNT_CUSTOM_STYLE_PROFILES>\n\n"
    )


def _format_notice_facts_section(vision_result: VisionAnalysisResult) -> str:
    facts = vision_result.notice_facts
    if not facts:
        return (
            "<CONFIRMED_FACTS>\n"
            "(구조화된 확정 팩트 없음: 사용자 메모와 이미지 OCR 텍스트에 명시된 내용만 사실로 사용하십시오.)\n"
            "</CONFIRMED_FACTS>\n\n"
        )
    lines = "\n".join(f"- {f.label}: {f.value}" for f in facts)
    return (
        "<CONFIRMED_FACTS>\n"
        "아래가 이번 공지에 쓸 수 있는 확정 사실 전부입니다. 모든 채널 원고는 이 값과 정확히 일치해야 하며, "
        "여기에 없는 사실을 새로 만들지 마십시오:\n"
        f"{lines}\n"
        "</CONFIRMED_FACTS>\n\n"
    )


def _format_approved_examples_section(
    approved_examples: dict[ChannelType, list[str]] | None,
    enabled_channels: list[ChannelType],
) -> str:
    if not approved_examples:
        return ""
    blocks: list[str] = []
    for ch in enabled_channels:
        samples = [s.strip() for s in approved_examples.get(ch, []) if s.strip()]
        if not samples:
            continue
        joined = "\n---\n".join(s[:700] for s in samples[:2])
        blocks.append(f"### [{ch}] 최근 관리자가 승인한 공지\n{joined}")
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    return (
        "<RECENTLY_APPROVED_EXAMPLES>\n"
        "아래는 같은 채널에서 관리자가 직접 다듬어 승인한 최근 공지입니다. 문장 길이, 줄바꿈, 항목 표기 방식을 "
        "참고하되, 여기에 나온 일정·가격·상품 등 사실 정보는 절대 가져오지 마십시오:\n\n"
        f"{body}\n"
        "</RECENTLY_APPROVED_EXAMPLES>\n\n"
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
    notice_category: NoticeCategoryType = "general",
    style_profiles: dict[ChannelType, AccountStyleProfile] | None = None,
    approved_examples: dict[ChannelType, list[str]] | None = None,
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

    style_section = _format_style_profiles_section(style_profiles)
    facts_section = _format_notice_facts_section(vision_result)
    examples_section = _format_approved_examples_section(approved_examples, enabled_channels)
    category_guide = NOTICE_CATEGORY_GUIDES.get(notice_category, NOTICE_CATEGORY_GUIDES["general"])
    active_channels_str = ", ".join(enabled_channels)

    user_prompt_content = (
        f"{feedback_section}"
        f"{style_section}"
        f"{examples_section}"
        f"{facts_section}"
        "<TASK>\n"
        f"0. 오늘 날짜: {today_kst_label()}\n"
        f"1. 이번 실행에서 활성화된 타겟 채널(enabled_channels): [{active_channels_str}]\n"
        f"2. 공지 유형 가이드: {category_guide}\n"
        f"3. 사용자 공지 핵심 요청사항: {user_prompt}\n"
        f"4. 이미지 분석 및 포스터 내 OCR 텍스트 추출 결과(JSON): {vision_result.model_dump_json(indent=2)}\n"
        f"5. 관련 해시태그 및 검색 키워드 세트(JSON): {tag_set.model_dump_json(indent=2)}\n\n"
        f"위 데이터를 결합하여 활성화된 채널([{active_channels_str}])의 맞춤형 공지 원고만 작성하십시오. "
        "활성화되지 않은 나머지 채널 필드는 반드시 `null`로 출력하십시오.\n"
        "</TASK>\n\n"
        "<OUTPUT_FORMAT>\n"
        "응답은 제공된 `SocialPostOutput` response_schema 규격과 100% 일치하는 JSON 객체로만 반환하십시오.\n"
        "- `enabled_channels` 배열에는 활성화된 채널 식별자를 그대로 기입하십시오.\n"
        "- `instagram` 활성화 시: 첫 줄(`hook_headline`)에 공지 핵심 제목(예: `[공지] ...` 또는 계정 고유 헤더)을 배치하고, "
        "`body`에는 `판매 장소 : 내용`, `판매 일정 : 내용`, `구매 제한 : 내용` 등 명확한 `항목명 : 내용` 라인 위주와 "
        "유의사항은 `* ...` 각주 줄로 깔끔하게 정리하여 카드뉴스 2번째 슬라이드 박스 추출이 완벽하게 되도록 작성하십시오. "
        f"`hashtags`는 관련성 높은 것만 3~{INSTAGRAM_MAX_HASHTAGS}개 넣으십시오 "
        f"(인스타그램 정책상 게시물당 최대 {INSTAGRAM_MAX_HASHTAGS}개. 계정 스타일 프로필이 해시태그를 쓰지 않으면 "
        "0개도 허용).\n"
        "- `x_twitter` 활성화 시: X는 한글·한자·가나·이모지를 1자당 2, 영문·숫자·공백·줄바꿈을 1, URL을 23으로 "
        f"계산합니다. 본문과 해시태그(2~3개)를 합친 가중치 글자 수가 반드시 {X_WEIGHTED_CHAR_LIMIT} 이하"
        "(순수 한글 기준 약 130자 이내)여야 합니다. 핵심 공지 사실만 압축하여 전달하십시오.\n"
        "- `dcinside` 활성화 시: 본문에 이모지와 존댓말, 광고성 금지어를 절대 포함하지 마십시오. "
        "갤러리 이용자들에게 핵심 공지/일정/정보를 담백한 음슴체(~함, ~임)로 명확히 전달하십시오.\n"
        "- `naver_cafe` 활성화 시: `head_tag`는 기본 `[대회공지]`(또는 허용된 말머리)를 사용하고, "
        "카페 회원들에게 안내하는 정중하고 상세한 공지문으로 작성하십시오. "
        "`html_body`와 `plain_body_preview`는 최소 300자 이상으로 작성하며 `search_tags`에는 `#`을 뺀 "
        "키워드 5~10개를 넣으십시오.\n"
        "- `kakao_channel` 활성화 시: 모바일 카톡방에서 한눈에 읽히도록 `title`(35자 이하), `body`(400자 이하 핵심 요약), "
        "`button_label`(하단 액션 버튼명)을 작성하십시오.\n"
        "- `naver_place` 활성화 시: 네이버 스마트플레이스 새소식 탭 방문객을 위해 `title`(35자 이하), "
        "`body`(500자 이하 친절한 매장 안내), `keywords`(지역/종목 키워드 3~5개)를 작성하십시오.\n"
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
    """Multi-channel notice & announcement writer agent with custom account style injection."""

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
        notice_category: NoticeCategoryType = "general",
        style_profiles: dict[ChannelType, AccountStyleProfile] | None = None,
        approved_examples: dict[ChannelType, list[str]] | None = None,
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
            notice_category=notice_category,
            style_profiles=style_profiles,
            approved_examples=approved_examples,
        )

        return await self.llm.generate_structured(
            model=self.llm.main_model,
            system_instruction=system_instruction,
            user_content=user_prompt_content,
            response_model=SocialPostOutput,
            thinking_level=thinking_level,
            cached_content_name=cached_name,
        )
