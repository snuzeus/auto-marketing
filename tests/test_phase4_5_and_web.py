from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.core.config_loader import AppSettings
from src.core.schema import (
    ChannelType,
    DCInsidePostSpec,
    ImageMetadata,
    InstagramPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
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

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(
                200, json={"id": "media_999", "path": "http://localhost:3000/uploads/post.jpg"}
            )
        if request.url.path.endswith("/posts"):
            return httpx.Response(
                200,
                json={
                    "id": "post_123",
                    "status": "DRAFT",
                    "dashboard_url": "http://localhost:4200/launches",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        postiz = PostizClient(
            base_url="http://localhost:3000/api/v1",
            api_key="pz_live_real_key",
            instagram_channel_id="insta_real_01",
            x_channel_id="x_real_01",
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


def test_web_api_campaign_lifecycle_edit_revokes_approval_and_blocks_fake_dispatch() -> None:
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

    store = CampaignStore(settings=settings, llm_client_factory=fake_llm_factory)
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
