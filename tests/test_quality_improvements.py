"""플랫폼 규격(X 가중치 글자 수, 인스타 해시태그 5개), 채널별 자기교정, 팩트 계층, 승인본 재사용 검증."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from src.agents.copy_agent import build_gemini_copywriter_request
from src.agents.validator_agent import (
    ValidatorAgent,
    validate_single_channel_deterministic,
)
from src.core.config_loader import load_personas_config, load_personas_raw
from src.core.history_repo import CampaignSQLiteRepository
from src.core.llm_client import LLMGenerationError
from src.core.schema import (
    DCInsidePostSpec,
    ImageMetadata,
    InstagramPostSpec,
    MultilingualTagSet,
    NoticeFact,
    SocialPostOutput,
    ValidationFailureDetail,
    VisionAnalysisResult,
    XTwitterPostSpec,
    x_weighted_length,
)


def _vision(facts: list[NoticeFact] | None = None) -> VisionAnalysisResult:
    return VisionAnalysisResult(
        primary_subject="리프트바운드 T1 플레이어 번들 입고 안내",
        visual_mood=["레드", "블랙"],
        key_selling_points=["11월 17일 오픈부터 판매", "인당 1개"],
        notice_facts=facts or [],
        suggested_alt_text="번들 상품 사진",
        image_meta=ImageMetadata(
            file_path="sample.jpg",
            width=1080,
            height=1350,
            aspect_ratio_float=0.8,
            aspect_ratio_label="4:5",
            is_instagram_compatible=True,
        ),
    )


def _good_dc() -> DCInsidePostSpec:
    return DCInsidePostSpec(gallery_id="boardgame", title="신팩 입고 공지함", body="내일 오픈부터 판매함. 인당 1개임")


def _good_x() -> XTwitterPostSpec:
    return XTwitterPostSpec(body="T1 번들 11/17 오픈부터 판매", hashtags=["#리프트바운드", "#T1"])


def _long_x() -> XTwitterPostSpec:
    return XTwitterPostSpec(body="가" * 150, hashtags=["#리프트바운드", "#T1"])


class _FakeCopyAgent:
    """요청된 채널 목록을 기록하고 미리 정한 응답(또는 예외)을 순서대로 돌려주는 CopyAgent 대역."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requested: list[list[str]] = []
        self.feedbacks: list[Any] = []

    async def generate_drafts(self, **kwargs: Any) -> SocialPostOutput:
        self.requested.append(list(kwargs["enabled_channels"]))
        self.feedbacks.append(kwargs.get("feedback_list"))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[no-any-return]


def _validator(semantic_results: list[Any] | None = None) -> ValidatorAgent:
    mock_llm = MagicMock()
    mock_llm.lite_model = "lite"
    if semantic_results is None:
        ok = MagicMock()
        ok.score = 95
        ok.failures = []
        mock_llm.generate_structured = AsyncMock(return_value=ok)
    else:
        mock_llm.generate_structured = AsyncMock(side_effect=semantic_results)
    return ValidatorAgent(mock_llm, load_personas_config(), enable_semantic_stage=True)


def test_x_weighted_length_counts_hangul_as_two_and_urls_as_23() -> None:
    assert x_weighted_length("abc 123") == 7
    assert x_weighted_length("가나다") == 6
    assert x_weighted_length("가 a") == 4
    assert x_weighted_length("공지 https://example.com/very/long/path?x=1") == 2 * 2 + 1 + 23
    # 한글 140자는 통과, 141자는 초과
    assert x_weighted_length("가" * 140) == 280
    assert x_weighted_length("가" * 141) > 280


def test_instagram_hashtags_capped_at_five_and_garbled_tag_flagged() -> None:
    cfg = load_personas_config()
    too_many = InstagramPostSpec(
        hook_headline="입고 안내", body="판매 일정 : 11월 17일", hashtags=[f"#태그{i}" for i in range(6)]
    )
    rules = {f.rule_violated for f in validate_single_channel_deterministic("instagram", too_many, cfg)}
    assert "hashtag_count" in rules

    garbled = InstagramPostSpec(
        hook_headline="입고 안내", body="판매 일정 : 11월 17일", hashtags=["#리프트바운드", "#리프트바ウンド"]
    )
    rules = {f.rule_violated for f in validate_single_channel_deterministic("instagram", garbled, cfg)}
    assert "garbled_hashtag" in rules

    no_tags = InstagramPostSpec(hook_headline="입고 안내", body="판매 일정 : 11월 17일", hashtags=[])
    assert validate_single_channel_deterministic("instagram", no_tags, cfg) == []


def test_copy_prompt_carries_confirmed_facts_today_and_approved_examples() -> None:
    facts = [
        NoticeFact(label="판매 일시", value="11월 17일(화) 매장 오픈부터", source="user_prompt"),
        NoticeFact(label="가격", value="10만원", source="user_prompt"),
    ]
    prompt, _ = build_gemini_copywriter_request(
        personas_yaml_raw=load_personas_raw(),
        vision_result=_vision(facts),
        tag_set=MultilingualTagSet(),
        user_prompt="[판매 일시] 내일 오픈부터",
        enabled_channels=["instagram"],
        approved_examples={"instagram": ["지난주 승인된 공지 본문"]},
    )
    assert "<CONFIRMED_FACTS>" in prompt
    assert "- 판매 일시: 11월 17일(화) 매장 오픈부터" in prompt
    assert "오늘 날짜:" in prompt
    assert "지난주 승인된 공지 본문" in prompt


async def test_self_correction_retries_only_the_failing_channel() -> None:
    first = SocialPostOutput(
        enabled_channels=["x_twitter", "dcinside"],
        x_twitter=_long_x(),
        dcinside=_good_dc(),
        generation_reasoning="1차",
    )
    second = SocialPostOutput(
        enabled_channels=["x_twitter"], x_twitter=_good_x(), generation_reasoning="2차"
    )
    agent = _FakeCopyAgent([first, second])

    draft, report, attempts = await _validator().run_with_self_correction(
        copy_agent=agent,  # type: ignore[arg-type]
        vision_result=_vision(),
        tag_set=MultilingualTagSet(),
        user_prompt="입고 공지",
        enabled_channels=["x_twitter", "dcinside"],
    )

    assert agent.requested == [["x_twitter", "dcinside"], ["x_twitter"]]
    assert [f.rule_violated for f in agent.feedbacks[1]] == ["strict_char_limit"]
    assert attempts == 2
    assert report.is_valid is True
    assert draft.x_twitter is not None and draft.x_twitter.body == _good_x().body
    assert draft.dcinside is not None  # 1차에 통과한 디시 원고는 그대로 유지


async def test_schema_error_falls_back_to_per_channel_generation() -> None:
    """합쳐서 생성한 응답이 스키마 오류로 통째로 실패해도, 채널별로 나눠 살릴 수 있는 채널은 살린다."""
    dc_only = SocialPostOutput(
        enabled_channels=["dcinside"], dcinside=_good_dc(), generation_reasoning="디시 단독"
    )
    x_only = SocialPostOutput(
        enabled_channels=["x_twitter"], x_twitter=_good_x(), generation_reasoning="X 단독"
    )
    agent = _FakeCopyAgent(
        [
            ValueError("joint response failed schema validation"),
            LLMGenerationError("x_twitter empty response"),  # 분할 재시도: x 실패
            dc_only,  # 분할 재시도: dc 성공
            x_only,  # 2회차: x만 재시도
        ]
    )

    draft, report, attempts = await _validator().run_with_self_correction(
        copy_agent=agent,  # type: ignore[arg-type]
        vision_result=_vision(),
        tag_set=MultilingualTagSet(),
        user_prompt="입고 공지",
        enabled_channels=["x_twitter", "dcinside"],
    )

    assert agent.requested == [["x_twitter", "dcinside"], ["x_twitter"], ["dcinside"], ["x_twitter"]]
    assert agent.feedbacks[3][0].rule_violated == "schema_validation"
    assert attempts == 2
    assert report.is_valid is True
    assert draft.enabled_channels == ["x_twitter", "dcinside"]


async def test_one_channel_failing_forever_does_not_block_the_other() -> None:
    dc_only = SocialPostOutput(
        enabled_channels=["dcinside"], dcinside=_good_dc(), generation_reasoning="디시 단독"
    )
    agent = _FakeCopyAgent(
        [
            ValueError("joint failed"),
            ValueError("x failed"),
            dc_only,
            ValueError("x failed again"),
            ValueError("x failed a third time"),
        ]
    )
    draft, report, attempts = await _validator().run_with_self_correction(
        copy_agent=agent,  # type: ignore[arg-type]
        vision_result=_vision(),
        tag_set=MultilingualTagSet(),
        user_prompt="입고 공지",
        enabled_channels=["x_twitter", "dcinside"],
    )
    assert attempts == 3
    assert draft.enabled_channels == ["dcinside"]
    assert draft.x_twitter is None
    assert report.is_valid is False
    assert {f.channel for f in report.failures} == {"x_twitter"}


async def test_unresolved_fact_check_findings_become_non_blocking_warnings() -> None:
    out = SocialPostOutput(
        enabled_channels=["dcinside"], dcinside=_good_dc(), generation_reasoning="생성"
    )
    finding = MagicMock()
    finding.score = 40
    finding.failures = [
        ValidationFailureDetail(
            channel="dcinside",
            rule_violated="unsupported_fact",
            reason="'한정 수량'은 입력에 없는 내용입니다.",
            fix_instruction="'한정 수량' 문구를 삭제하십시오.",
        )
    ]
    agent = _FakeCopyAgent([out, out])

    draft, report, attempts = await _validator([finding, finding]).run_with_self_correction(
        copy_agent=agent,  # type: ignore[arg-type]
        vision_result=_vision(),
        tag_set=MultilingualTagSet(),
        user_prompt="입고 공지",
        enabled_channels=["dcinside"],
        max_attempts=2,
    )

    assert attempts == 2
    assert agent.feedbacks[1][0].rule_violated == "unsupported_fact"  # 재시도에 피드백으로 전달
    assert report.is_valid is True  # 규격 위반은 없으므로 승인은 가능
    assert [w.rule_violated for w in report.warnings] == ["unsupported_fact"]
    assert draft.dcinside is not None


async def test_all_channels_failing_raises_generation_error() -> None:
    agent = _FakeCopyAgent([ValueError("bad"), ValueError("bad"), ValueError("bad")])
    with pytest.raises(LLMGenerationError, match="모든 채널"):
        await _validator().run_with_self_correction(
            copy_agent=agent,  # type: ignore[arg-type]
            vision_result=_vision(),
            tag_set=MultilingualTagSet(),
            user_prompt="입고 공지",
            enabled_channels=["dcinside"],
        )


class _StubVision(BaseModel):
    primary_subject: str = "입고 안내"


class _StubSession(BaseModel):
    session_id: str
    created_at: str
    notice_category: str = "tcg_product_release"
    user_prompt: str = "입고"
    enabled_channels: list[str] = ["instagram"]
    vision_result: _StubVision = _StubVision()
    drafts: dict[str, Any] = {}


def test_history_returns_recent_approved_texts_for_same_channel_and_category(tmp_path: Path) -> None:
    repo = CampaignSQLiteRepository(db_path=tmp_path / "hist.db")

    def _session(sid: str, created: str, status: str, body: str, category: str) -> _StubSession:
        return _StubSession(
            session_id=sid,
            created_at=created,
            notice_category=category,
            drafts={
                "instagram": {
                    "status": status,
                    "spec": {"hook_headline": f"헤드라인 {sid}", "body": body, "hashtags": ["#태그"]},
                }
            },
        )

    repo.save_session(_session("a", "2026-11-01T00:00:00", "approved", "오래된 승인본", "tcg_product_release"))
    repo.save_session(_session("b", "2026-11-02T00:00:00", "draft", "미승인 초안", "tcg_product_release"))
    repo.save_session(_session("c", "2026-11-03T00:00:00", "approved", "최신 승인본", "tcg_product_release"))
    repo.save_session(_session("d", "2026-11-04T00:00:00", "approved", "다른 유형", "tcg_tournament"))

    found = repo.list_approved_examples("instagram", "tcg_product_release", limit=2)
    assert found == ["헤드라인 c\n\n최신 승인본", "헤드라인 a\n\n오래된 승인본"]
    assert "#태그" not in found[0]
    assert repo.list_approved_examples("instagram", "tcg_product_release", exclude_session_id="c") == [
        "헤드라인 a\n\n오래된 승인본"
    ]
    assert repo.list_approved_examples("x_twitter", "tcg_product_release") == []


def test_x_body_trailing_hashtags_are_not_duplicated() -> None:
    from src.core.schema import XTwitterPostSpec

    spec = XTwitterPostSpec(
        body="입고 안내\n\n구매 제한: 인당 1개\n\n#리프트바운드 #T1",
        hashtags=["#리프트바운드", "#T1"],
    )
    assert spec.body == "입고 안내\n\n구매 제한: 인당 1개"
    assert spec.full_tweet.count("#T1") == 1

    # 목록에 없는 태그와 문장 속 태그는 보존한다.
    kept = XTwitterPostSpec(body="#T1 번들 입고 #한정", hashtags=["#T1"])
    assert kept.body == "#T1 번들 입고 #한정"


def test_naver_cafe_target_comes_from_env_settings() -> None:
    from types import SimpleNamespace

    from src.core.config_loader import AppSettings, apply_naver_cafe_target

    spec = SimpleNamespace(club_id="12345678", menu_id=42)
    apply_naver_cafe_target(spec, AppSettings(naver_cafe_club_id="12345678", naver_cafe_menu_id=7))
    assert (spec.club_id, spec.menu_id) == ("12345678", 42)  # placeholder -> untouched

    apply_naver_cafe_target(spec, AppSettings(naver_cafe_club_id="555", naver_cafe_menu_id=7))
    assert (spec.club_id, spec.menu_id) == ("555", 7)


def test_naver_cafe_head_tag_is_not_duplicated() -> None:
    from src.core.schema import NaverCafePostSpec
    from src.integrations.naver_cafe_client import NaverCafeClient

    spec = NaverCafePostSpec(
        club_id="1",
        menu_id=1,
        head_tag="[입고공지]",
        subject="[입고공지] 번들 입고 안내",
        html_body="<p>본문</p>",
        plain_body_preview="본문",
        search_tags=["a", "b", "c", "d", "e"],
    )
    assert spec.subject == "번들 입고 안내"

    encoded = NaverCafeClient.build_encoded_fields(
        "[입고공지]", "[입고공지][입고공지] 번들", "<p>x</p>", []
    )
    assert encoded["raw_subject"] == "[입고공지] 번들"
    other = NaverCafeClient.build_encoded_fields("[입고공지]", "[한정] 번들", "<p>x</p>", [])
    assert other["raw_subject"] == "[입고공지] [한정] 번들"


def test_old_history_with_removed_channel_still_loads() -> None:
    from src.web.app import CampaignSession

    session = CampaignSession.model_validate(
        {
            "session_id": "old",
            "image_path": "x.jpg",
            "image_filename": "x.jpg",
            "user_prompt": "공지",
            "enabled_channels": ["x_twitter", "kakao_channel"],
            "selected_style_ids": {"kakao_channel": "p"},
            "vision_result": {
                "primary_subject": "s",
                "visual_mood": [],
                "key_selling_points": ["a", "b"],
                "extracted_text": [],
                "suggested_alt_text": "a",
                "image_meta": {
                    "file_path": "x.jpg",
                    "width": 800,
                    "height": 1000,
                    "aspect_ratio_float": 0.8,
                    "aspect_ratio_label": "4:5",
                    "is_instagram_compatible": True,
                },
            },
            "tag_set": {},
            "generation_reasoning": "r",
            "drafts": {"kakao_channel": {"channel": "kakao_channel"}},
            "dispatch_results": {"kakao_channel": {}},
        }
    )
    assert session.enabled_channels == ["x_twitter"]
    assert not session.drafts and not session.selected_style_ids and not session.dispatch_results
