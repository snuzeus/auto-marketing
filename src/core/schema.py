from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ChannelType = Literal["instagram", "x_twitter", "dcinside", "naver_cafe"]
ALL_CHANNELS_LIST: tuple[ChannelType, ...] = (
    "instagram",
    "x_twitter",
    "dcinside",
    "naver_cafe",
)
DraftApprovalStatus = Literal["draft", "approved"]


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
    key_selling_points: list[str] = Field(
        min_length=2, max_length=5, description="시각적으로 도출된 마케팅 소구점"
    )
    suggested_alt_text: str = Field(description="접근성 및 SEO를 위한 이미지 대체 텍스트")
    image_meta: ImageMetadata


class MultilingualTagSet(BaseModel):
    ko_tags: list[str] = Field(
        default_factory=list, description="'#'이 포함된 한국어 SEO 해시태그 리스트"
    )
    en_tags: list[str] = Field(
        default_factory=list, description="'#'이 포함된 글로벌 영어 SEO 해시태그 리스트"
    )
    ja_tags: list[str] = Field(
        default_factory=list, description="'#'이 포함된 일본어 현지화 해시태그 리스트"
    )
    instagram_combined: list[str] = Field(
        default_factory=list, description="인스타그램용 한/영/일 최적 배합 태그 (10~15개)"
    )
    x_combined: list[str] = Field(
        default_factory=list, description="X(트위터)용 고효율 핵심 태그 (2~3개)"
    )
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

    @model_validator(mode="after")
    def verify_actual_tweet_length(self) -> XTwitterPostSpec:
        actual_len = len(self.full_tweet)
        if actual_len > 280:
            raise ValueError(
                f"X(트위터) 실제 조합된 트윗 길이가 280자를 초과했습니다: {actual_len}자"
            )
        return self


class DCInsidePostSpec(BaseModel):
    gallery_id: str = Field(default="gadget", description="타겟 디시인사이드 갤러리 식별자")
    title: str = Field(max_length=40, description="호기심을 유발하는 갤러리 자연스러운 제목")
    body: str = Field(description="음슴체(~함, ~임) 및 단문 위주의 광고 배제 본문")
    contains_forbidden_words: bool = Field(default=False)


class NaverCafePostSpec(BaseModel):
    club_id: str = Field(description="네이버 카페 고유 clubid")
    menu_id: int = Field(description="네이버 카페 게시판 menuid")
    head_tag: str = Field(default="[후기]", description="말머리 태그 (예: '[후기]', '[정보공유]')")
    subject: str = Field(
        max_length=50, description="C-Rank 핵심 키워드가 포함된 네이버 카페 게시글 제목"
    )
    html_body: str = Field(
        description="네이버 카페 에디터 호환 HTML(<p>, <br>, <b>) 본문 (최소 300자 이상)"
    )
    plain_body_preview: str = Field(
        description="텔레그램 미리보기 및 글자 수 검사용 순수 텍스트 본문"
    )
    search_tags: list[str] = Field(
        min_length=5,
        max_length=10,
        description="'#' 기호를 제외한 네이버 카페 하단 검색 태그 5~10개",
    )


class SocialPostOutput(BaseModel):
    enabled_channels: list[ChannelType] = Field(
        description="이번 실행에서 활성화된 타겟 채널 리스트"
    )
    instagram: InstagramPostSpec | None = None
    x_twitter: XTwitterPostSpec | None = None
    dcinside: DCInsidePostSpec | None = None
    naver_cafe: NaverCafePostSpec | None = None
    generation_reasoning: str = Field(description="활성 채널별 페르소나 및 피드백 반영 요약")

    @model_validator(mode="after")
    def check_channel_isolation(self) -> SocialPostOutput:
        """Ensure disabled channels remain None and enabled channels are populated."""
        all_channels: tuple[ChannelType, ...] = (
            "instagram",
            "x_twitter",
            "dcinside",
            "naver_cafe",
        )
        enabled_set = set(self.enabled_channels)
        for ch in all_channels:
            val = getattr(self, ch)
            if ch in enabled_set and val is None:
                raise ValueError(f"활성화된 채널 '{ch}'의 생성 결과가 누락되었습니다(None).")
            if ch not in enabled_set and val is not None:
                raise ValueError(
                    f"비활성화된 채널 '{ch}'의 필드는 반드시 None이어야 합니다."
                )
        return self


class ValidationFailureDetail(BaseModel):
    channel: Literal["instagram", "x_twitter", "dcinside", "naver_cafe", "schema"]
    rule_violated: str
    reason: str
    fix_instruction: str


class ValidationReport(BaseModel):
    is_valid: bool
    failures: list[ValidationFailureDetail] = Field(default_factory=list)


ChannelDraftSpec = (
    InstagramPostSpec | XTwitterPostSpec | DCInsidePostSpec | NaverCafePostSpec
)


class ChannelDraftState(BaseModel):
    """Per-channel draft state supporting independent edit, regeneration, and approval reset."""

    channel: ChannelType
    spec: ChannelDraftSpec
    status: DraftApprovalStatus = Field(default="draft")
    validation: ValidationReport = Field(
        default_factory=lambda: ValidationReport(is_valid=True, failures=[])
    )
    revision: int = Field(default=1, ge=1)

    def update_spec(
        self,
        new_spec: ChannelDraftSpec,
        validation_report: ValidationReport | None = None,
    ) -> None:
        """Update the channel draft content and automatically revoke approval."""
        self.spec = new_spec
        self.status = "draft"
        self.revision += 1
        if validation_report is not None:
            self.validation = validation_report

    def approve(self) -> None:
        """Mark draft as approved only when current validation passes."""
        if not self.validation.is_valid:
            msg = (
                f"검증에 실패한 초안({self.channel})은 승인할 수 없습니다. "
                "먼저 위반 항목을 수정하세요."
            )
            raise ValueError(msg)
        self.status = "approved"

    def unapprove(self) -> None:
        """Explicitly revoke approval status."""
        self.status = "draft"
