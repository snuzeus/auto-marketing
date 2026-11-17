from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from src.agents.validator_agent import validate_single_channel_deterministic
from src.core.config_loader import (
    load_app_settings,
    load_personas_config,
    resolve_enabled_channels,
)
from src.core.llm_client import GeminiLLMClient, LLMConfigurationError
from src.core.schema import (
    ChannelDraftState,
    DCInsidePostSpec,
    InstagramPostSpec,
    NaverCafePostSpec,
    SocialPostOutput,
    ValidationFailureDetail,
    ValidationReport,
    XTwitterPostSpec,
)


def test_load_personas_and_resolve_default_channels() -> None:
    cfg = load_personas_config(Path("config/personas.yaml"))
    resolved = resolve_enabled_channels(cfg, None)
    assert resolved == ["instagram", "x_twitter", "dcinside", "naver_cafe"]


def test_resolve_enabled_channels_cli_override_and_dedup() -> None:
    cfg = load_personas_config(Path("config/personas.yaml"))
    resolved = resolve_enabled_channels(cfg, " naver_cafe , instagram , naver_cafe ")
    assert resolved == ["naver_cafe", "instagram"]


def test_resolve_enabled_channels_invalid_raises() -> None:
    cfg = load_personas_config(Path("config/personas.yaml"))
    with pytest.raises(ValueError, match="지원하지 않는 플랫폼"):
        resolve_enabled_channels(cfg, "instagram,tiktok")


def test_resolve_enabled_channels_disabled_in_yaml() -> None:
    cfg = {
        "active_channels": ["instagram", "x_twitter"],
        "channels": {
            "instagram": {"enabled": False},
            "x_twitter": {"enabled": True},
        },
    }
    assert resolve_enabled_channels(cfg) == ["x_twitter"]


def test_x_twitter_char_limit_validation() -> None:
    valid_spec = XTwitterPostSpec(
        body="짧고 강렬한 후킹 문장!",
        hashtags=["#데스크테리어", "#desksetup"],
        total_char_count=32,
    )
    assert valid_spec.weighted_length <= 280
    # 모델이 적어낸 숫자 대신 실제 X 가중치 글자 수로 덮어쓴다 (한글 1자 = 2).
    assert valid_spec.total_char_count == valid_spec.weighted_length

    # 한도 초과는 예외가 아니라 Stage 1 검증 실패로 보고되어 자기교정 피드백에 쓰인다.
    too_long = XTwitterPostSpec(
        body="가" * 150,  # 한글 150자 = 가중치 300
        hashtags=["#태그1", "#태그2"],
        total_char_count=200,
    )
    assert too_long.weighted_length > 280
    failures = validate_single_channel_deterministic(
        "x_twitter", too_long, load_personas_config()
    )
    assert "strict_char_limit" in {f.rule_violated for f in failures}


def test_social_post_output_channel_isolation() -> None:
    insta = InstagramPostSpec(
        hook_headline="감각적인 첫 줄",
        body="본문 내용입니다.",
        hashtags=[f"#태그{i}" for i in range(10)],
    )
    # Valid when only instagram is enabled and populated
    out = SocialPostOutput(
        enabled_channels=["instagram"],
        instagram=insta,
        generation_reasoning="인스타그램 단독 생성",
    )
    assert out.instagram is not None
    assert out.x_twitter is None

    # Invalid if disabled channel is populated
    with pytest.raises(ValidationError, match="비활성화된 채널 'dcinside'"):
        SocialPostOutput(
            enabled_channels=["instagram"],
            instagram=insta,
            dcinside=DCInsidePostSpec(title="제목", body="본문임"),
            generation_reasoning="오류 케이스",
        )

    # Invalid if enabled channel is None
    with pytest.raises(ValidationError, match="활성화된 채널 'naver_cafe'"):
        SocialPostOutput(
            enabled_channels=["instagram", "naver_cafe"],
            instagram=insta,
            naver_cafe=None,
            generation_reasoning="누락 케이스",
        )


def test_channel_draft_state_approval_revoked_on_edit() -> None:
    dc_spec = DCInsidePostSpec(gallery_id="gadget", title="키보드 산 후기", body="타건감 진짜 좋음")
    state = ChannelDraftState(channel="dcinside", spec=dc_spec)
    assert state.status == "draft"
    assert state.revision == 1

    state.approve()
    assert state.status == "approved"

    # Editing a draft must automatically revoke approval
    updated_spec = DCInsidePostSpec(
        gallery_id="gadget", title="키보드 일주일 사용기", body="스테빌 잡으니까 더 좋음"
    )
    state.update_spec(updated_spec)
    assert state.status == "draft"
    assert state.revision == 2
    assert isinstance(state.spec, DCInsidePostSpec)
    assert state.spec.title == "키보드 일주일 사용기"


def test_channel_draft_state_cannot_approve_invalid_draft() -> None:
    cafe_spec = NaverCafePostSpec(
        club_id="123",
        menu_id=1,
        head_tag="[후기]",
        subject="제목",
        html_body="<p>짧은 본문</p>",
        plain_body_preview="짧은 본문",
        search_tags=["태그1", "태그2", "태그3", "태그4", "태그5"],
    )
    invalid_report = ValidationReport(
        is_valid=False,
        failures=[
            ValidationFailureDetail(
                channel="naver_cafe",
                rule_violated="min_body_length",
                reason="본문 300자 미만",
                fix_instruction="300자 이상 작성하세요.",
            )
        ],
    )
    state = ChannelDraftState(
        channel="naver_cafe", spec=cafe_spec, validation=invalid_report
    )
    with pytest.raises(ValueError, match="검증에 실패한 초안"):
        state.approve()


def test_llm_client_rejects_missing_or_placeholder_key() -> None:
    with pytest.raises(LLMConfigurationError):
        GeminiLLMClient(api_key="")

    with pytest.raises(LLMConfigurationError):
        GeminiLLMClient(api_key="AIzaSyYourGoogleGeminiApiKey...")


def test_load_app_settings_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MAIN_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_LITE_MODEL", raising=False)
    empty_env = tmp_path / ".env.empty"
    empty_env.write_text("", encoding="utf-8")

    settings = load_app_settings(dotenv_path=empty_env)
    assert settings.gemini_main_model == "gemini-3.8-flash"
    assert settings.gemini_lite_model == "gemini-3.5-flash-lite"
    assert settings.has_gemini_key is False


class _DummyResponseModel(BaseModel):
    answer: str


async def test_llm_client_generate_structured_with_mock_sdk() -> None:
    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    mock_response.parsed = None
    mock_response.text = '{"answer": "ok"}'
    mock_genai_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    llm = GeminiLLMClient(api_key="AIzaRealTestKey12345", client=mock_genai_client)
    result = await llm.generate_structured(
        model=llm.main_model,
        system_instruction="sys",
        user_content="hello",
        response_model=_DummyResponseModel,
        thinking_level="low",
    )
    assert result.answer == "ok"
    mock_genai_client.aio.models.generate_content.assert_awaited_once()


async def test_llm_client_mcp_tool_loop() -> None:
    mock_genai_client = MagicMock()

    fn_call = MagicMock()
    fn_call.name = "dispatch_dcinside_pack"
    fn_call.args = {"gallery_id": "gadget", "title": "제목", "body": "본문임"}

    first_resp = MagicMock()
    first_resp.function_calls = [fn_call]
    first_resp.candidates = [MagicMock(content=MagicMock())]

    second_resp = MagicMock()
    second_resp.function_calls = []

    mock_genai_client.aio.models.generate_content = AsyncMock(
        side_effect=[first_resp, second_resp]
    )

    llm = GeminiLLMClient(api_key="AIzaRealTestKey12345", client=mock_genai_client)

    async def fake_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"status": "copied", "tool": name, "gallery": args["gallery_id"]}

    calls = await llm.run_mcp_tool_loop(
        model=llm.main_model,
        system_instruction="sys",
        prompt="run tools",
        tools=[],
        tool_executor=fake_executor,
    )
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "dispatch_dcinside_pack"
    assert calls[0]["result"]["status"] == "copied"
