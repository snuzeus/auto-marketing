from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from src.agents.copy_agent import CopyAgent
from src.agents.style_agent import (
    DEFAULT_STYLE_PROFILES_PATH,
    StyleAnalyzerAgent,
    StyleProfileManager,
)
from src.agents.tag_engine import TagEngine
from src.agents.validator_agent import (
    ValidatorAgent,
    validate_single_channel_deterministic,
)
from src.agents.vision_agent import VisionAgent
from src.core.card_renderer import build_default_card_news_spec, render_notice_card_png
from src.core.config_loader import (
    ALL_SUPPORTED_CHANNELS,
    AppSettings,
    apply_naver_cafe_target,
    load_app_settings,
    load_personas_config,
    load_personas_raw,
    resolve_enabled_channels,
)
from src.core.history_repo import (
    DEFAULT_DB_PATH,
    CampaignHistorySummary,
    CampaignSQLiteRepository,
)
from src.core.llm_client import (
    GeminiLLMClient,
    LLMConfigurationError,
    LLMGenerationError,
)
from src.core.schema import (
    AccountStyleProfile,
    CardNewsBoxSpec,
    CardNewsSpec,
    CardThemeType,
    ChannelDraftSpec,
    ChannelDraftState,
    ChannelType,
    DCInsidePostSpec,
    InstagramPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
    NaverPlacePostSpec,
    REMOVED_CHANNELS,
    NoticeCategoryType,
    ValidationFailureDetail,
    ValidationReport,
    VisionAnalysisResult,
    XTwitterPostSpec,
    x_weighted_length,
)
from src.integrations.dcinside_router import DCInsideIntegrationError
from src.integrations.naver_cafe_client import NaverCafeIntegrationError
from src.integrations.postiz_client import PostizIntegrationError
from src.mcp.tools import MCPToolDispatcher

UPLOAD_DIR = Path(".runtime_uploads")
STATIC_DIR = Path(__file__).parent / "static"


class CampaignSession(BaseModel):
    session_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    image_path: str
    image_filename: str
    user_prompt: str
    notice_category: NoticeCategoryType = "general"
    selected_style_ids: dict[ChannelType, str] = Field(default_factory=dict)
    enabled_channels: list[ChannelType]
    vision_result: VisionAnalysisResult
    tag_set: MultilingualTagSet
    generation_reasoning: str
    attempts_used: int = 1
    drafts: dict[ChannelType, ChannelDraftState] = Field(default_factory=dict)
    dispatch_results: dict[ChannelType, dict[str, Any]] = Field(default_factory=dict)
    postiz_media_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def drop_removed_channels(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        if isinstance(cleaned.get("enabled_channels"), list):
            cleaned["enabled_channels"] = [
                c for c in cleaned["enabled_channels"] if c not in REMOVED_CHANNELS
            ]
        for key in ("selected_style_ids", "drafts", "dispatch_results"):
            if isinstance(cleaned.get(key), dict):
                cleaned[key] = {
                    k: v for k, v in cleaned[key].items() if k not in REMOVED_CHANNELS
                }
        return cleaned


class InstagramUpdatePayload(BaseModel):
    hook_headline: str
    body: str
    hashtags: list[str]


class XTwitterUpdatePayload(BaseModel):
    body: str
    hashtags: list[str]


class DCInsideUpdatePayload(BaseModel):
    gallery_id: str
    title: str
    body: str


class NaverCafeUpdatePayload(BaseModel):
    club_id: str
    menu_id: int
    head_tag: str
    subject: str
    html_body: str
    plain_body_preview: str
    search_tags: list[str]


class NaverPlaceUpdatePayload(BaseModel):
    title: str
    body: str
    keywords: list[str] = Field(default_factory=list)


class RegenerateChannelRequest(BaseModel):
    feedback_note: str = ""
    style_profile_id: str | None = None


class CardNewsPromptRequest(BaseModel):
    prompt: str


class DispatchChannelRequest(BaseModel):
    approval_mode: str | None = None


class LearnStyleRequest(BaseModel):
    platform: ChannelType
    account_handle: str
    sample_posts: list[str]


class CampaignStore:
    """Session store backed by SQLite history persistence and dependency-injectable factories."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        personas_path: Path = Path("config/personas.yaml"),
        style_profiles_path: Path = DEFAULT_STYLE_PROFILES_PATH,
        db_path: Path = DEFAULT_DB_PATH,
        llm_client_factory: Any | None = None,
        dispatcher_factory: Any | None = None,
        upload_dir: Path = UPLOAD_DIR,
    ) -> None:
        self._settings = settings
        self.upload_dir = Path(upload_dir)
        self._cached_llm: tuple[tuple[str, str, str, str], GeminiLLMClient] | None = None
        self.personas_path = personas_path
        self.style_manager = StyleProfileManager(storage_path=style_profiles_path)
        self.history_repo = CampaignSQLiteRepository(db_path=db_path)
        self.sessions: dict[str, CampaignSession] = {}
        self._llm_client_factory = llm_client_factory
        self._dispatcher_factory = dispatcher_factory

    def save_session(self, session: CampaignSession) -> None:
        self.sessions[session.session_id] = session
        self.history_repo.save_session(session)

    def get_session(self, session_id: str) -> CampaignSession | None:
        if session_id in self.sessions:
            return self.sessions[session_id]
        raw_json = self.history_repo.load_session_json(session_id)
        if raw_json:
            loaded = CampaignSession.model_validate_json(raw_json)
            self.sessions[session_id] = loaded
            return loaded
        return None

    def get_settings(self) -> AppSettings:
        if self._settings is not None:
            return self._settings
        return load_app_settings()

    def get_llm_client(self) -> GeminiLLMClient:
        if self._llm_client_factory is not None:
            client: GeminiLLMClient = self._llm_client_factory()
            return client
        s = self.get_settings()
        # 같은 설정이면 클라이언트를 재사용해 페르소나 캐시 핸들이 요청 간에 유지되게 한다.
        key = (
            s.gemini_api_key,
            s.gemini_main_model,
            s.gemini_lite_model,
            s.gemini_default_thinking_level,
        )
        if self._cached_llm is not None and self._cached_llm[0] == key:
            return self._cached_llm[1]
        created = GeminiLLMClient(
            api_key=s.gemini_api_key,
            main_model=s.gemini_main_model,
            lite_model=s.gemini_lite_model,
            default_thinking_level=s.gemini_default_thinking_level,
        )
        self._cached_llm = (key, created)
        return created

    def collect_approved_examples(
        self,
        channels: list[ChannelType],
        notice_category: str,
        exclude_session_id: str | None = None,
    ) -> dict[ChannelType, list[str]]:
        """채널별로 같은 공지 유형의 최근 승인본을 모아 문체 참고 예시로 돌려준다."""
        examples: dict[ChannelType, list[str]] = {}
        for ch in channels:
            found = self.history_repo.list_approved_examples(
                channel=ch,
                notice_category=notice_category,
                limit=2,
                exclude_session_id=exclude_session_id,
            )
            if found:
                examples[ch] = found
        return examples

    def get_dispatcher(self, enabled_channels: list[ChannelType]) -> MCPToolDispatcher:
        if self._dispatcher_factory is not None:
            disp: MCPToolDispatcher = self._dispatcher_factory(enabled_channels)
            return disp
        return MCPToolDispatcher(
            settings=self.get_settings(),
            enabled_channels=enabled_channels,
        )


def create_app(store: CampaignStore | None = None) -> FastAPI:
    app = FastAPI(
        title="TCG Notice & Style Studio",
        description="Account-Style Learning & Multi-Channel TCG Notice Automation Bot",
        version="0.3.0",
    )
    app_store = store or CampaignStore()
    app.state.store = app_store

    app_store.upload_dir.mkdir(parents=True, exist_ok=True)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def serve_index() -> HTMLResponse:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>UI File Not Found</h1>", status_code=404)
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def get_health_and_config() -> dict[str, Any]:
        settings = app_store.get_settings()
        personas_cfg = load_personas_config(app_store.personas_path)
        return {
            "status": "ok",
            "supported_channels": list(ALL_SUPPORTED_CHANNELS),
            "default_enabled_channels": resolve_enabled_channels(personas_cfg),
            "models": {
                "main_model": settings.gemini_main_model,
                "lite_model": settings.gemini_lite_model,
                "thinking_level": settings.gemini_default_thinking_level,
            },
            "integrations_configured": {
                "gemini": settings.has_gemini_key,
                "postiz": settings.has_postiz_credentials,
                "naver_cafe_api": settings.has_naver_credentials,
                "telegram": settings.has_telegram_credentials,
                "local_clipboard": settings.enable_local_clipboard,
            },
            "channel_defaults": {
                "dcinside_gallery": settings.dcinside_default_gallery,
                "naver_cafe_club_id": settings.naver_cafe_club_id or "12345678",
                "naver_cafe_menu_id": settings.naver_cafe_menu_id or 42,
                "naver_allowed_head_tags": personas_cfg.get("channels", {})
                .get("naver_cafe", {})
                .get(
                    "allowed_head_tags",
                    [
                        "[대회공지]",
                        "[입고공지]",
                        "[매장일정]",
                        "[공지]",
                        "[안내]",
                        "[이벤트]",
                        "[후기]",
                        "[정보공유]",
                    ],
                ),
            },
        }

    # --- Account Style Profile Endpoints ---

    @app.get("/api/style-profiles")
    async def list_style_profiles(platform: ChannelType | None = None) -> list[AccountStyleProfile]:
        return app_store.style_manager.list_profiles(platform=platform)

    @app.post("/api/style-profiles/learn")
    async def learn_and_save_style_profile(req: LearnStyleRequest) -> AccountStyleProfile:
        handle = req.account_handle.strip()
        if not handle:
            raise HTTPException(status_code=400, detail="계정 아이디 또는 이름을 입력해 주세요.")
        samples = [s.strip() for s in req.sample_posts if s.strip()]
        if not samples:
            raise HTTPException(
                status_code=400, detail="학습할 기존 공지글 샘플을 최소 1개 이상 입력해 주세요."
            )

        try:
            llm = app_store.get_llm_client()
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        analyzer = StyleAnalyzerAgent(llm)
        try:
            profile = await analyzer.learn_account_style(
                platform=req.platform,
                account_handle=handle,
                sample_posts=samples,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"계정 스타일 학습 중 오류가 발생했습니다: {exc}"
            ) from exc

        return app_store.style_manager.save_profile(profile)

    @app.delete("/api/style-profiles/{profile_id}")
    async def delete_style_profile(profile_id: str) -> dict[str, Any]:
        deleted = app_store.style_manager.delete_profile(profile_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="삭제할 스타일 프로필을 찾을 수 없습니다.")
        return {"deleted": True, "profile_id": profile_id}

    # --- Campaign History Endpoints (SQLite) ---

    @app.get("/api/history")
    async def list_campaign_history(limit: int = 30) -> list[CampaignHistorySummary]:
        return app_store.history_repo.list_recent(limit=limit)

    @app.delete("/api/history/{session_id}")
    async def delete_campaign_history(session_id: str) -> dict[str, Any]:
        app_store.sessions.pop(session_id, None)
        deleted = app_store.history_repo.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="삭제할 공지 이력을 찾을 수 없습니다.")
        return {"deleted": True, "session_id": session_id}

    # --- Campaign & Notice Generation Endpoints ---

    @app.get("/api/campaigns/{session_id}/image")
    async def get_campaign_image(session_id: str) -> FileResponse:
        session = _get_session_or_404(app_store, session_id)
        img_path = Path(session.image_path)
        if not img_path.exists():
            raise HTTPException(status_code=404, detail="업로드된 이미지를 찾을 수 없습니다.")
        return FileResponse(str(img_path))

    @app.get("/api/campaigns/{session_id}/channels/{channel}/card-image")
    async def get_rendered_notice_card_png(
        session_id: str,
        channel: ChannelType,
        inline: bool = False,
    ) -> Response:
        """Render and return/download a 4:5 (1080x1350) PNG notice card image for Instagram/SNS."""
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")

        png_bytes = _render_channel_notice_card_bytes(session, channel, app_store)
        filename = f"notice_card_{session_id}_{channel}.png"
        disposition = f'inline; filename="{filename}"' if inline else f'attachment; filename="{filename}"'
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": disposition},
        )

    @app.get("/api/campaigns/{session_id}")
    async def get_campaign(session_id: str) -> CampaignSession:
        return _get_session_or_404(app_store, session_id)

    @app.post("/api/campaigns")
    async def create_campaign(
        image: UploadFile = File(...),
        prompt: str = Form(...),
        channels: str = Form(""),
        notice_category: str = Form("tcg_tournament"),
        style_profile_map: str = Form("{}"),
    ) -> CampaignSession:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise HTTPException(status_code=400, detail="공지할 핵심 내용을 입력해 주세요.")

        valid_categories = (
            "general",
            "schedule_change",
            "urgent",
            "event_open",
            "tcg_tournament",
            "tcg_product_release",
            "tcg_store_schedule",
            "tcg_weekly_schedule",
            "tcg_tournament_result",
        )
        cat: NoticeCategoryType = (
            notice_category  # type: ignore[assignment]
            if notice_category in valid_categories
            else "tcg_tournament"
        )

        selected_style_ids: dict[ChannelType, str] = {}
        resolved_profiles: dict[ChannelType, AccountStyleProfile] = {}
        if style_profile_map.strip():
            try:
                parsed_map = json.loads(style_profile_map)
                if isinstance(parsed_map, dict):
                    for ch_key, prof_id in parsed_map.items():
                        if ch_key in ALL_SUPPORTED_CHANNELS and prof_id:
                            prof = app_store.style_manager.get_profile(str(prof_id))
                            if prof is not None:
                                selected_style_ids[ch_key] = prof.profile_id
                                resolved_profiles[ch_key] = prof
            except Exception:
                pass

        personas_cfg = load_personas_config(app_store.personas_path)
        personas_raw = load_personas_raw(app_store.personas_path)
        try:
            enabled_channels = resolve_enabled_channels(personas_cfg, channels or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            llm = app_store.get_llm_client()
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        session_id = uuid.uuid4().hex[:12]
        suffix = Path(image.filename or "upload.jpg").suffix or ".jpg"
        saved_path = app_store.upload_dir / f"{session_id}{suffix}"
        with saved_path.open("wb") as buffer:
            shutil.copyfileobj(image.file, buffer)

        try:
            vision_agent = VisionAgent(llm)
            tag_engine = TagEngine(llm)
            copy_agent = CopyAgent(llm, personas_raw)
            validator = ValidatorAgent(llm, personas_cfg, enable_semantic_stage=True)

            vision_result = await vision_agent.analyze(str(saved_path), clean_prompt)
            tag_set = await tag_engine.generate_tags(
                vision_result=vision_result,
                user_prompt=clean_prompt,
                enabled_channels=enabled_channels,
                personas_config=personas_cfg,
            )
            post_output, gen_report, attempts = await validator.run_with_self_correction(
                copy_agent=copy_agent,
                vision_result=vision_result,
                tag_set=tag_set,
                user_prompt=clean_prompt,
                enabled_channels=enabled_channels,
                max_attempts=3,
                notice_category=cat,
                style_profiles=resolved_profiles,
                approved_examples=app_store.collect_approved_examples(enabled_channels, cat),
            )
        except (LLMConfigurationError, LLMGenerationError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"AI 파이프라인 실행 실패: {exc}") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"AI 모델 호출 중 오류가 발생했습니다: {exc}"
            ) from exc

        apply_naver_cafe_target(post_output.naver_cafe, app_store.get_settings())

        drafts: dict[ChannelType, ChannelDraftState] = {}
        for ch in enabled_channels:
            spec = getattr(post_output, ch)
            if spec is not None:
                ch_failures = validate_single_channel_deterministic(ch, spec, personas_cfg)
                drafts[ch] = ChannelDraftState(
                    channel=ch,
                    spec=spec,
                    status="draft",
                    validation=ValidationReport(
                        is_valid=len(ch_failures) == 0,
                        failures=ch_failures,
                        warnings=[w for w in gen_report.warnings if w.channel == ch],
                    ),
                    revision=1,
                )

        reasoning_text = post_output.generation_reasoning
        failed_channels = [ch for ch in enabled_channels if ch not in drafts]
        if failed_channels:
            failed_reasons = "; ".join(
                f.reason for f in gen_report.failures if f.channel in failed_channels
            )
            reasoning_text = (
                f"{reasoning_text}\n[생성 실패 채널: {', '.join(failed_channels)}] {failed_reasons}"
            ).strip()

        session = CampaignSession(
            session_id=session_id,
            image_path=str(saved_path),
            image_filename=image.filename or saved_path.name,
            user_prompt=clean_prompt,
            notice_category=cat,
            selected_style_ids=selected_style_ids,
            enabled_channels=enabled_channels,
            vision_result=vision_result,
            tag_set=tag_set,
            generation_reasoning=reasoning_text,
            attempts_used=attempts,
            drafts=drafts,
        )
        for ch_key in session.drafts:
            session.drafts[ch_key].card_news = _build_session_channel_card_news(
                session, ch_key, app_store
            )
        app_store.save_session(session)
        return session

    @app.put("/api/campaigns/{session_id}/channels/{channel}")
    async def update_channel_draft(
        session_id: str,
        channel: ChannelType,
        payload: dict[str, Any],
    ) -> ChannelDraftState:
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(
                status_code=400, detail=f"채널 '{channel}'은 현재 캠페인에서 활성화되지 않았습니다."
            )

        personas_cfg = load_personas_config(app_store.personas_path)
        new_spec, schema_failures = _parse_channel_update_payload(channel, payload)

        det_failures = validate_single_channel_deterministic(channel, new_spec, personas_cfg)
        all_failures = schema_failures + det_failures
        report = ValidationReport(is_valid=len(all_failures) == 0, failures=all_failures)

        draft_state = session.drafts[channel]
        prev_theme: CardThemeType = (
            draft_state.card_news.theme if draft_state.card_news is not None else "cyber_cyan"
        )
        draft_state.update_spec(new_spec, validation_report=report)
        draft_state.card_news = _build_session_channel_card_news(
            session, channel, app_store, theme=prev_theme
        )
        session.dispatch_results.pop(channel, None)
        app_store.save_session(session)
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/regenerate")
    async def regenerate_single_channel(
        session_id: str,
        channel: ChannelType,
        req: RegenerateChannelRequest | None = None,
    ) -> ChannelDraftState:
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.enabled_channels:
            raise HTTPException(
                status_code=400, detail=f"채널 '{channel}'은 현재 캠페인에서 활성화되지 않았습니다."
            )

        try:
            llm = app_store.get_llm_client()
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        personas_cfg = load_personas_config(app_store.personas_path)
        personas_raw = load_personas_raw(app_store.personas_path)
        copy_agent = CopyAgent(llm, personas_raw)
        validator = ValidatorAgent(llm, personas_cfg, enable_semantic_stage=True)

        extra_prompt = session.user_prompt
        if req and req.feedback_note.strip():
            extra_prompt = (
                f"{session.user_prompt}\n[사용자 추가 수정 지침 ({channel})]: "
                f"{req.feedback_note.strip()}"
            )

        style_id = (
            req.style_profile_id
            if (req and req.style_profile_id is not None)
            else session.selected_style_ids.get(channel)
        )
        style_map: dict[ChannelType, AccountStyleProfile] = {}
        if style_id:
            prof = app_store.style_manager.get_profile(style_id)
            if prof is not None:
                style_map[channel] = prof
                session.selected_style_ids[channel] = style_id

        try:
            post_output, regen_report, _ = await validator.run_with_self_correction(
                copy_agent=copy_agent,
                vision_result=session.vision_result,
                tag_set=session.tag_set,
                user_prompt=extra_prompt,
                enabled_channels=[channel],
                max_attempts=3,
                notice_category=session.notice_category,
                style_profiles=style_map,
                approved_examples=app_store.collect_approved_examples(
                    [channel], session.notice_category, exclude_session_id=session.session_id
                ),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"'{channel}' 채널 재생성 실패: {exc}"
            ) from exc

        new_spec = getattr(post_output, channel, None)
        if new_spec is None:
            raise HTTPException(
                status_code=502, detail=f"'{channel}' 채널 재생성 결과가 비어 있습니다."
            )

        if isinstance(new_spec, NaverCafePostSpec):
            apply_naver_cafe_target(new_spec, app_store.get_settings())
            prev = session.drafts[channel].spec if channel in session.drafts else None
            # 사용자가 직접 바꿔 저장한 카페/게시판은 다시 쓰기 후에도 유지한다.
            if isinstance(prev, NaverCafePostSpec) and prev.club_id != "12345678":
                new_spec.club_id, new_spec.menu_id = prev.club_id, prev.menu_id

        ch_failures = validate_single_channel_deterministic(channel, new_spec, personas_cfg)
        report = ValidationReport(
            is_valid=len(ch_failures) == 0,
            failures=ch_failures,
            warnings=[w for w in regen_report.warnings if w.channel == channel],
        )

        prev_theme: CardThemeType = "cyber_cyan"
        if channel in session.drafts:
            if session.drafts[channel].card_news is not None:
                prev_theme = session.drafts[channel].card_news.theme  # type: ignore[union-attr]
            session.drafts[channel].update_spec(new_spec, validation_report=report)
        else:
            session.drafts[channel] = ChannelDraftState(
                channel=channel,
                spec=new_spec,
                status="draft",
                validation=report,
                revision=1,
            )
        session.drafts[channel].card_news = _build_session_channel_card_news(
            session, channel, app_store, theme=prev_theme
        )
        session.dispatch_results.pop(channel, None)
        app_store.save_session(session)
        return session.drafts[channel]

    @app.post("/api/campaigns/{session_id}/channels/{channel}/card-news/prompt")
    async def modify_card_news_with_prompt(
        session_id: str,
        channel: ChannelType,
        req: CardNewsPromptRequest,
    ) -> ChannelDraftState:
        """Modify a channel's structured CardNewsSpec using an AI instruction prompt."""
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")

        clean_prompt = req.prompt.strip()
        if not clean_prompt:
            raise HTTPException(status_code=400, detail="카드뉴스 수정 요청 프롬프트를 입력해 주세요.")

        try:
            llm = app_store.get_llm_client()
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        draft_state = session.drafts[channel]
        current_card = _ensure_channel_card_news(session, channel, app_store)
        _, body_text = _extract_channel_card_headline_and_body(session, channel)

        sys_inst = (
            "당신은 TCG 매장 공지 카드뉴스 디자이너입니다. 사용자의 수정 요청을 반영하여 `CardNewsSpec`을 업데이트하십시오.\n"
            "- 박스 안 텍스트(`boxes[i].value`)는 문장형 서술어(~입니다, ~바랍니다 등)를 제거하고 핵심 키워드/명사구로 최대한 짧고 단순하게 유지하십시오.\n"
            "- 사용자가 2개 항목 나란히 배치를 원하면 `값1 / 값2` 형식을 사용하십시오.\n"
            "- 색상/테마 요청에 따라 `theme`을 적절히 선택하십시오: "
            "'cyber_cyan'(블루/네이비), 'crimson_red'(T1/레드/검빨), 'royal_gold'(골드/프리미엄), "
            "'emerald_green'(그린), 'violet_purple'(퍼플), 'minimal_light'(깔끔한 화이트/다이어그램 스타일)."
        )
        user_content = (
            f"### 현재 카드뉴스 스펙(JSON)\n{current_card.model_dump_json(indent=2)}\n\n"
            f"### 원본 공지 본문 참고\n{body_text}\n\n"
            f"### 사용자 수정 프롬프트\n{clean_prompt}"
        )

        try:
            new_card = await llm.generate_structured(
                model=llm.main_model,
                system_instruction=sys_inst,
                user_content=user_content,
                response_model=CardNewsSpec,
                thinking_level="low",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"카드뉴스 AI 수정 중 오류가 발생했습니다: {exc}"
            ) from exc

        draft_state.card_news = new_card
        draft_state.revision += 1
        draft_state.unapprove()
        session.dispatch_results.pop(channel, None)
        app_store.save_session(session)
        return draft_state

    @app.put("/api/campaigns/{session_id}/channels/{channel}/card-news")
    async def update_card_news_direct(
        session_id: str,
        channel: ChannelType,
        payload: CardNewsSpec,
    ) -> ChannelDraftState:
        """Directly update a channel's CardNewsSpec (texts, boxes, footnotes, theme)."""
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")

        cleaned_boxes = [
            CardNewsBoxSpec(label=b.label.strip() or "안내", value=b.value.strip())
            for b in payload.boxes
            if b.label.strip() or b.value.strip()
        ]
        if not cleaned_boxes:
            cleaned_boxes = [CardNewsBoxSpec(label="공지 안내", value="매장 공지 참고")]

        sanitized = CardNewsSpec(
            main_title=payload.main_title.strip() or "매장 공지 안내",
            subtitle_banner=payload.subtitle_banner.strip() or "OFFICIAL NOTICE",
            boxes=cleaned_boxes[:6],
            footnotes=[f.strip() for f in payload.footnotes if f.strip()][:3],
            footer_brand=payload.footer_brand.strip() or "OFFICIAL TCG STORE",
            theme=payload.theme,
        )

        draft_state = session.drafts[channel]
        draft_state.card_news = sanitized
        draft_state.revision += 1
        draft_state.unapprove()
        session.dispatch_results.pop(channel, None)
        app_store.save_session(session)
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/approve")
    async def approve_channel(session_id: str, channel: ChannelType) -> ChannelDraftState:
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")
        draft_state = session.drafts[channel]
        try:
            draft_state.approve()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app_store.save_session(session)
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/unapprove")
    async def unapprove_channel(session_id: str, channel: ChannelType) -> ChannelDraftState:
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")
        draft_state = session.drafts[channel]
        draft_state.unapprove()
        app_store.save_session(session)
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/dispatch")
    async def dispatch_approved_channel(
        session_id: str,
        channel: ChannelType,
        req: DispatchChannelRequest | None = None,
    ) -> dict[str, Any]:
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")

        draft_state = session.drafts[channel]
        if draft_state.status != "approved":
            raise HTTPException(
                status_code=400,
                detail=f"승인되지 않은 초안({channel})은 외부로 전송할 수 없습니다. 먼저 '승인' 버튼을 눌러 확정하세요.",
            )

        dispatcher = app_store.get_dispatcher([channel])
        spec = draft_state.spec

        try:
            if channel == "instagram" and isinstance(spec, InstagramPostSpec):
                result = await _dispatch_postiz_with_notice_card(
                    session=session,
                    channel="instagram",
                    content=spec.full_caption,
                    dispatcher=dispatcher,
                    store=app_store,
                )

            elif channel == "x_twitter" and isinstance(spec, XTwitterPostSpec):
                result = await _dispatch_postiz_with_notice_card(
                    session=session,
                    channel="x_twitter",
                    content=spec.full_tweet,
                    dispatcher=dispatcher,
                    store=app_store,
                )

            elif channel == "naver_cafe" and isinstance(spec, NaverCafePostSpec):
                personas_cfg = load_personas_config(app_store.personas_path)
                default_mode = (
                    personas_cfg.get("channels", {})
                    .get("naver_cafe", {})
                    .get("approval_mode", "telegram_confirm_api")
                )
                mode = req.approval_mode if req and req.approval_mode else default_mode
                result = await dispatcher.execute_tool(
                    "publish_naver_cafe_article",
                    {
                        "club_id": spec.club_id,
                        "menu_id": spec.menu_id,
                        "head_tag": spec.head_tag,
                        "subject": spec.subject,
                        "html_body": spec.html_body,
                        "search_tags": spec.search_tags,
                        "image_path": session.image_path,
                        "approval_mode": mode,
                    },
                )

            elif channel == "dcinside" and isinstance(spec, DCInsidePostSpec):
                result = await dispatcher.execute_tool(
                    "dispatch_dcinside_pack",
                    {
                        "gallery_id": spec.gallery_id,
                        "title": spec.title,
                        "body": spec.body,
                        "image_path": session.image_path,
                    },
                )
            elif channel == "naver_place" and isinstance(spec, NaverPlacePostSpec):
                import pyperclip

                text_to_copy = spec.full_news
                copied = False
                if app_store.get_settings().enable_local_clipboard:
                    try:
                        pyperclip.copy(text_to_copy)
                        copied = True
                    except Exception:
                        copied = False
                if not copied:
                    raise HTTPException(
                        status_code=503,
                        detail=f"{channel} 원고 클립보드 복사에 실패했습니다. 화면의 '공지 전문 복사' 버튼을 이용해 주세요.",
                    )
                result = {
                    "mode": "local_clipboard",
                    "channel": channel,
                    "clipboard_copied": True,
                }
            else:
                raise HTTPException(status_code=400, detail="유효하지 않은 채널 스펙입니다.")

        except (
            PostizIntegrationError,
            NaverCafeIntegrationError,
            DCInsideIntegrationError,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"외부 서비스 연동 중 오류가 발생했습니다: {exc}"
            ) from exc

        session.dispatch_results[channel] = result
        app_store.save_session(session)
        return {"channel": channel, "dispatched": True, "result": result}

    return app


def _get_session_or_404(store: CampaignStore, session_id: str) -> CampaignSession:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"캠페인 세션({session_id})을 찾을 수 없습니다.")
    return session


def _parse_channel_update_payload(
    channel: ChannelType,
    payload: dict[str, Any],
) -> tuple[ChannelDraftSpec, list[ValidationFailureDetail]]:
    failures: list[ValidationFailureDetail] = []

    if channel == "instagram":
        upd = InstagramUpdatePayload.model_validate(payload)
        tags = [
            t.strip() if t.strip().startswith("#") else f"#{t.strip()}"
            for t in upd.hashtags
            if t.strip()
        ]
        spec_insta = InstagramPostSpec.model_construct(
            hook_headline=upd.hook_headline,
            body=upd.body,
            hashtags=tags,
        )
        return spec_insta, failures

    if channel == "x_twitter":
        upd_x = XTwitterUpdatePayload.model_validate(payload)
        tags = [
            t.strip() if t.strip().startswith("#") else f"#{t.strip()}"
            for t in upd_x.hashtags
            if t.strip()
        ]
        full_str = f"{upd_x.body}\n\n{' '.join(tags)}".strip()
        # 길이 한도와 해시태그 개수는 Stage 1 결정적 검증기가 X 가중치 기준으로 판정한다.
        total_len = x_weighted_length(full_str)
        spec_x = XTwitterPostSpec.model_construct(
            body=upd_x.body,
            hashtags=tags,
            total_char_count=total_len,
        )
        return spec_x, failures

    if channel == "dcinside":
        upd_dc = DCInsideUpdatePayload.model_validate(payload)
        spec_dc = DCInsidePostSpec.model_construct(
            gallery_id=upd_dc.gallery_id.strip() or "boardgame",
            title=upd_dc.title,
            body=upd_dc.body,
            contains_forbidden_words=False,
        )
        return spec_dc, failures

    if channel == "naver_place":
        upd_place = NaverPlaceUpdatePayload.model_validate(payload)
        spec_place = NaverPlacePostSpec.model_construct(
            title=upd_place.title,
            body=upd_place.body,
            keywords=[k.strip() for k in upd_place.keywords if k.strip()],
        )
        return spec_place, failures

    upd_cafe = NaverCafeUpdatePayload.model_validate(payload)
    clean_tags = [t.strip() for t in upd_cafe.search_tags if t.strip()]
    if not (5 <= len(clean_tags) <= 10):
        failures.append(
            ValidationFailureDetail(
                channel="naver_cafe",
                rule_violated="search_tag_count",
                reason=f"네이버 카페 검색 태그 개수가 {len(clean_tags)}개입니다.",
                fix_instruction="검색 태그를 5~10개 사이로 입력하세요.",
            )
        )
    if len(upd_cafe.subject) > 50:
        failures.append(
            ValidationFailureDetail(
                channel="naver_cafe",
                rule_violated="max_subject_length",
                reason=f"네이버 카페 제목이 {len(upd_cafe.subject)}자로 50자를 초과했습니다.",
                fix_instruction="제목을 50자 이하로 줄이세요.",
            )
        )
    spec_cafe = NaverCafePostSpec.model_construct(
        club_id=upd_cafe.club_id,
        menu_id=upd_cafe.menu_id,
        head_tag=upd_cafe.head_tag,
        subject=upd_cafe.subject,
        html_body=upd_cafe.html_body,
        plain_body_preview=upd_cafe.plain_body_preview,
        search_tags=clean_tags,
    )
    return spec_cafe, failures


def _extract_channel_card_headline_and_body(
    session: CampaignSession,
    channel: ChannelType,
) -> tuple[str, str]:
    spec = session.drafts[channel].spec
    headline = session.vision_result.primary_subject
    body_text = ""

    if isinstance(spec, InstagramPostSpec):
        headline = spec.hook_headline
        body_text = spec.body
    elif isinstance(spec, XTwitterPostSpec):
        lines = spec.body.splitlines()
        headline = lines[0] if lines else "TCG 매장 공지"
        body_text = "\n".join(lines[1:]) if len(lines) > 1 else spec.body
    elif isinstance(spec, DCInsidePostSpec):
        headline = spec.title
        body_text = spec.body
    elif isinstance(spec, NaverCafePostSpec):
        headline = f"{spec.head_tag} {spec.subject}"
        body_text = spec.plain_body_preview
    elif isinstance(spec, NaverPlacePostSpec):
        headline = spec.title
        body_text = spec.body

    return headline, body_text


def _resolve_channel_brand_label(
    session: CampaignSession,
    channel: ChannelType,
    store: CampaignStore,
) -> str:
    brand_label = "OFFICIAL TCG STORE"
    style_id = session.selected_style_ids.get(channel)
    if style_id:
        profile = store.style_manager.get_profile(style_id)
        if profile is not None and profile.account_handle.strip():
            brand_label = profile.account_handle.strip()
    return brand_label


def _build_session_channel_card_news(
    session: CampaignSession,
    channel: ChannelType,
    store: CampaignStore,
    theme: CardThemeType = "cyber_cyan",
) -> CardNewsSpec:
    headline, body_text = _extract_channel_card_headline_and_body(session, channel)
    brand_label = _resolve_channel_brand_label(session, channel, store)
    return build_default_card_news_spec(
        headline=headline,
        body=body_text,
        notice_category=session.notice_category,
        user_prompt=session.user_prompt,
        brand_label=brand_label,
        theme=theme,
    )


def _ensure_channel_card_news(
    session: CampaignSession,
    channel: ChannelType,
    store: CampaignStore,
) -> CardNewsSpec:
    draft_state = session.drafts[channel]
    if draft_state.card_news is not None:
        return draft_state.card_news
    built = _build_session_channel_card_news(session, channel, store)
    draft_state.card_news = built
    return built


def _render_channel_notice_card_bytes(
    session: CampaignSession,
    channel: ChannelType,
    store: CampaignStore,
) -> bytes:
    headline, body_text = _extract_channel_card_headline_and_body(session, channel)
    brand_label = _resolve_channel_brand_label(session, channel, store)
    card_news = _ensure_channel_card_news(session, channel, store)

    return render_notice_card_png(
        headline=headline,
        body=body_text,
        category_badge=session.notice_category.replace("_", " ").upper(),
        source_image_path=session.image_path,
        notice_category=session.notice_category,
        user_prompt=session.user_prompt,
        brand_label=brand_label,
        include_source_thumbnail=False,
        card_news=card_news,
    )


def _save_channel_notice_card_file(
    session: CampaignSession,
    channel: ChannelType,
    store: CampaignStore,
) -> Path:
    store.upload_dir.mkdir(parents=True, exist_ok=True)
    png_bytes = _render_channel_notice_card_bytes(session, channel, store)
    card_path = store.upload_dir / f"{session.session_id}_{channel}_notice_card.png"
    card_path.write_bytes(png_bytes)
    return card_path


async def _dispatch_postiz_with_notice_card(
    session: CampaignSession,
    channel: Literal["instagram", "x_twitter"],
    content: str,
    dispatcher: MCPToolDispatcher,
    store: CampaignStore,
) -> dict[str, Any]:
    """Upload slide 1 (source photo) + slide 2 (rendered notice card PNG) and create Postiz draft."""
    # 매번 다시 업로드한다: 게시물 생성에는 media_id와 path가 모두 필요하고, 요청마다 새 클라이언트가
    # 만들어지므로 이전에 저장한 media_id만으로는 path를 알 수 없다.
    media_res = await dispatcher.execute_tool(
        "upload_media",
        {
            "file_path": session.image_path,
            "alt_text": session.vision_result.suggested_alt_text,
        },
    )
    session.postiz_media_id = str(media_res["media_id"])

    card_path = _save_channel_notice_card_file(session, channel, store)
    card_media_res = await dispatcher.execute_tool(
        "upload_media",
        {
            "file_path": str(card_path),
            "alt_text": f"{session.vision_result.primary_subject} 공지 안내 카드뉴스",
        },
    )
    card_media_id = str(card_media_res["media_id"])

    return await dispatcher.execute_tool(
        "create_draft_post",
        {
            "channel": channel,
            "content": content,
            "media_ids": [session.postiz_media_id, card_media_id],
        },
    )
