from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from src.agents.copy_agent import CopyAgent, build_gemini_copywriter_request
from src.agents.tag_engine import TagEngine
from src.agents.validator_agent import (
    ValidatorAgent,
    validate_single_channel_deterministic,
)
from src.agents.vision_agent import VisionAgent, inspect_image_metadata
from src.core.config_loader import load_personas_config, load_personas_raw
from src.core.schema import (
    DCInsidePostSpec,
    ImageMetadata,
    InstagramPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
    SocialPostOutput,
    VisionAnalysisResult,
    XTwitterPostSpec,
)


def _create_sample_image(path: Path, width: int = 800, height: int = 1000) -> Path:
    img = Image.new("RGB", (width, height), color=(120, 180, 140))
    img.save(path, format="JPEG")
    return path


def _sample_vision_result(img_path: str = "sample.jpg") -> VisionAnalysisResult:
    return VisionAnalysisResult(
        primary_subject="커스텀 알루미늄 기계식 키보드",
        visual_mood=["미니멀", "차분한", "데스크테리어"],
        key_selling_points=["풀알루미늄 하우징", "도각거리는 타건음", "무선 3모드 지원"],
        suggested_alt_text="책상 위에 놓인 실버 커스텀 기계식 키보드",
        image_meta=ImageMetadata(
            file_path=img_path,
            width=800,
            height=1000,
            aspect_ratio_float=0.8,
            aspect_ratio_label="4:5",
            is_instagram_compatible=True,
        ),
    )


def _sample_tags() -> MultilingualTagSet:
    return MultilingualTagSet(
        ko_tags=["#데스크테리어", "#기계식키보드", "#커스텀키보드", "#키보드추천", "#내돈내산"],
        en_tags=["#desksetup", "#mechanicalkeyboard", "#customkeyboard"],
        ja_tags=["#デスク周り", "#ガジェット"],
        instagram_combined=[f"#인스타태그{i}" for i in range(12)],
        x_combined=["#데스크테리어", "#desksetup"],
        naver_cafe_search_tags=[
            "기계식키보드추천",
            "커스텀키보드",
            "풀알루미늄키보드",
            "데스크테리어소품",
            "사무실키보드",
        ],
    )


def test_inspect_image_metadata_ratios(tmp_path: Path) -> None:
    p_4_5 = _create_sample_image(tmp_path / "four_five.jpg", 800, 1000)
    meta = inspect_image_metadata(p_4_5)
    assert meta.aspect_ratio_label == "4:5"
    assert meta.is_instagram_compatible is True

    p_tall = _create_sample_image(tmp_path / "tall.jpg", 400, 1000)
    meta_tall = inspect_image_metadata(p_tall)
    assert meta_tall.is_instagram_compatible is False


async def test_vision_agent_analyze(tmp_path: Path) -> None:
    img_path = _create_sample_image(tmp_path / "item.jpg", 1000, 1000)
    mock_llm = MagicMock()
    mock_llm.analyze_image_structured = AsyncMock(
        return_value=MagicMock(
            primary_subject="수제 말차 라떼",
            visual_mood=["따뜻한", "감성적인"],
            key_selling_points=["제주산 첫물 말차", "고소한 오트밀크"],
            suggested_alt_text="유리잔에 담긴 초록빛 말차 라떼",
        )
    )

    agent = VisionAgent(mock_llm)
    res = await agent.analyze(str(img_path), "신메뉴 말차 라떼")
    assert res.primary_subject == "수제 말차 라떼"
    assert res.image_meta.aspect_ratio_label == "1:1"
    assert res.image_meta.is_instagram_compatible is True


async def test_tag_engine_normalizes_hashes() -> None:
    mock_llm = MagicMock()
    mock_llm.lite_model = "gemini-2.5-flash-lite"
    mock_llm.generate_structured = AsyncMock(
        return_value=MultilingualTagSet(
            ko_tags=["데스크테리어", "#키보드"],
            en_tags=["desksetup"],
            ja_tags=["ガジェット"],
            instagram_combined=[f"tag{i}" for i in range(12)],
            x_combined=["tagA", "#tagB"],
            naver_cafe_search_tags=["#카페키워드1", "카페키워드2", "#카페키워드3", "키워드4", "키워드5"],
        )
    )
    engine = TagEngine(mock_llm)
    out = await engine.generate_tags(
        vision_result=_sample_vision_result(),
        user_prompt="키보드 홍보",
        enabled_channels=["instagram", "naver_cafe"],
    )
    assert all(t.startswith("#") for t in out.instagram_combined)
    assert all("#" not in t for t in out.naver_cafe_search_tags)


def test_validator_stage1_catches_dcinside_and_naver_cafe_violations() -> None:
    personas_cfg = load_personas_config()

    # DCInside with honorifics, emoji, and forbidden word
    bad_dc = DCInsidePostSpec(
        gallery_id="gadget",
        title="안녕하세요 여러분 키보드 소개해 드립니다",
        body="타건감이 정말 좋습니다 😊 할인 프로모션 중이에요.",
    )
    dc_failures = validate_single_channel_deterministic("dcinside", bad_dc, personas_cfg)
    violated_rules = {f.rule_violated for f in dc_failures}
    assert "no_honorifics" in violated_rules
    assert "no_emojis" in violated_rules
    assert "forbidden_words" in violated_rules

    # Naver Cafe with short body (<300 chars), invalid head_tag, '#' in search_tags
    bad_cafe = NaverCafePostSpec.model_construct(
        club_id="123",
        menu_id=42,
        head_tag="[광고]",
        subject="키보드 후기",
        html_body="<p>너무 짧아요</p>",
        plain_body_preview="너무 짧아요 최저가 좌표 공유",
        search_tags=["#태그1", "태그2", "태그3", "태그4", "태그5"],
    )
    cafe_failures = validate_single_channel_deterministic("naver_cafe", bad_cafe, personas_cfg)
    cafe_rules = {f.rule_violated for f in cafe_failures}
    assert "min_body_length" in cafe_rules
    assert "allowed_head_tags" in cafe_rules
    assert "search_tags_no_hash" in cafe_rules
    assert "forbidden_words" in cafe_rules


async def test_validator_self_correction_loop_escalates_thinking_level() -> None:
    personas_cfg = load_personas_config()
    personas_raw = load_personas_raw()

    # Attempt 1 returns invalid DCInside draft (honorifics); Attempt 2 returns valid draft
    bad_output = SocialPostOutput(
        enabled_channels=["dcinside"],
        dcinside=DCInsidePostSpec(
            gallery_id="gadget",
            title="키보드 샀습니다",
            body="정말 좋습니다 추천합니다.",
        ),
        generation_reasoning="1차 생성",
    )
    good_output = SocialPostOutput(
        enabled_channels=["dcinside"],
        dcinside=DCInsidePostSpec(
            gallery_id="gadget",
            title="알루미늄 키보드 3일 써본 후기",
            body="스테빌 소리 깔끔하고 도각거림 ㄹㅇ 실물깡패임",
        ),
        generation_reasoning="2차 교정 완료",
    )

    recorded_levels: list[str] = []

    mock_llm = MagicMock()
    mock_llm.main_model = "gemini-2.5-flash"
    mock_llm.lite_model = "gemini-2.5-flash-lite"
    mock_llm.create_or_get_persona_cache = AsyncMock(return_value=None)

    async def fake_generate_structured(**kwargs: Any) -> Any:
        if kwargs["response_model"] is SocialPostOutput:
            recorded_levels.append(kwargs["thinking_level"])
            return bad_output if len(recorded_levels) == 1 else good_output
        # Semantic check score >= 80
        score_mock = MagicMock()
        score_mock.score = 92
        score_mock.failures = []
        return score_mock

    mock_llm.generate_structured = AsyncMock(side_effect=fake_generate_structured)

    copy_agent = CopyAgent(mock_llm, personas_raw)
    validator = ValidatorAgent(mock_llm, personas_cfg, enable_semantic_stage=True)

    final_draft, report, attempts = await validator.run_with_self_correction(
        copy_agent=copy_agent,
        vision_result=_sample_vision_result(),
        tag_set=_sample_tags(),
        user_prompt="알루미늄 키보드 후기",
        enabled_channels=["dcinside"],
        max_attempts=3,
    )

    assert attempts == 2
    assert report.is_valid is True
    assert recorded_levels == ["low", "medium"]
    assert final_draft.dcinside is not None
    assert "실물깡패임" in final_draft.dcinside.body


def test_build_gemini_copywriter_request_includes_feedback() -> None:
    personas_raw = load_personas_raw()
    prompt_str, cfg = build_gemini_copywriter_request(
        personas_yaml_raw=personas_raw,
        vision_result=_sample_vision_result(),
        tag_set=_sample_tags(),
        user_prompt="키보드 소개",
        enabled_channels=["instagram", "x_twitter"],
        thinking_level="medium",
    )
    assert "instagram, x_twitter" in prompt_str
    assert cfg.response_mime_type == "application/json"
    assert InstagramPostSpec is not None
    assert XTwitterPostSpec is not None
