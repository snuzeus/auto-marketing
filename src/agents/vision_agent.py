from __future__ import annotations

from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from src.core.llm_client import GeminiLLMClient
from src.core.schema import ImageMetadata, VisionAnalysisResult


class _LLMSubstantiveVisionOutput(BaseModel):
    """Internal LLM response schema before attaching deterministic Pillow ImageMetadata."""

    primary_subject: str = Field(description="이미지 내 핵심 피사체 및 상품/장소 요약")
    visual_mood: list[str] = Field(description="색감, 조명, 분위기 키워드")
    key_selling_points: list[str] = Field(
        min_length=2, max_length=5, description="시각적으로 도출된 마케팅 소구점"
    )
    suggested_alt_text: str = Field(description="접근성 및 SEO를 위한 이미지 대체 텍스트")


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


class VisionAgent:
    """Multimodal vision analyzer using Pillow + Gemini main workhorse model."""

    def __init__(self, llm_client: GeminiLLMClient) -> None:
        self.llm = llm_client

    async def analyze(self, image_path: str, user_prompt: str) -> VisionAnalysisResult:
        image_meta = inspect_image_metadata(image_path)

        system_instruction = (
            "당신은 SNS 퍼포먼스 마케팅 및 비주얼 브랜딩 전문 디렉터입니다.\n"
            "제공된 상품/장소/상황 이미지와 사용자의 프롬프트를 분석하여 핵심 피사체, "
            "시각적 분위기 키워드, 구매나 방문 욕구를 자극하는 마케팅 소구점(2~5개), "
            "그리고 시각장애인 접근성 및 검색엔진 최적화(SEO)를 위한 한국어 대체 텍스트(alt text)를 "
            "정확한 JSON 형식으로 추출하십시오."
        )

        prompt = (
            f"사용자 요청 프롬프트: {user_prompt}\n"
            f"이미지 해상도: {image_meta.width}x{image_meta.height} ({image_meta.aspect_ratio_label})\n"
            "이 이미지의 시각적 특징과 마케팅 소구점을 분석하세요."
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
            suggested_alt_text=llm_vision.suggested_alt_text,
            image_meta=image_meta,
        )
