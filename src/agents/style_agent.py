from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from src.core.llm_client import GeminiLLMClient
from src.core.schema import REMOVED_CHANNELS, AccountStyleProfile, ChannelType

DEFAULT_STYLE_PROFILES_PATH = Path("config/style_profiles.json")


class _LLMExtractedStyleSpec(BaseModel):
    tone_summary: str = Field(description="전반적인 공지 말투, 어조, 문장 호흡 요약")
    opening_pattern: str = Field(description="첫 줄 헤드라인([공지] 등) 및 도입부 인사말 패턴")
    bullet_and_formatting_rules: list[str] = Field(
        min_length=2,
        max_length=6,
        description="줄바꿈 빈도, 글머리 기호(•, -, ▪, 📌 등), 일시/장소/내용 구분 서식 규칙",
    )
    emoji_policy: str = Field(description="이모지 사용 빈도, 자주 쓰는 이모지 종류 및 배치 위치")
    closing_signature: str = Field(description="마무리 인사말, 문의처 안내, 고정 서명 패턴")


class StyleProfileManager:
    """Persists and retrieves per-account writing style profiles from disk."""

    def __init__(self, storage_path: Path | str = DEFAULT_STYLE_PROFILES_PATH) -> None:
        self.storage_path = Path(storage_path)

    def list_profiles(self, platform: ChannelType | None = None) -> list[AccountStyleProfile]:
        if not self.storage_path.exists():
            return []
        try:
            raw_list = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(raw_list, list):
            return []
        profiles = [
            AccountStyleProfile.model_validate(item)
            for item in raw_list
            if item.get("platform") not in REMOVED_CHANNELS
        ]
        if platform is not None:
            return [p for p in profiles if p.platform == platform]
        return profiles

    def get_profile(self, profile_id: str) -> AccountStyleProfile | None:
        for p in self.list_profiles():
            if p.profile_id == profile_id:
                return p
        return None

    def save_profile(self, profile: AccountStyleProfile) -> AccountStyleProfile:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        existing = [p for p in self.list_profiles() if p.profile_id != profile.profile_id]
        existing.append(profile)
        serialized = [p.model_dump() for p in existing]
        self.storage_path.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        existing = self.list_profiles()
        filtered = [p for p in existing if p.profile_id != profile_id]
        if len(filtered) == len(existing):
            return False
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(
            json.dumps([p.model_dump() for p in filtered], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True


def make_profile_id(platform: ChannelType, account_handle: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9가-힣_]+", "_", account_handle.strip().lstrip("@")).strip("_").lower()
    if not slug:
        slug = "account"
    return f"{platform}_{slug}"


class StyleAnalyzerAgent:
    """Analyzes sample posts from an Instagram or X account to learn its notice/post writing style."""

    def __init__(self, llm_client: GeminiLLMClient) -> None:
        self.llm = llm_client

    async def learn_account_style(
        self,
        platform: ChannelType,
        account_handle: str,
        sample_posts: list[str],
    ) -> AccountStyleProfile:
        cleaned_samples = [s.strip() for s in sample_posts if s.strip()]
        if not cleaned_samples:
            raise ValueError("스타일 학습을 위해 최소 1개 이상의 기존 작성글 샘플을 입력해 주세요.")

        system_instruction = (
            "당신은 SNS 계정 브랜딩 및 문체 분석 전문가입니다.\n"
            "사용자가 제공한 특정 계정(Instagram, X 등)의 실제 공지글/게시물 샘플들을 정밀 분석하여, "
            "앞으로 새로운 공지사항을 작성할 때 그 계정의 운영자가 직접 쓴 것처럼 100% 동일한 문체와 서식을 "
            "재현할 수 있도록 스타일 규칙을 추출하십시오.\n"
            "특히 다음 요소를 집중 분석하십시오:\n"
            "1. 첫 줄 공지 헤더 표기법 (예: '[공지]', '📢 안내 말씀',괄호 스타일 등) 및 첫 인사말\n"
            "2. 종결어미 (예: '~ 안내드립니다', '~ 참고 부탁드려요', '~ 공지함' 등)\n"
            "3. 일시/장소/내용을 나열할 때 쓰는 기호 (예: •, -, ▪, ✔, 숫자 등)와 줄바꿈 간격\n"
            "4. 이모지 사용 빈도와 위치\n"
            "5. 끝인사 및 고정 마무리 문구"
        )

        formatted_samples = "\n\n---\n\n".join(
            f"[샘플 {idx + 1}]\n{sample}" for idx, sample in enumerate(cleaned_samples)
        )
        user_content = (
            f"대상 플랫폼: {platform}\n"
            f"계정 이름/핸들: {account_handle}\n\n"
            f"기존 작성글 샘플:\n{formatted_samples}\n\n"
            "위 샘플들의 고유한 작성 스타일을 추출하여 JSON으로 반환하십시오."
        )

        extracted = await self.llm.generate_structured(
            model=self.llm.lite_model,
            system_instruction=system_instruction,
            user_content=user_content,
            response_model=_LLMExtractedStyleSpec,
            thinking_level="low",
        )

        profile_id = make_profile_id(platform, account_handle)
        return AccountStyleProfile(
            profile_id=profile_id,
            account_handle=account_handle.strip(),
            platform=platform,
            tone_summary=extracted.tone_summary,
            opening_pattern=extracted.opening_pattern,
            bullet_and_formatting_rules=extracted.bullet_and_formatting_rules,
            emoji_policy=extracted.emoji_policy,
            closing_signature=extracted.closing_signature,
            sample_posts=cleaned_samples[:5],
        )
