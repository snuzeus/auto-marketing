from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.core.card_renderer import parse_notice_card_structure, render_notice_card_png
from src.core.config_loader import AppSettings
from src.core.schema import (
    CardNewsBoxSpec,
    CardNewsSpec,
    ChannelType,
    DCInsidePostSpec,
    ImageMetadata,
    InstagramPostSpec,
    KakaoChannelPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
    NaverPlacePostSpec,
    SocialPostOutput,
    VisionAnalysisResult,
    XTwitterPostSpec,
)
from src.integrations.dcinside_router import DCInsideIntegrationError, DCInsideRouter
from src.integrations.naver_cafe_client import NaverCafeClient, NaverCafeIntegrationError
from src.integrations.postiz_client import PostizClient, PostizIntegrationError
from src.mcp.tools import MCPToolDispatcher
from src.web.app import CampaignStore, create_app


def _make_jpeg_bytes(width: int = 800, height: int = 1000) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=(40, 90, 140))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_postiz_client_rejects_missing_credentials() -> None:
    client = PostizClient(base_url="http://localhost:3000/api/v1", api_key="")
    with pytest.raises(PostizIntegrationError, match="Postiz API가 설정되지 않았습니다"):
        import asyncio

        asyncio.run(client.upload_media("nonexistent.jpg", "alt"))


async def test_postiz_client_upload_and_create_draft(tmp_path: Path) -> None:
    img_file = tmp_path / "post.jpg"
    img_file.write_bytes(_make_jpeg_bytes())

    posted: list[dict[str, Any]] = []
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("Authorization", ""))
        path = request.url.path
        if path.endswith("/integrations"):
            return httpx.Response(
                200,
                json=[
                    {"id": "int_x", "identifier": "x", "name": "me", "disabled": False},
                    {"id": "int_ig", "identifier": "instagram", "name": "me", "disabled": False},
                ],
            )
        if path.endswith("/upload"):
            return httpx.Response(
                200, json={"id": "media_999", "path": "http://localhost:4007/uploads/post.jpg"}
            )
        if path.endswith("/posts"):
            posted.append(json.loads(request.read().decode("utf-8")))
            return httpx.Response(200, json=[{"postId": "post_123", "integration": "int_ig"}])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        # Channel IDs left empty -> resolved from GET /integrations by identifier.
        postiz = PostizClient(
            base_url="http://localhost:4007/api/public/v1",
            api_key="real_key",
            http_client=http_client,
        )
        media_res = await postiz.upload_media(str(img_file), "대체 텍스트")
        assert media_res["media_id"] == "media_999"
        assert media_res["aspect_ratio"] == "4:5"

        draft_res = await postiz.create_draft_post(
            channel="instagram",
            content="인스타 캡션 본문",
            media_ids=["media_999"],
        )
        assert draft_res["post_id"] == "post_123"
        assert draft_res["status"] == "DRAFT"
        assert draft_res["dashboard_url"] == "http://localhost:4007/launches"

        assert set(auth_headers) == {"real_key"}  # raw key, no Bearer prefix
        body = posted[0]
        assert body["type"] == "draft"
        post = body["posts"][0]
        assert post["integration"] == {"id": "int_ig"}
        assert post["settings"]["__type"] == "instagram"
        assert post["settings"]["post_type"] == "post"
        assert post["value"][0]["content"] == "인스타 캡션 본문"
        assert post["value"][0]["image"][0]["id"] == "media_999"
        assert post["value"][0]["image"][0]["path"].endswith("/uploads/post.jpg")

        x_res = await postiz.create_draft_post(channel="x_twitter", content="x", media_ids=[])
        assert x_res["channel"] == "x_twitter"
        assert posted[1]["posts"][0]["integration"] == {"id": "int_x"}
        assert posted[1]["posts"][0]["settings"]["__type"] == "x"


async def test_postiz_client_errors_when_channel_not_connected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        postiz = PostizClient(
            base_url="http://localhost:4007/api/public/v1",
            api_key="real_key",
            http_client=http_client,
        )
        with pytest.raises(PostizIntegrationError, match="채널이 없습니다"):
            await postiz.create_draft_post(channel="x_twitter", content="x", media_ids=[])


async def test_naver_cafe_oauth_refresh_on_401(tmp_path: Path) -> None:
    img_file = tmp_path / "cafe.jpg"
    img_file.write_bytes(_make_jpeg_bytes())

    call_count = {"articles": 0, "token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2.0/token" in str(request.url):
            call_count["token"] += 1
            return httpx.Response(200, json={"access_token": "refreshed_token_abc"})
        if "/articles" in request.url.path:
            call_count["articles"] += 1
            auth = request.headers.get("Authorization", "")
            if auth == "Bearer expired_token":
                return httpx.Response(401, json={"errorMessage": "Unauthorized"})
            return httpx.Response(
                200,
                json={
                    "message": {
                        "result": {
                            "articleId": 777,
                            "articleUrl": "https://cafe.naver.com/test/777",
                        }
                    }
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        cafe = NaverCafeClient(
            client_id="cid",
            client_secret="csecret",
            access_token="expired_token",
            refresh_token="valid_refresh_token",
            enable_local_clipboard=False,
            http_client=http_client,
        )
        res = await cafe.publish_article(
            club_id="12345",
            menu_id=10,
            head_tag="[후기]",
            subject="키보드 사용기",
            html_body="<p>" + ("본문 내용입니다. " * 35) + "</p>",
            search_tags=["키보드", "데스크테리어", "기계식키보드", "추천", "후기"],
            image_path=str(img_file),
            approval_mode="direct_api",
        )
        assert res["article_id"] == 777
        assert call_count["token"] == 1
        assert call_count["articles"] == 2


async def test_dcinside_router_fails_honestly_when_both_channels_unavailable(
    tmp_path: Path,
) -> None:
    img_file = tmp_path / "dc.jpg"
    img_file.write_bytes(_make_jpeg_bytes())

    router = DCInsideRouter(
        telegram_bot_token="",
        telegram_chat_id="",
        enable_local_clipboard=False,
    )
    with pytest.raises(DCInsideIntegrationError, match="디시인사이드 원고 전달 실패"):
        await router.dispatch_pack(
            gallery_id="gadget",
            title="제목임",
            body="본문임",
            image_path=str(img_file),
        )


async def test_mcp_dispatcher_blocks_disabled_channels() -> None:
    settings = AppSettings(gemini_api_key="AIzaRealKey")
    dispatcher = MCPToolDispatcher(
        settings=settings,
        enabled_channels=["instagram"],
    )
    with pytest.raises(PermissionError, match="비활성화된 채널"):
        await dispatcher.execute_tool(
            "dispatch_dcinside_pack",
            {
                "gallery_id": "gadget",
                "title": "제목",
                "body": "본문임",
                "image_path": "test.jpg",
            },
        )


def test_web_api_campaign_lifecycle_edit_revokes_approval_and_blocks_fake_dispatch(
    tmp_path: Path,
) -> None:
    """
    Verify all user principles on the FastAPI server:
    1. Server-side image analysis + channel draft generation
    2. Independent edit, regeneration, approval per channel
    3. Editing an approved draft automatically revokes approval ('approved' -> 'draft')
    4. Dispatching without real external credentials fails honestly (503) instead of faking success
    """
    settings = AppSettings(
        gemini_api_key="AIzaTestServerKey",
        postiz_api_key="",  # Not configured -> must fail honestly on dispatch
        naver_access_token="",  # Not configured
        enable_local_clipboard=False,
    )

    def fake_llm_factory() -> Any:
        mock_llm = MagicMock()
        mock_llm.main_model = "gemini-2.5-flash"
        mock_llm.lite_model = "gemini-2.5-flash-lite"
        mock_llm.create_or_get_persona_cache = AsyncMock(return_value=None)
        mock_llm.analyze_image_structured = AsyncMock(
            return_value=MagicMock(
                primary_subject="실버 커스텀 키보드",
                visual_mood=["모던", "깔끔한"],
                key_selling_points=["풀알루미늄", "무선 연결"],
                suggested_alt_text="실버 키보드 사진",
            )
        )

        async def fake_gen(**kwargs: Any) -> Any:
            model_cls = kwargs["response_model"]
            if model_cls is MultilingualTagSet:
                return MultilingualTagSet(
                    ko_tags=["#키보드", "#데스크테리어"],
                    en_tags=["#desksetup"],
                    ja_tags=["#ガジェット"],
                    instagram_combined=[f"#태그{i}" for i in range(10)],
                    x_combined=["#키보드", "#desksetup"],
                    naver_cafe_search_tags=["키보드1", "키보드2", "키보드3", "키보드4", "키보드5"],
                )
            if model_cls is SocialPostOutput:
                user_content: str = kwargs["user_content"]
                # Check if regenerating single channel x_twitter or initial [x_twitter, dcinside]
                if "[x_twitter]" in user_content and "dcinside" not in user_content.split("</TASK>")[0]:
                    return SocialPostOutput(
                        enabled_channels=["x_twitter"],
                        x_twitter=XTwitterPostSpec(
                            body="새로 생성된 트윗 본문! 타건감 대박",
                            hashtags=["#키보드", "#desksetup"],
                            total_char_count=38,
                        ),
                        generation_reasoning="X 단독 재생성",
                    )
                return SocialPostOutput(
                    enabled_channels=["x_twitter", "dcinside"],
                    x_twitter=XTwitterPostSpec(
                        body="초기 트윗 문구입니다! 알루미늄 하우징의 묵직함",
                        hashtags=["#키보드", "#desksetup"],
                        total_char_count=44,
                    ),
                    dcinside=DCInsidePostSpec(
                        gallery_id="gadget",
                        title="알루미늄 키보드 산 후기",
                        body="타건감 진짜 도각거리고 묵직함 ㄹㅇ 실물깡패임",
                    ),
                    generation_reasoning="초기 생성",
                )
            # Semantic score
            s = MagicMock()
            s.score = 95
            s.failures = []
            return s

        mock_llm.generate_structured = AsyncMock(side_effect=fake_gen)
        return mock_llm

    # 실제 config/campaigns_history.db 와 .runtime_uploads 를 오염시키지 않도록 임시 경로를 쓴다.
    store = CampaignStore(
        settings=settings,
        style_profiles_path=tmp_path / "styles.json",
        db_path=tmp_path / "hist.db",
        llm_client_factory=fake_llm_factory,
        upload_dir=tmp_path / "uploads",
    )
    app = create_app(store=store)
    client = TestClient(app)

    # 1. Create campaign with image + prompt + channels
    files = {"image": ("keyboard.jpg", _make_jpeg_bytes(), "image/jpeg")}
    data = {"prompt": "커스텀 키보드 후기", "channels": "x_twitter,dcinside"}
    res = client.post("/api/campaigns", files=files, data=data)
    assert res.status_code == 200
    session_data = res.json()
    session_id = session_data["session_id"]
    assert session_data["enabled_channels"] == ["x_twitter", "dcinside"]
    assert session_data["drafts"]["x_twitter"]["status"] == "draft"
    assert session_data["drafts"]["dcinside"]["status"] == "draft"

    # 2. Approve x_twitter draft
    res_app = client.post(f"/api/campaigns/{session_id}/channels/x_twitter/approve")
    assert res_app.status_code == 200
    assert res_app.json()["status"] == "approved"

    # 3. Edit approved x_twitter draft -> MUST automatically revoke approval back to "draft"
    res_edit = client.put(
        f"/api/campaigns/{session_id}/channels/x_twitter",
        json={
            "body": "수정한 트윗 문구입니다!",
            "hashtags": ["#키보드", "#데스크테리어"],
        },
    )
    assert res_edit.status_code == 200
    edited_state = res_edit.json()
    assert edited_state["status"] == "draft"
    assert edited_state["revision"] == 2
    assert edited_state["spec"]["body"] == "수정한 트윗 문구입니다!"

    # 4. Dispatching an unapproved draft must fail with 400
    res_disp_unapproved = client.post(
        f"/api/campaigns/{session_id}/channels/x_twitter/dispatch", json={}
    )
    assert res_disp_unapproved.status_code == 400

    # 5. Re-approve x_twitter, then attempt dispatch without Postiz API key -> must fail honestly with 503
    client.post(f"/api/campaigns/{session_id}/channels/x_twitter/approve")
    res_disp_no_key = client.post(
        f"/api/campaigns/{session_id}/channels/x_twitter/dispatch", json={}
    )
    assert res_disp_no_key.status_code == 503
    assert "Postiz API가 설정되지 않았습니다" in res_disp_no_key.json()["detail"]

    # 6. Independently regenerate x_twitter while dcinside remains untouched
    client.post(f"/api/campaigns/{session_id}/channels/dcinside/approve")
    res_regen = client.post(
        f"/api/campaigns/{session_id}/channels/x_twitter/regenerate",
        json={"feedback_note": "더 짧고 강렬하게"},
    )
    assert res_regen.status_code == 200
    regen_state = res_regen.json()
    assert regen_state["status"] == "draft"
    assert "새로 생성된 트윗 본문" in regen_state["spec"]["body"]

    # Confirm dcinside approval was NOT affected by x_twitter regeneration
    session_now = client.get(f"/api/campaigns/{session_id}").json()
    assert session_now["drafts"]["dcinside"]["status"] == "approved"

    # Confirm unused imports for schema completeness
    assert InstagramPostSpec is not None
    assert NaverCafePostSpec is not None
    assert VisionAnalysisResult is not None
    assert ImageMetadata is not None
    assert NaverCafeIntegrationError is not None
    assert ChannelType is not None


def test_account_style_learning_and_injection_in_campaign(tmp_path: Path) -> None:
    """Verify learning an Instagram/X account's notice style and applying it to notice generation."""
    settings = AppSettings(gemini_api_key="AIzaTestServerKey")
    style_file = tmp_path / "style_profiles.json"

    captured_prompts: list[str] = []

    def fake_llm_factory() -> Any:
        mock_llm = MagicMock()
        mock_llm.main_model = "gemini-3.5-flash-lite"
        mock_llm.lite_model = "gemini-3.5-flash-lite"
        mock_llm.create_or_get_persona_cache = AsyncMock(return_value=None)
        mock_llm.analyze_image_structured = AsyncMock(
            return_value=MagicMock(
                primary_subject="11월 18일 매장 임시 휴무 공지 포스터",
                visual_mood=["단정한", "공식 안내"],
                key_selling_points=["11월 18일 임시 휴무", "11월 19일 정상 영업"],
                extracted_text=["임시 휴무 안내", "2026.11.18(월)", "내부 설비 점검"],
                suggested_alt_text="11월 18일 임시 휴무 안내문 이미지",
            )
        )

        async def fake_gen(**kwargs: Any) -> Any:
            model_cls = kwargs["response_model"]
            if model_cls.__name__ == "_LLMExtractedStyleSpec":
                m = MagicMock()
                m.tone_summary = "정중하고 차분한 공식 공지 톤 (~안내드립니다)"
                m.opening_pattern = "안녕하세요, 성수 스튜디오입니다. [공지]"
                m.bullet_and_formatting_rules = ["항목 앞에 📌 기호 사용", "문단 사이 빈 줄 1개"]
                m.emoji_policy = "공지 제목에만 📢/📌 제한적 사용"
                m.closing_signature = "감사합니다. 성수 스튜디오 드림."
                return m
            if model_cls is MultilingualTagSet:
                return MultilingualTagSet(
                    ko_tags=["#공지사항", "#휴무안내"],
                    en_tags=["#notice"],
                    ja_tags=["#お知らせ"],
                    instagram_combined=[f"#공지태그{i}" for i in range(10)],
                    x_combined=["#공지사항", "#휴무안내"],
                    naver_cafe_search_tags=["공지", "휴무안내", "일정안내", "매장소식", "영업시간"],
                )
            if model_cls is SocialPostOutput:
                captured_prompts.append(kwargs["user_content"])
                return SocialPostOutput(
                    enabled_channels=["instagram"],
                    instagram=InstagramPostSpec(
                        hook_headline="📢 [공지] 11월 18일(월) 매장 임시 휴무 안내",
                        body=(
                            "안녕하세요, 성수 스튜디오입니다.\n\n"
                            "📌 휴무 일시: 11월 18일(월)\n"
                            "📌 사유: 내부 설비 점검\n\n"
                            "감사합니다. 성수 스튜디오 드림."
                        ),
                        hashtags=[f"#공지태그{i}" for i in range(5)],
                    ),
                    generation_reasoning="성수 스튜디오 계정 스타일 적용 완료",
                )
            s = MagicMock()
            s.score = 96
            s.failures = []
            return s

        mock_llm.generate_structured = AsyncMock(side_effect=fake_gen)
        return mock_llm

    store = CampaignStore(
        settings=settings,
        style_profiles_path=style_file,
        db_path=tmp_path / "test_history.db",
        llm_client_factory=fake_llm_factory,
        upload_dir=tmp_path / "uploads",
    )
    app = create_app(store=store)
    client = TestClient(app)

    # 1. Learn a new Instagram account style
    res_learn = client.post(
        "/api/style-profiles/learn",
        json={
            "platform": "instagram",
            "account_handle": "@seongsu_studio",
            "sample_posts": [
                "📢 [공지] 추석 연휴 휴무 안내\n안녕하세요, 성수 스튜디오입니다.\n📌 일시: 9월 16일~18일\n감사합니다. 성수 스튜디오 드림."
            ],
        },
    )
    assert res_learn.status_code == 200
    prof_data = res_learn.json()
    profile_id = prof_data["profile_id"]
    assert prof_data["account_handle"] == "@seongsu_studio"

    # 2. List learned profiles
    res_list = client.get("/api/style-profiles?platform=instagram")
    assert res_list.status_code == 200
    assert len(res_list.json()) == 1

    # 3. Create notice campaign applying the learned style profile
    files = {"image": ("notice.jpg", _make_jpeg_bytes(), "image/jpeg")}
    data = {
        "prompt": "11월 18일 내부 설비 점검 임시 휴무",
        "channels": "instagram",
        "notice_category": "schedule_change",
        "style_profile_map": f'{{"instagram": "{profile_id}"}}',
    }
    res_camp = client.post("/api/campaigns", files=files, data=data)
    assert res_camp.status_code == 200
    camp_json = res_camp.json()
    assert camp_json["notice_category"] == "schedule_change"
    assert camp_json["selected_style_ids"]["instagram"] == profile_id
    assert "2026.11.18(월)" in camp_json["vision_result"]["extracted_text"]
    assert len(captured_prompts) == 1
    assert "@seongsu_studio" in captured_prompts[0]
    assert "ACCOUNT_CUSTOM_STYLE_PROFILES" in captured_prompts[0]

    # 4. Verify PNG notice card rendering endpoint
    session_id = camp_json["session_id"]
    res_png = client.get(f"/api/campaigns/{session_id}/channels/instagram/card-image")
    assert res_png.status_code == 200
    assert res_png.headers["content-type"] == "image/png"
    assert res_png.content.startswith(b"\x89PNG")

    # 5. Verify SQLite history persistence & listing
    res_hist = client.get("/api/history")
    assert res_hist.status_code == 200
    hist_items = res_hist.json()
    assert len(hist_items) == 1
    assert hist_items[0]["session_id"] == session_id
    assert hist_items[0]["primary_subject"] == "11월 18일 매장 임시 휴무 공지 포스터"

    # Confirm new channel models
    assert KakaoChannelPostSpec is not None
    assert NaverPlacePostSpec is not None


def test_card_renderer_structured_boxes_and_emoji_stripping() -> None:
    """Verify HUD structure parsing, 2-column splitting, emoji sanitization, and 4:5 PNG size."""
    body = (
        "안녕하세요, 카드샵 아레나입니다.\n"
        "📍 판매 장소 : 롯데월드몰 아트리움 / 트렌드팟 바이 성수\n"
        "📅 판매 일정 : 10월 29일 오픈\n"
        "[구매 제한] 1인 1개\n"
        "* 본 상품은 매장 재고 소진 시 조기 마감될 수 있습니다."
    )
    boxes, footnotes = parse_notice_card_structure(
        body=body,
        notice_category="tcg_product_release",
    )
    assert len(boxes) == 3
    assert boxes[0].label == "판매 장소"
    assert boxes[0].columns == ["롯데월드몰 아트리움", "트렌드팟 바이 성수"]
    assert boxes[1].label == "판매 일정"
    assert boxes[1].value == "10월 29일 오픈"
    assert boxes[2].label == "구매 제한"
    assert boxes[2].value == "1인 1개"
    assert len(footnotes) == 1
    assert "조기 마감" in footnotes[0]

    png_bytes = render_notice_card_png(
        headline="📢 [신팩] 2026 챔피언십 세트 발매 안내",
        body=body,
        notice_category="tcg_product_release",
        brand_label="@arena_tcg",
    )
    assert png_bytes.startswith(b"\x89PNG")
    with Image.open(io.BytesIO(png_bytes)) as img:
        assert img.size == (1080, 1350)
        assert img.mode == "RGB"


def test_web_dispatch_instagram_uploads_original_and_notice_card_to_postiz(
    tmp_path: Path,
) -> None:
    """
    Verify approving and dispatching Instagram uploads BOTH images in order:
    1) Slide 1: User's uploaded photo
    2) Slide 2: Generated 4:5 notice card PNG
    and forwards `[original_id, card_id]` to Postiz `create_draft_post`.
    """
    settings = AppSettings(
        gemini_api_key="AIzaTestServerKey",
        postiz_base_url="http://localhost:4007/api/public/v1",
        postiz_api_key="real_key",
        postiz_instagram_channel_id="ch_insta_real_01",
    )

    recorded_uploads: list[tuple[str, bytes]] = []
    recorded_posts: list[dict[str, Any]] = []

    def postiz_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/integrations"):
            return httpx.Response(
                200, json=[{"id": "ch_insta_real_01", "identifier": "instagram", "name": "me"}]
            )
        if request.url.path.endswith("/upload"):
            idx = len(recorded_uploads) + 1
            raw_body = request.read()
            recorded_uploads.append((str(request.headers.get("content-type", "")), raw_body))
            return httpx.Response(
                200,
                json={"id": f"media_slide_{idx}", "path": f"http://localhost:3000/m/{idx}.png"},
            )
        if request.url.path.endswith("/posts"):
            payload = json.loads(request.read().decode("utf-8"))
            recorded_posts.append(payload)
            return httpx.Response(200, json=[{"postId": "post_carousel_01", "integration": "x"}])
        return httpx.Response(404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(postiz_handler))
    postiz_client = PostizClient(
        base_url=settings.postiz_base_url,
        api_key=settings.postiz_api_key,
        instagram_channel_id=settings.postiz_instagram_channel_id,
        http_client=http_client,
    )

    def fake_dispatcher_factory(enabled_channels: list[ChannelType]) -> MCPToolDispatcher:
        return MCPToolDispatcher(
            settings=settings,
            enabled_channels=enabled_channels,
            postiz_client=postiz_client,
        )

    def fake_llm_factory() -> Any:
        mock_llm = MagicMock()
        mock_llm.main_model = "gemini-3.5-flash-lite"
        mock_llm.lite_model = "gemini-3.5-flash-lite"
        mock_llm.create_or_get_persona_cache = AsyncMock(return_value=None)
        mock_llm.analyze_image_structured = AsyncMock(
            return_value=MagicMock(
                primary_subject="원피스카드 OP-11 신팩 입고",
                visual_mood=["공식 안내", "명료한"],
                key_selling_points=["11월 20일 판매", "1인 1박스 제한"],
                extracted_text=["OP-11", "11월 20일 오픈"],
                suggested_alt_text="원피스카드 OP-11 포스터",
            )
        )

        async def fake_gen(**kwargs: Any) -> Any:
            model_cls = kwargs["response_model"]
            if model_cls is MultilingualTagSet:
                return MultilingualTagSet(
                    ko_tags=["#원피스카드", "#신팩입고"],
                    en_tags=["#onepiececardgame"],
                    ja_tags=["#ワンピカード"],
                    instagram_combined=[f"#카드태그{i}" for i in range(10)],
                    x_combined=["#원피스카드", "#입고공지"],
                    naver_cafe_search_tags=["원피스카드", "신팩", "TCG", "카드샵", "부스터팩"],
                )
            if model_cls is SocialPostOutput:
                return SocialPostOutput(
                    enabled_channels=["instagram"],
                    instagram=InstagramPostSpec(
                        hook_headline="OP-11 신팩 발매 안내",
                        body=(
                            "판매 장소 : 강남 본점 / 홍대 지점\n"
                            "판매 일정 : 11월 20일 오픈\n"
                            "구매 제한 : 1인 1박스\n"
                            "* 재고 소진 시 조기 종료됩니다."
                        ),
                        hashtags=[f"#카드태그{i}" for i in range(5)],
                    ),
                    generation_reasoning="신상품 공지 생성 완료",
                )
            s = MagicMock()
            s.score = 98
            s.failures = []
            return s

        mock_llm.generate_structured = AsyncMock(side_effect=fake_gen)
        return mock_llm

    store = CampaignStore(
        settings=settings,
        style_profiles_path=tmp_path / "styles.json",
        db_path=tmp_path / "hist.db",
        llm_client_factory=fake_llm_factory,
        dispatcher_factory=fake_dispatcher_factory,
        upload_dir=tmp_path / "uploads",
    )
    app = create_app(store=store)
    client = TestClient(app)

    session_id = ""
    try:
        res = client.post(
            "/api/campaigns",
            files={"image": ("op11_poster.jpg", _make_jpeg_bytes(), "image/jpeg")},
            data={
                "prompt": "[종목] 원피스카드\n[판매 일정] 11월 20일 오픈\n[구매 제한] 1인 1박스",
                "channels": "instagram",
                "notice_category": "tcg_product_release",
            },
        )
        assert res.status_code == 200
        session_id = res.json()["session_id"]

        # Verify inline preview header vs attachment header
        res_inline = client.get(
            f"/api/campaigns/{session_id}/channels/instagram/card-image?inline=1"
        )
        assert res_inline.status_code == 200
        assert res_inline.headers["content-disposition"].startswith("inline;")

        # Approve and dispatch
        res_app = client.post(f"/api/campaigns/{session_id}/channels/instagram/approve")
        assert res_app.status_code == 200

        res_disp = client.post(
            f"/api/campaigns/{session_id}/channels/instagram/dispatch", json={}
        )
        assert res_disp.status_code == 200

        # Confirm 2 media uploads (slide 1 = original jpg, slide 2 = generated PNG)
        assert len(recorded_uploads) == 2
        assert b"notice_card.png" in recorded_uploads[1][1]
        assert b"\x89PNG" in recorded_uploads[1][1]

        # Confirm create_draft_post attached BOTH slides in exact order
        assert len(recorded_posts) == 1
        assert recorded_posts[0]["type"] == "draft"
        images = recorded_posts[0]["posts"][0]["value"][0]["image"]
        assert [img["id"] for img in images] == ["media_slide_1", "media_slide_2"]
    finally:
        if session_id:
            for artifact in Path(".runtime_uploads").glob(f"{session_id}*"):
                artifact.unlink(missing_ok=True)


def test_card_news_prompt_and_direct_edit_endpoints(tmp_path: Path) -> None:
    """Verify AI prompt modification and direct editing of `CardNewsSpec` + PNG rendering."""
    settings = AppSettings(gemini_api_key="AIzaTestServerKey")

    def fake_llm_factory() -> Any:
        mock_llm = MagicMock()
        mock_llm.main_model = "gemini-3.5-flash-lite"
        mock_llm.lite_model = "gemini-3.5-flash-lite"
        mock_llm.create_or_get_persona_cache = AsyncMock(return_value=None)
        mock_llm.analyze_image_structured = AsyncMock(
            return_value=MagicMock(
                primary_subject="리프트바운드 T1 플레이어 번들",
                visual_mood=["간결한", "공식"],
                key_selling_points=["11월 17일 판매", "인당 1개"],
                extracted_text=["T1 PLAYER BUNDLE"],
                suggested_alt_text="T1 플레이어 번들 이미지",
            )
        )

        async def fake_gen(**kwargs: Any) -> Any:
            model_cls = kwargs["response_model"]
            if model_cls is MultilingualTagSet:
                return MultilingualTagSet(
                    ko_tags=["#리프트바운드", "#T1번들"],
                    en_tags=["#riftbound"],
                    ja_tags=["#リフトバウンド"],
                    instagram_combined=[f"#카드태그{i}" for i in range(10)],
                    x_combined=["#리프트바운드", "#T1"],
                    naver_cafe_search_tags=["리프트바운드", "T1", "카드샵", "번들", "신상품"],
                )
            if model_cls is SocialPostOutput:
                return SocialPostOutput(
                    enabled_channels=["instagram"],
                    instagram=InstagramPostSpec(
                        hook_headline="리프트바운드 T1 번들 입고",
                        body=(
                            "판매 일시 : 11월 17일 오픈입니다.\n"
                            "구매 제한 : 인당 1개\n"
                            "판매 가격 : 10만원입니다"
                        ),
                        hashtags=[f"#카드태그{i}" for i in range(5)],
                    ),
                    generation_reasoning="초기 생성",
                )
            if model_cls is CardNewsSpec:
                return CardNewsSpec(
                    main_title="신상품 판매 안내",
                    subtitle_banner="T1 플레이어 번들",
                    boxes=[
                        CardNewsBoxSpec(label="일시 / 장소", value="11월 17일 오픈 / 신촌 리얼카드샵"),
                        CardNewsBoxSpec(label="가격 / 제한", value="10만원 / 1인 1개"),
                    ],
                    footnotes=["* 선착순 소진 시 마감"],
                    footer_brand="@real_cardshop",
                    theme="crimson_red",
                )
            s = MagicMock()
            s.score = 98
            s.failures = []
            return s

        mock_llm.generate_structured = AsyncMock(side_effect=fake_gen)
        return mock_llm

    store = CampaignStore(
        settings=settings,
        style_profiles_path=tmp_path / "styles.json",
        db_path=tmp_path / "hist.db",
        llm_client_factory=fake_llm_factory,
        upload_dir=tmp_path / "uploads",
    )
    app = create_app(store=store)
    client = TestClient(app)

    session_id = ""
    try:
        res = client.post(
            "/api/campaigns",
            files={"image": ("t1.jpg", _make_jpeg_bytes(), "image/jpeg")},
            data={
                "prompt": "[종목] 리프트바운드\n[입고 상품] T1 플레이어 번들\n[판매 일시] 11월 17일 오픈\n[구매 제한] 인당 1개\n[판매 가격] 10만원",
                "channels": "instagram",
                "notice_category": "tcg_product_release",
            },
        )
        assert res.status_code == 200
        data = res.json()
        session_id = data["session_id"]

        # Verify default CardNewsSpec stripped verbose endings like '~입니다'
        initial_card = data["drafts"]["instagram"]["card_news"]
        assert initial_card is not None
        assert any(b["value"] == "11월 17일 오픈" for b in initial_card["boxes"])
        assert any(b["value"] == "10만원" for b in initial_card["boxes"])

        # 1. Modify card news via AI Prompt
        res_prompt = client.post(
            f"/api/campaigns/{session_id}/channels/instagram/card-news/prompt",
            json={"prompt": "텍스트 적게 단순화하고 레드 테마로 변경"},
        )
        assert res_prompt.status_code == 200
        prompt_state = res_prompt.json()
        assert prompt_state["revision"] == 2
        assert prompt_state["card_news"]["theme"] == "crimson_red"
        assert len(prompt_state["card_news"]["boxes"]) == 2

        # Render PNG after prompt edit
        res_png_red = client.get(f"/api/campaigns/{session_id}/channels/instagram/card-image")
        assert res_png_red.status_code == 200
        with Image.open(io.BytesIO(res_png_red.content)) as img_red:
            assert img_red.size == (1080, 1350)

        # 2. Direct update via PUT with minimal_light theme
        res_put = client.put(
            f"/api/campaigns/{session_id}/channels/instagram/card-news",
            json={
                "main_title": "T1 번들 판매",
                "subtitle_banner": "신촌 리얼카드샵",
                "boxes": [
                    {"label": "판매 일시", "value": "11월 17일 오픈"},
                    {"label": "구매 조건", "value": "10만원 / 1인 1개"},
                ],
                "footnotes": [],
                "footer_brand": "신촌 리얼카드샵",
                "theme": "minimal_light",
            },
        )
        assert res_put.status_code == 200
        put_state = res_put.json()
        assert put_state["revision"] == 3
        assert put_state["card_news"]["theme"] == "minimal_light"

        res_png_light = client.get(f"/api/campaigns/{session_id}/channels/instagram/card-image")
        assert res_png_light.status_code == 200
        with Image.open(io.BytesIO(res_png_light.content)) as img_light:
            assert img_light.size == (1080, 1350)
    finally:
        if session_id:
            for artifact in Path(".runtime_uploads").glob(f"{session_id}*"):
                artifact.unlink(missing_ok=True)


async def test_dcinside_telegram_caption_escapes_html(tmp_path: Path) -> None:
    """Markdown-breaking characters in the body must not break the Telegram caption."""
    from src.integrations.dcinside_router import DCInsideRouter

    img_file = tmp_path / "dc.jpg"
    img_file.write_bytes(_make_jpeg_bytes())
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = request.read().decode("utf-8", errors="ignore")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        router = DCInsideRouter(
            telegram_bot_token="111:real",
            telegram_chat_id="42",
            enable_local_clipboard=False,
            http_client=http_client,
        )
        res = await router.dispatch_pack("gadget", "제목_*`", "본문 <b> & `code`", str(img_file))

    assert res["telegram_sent"] is True
    assert "parse_mode" in sent["body"] and "HTML" in sent["body"]
    assert "&lt;b&gt; &amp;" in sent["body"]


async def test_naver_refresh_persists_tokens_via_callback() -> None:
    saved: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new_acc", "refresh_token": "new_ref"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        from src.integrations.naver_cafe_client import NaverCafeClient

        client = NaverCafeClient(
            client_id="id",
            client_secret="secret",
            access_token="old",
            refresh_token="old_ref",
            http_client=http_client,
            on_token_refresh=lambda a, r: saved.append((a, r)),
        )
        assert await client.refresh_access_token() == "new_acc"

    assert saved == [("new_acc", "new_ref")]


def test_update_env_file_preserves_other_lines(tmp_path: Path) -> None:
    from src.core.env_file import update_env_file

    env = tmp_path / ".env"
    env.write_text('A="1"\n# NAVER_ACCESS_TOKEN="c"\nNAVER_ACCESS_TOKEN="old"\n', encoding="utf-8")
    update_env_file(env, {"NAVER_ACCESS_TOKEN": "new", "NAVER_REFRESH_TOKEN": "r"})
    text = env.read_text(encoding="utf-8")
    assert 'A="1"' in text and '# NAVER_ACCESS_TOKEN="c"' in text
    assert 'NAVER_ACCESS_TOKEN="new"' in text and 'NAVER_REFRESH_TOKEN="r"' in text


def _approval_handler(decision: str | None, chat_id: int = 42, calls: list[str] | None = None):
    """Mock Telegram + Naver. `decision`: 'approve' | 'reject' | None (never clicks)."""
    state: dict[str, Any] = {"token": None, "naver_posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append(path.rsplit("/", 1)[-1])
        if path.endswith("/sendMessage"):
            body = json.loads(request.read().decode("utf-8"))
            state["token"] = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        if path.endswith("/getUpdates"):
            if decision is None:
                return httpx.Response(200, json={"ok": True, "result": []})
            data = f"naver_{decision}:{state['token']}"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "callback_query": {
                                "id": "cb1",
                                "data": data,
                                "message": {"chat": {"id": chat_id}},
                            },
                        }
                    ],
                },
            )
        if path.endswith("/articles"):
            state["naver_posts"] += 1
            return httpx.Response(
                200, json={"message": {"result": {"articleId": 9, "articleUrl": "http://x/9"}}}
            )
        return httpx.Response(200, json={"ok": True})

    return handler, state


async def _run_cafe_publish(handler, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    img = tmp_path / "c.jpg"
    img.write_bytes(_make_jpeg_bytes())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = NaverCafeClient(
            access_token="tok",
            telegram_bot_token="111:real",
            telegram_chat_id="42",
            http_client=http_client,
            poll_interval_s=0,
            **kwargs,
        )
        return await client.publish_article(
            club_id="1", menu_id=2, head_tag="[공지]", subject="제목", html_body="<p>본문</p>",
            search_tags=["태그"], image_path=str(img), approval_mode="telegram_confirm_api",
        )


async def test_naver_telegram_approval_posts_only_after_approve(tmp_path: Path) -> None:
    calls: list[str] = []
    handler, state = _approval_handler("approve", calls=calls)
    res = await _run_cafe_publish(handler, tmp_path)
    assert res["status"] == "posted" and res["approved_via"] == "telegram"
    assert state["naver_posts"] == 1
    assert calls.index("sendMessage") < calls.index("articles")


async def test_naver_telegram_reject_does_not_post(tmp_path: Path) -> None:
    handler, state = _approval_handler("reject")
    res = await _run_cafe_publish(handler, tmp_path)
    assert res["status"] == "rejected" and res["posted"] is False
    assert state["naver_posts"] == 0


async def test_naver_telegram_timeout_does_not_post(tmp_path: Path) -> None:
    handler, state = _approval_handler(None)
    res = await _run_cafe_publish(handler, tmp_path, approval_timeout_s=0.05)
    assert res["status"] == "timeout" and state["naver_posts"] == 0


async def test_naver_telegram_ignores_clicks_from_other_chats(tmp_path: Path) -> None:
    handler, state = _approval_handler("approve", chat_id=999)
    res = await _run_cafe_publish(handler, tmp_path, approval_timeout_s=0.05)
    assert res["status"] == "timeout" and state["naver_posts"] == 0


async def test_naver_confirm_mode_requires_telegram(tmp_path: Path) -> None:
    img = tmp_path / "c.jpg"
    img.write_bytes(_make_jpeg_bytes())
    client = NaverCafeClient(access_token="tok")
    with pytest.raises(NaverCafeIntegrationError, match="텔레그램 승인이 필요"):
        await client.publish_article(
            club_id="1", menu_id=2, head_tag="", subject="s", html_body="b",
            search_tags=[], image_path=str(img), approval_mode="telegram_confirm_api",
        )
