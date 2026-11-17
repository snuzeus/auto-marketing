from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, Field

from src.core.clock import today_kst_label
from src.core.llm_client import GeminiLLMClient
from src.core.schema import ImageMetadata, NoticeFact, VisionAnalysisResult


class _LLMSubstantiveVisionOutput(BaseModel):
    """Internal LLM response schema before attaching deterministic Pillow ImageMetadata."""

    primary_subject: str = Field(description="공지 이미지/포스터의 핵심 주제 및 목적 요약")
    visual_mood: list[str] = Field(description="디자인 분위기, 색상, 구성 키워드")
    key_selling_points: list[str] = Field(
        min_length=2, max_length=5, description="공지에서 전달해야 할 핵심 사실/일정/주의사항 요약 (2~5개)"
    )
    extracted_text: list[str] = Field(
        default_factory=list,
        description="이미지 내에 적혀 있는 모든 주요 텍스트(날짜, 시간, 장소, 변경사항, 안내 문구) OCR 추출 리스트",
    )
    notice_facts: list[NoticeFact] = Field(
        default_factory=list,
        description=(
            "사용자 메모와 이미지 글자에서 직접 확인되는 확정 사실만 항목별로 나열 "
            "(종목, 상품, 일시, 장소, 가격, 구매 제한, 접수 방법, 정원, 상품/참가상 등). "
            "추측하거나 지어낸 내용은 넣지 않는다."
        ),
    )
    suggested_alt_text: str = Field(description="시각장애인 접근성 및 검색 최적화를 위한 공지 이미지 대체 텍스트")


def inspect_image_metadata(image_path: str | Path) -> ImageMetadata:
    """Inspect image dimensions and Instagram aspect ratio compatibility using Pillow."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"이미지 파일을 찾을 수 없습니다: {path}")

    with Image.open(path) as img:
        width, height = img.size

    if height <= 0 or width <= 0:
        raise ValueError(f"유효하지 않은 이미지 해상도입니다: {width}x{height}")

    ratio = round(width / height, 4)

    # Instagram feed supported aspect ratios: between 4:5 (0.8) and 1.91:1 (1.91)
    is_compatible = 0.79 <= ratio <= 1.92

    if abs(ratio - 1.0) <= 0.05:
        label = "1:1"
    elif abs(ratio - 0.8) <= 0.05:
        label = "4:5"
    elif abs(ratio - 1.91) <= 0.08 or abs(ratio - (16 / 9)) <= 0.08:
        label = "1.91:1" if abs(ratio - 1.91) <= 0.05 else "16:9"
    elif abs(ratio - 0.75) <= 0.05:
        label = "3:4"
    else:
        label = f"{width}:{height}"

    return ImageMetadata(
        file_path=str(path),
        width=width,
        height=height,
        aspect_ratio_float=ratio,
        aspect_ratio_label=label,
        is_instagram_compatible=is_compatible,
    )


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


class VisionAgent:
    """Multimodal vision & OCR analyzer for notice/announcement images."""

    def __init__(self, llm_client: GeminiLLMClient) -> None:
        self.llm = llm_client

    async def analyze(self, image_path: str, user_prompt: str) -> VisionAnalysisResult:
        image_meta = inspect_image_metadata(image_path)

        system_instruction = (
            "당신은 SNS 공지사항 및 안내문 분석 전문 AI 디렉터입니다.\n"
            "제공된 이미지(공지 포스터, 일정표, 안내문, 매장/행사 사진 등)와 사용자의 메모를 정밀 분석하여:\n"
            "1. 이미지 안에 적힌 모든 글자(일시, 기간, 시간, 장소, 변경 내용, 주의사항 등)를 OCR로 정확히 읽어 `extracted_text`에 나열하십시오.\n"
            "2. 공지의 핵심 목적(`primary_subject`)과 독자가 반드시 알아야 할 핵심 안내 사항 2~5가지(`key_selling_points`)를 정리하십시오.\n"
            "3. 시각적 분위기(`visual_mood`)와 이미지 대체 텍스트(`suggested_alt_text`)를 JSON 형식으로 추출하십시오.\n"
            "4. `notice_facts`에는 사용자 메모에 적혀 있거나 이미지 글자로 읽히는 사실만 `항목명`/`값`으로 정리하십시오.\n"
            "   - 메모나 이미지에 없는 내용(한정 수량, 조기 품절, 혜택, 가격, 시간 등)을 추측해서 넣지 마십시오.\n"
            "   - 사진의 분위기나 인상(고급스러움, 한정판 느낌 등)은 사실이 아니므로 `notice_facts`에 넣지 마십시오.\n"
            "   - '내일', '모레', '이번 주 토요일' 같은 상대 날짜는 오늘 날짜를 기준으로 계산해 "
            "'11월 17일(화)'처럼 절대 날짜와 요일로 바꿔 적으십시오. 시간 표현(오전 11시, 오픈부터 등)은 그대로 유지합니다.\n"
            "   - 메모와 이미지 내용이 서로 다르면 사용자 메모를 우선합니다."
        )

        prompt = (
            f"오늘 날짜: {today_kst_label()}\n"
            f"사용자 공지 내용/메모: {user_prompt}\n"
            f"이미지 해상도: {image_meta.width}x{image_meta.height} ({image_meta.aspect_ratio_label})\n"
            "이 이미지의 텍스트(OCR)와 핵심 공지 정보를 빠짐없이 분석하세요."
        )

        llm_vision = await self.llm.analyze_image_structured(
            image_path=image_path,
            system_instruction=system_instruction,
            user_prompt=prompt,
            response_model=_LLMSubstantiveVisionOutput,
        )

        return VisionAnalysisResult(
            primary_subject=llm_vision.primary_subject,
            visual_mood=llm_vision.visual_mood,
            key_selling_points=llm_vision.key_selling_points,
            extracted_text=_as_list(getattr(llm_vision, "extracted_text", None)),
            notice_facts=[
                f if isinstance(f, NoticeFact) else NoticeFact.model_validate(f)
                for f in _as_list(getattr(llm_vision, "notice_facts", None))
            ],
            suggested_alt_text=llm_vision.suggested_alt_text,
            image_meta=image_meta,
        )
