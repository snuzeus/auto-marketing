from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ChannelType = Literal[
    "instagram",
    "x_twitter",
    "dcinside",
    "naver_cafe",
    "kakao_channel",
    "naver_place",
]
ALL_CHANNELS_LIST: tuple[ChannelType, ...] = (
    "instagram",
    "x_twitter",
    "dcinside",
    "naver_cafe",
    "kakao_channel",
    "naver_place",
)
DraftApprovalStatus = Literal["draft", "approved"]
NoticeCategoryType = Literal[
    "general",
    "schedule_change",
    "urgent",
    "event_open",
    "tcg_tournament",
    "tcg_product_release",
    "tcg_store_schedule",
    "tcg_weekly_schedule",
    "tcg_tournament_result",
]


X_WEIGHTED_CHAR_LIMIT = 280
INSTAGRAM_MAX_HASHTAGS = 5
_X_URL_PATTERN = re.compile(r"https?://\S+")
_X_URL_WEIGHT = 23
# twitter-text v3: 아래 코드포인트 범위는 1, 그 외(한글·한자·가나·이모지 등)는 2로 계산
_X_LIGHT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x10FF),
    (0x2000, 0x200D),
    (0x2010, 0x201F),
    (0x2032, 0x2037),
)


def x_weighted_length(text: str) -> int:
    """X(트위터)가 실제로 적용하는 가중치 글자 수. 한글 1자 = 2, URL = 23."""
    normalized = unicodedata.normalize("NFC", text)
    urls = _X_URL_PATTERN.findall(normalized)
    without_urls = _X_URL_PATTERN.sub("", normalized)
    total = len(urls) * _X_URL_WEIGHT
    for ch in without_urls:
        cp = ord(ch)
        total += 1 if any(lo <= cp <= hi for lo, hi in _X_LIGHT_RANGES) else 2
    return total


class ImageMetadata(BaseModel):
    file_path: str
    width: int
    height: int
    aspect_ratio_float: float
    aspect_ratio_label: str = Field(description="예: '4:5', '1:1', '16:9'")
    is_instagram_compatible: bool


class NoticeFact(BaseModel):
    """공지에 들어갈 수 있는 확정 사실 1건. 원고와 검증은 이 목록만 근거로 삼는다."""

    label: str = Field(description="항목명 (예: '종목', '상품', '일시', '장소', '가격', '구매 제한', '접수 방법')")
    value: str = Field(description="항목 값. 상대 날짜(내일, 이번 주 토요일 등)는 절대 날짜로 변환해 기입")
    source: Literal["user_prompt", "image", "both"] = Field(
        default="user_prompt",
        description="근거 출처: 사용자 메모 / 이미지 내 글자 / 둘 다",
    )


class VisionAnalysisResult(BaseModel):
    primary_subject: str = Field(description="공지 포스터/이미지의 핵심 주제 및 목적 요약")
    visual_mood: list[str] = Field(description="디자인 톤, 분위기, 시각적 구성 키워드")
    key_selling_points: list[str] = Field(
        min_length=2, max_length=5, description="공지에서 반드시 전달해야 할 핵심 사실/안내 항목"
    )
    extracted_text: list[str] = Field(
        default_factory=list,
        description="이미지(포스터/안내문/일정표) 내부에서 OCR로 읽어낸 날짜, 시간, 장소, 주요 문구 리스트",
    )
    notice_facts: list[NoticeFact] = Field(
        default_factory=list,
        description="사용자 메모와 이미지에서 확인된 확정 사실 목록 (추측 금지)",
    )
    suggested_alt_text: str = Field(description="시각장애인 접근성 및 공지 요약 대체 텍스트")
    image_meta: ImageMetadata


class AccountStyleProfile(BaseModel):
    """Learned writing style profile for a specific SNS account."""

    profile_id: str = Field(description="고유 프로필 식별자 (예: 'insta_official')")
    account_handle: str = Field(description="계정 아이디 또는 표시 이름 (예: '@official_studio')")
    platform: ChannelType = Field(description="대상 플랫폼")
    tone_summary: str = Field(description="AI가 분석한 전반적인 공지 말투 및 톤앤매너 요약")
    opening_pattern: str = Field(description="도입부 인사말 및 첫 줄 공지 헤더 패턴")
    bullet_and_formatting_rules: list[str] = Field(
        default_factory=list,
        description="줄바꿈 간격, 글머리 기호(•, -, ▪ 등), 날짜/일시 표기 방식 규칙",
    )
    emoji_policy: str = Field(description="이모지 사용 빈도, 선호 이모지 및 배치 위치")
    closing_signature: str = Field(description="마무리 인사, 문의 안내 및 계정 고정 서명 패턴")
    sample_posts: list[str] = Field(
        default_factory=list,
        description="학습에 사용된 기존 공지글 원문 샘플 리스트",
    )


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
        default_factory=list, description="인스타그램용 핵심 해시태그 (3~5개, 최대 5개)"
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
    hashtags: list[str] = Field(
        default_factory=list,
        description="관련성 높은 해시태그 0~5개 (인스타그램 게시물당 최대 5개)",
    )

    @property
    def full_caption(self) -> str:
        tags_str = " ".join(self.hashtags)
        return f"{self.hook_headline}\n\n{self.body}\n\n{tags_str}".strip()


class XTwitterPostSpec(BaseModel):
    body: str = Field(description="후킹 문장과 본문이 결합된 텍스트 (해시태그 제외)")
    hashtags: list[str] = Field(default_factory=list, description="핵심 해시태그 2~3개")
    total_char_count: int = Field(
        default=0,
        description="X 가중치 기준 전체 글자 수 (한글 1자=2, 영문/숫자/공백=1, URL=23). 반드시 280 이하",
    )

    @property
    def full_tweet(self) -> str:
        tags_str = " ".join(self.hashtags)
        return f"{self.body}\n\n{tags_str}".strip()

    @property
    def weighted_length(self) -> int:
        return x_weighted_length(self.full_tweet)

    @model_validator(mode="after")
    def sync_weighted_char_count(self) -> XTwitterPostSpec:
        # 모델이 적어낸 숫자는 믿지 않고 실제 가중치 글자 수로 덮어쓴다.
        # 한도 초과 여부는 예외가 아니라 ValidatorAgent Stage 1이 판정해 자기교정 피드백으로 돌려준다.
        self.total_char_count = x_weighted_length(self.full_tweet)
        return self


class DCInsidePostSpec(BaseModel):
    gallery_id: str = Field(default="boardgame", description="타겟 디시인사이드 갤러리 식별자")
    title: str = Field(max_length=40, description="호기심을 유발하는 갤러리 자연스러운 제목")
    body: str = Field(description="음슴체(~함, ~임) 및 단문 위주의 광고 배제 본문")
    contains_forbidden_words: bool = Field(default=False)


class NaverCafePostSpec(BaseModel):
    club_id: str = Field(description="네이버 카페 고유 clubid")
    menu_id: int = Field(description="네이버 카페 게시판 menuid")
    head_tag: str = Field(default="[대회공지]", description="말머리 태그 (예: '[대회공지]', '[입고공지]')")
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


class KakaoChannelPostSpec(BaseModel):
    title: str = Field(max_length=35, description="카카오톡 채널 메시지 상단 핵심 제목")
    body: str = Field(description="모바일 채팅창 가독성에 최적화된 400자 이내 공지 요약 본문")
    button_label: str = Field(
        default="매장 공지 자세히 보기",
        description="카카오톡 채널 하단 액션 버튼 문구",
    )

    @property
    def full_message(self) -> str:
        return f"[{self.title}]\n\n{self.body}\n\n🔗 버튼: {self.button_label}".strip()


class NaverPlacePostSpec(BaseModel):
    title: str = Field(max_length=35, description="네이버 스마트플레이스 새소식 제목")
    body: str = Field(description="방문객에게 신뢰감을 주는 500자 이내 매장 소식 본문")
    keywords: list[str] = Field(
        default_factory=list,
        description="플레이스 지역/종목 검색 노출 키워드 3~5개",
    )

    @property
    def full_news(self) -> str:
        kw = ", ".join(self.keywords)
        return f"{self.title}\n\n{self.body}\n\n키워드: {kw}".strip()


class SocialPostOutput(BaseModel):
    enabled_channels: list[ChannelType] = Field(
        description="이번 실행에서 활성화된 타겟 채널 리스트"
    )
    instagram: InstagramPostSpec | None = None
    x_twitter: XTwitterPostSpec | None = None
    dcinside: DCInsidePostSpec | None = None
    naver_cafe: NaverCafePostSpec | None = None
    kakao_channel: KakaoChannelPostSpec | None = None
    naver_place: NaverPlacePostSpec | None = None
    generation_reasoning: str = Field(description="활성 채널별 페르소나 및 피드백 반영 요약")

    @model_validator(mode="after")
    def check_channel_isolation(self) -> SocialPostOutput:
        """Ensure disabled channels remain None and enabled channels are populated."""
        enabled_set = set(self.enabled_channels)
        for ch in ALL_CHANNELS_LIST:
            val = getattr(self, ch)
            if ch in enabled_set and val is None:
                raise ValueError(f"활성화된 채널 '{ch}'의 생성 결과가 누락되었습니다(None).")
            if ch not in enabled_set and val is not None:
                raise ValueError(
                    f"비활성화된 채널 '{ch}'의 필드는 반드시 None이어야 합니다."
                )
        return self


class ValidationFailureDetail(BaseModel):
    channel: Literal[
        "instagram",
        "x_twitter",
        "dcinside",
        "naver_cafe",
        "kakao_channel",
        "naver_place",
        "schema",
    ]
    rule_violated: str
    reason: str
    fix_instruction: str


class ValidationReport(BaseModel):
    is_valid: bool
    failures: list[ValidationFailureDetail] = Field(default_factory=list)
    warnings: list[ValidationFailureDetail] = Field(
        default_factory=list,
        description="승인을 막지는 않지만 사람이 확인해야 하는 항목 (예: 팩트 검증 경고)",
    )


ChannelDraftSpec = (
    InstagramPostSpec
    | XTwitterPostSpec
    | DCInsidePostSpec
    | NaverCafePostSpec
    | KakaoChannelPostSpec
    | NaverPlacePostSpec
)

CardThemeType = Literal[
    "cyber_cyan",
    "crimson_red",
    "royal_gold",
    "emerald_green",
    "violet_purple",
    "minimal_light",
]


class CardNewsBoxSpec(BaseModel):
    label: str = Field(
        description="박스 상단 항목명 (예: '판매 장소', '판매 일시', '구매 제한', '판매 가격' 등 2~10자)"
    )
    value: str = Field(
        description="박스 내부 핵심 텍스트 (문장형 서술어를 빼고 핵심 키워드/명사구로만 짧게 작성. 2열 분할 시 '값1 / 값2' 형식)"
    )


class CardNewsSpec(BaseModel):
    main_title: str = Field(
        description="카드뉴스 최상단 메인 타이틀 (예: '신상품 판매 안내', '대회 참가 안내' 등 12자 이내)"
    )
    subtitle_banner: str = Field(
        description="타이틀 하단 배너 문구 (예: '리프트바운드 T1 플레이어 번들' 등 25자 이내)"
    )
    boxes: list[CardNewsBoxSpec] = Field(
        default_factory=list,
        min_length=1,
        max_length=6,
        description="중앙 핵심 정보 박스 리스트 (1~6개, 텍스트를 최대한 짧고 단순하게 요약)",
    )
    footnotes: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="박스 하단 참고/유의사항 문구 리스트 (최대 3줄)",
    )
    footer_brand: str = Field(
        default="OFFICIAL TCG STORE",
        description="최하단 브랜드/매장명 표기",
    )
    theme: CardThemeType = Field(
        default="cyber_cyan",
        description="카드뉴스 색상 테마 ('cyber_cyan', 'crimson_red', 'royal_gold', 'emerald_green', 'violet_purple', 'minimal_light')",
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
    card_news: CardNewsSpec | None = None

    def update_spec(
        self,
        new_spec: ChannelDraftSpec,
        validation_report: ValidationReport | None = None,
    ) -> None:
        """Update the channel draft content and automatically revoke approval."""
        self.spec = new_spec
        self.status = "draft"
        self.revision += 1
        self.card_news = None
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
