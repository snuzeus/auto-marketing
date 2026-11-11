from __future__ import annotations

from typing import Any

from src.core.llm_client import GeminiLLMClient
from src.core.schema import ChannelType, MultilingualTagSet, VisionAnalysisResult


class TagEngine:
    """Ultra-fast tag extractor using `gemini-2.5-flash-lite` (or configured lite model)."""

    def __init__(self, llm_client: GeminiLLMClient) -> None:
        self.llm = llm_client

    async def generate_tags(
        self,
        vision_result: VisionAnalysisResult,
        user_prompt: str,
        enabled_channels: list[ChannelType],
        personas_config: dict[str, Any] | None = None,
    ) -> MultilingualTagSet:
        seo_cfg = (personas_config or {}).get("global_seo", {})
        system_instruction = (
            "당신은 글로벌 SNS(Instagram, X) 및 네이버 카페(C-Rank / D.I.A.+) 검색 최적화(SEO) 전문가입니다.\n"
            "이미지 비전 분석 결과와 사용자 요청을 바탕으로 다음 규칙에 맞춰 해시태그 및 검색 키워드를 추출하십시오:\n"
            "1. ko_tags, en_tags, ja_tags, instagram_combined, x_combined의 모든 태그는 반드시 '#'으로 시작해야 합니다.\n"
            "2. instagram_combined는 한국어(50%), 영어(30%), 일본어(20%) 비율로 10~15개를 조합하십시오.\n"
            "3. x_combined는 가장 파급력 높은 핵심 해시태그 2~3개만 선정하십시오.\n"
            "4. naver_cafe_search_tags는 '#' 기호를 절대 포함하지 않는 순수 검색 키워드 5~10개로 작성하십시오."
        )

        active_str = ", ".join(enabled_channels)
        user_content = (
            f"활성화된 채널: [{active_str}]\n"
            f"사용자 프롬프트: {user_prompt}\n"
            f"비전 분석 결과: {vision_result.model_dump_json(indent=2)}\n"
            f"글로벌 SEO 가이드라인: {seo_cfg}\n"
            "위 정보를 바탕으로 MultilingualTagSet JSON을 생성하십시오."
        )

        raw_tags = await self.llm.generate_structured(
            model=self.llm.lite_model,
            system_instruction=system_instruction,
            user_content=user_content,
            response_model=MultilingualTagSet,
            thinking_level="low",
        )
        return self._normalize_tags(raw_tags)

    @staticmethod
    def _normalize_tags(tag_set: MultilingualTagSet) -> MultilingualTagSet:
        """Ensure '#' prefix on SNS hashtags and strip '#' from Naver Cafe search tags."""

        def ensure_hash(tags: list[str]) -> list[str]:
            cleaned: list[str] = []
            for t in tags:
                s = t.strip().replace(" ", "")
                if not s:
                    continue
                if not s.startswith("#"):
                    s = f"#{s}"
                cleaned.append(s)
            return cleaned

        def strip_hash(tags: list[str]) -> list[str]:
            cleaned: list[str] = []
            for t in tags:
                s = t.strip().lstrip("#").strip()
                if s:
                    cleaned.append(s)
            return cleaned

        return MultilingualTagSet(
            ko_tags=ensure_hash(tag_set.ko_tags),
            en_tags=ensure_hash(tag_set.en_tags),
            ja_tags=ensure_hash(tag_set.ja_tags),
            instagram_combined=ensure_hash(tag_set.instagram_combined)[:15],
            x_combined=ensure_hash(tag_set.x_combined)[:3],
            naver_cafe_search_tags=strip_hash(tag_set.naver_cafe_search_tags)[:10],
        )
