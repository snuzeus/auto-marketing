# `src/core/` Specification (`src/core/SPEC.md`)

본 문서는 `Phase 2` 구현 시 참조하는 코어 데이터 모델 및 Gemini API 래퍼 명세입니다.

## 1. `src/core/config_loader.py` — 동적 플랫폼 선택 리졸버

```
from typing import Literal

ChannelType = Literal["instagram", "x_twitter", "dcinside", "naver_cafe"]
ALL_SUPPORTED_CHANNELS: tuple[ChannelType, ...] = (
    "instagram",
    "x_twitter",
    "dcinside",
    "naver_cafe",
)


def resolve_enabled_channels(
    personas_config: dict,
    cli_channels_arg: str | None = None,
) -> list[ChannelType]:
    """
    1순위: CLI `--channels` 인자 (예: "instagram,naver_cafe")
    2순위: config/personas.yaml의 `active_channels` 중 `channels.<name>.enabled == True`인 채널
    """
    if cli_channels_arg:
        requested = [c.strip().lower() for c in cli_channels_arg.split(",") if c.strip()]
        invalid = [c for c in requested if c not in ALL_SUPPORTED_CHANNELS]
        if invalid:
            raise ValueError(f"지원하지 않는 플랫폼입니다: {invalid}. 지원 목록: {ALL_SUPPORTED_CHANNELS}")
        return requested  # type: ignore[return-value]

    active_list = personas_config.get("active_channels", list(ALL_SUPPORTED_CHANNELS))
    channels_cfg = personas_config.get("channels", {})
    resolved: list[ChannelType] = [
        ch for ch in active_list
        if ch in ALL_SUPPORTED_CHANNELS and channels_cfg.get(ch, {}).get("enabled", True)
    ]
    if not resolved:
        raise ValueError("활성화된 플랫폼이 하나도 없습니다. personas.yaml 또는 --channels 옵션을 확인하세요.")
    return resolved

```

## 2. `src/core/schema.py` — Pydantic v2 데이터 계약 (Gemini `response_schema` 호환)

```
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, field_validator

ChannelType = Literal["instagram", "x_twitter", "dcinside", "naver_cafe"]


class ImageMetadata(BaseModel):
    file_path: str
    width: int
    height: int
    aspect_ratio_float: float
    aspect_ratio_label: str = Field(description="예: '4:5', '1:1', '16:9'")
    is_instagram_compatible: bool


class VisionAnalysisResult(BaseModel):
    primary_subject: str = Field(description="이미지 내 핵심 피사체 및 상품/장소 요약")
    visual_mood: list[str] = Field(description="색감, 조명, 분위기 키워드")
    key_selling_points: list[str] = Field(min_length=2, max_length=5, description="시각적으로 도출된 마케팅 소구점")
    suggested_alt_text: str = Field(description="접근성 및 SEO를 위한 이미지 대체 텍스트")
    image_meta: ImageMetadata


class MultilingualTagSet(BaseModel):
    ko_tags: list[str] = Field(default_factory=list, description="'#'이 포함된 한국어 SEO 해시태그 리스트")
    en_tags: list[str] = Field(default_factory=list, description="'#'이 포함된 글로벌 영어 SEO 해시태그 리스트")
    ja_tags: list[str] = Field(default_factory=list, description="'#'이 포함된 일본어 현지화 해시태그 리스트")
    instagram_combined: list[str] = Field(default_factory=list, description="인스타그램용 한/영/일 최적 배합 태그 (10~15개)")
    x_combined: list[str] = Field(default_factory=list, description="X(트위터)용 고효율 핵심 태그 (2~3개)")
    naver_cafe_search_tags: list[str] = Field(
        default_factory=list,
        description="네이버 카페 C-Rank/D.I.A.+ 검색 최적화용 태그 ('#' 제외한 순수 키워드 5~10개)",
    )


class InstagramPostSpec(BaseModel):
    hook_headline: str = Field(description="첫 줄에 노출될 감각적인 후킹 문장")
    body: str = Field(description="줄바꿈과 이모지가 적용된 인스타그램 본문 (해시태그 제외)")
    hashtags: list[str] = Field(min_length=10, max_length=15)

    @property
    def full_caption(self) -> str:
        tags_str = " ".join(self.hashtags)
        return f"{self.hook_headline}\n\n{self.body}\n\n{tags_str}".strip()


class XTwitterPostSpec(BaseModel):
    body: str = Field(description="후킹 문장과 본문이 결합된 텍스트 (해시태그 제외)")
    hashtags: list[str] = Field(min_length=2, max_length=3)
    total_char_count: int = Field(description="최종 조합된 전체 문자열 길이 (반드시 280자 이하)")

    @property
    def full_tweet(self) -> str:
        tags_str = " ".join(self.hashtags)
        return f"{self.body}\n\n{tags_str}".strip()

    @field_validator("total_char_count")
    @classmethod
    def check_limit(cls, v: int) -> int:
        if v > 280:
            raise ValueError(f"X(트위터) 총 글자 수가 280자를 초과했습니다: {v}자")
        return v


class DCInsidePostSpec(BaseModel):
    gallery_id: str = Field(default="gadget", description="타겟 디시인사이드 갤러리 식별자")
    title: str = Field(max_length=40, description="호기심을 유발하는 갤러리 자연스러운 제목")
    body: str = Field(description="음슴체(~함, ~임) 및 단문 위주의 광고 배제 본문")
    contains_forbidden_words: bool = Field(default=False)


class NaverCafePostSpec(BaseModel):
    club_id: str = Field(description="네이버 카페 고유 clubid")
    menu_id: int = Field(description="네이버 카페 게시판 menuid")
    head_tag: str = Field(default="[후기]", description="말머리 태그 (예: '[후기]', '[정보공유]')")
    subject: str = Field(max_length=50, description="C-Rank 핵심 키워드가 포함된 네이버 카페 게시글 제목")
    html_body: str = Field(description="네이버 카페 에디터 호환 HTML(<p>, <br>, <b>) 본문 (최소 300자 이상)")
    plain_body_preview: str = Field(description="텔레그램 미리보기 및 글자 수 검사용 순수 텍스트 본문")
    search_tags: list[str] = Field(
        min_length=5,
        max_length=10,
        description="'#' 기호를 제외한 네이버 카페 하단 검색 태그 5~10개",
    )


class SocialPostOutput(BaseModel):
    enabled_channels: list[ChannelType] = Field(description="이번 실행에서 활성화된 타겟 채널 리스트")
    instagram: InstagramPostSpec | None = None
    x_twitter: XTwitterPostSpec | None = None
    dcinside: DCInsidePostSpec | None = None
    naver_cafe: NaverCafePostSpec | None = None
    generation_reasoning: str = Field(description="활성 채널별 페르소나 및 피드백 반영 요약")


class ValidationFailureDetail(BaseModel):
    channel: Literal["instagram", "x_twitter", "dcinside", "naver_cafe", "schema"]
    rule_violated: str
    reason: str
    fix_instruction: str


class ValidationReport(BaseModel):
    is_valid: bool
    failures: list[ValidationFailureDetail] = Field(default_factory=list)

```

## 3. `src/core/llm_client.py` — Google GenAI SDK (`google-genai`) 비동기 래퍼

* `genai.Client(api_key=...).aio` 비동기 클라이언트를 캡슐화합니다.

* **핵심 토큰 절감 장치:**

  1. `thinking_config=types.ThinkingConfig(thinking_level="low")`를 기본 주입하여 `gemini-3.8-flash` 호출 시 불필요한 내부 추론 토큰 폭발을 차단합니다.

  2. `create_or_get_persona_cache()`를 통해 `personas.yaml`을 캐싱하여 반복 호출 및 Self-Correction 재시도 시 95% 입력 토큰 할인을 받습니다.

* **필수 메서드 시그니처:**

  * `async def analyze_image_structured(self, image_path: str, system_instruction: str, user_prompt: str, response_model: type[T]) -> T`

  * `async def generate_structured(self, model: str, system_instruction: str, user_content: str, response_model: type[T], thinking_level: Literal["low", "medium", "high"] = "low", cached_content_name: str | None = None) -> T`

  * `async def run_mcp_tool_loop(self, model: str, system_instruction: str, prompt: str, tools: list[types.Tool], tool_executor: Callable) -> list[dict]`
