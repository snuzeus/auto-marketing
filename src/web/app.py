from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agents.copy_agent import CopyAgent
from src.agents.tag_engine import TagEngine
from src.agents.validator_agent import (
    ValidatorAgent,
    validate_single_channel_deterministic,
)
from src.agents.vision_agent import VisionAgent
from src.core.config_loader import (
    ALL_SUPPORTED_CHANNELS,
    AppSettings,
    load_app_settings,
    load_personas_config,
    load_personas_raw,
    resolve_enabled_channels,
)
from src.core.llm_client import (
    GeminiLLMClient,
    LLMConfigurationError,
    LLMGenerationError,
)
from src.core.schema import (
    ChannelDraftSpec,
    ChannelDraftState,
    ChannelType,
    DCInsidePostSpec,
    InstagramPostSpec,
    MultilingualTagSet,
    NaverCafePostSpec,
    ValidationFailureDetail,
    ValidationReport,
    VisionAnalysisResult,
    XTwitterPostSpec,
)
from src.integrations.dcinside_router import DCInsideIntegrationError
from src.integrations.naver_cafe_client import NaverCafeIntegrationError
from src.integrations.postiz_client import PostizIntegrationError
from src.mcp.tools import MCPToolDispatcher

UPLOAD_DIR = Path(".runtime_uploads")
STATIC_DIR = Path(__file__).parent / "static"


class CampaignSession(BaseModel):
    session_id: str
    image_path: str
    image_filename: str
    user_prompt: str
    enabled_channels: list[ChannelType]
    vision_result: VisionAnalysisResult
    tag_set: MultilingualTagSet
    generation_reasoning: str
    attempts_used: int = 1
    drafts: dict[ChannelType, ChannelDraftState] = Field(default_factory=dict)
    dispatch_results: dict[ChannelType, dict[str, Any]] = Field(default_factory=dict)
    postiz_media_id: str | None = None


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


class RegenerateChannelRequest(BaseModel):
    feedback_note: str = ""


class DispatchChannelRequest(BaseModel):
    approval_mode: str | None = None


class CampaignStore:
    """In-memory session store for active campaigns and dependency-injectable factories."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        personas_path: Path = Path("config/personas.yaml"),
        llm_client_factory: Any | None = None,
        dispatcher_factory: Any | None = None,
    ) -> None:
        self._settings = settings
        self.personas_path = personas_path
        self.sessions: dict[str, CampaignSession] = {}
        self._llm_client_factory = llm_client_factory
        self._dispatcher_factory = dispatcher_factory

    def get_settings(self) -> AppSettings:
        if self._settings is not None:
            return self._settings
        return load_app_settings()

    def get_llm_client(self) -> GeminiLLMClient:
        if self._llm_client_factory is not None:
            client: GeminiLLMClient = self._llm_client_factory()
            return client
        s = self.get_settings()
        return GeminiLLMClient(
            api_key=s.gemini_api_key,
            main_model=s.gemini_main_model,
            lite_model=s.gemini_lite_model,
            default_thinking_level=s.gemini_default_thinking_level,
        )

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
        title="Auto-Marketing AI Studio",
        description="Image + Prompt based Multi-Channel SNS Copywriting & Human-in-the-Loop Approval Server",
        version="0.1.0",
    )
    app_store = store or CampaignStore()
    app.state.store = app_store

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
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
                .get("allowed_head_tags", ["[후기]", "[정보공유]", "[내돈내산]", "[일상/소통]"]),
            },
        }

    @app.get("/api/campaigns/{session_id}/image")
    async def get_campaign_image(session_id: str) -> FileResponse:
        session = _get_session_or_404(app_store, session_id)
        img_path = Path(session.image_path)
        if not img_path.exists():
            raise HTTPException(status_code=404, detail="업로드된 이미지를 찾을 수 없습니다.")
        return FileResponse(str(img_path))

    @app.get("/api/campaigns/{session_id}")
    async def get_campaign(session_id: str) -> CampaignSession:
        return _get_session_or_404(app_store, session_id)

    @app.post("/api/campaigns")
    async def create_campaign(
        image: UploadFile = File(...),
        prompt: str = Form(...),
        channels: str = Form(""),
    ) -> CampaignSession:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise HTTPException(status_code=400, detail="홍보 프롬프트를 입력해 주세요.")

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
        saved_path = UPLOAD_DIR / f"{session_id}{suffix}"
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
            post_output, _, attempts = await validator.run_with_self_correction(
                copy_agent=copy_agent,
                vision_result=vision_result,
                tag_set=tag_set,
                user_prompt=clean_prompt,
                enabled_channels=enabled_channels,
                max_attempts=3,
            )
        except (LLMConfigurationError, LLMGenerationError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"AI 파이프라인 실행 실패: {exc}") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"AI 모델 호출 중 오류가 발생했습니다: {exc}"
            ) from exc

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
                    ),
                    revision=1,
                )

        session = CampaignSession(
            session_id=session_id,
            image_path=str(saved_path),
            image_filename=image.filename or saved_path.name,
            user_prompt=clean_prompt,
            enabled_channels=enabled_channels,
            vision_result=vision_result,
            tag_set=tag_set,
            generation_reasoning=post_output.generation_reasoning,
            attempts_used=attempts,
            drafts=drafts,
        )
        app_store.sessions[session_id] = session
        return session

    @app.put("/api/campaigns/{session_id}/channels/{channel}")
    async def update_channel_draft(
        session_id: str,
        channel: ChannelType,
        payload: dict[str, Any],
    ) -> ChannelDraftState:
        """
        Update a single channel's draft content, re-run server-side validation,
        and automatically revoke approval (`approved -> draft`).
        """
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
        draft_state.update_spec(new_spec, validation_report=report)
        # Clear any stale dispatch badge when content changes
        session.dispatch_results.pop(channel, None)
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/regenerate")
    async def regenerate_single_channel(
        session_id: str,
        channel: ChannelType,
        req: RegenerateChannelRequest | None = None,
    ) -> ChannelDraftState:
        """Independently regenerate only the requested channel and reset approval to `draft`."""
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

        try:
            post_output, _, _ = await validator.run_with_self_correction(
                copy_agent=copy_agent,
                vision_result=session.vision_result,
                tag_set=session.tag_set,
                user_prompt=extra_prompt,
                enabled_channels=[channel],
                max_attempts=3,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"'{channel}' 채널 재생성 실패: {exc}"
            ) from exc

        new_spec = getattr(post_output, channel)
        if new_spec is None:
            raise HTTPException(
                status_code=502, detail=f"'{channel}' 채널 재생성 결과가 비어 있습니다."
            )

        ch_failures = validate_single_channel_deterministic(channel, new_spec, personas_cfg)
        report = ValidationReport(is_valid=len(ch_failures) == 0, failures=ch_failures)

        if channel in session.drafts:
            session.drafts[channel].update_spec(new_spec, validation_report=report)
        else:
            session.drafts[channel] = ChannelDraftState(
                channel=channel,
                spec=new_spec,
                status="draft",
                validation=report,
                revision=1,
            )
        session.dispatch_results.pop(channel, None)
        return session.drafts[channel]

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
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/unapprove")
    async def unapprove_channel(session_id: str, channel: ChannelType) -> ChannelDraftState:
        session = _get_session_or_404(app_store, session_id)
        if channel not in session.drafts:
            raise HTTPException(status_code=404, detail=f"채널 '{channel}' 초안이 없습니다.")
        draft_state = session.drafts[channel]
        draft_state.unapprove()
        return draft_state

    @app.post("/api/campaigns/{session_id}/channels/{channel}/dispatch")
    async def dispatch_approved_channel(
        session_id: str,
        channel: ChannelType,
        req: DispatchChannelRequest | None = None,
    ) -> dict[str, Any]:
        """
        Dispatch an approved channel draft to its external integration (Postiz Draft, Naver Cafe, DCInside).
        Never fakes success if credentials or external servers are unavailable.
        """
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
                if not session.postiz_media_id:
                    media_res = await dispatcher.execute_tool(
                        "upload_media",
                        {
                            "file_path": session.image_path,
                            "alt_text": session.vision_result.suggested_alt_text,
                        },
                    )
                    session.postiz_media_id = str(media_res["media_id"])
                result = await dispatcher.execute_tool(
                    "create_draft_post",
                    {
                        "channel": "instagram",
                        "content": spec.full_caption,
                        "media_ids": [session.postiz_media_id],
                    },
                )

            elif channel == "x_twitter" and isinstance(spec, XTwitterPostSpec):
                if not session.postiz_media_id:
                    media_res = await dispatcher.execute_tool(
                        "upload_media",
                        {
                            "file_path": session.image_path,
                            "alt_text": session.vision_result.suggested_alt_text,
                        },
                    )
                    session.postiz_media_id = str(media_res["media_id"])
                result = await dispatcher.execute_tool(
                    "create_draft_post",
                    {
                        "channel": "x_twitter",
                        "content": spec.full_tweet,
                        "media_ids": [session.postiz_media_id],
                    },
                )

            elif channel == "naver_cafe" and isinstance(spec, NaverCafePostSpec):
                personas_cfg = load_personas_config(app_store.personas_path)
                default_mode = (
                    personas_cfg.get("channels", {})
                    .get("naver_cafe", {})
                    .get("approval_mode", "telegram_confirm_api")
                )
                mode = (req.approval_mode if req and req.approval_mode else default_mode)
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
            else:
                raise HTTPException(status_code=400, detail="유효하지 않은 채널 스펙입니다.")

        except (
            PostizIntegrationError,
            NaverCafeIntegrationError,
            DCInsideIntegrationError,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"외부 서비스 연동 중 오류가 발생했습니다: {exc}"
            ) from exc

        session.dispatch_results[channel] = result
        return {"channel": channel, "dispatched": True, "result": result}

    return app


def _get_session_or_404(store: CampaignStore, session_id: str) -> CampaignSession:
    session = store.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"캠페인 세션({session_id})을 찾을 수 없습니다.")
    return session


def _parse_channel_update_payload(
    channel: ChannelType,
    payload: dict[str, Any],
) -> tuple[ChannelDraftSpec, list[ValidationFailureDetail]]:
    """
    Construct a ChannelDraftSpec from user-edited fields while collecting schema-level
    violations (e.g., X > 280 chars or hashtag count out of bounds) into ValidationFailureDetail
    instead of losing the user's edited text.
    """
    failures: list[ValidationFailureDetail] = []

    if channel == "instagram":
        upd = InstagramUpdatePayload.model_validate(payload)
        tags = [t.strip() if t.strip().startswith("#") else f"#{t.strip()}" for t in upd.hashtags if t.strip()]
        spec_insta = InstagramPostSpec.model_construct(
            hook_headline=upd.hook_headline,
            body=upd.body,
            hashtags=tags,
        )
        return spec_insta, failures

    if channel == "x_twitter":
        upd_x = XTwitterUpdatePayload.model_validate(payload)
        tags = [t.strip() if t.strip().startswith("#") else f"#{t.strip()}" for t in upd_x.hashtags if t.strip()]
        full_str = f"{upd_x.body}\n\n{' '.join(tags)}".strip()
        total_len = len(full_str)
        if not (2 <= len(tags) <= 3):
            failures.append(
                ValidationFailureDetail(
                    channel="x_twitter",
                    rule_violated="hashtag_count",
                    reason=f"X(트위터) 해시태그 개수가 {len(tags)}개입니다.",
                    fix_instruction="해시태그를 2~3개로 조정하세요.",
                )
            )
        spec_x = XTwitterPostSpec.model_construct(
            body=upd_x.body,
            hashtags=tags,
            total_char_count=total_len,
        )
        return spec_x, failures

    if channel == "dcinside":
        upd_dc = DCInsideUpdatePayload.model_validate(payload)
        spec_dc = DCInsidePostSpec.model_construct(
            gallery_id=upd_dc.gallery_id.strip() or "gadget",
            title=upd_dc.title,
            body=upd_dc.body,
            contains_forbidden_words=False,
        )
        return spec_dc, failures

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
