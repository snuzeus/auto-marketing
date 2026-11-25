from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ChannelType = Literal[
    "instagram",
    "x_twitter",
    "dcinside",
    "naver_cafe",
    "kakao_channel",
    "naver_place",
]
ThinkingLevelType = Literal["low", "medium", "high"]

ALL_SUPPORTED_CHANNELS: tuple[ChannelType, ...] = (
    "instagram",
    "x_twitter",
    "dcinside",
    "naver_cafe",
    "kakao_channel",
    "naver_place",
)

DEFAULT_PERSONAS_PATH = Path("config/personas.yaml")


class AppSettings(BaseModel):
    """Server environment settings loaded strictly from server environment variables."""

    gemini_api_key: str = Field(default="")
    gemini_main_model: str = Field(default="gemini-3.8-flash")
    gemini_lite_model: str = Field(default="gemini-3.5-flash-lite")
    gemini_default_thinking_level: ThinkingLevelType = Field(default="low")

    postiz_base_url: str = Field(default="http://localhost:3000/api/v1")
    postiz_api_key: str = Field(default="")
    postiz_instagram_channel_id: str = Field(default="")
    postiz_x_channel_id: str = Field(default="")

    naver_client_id: str = Field(default="")
    naver_client_secret: str = Field(default="")
    naver_access_token: str = Field(default="")
    naver_refresh_token: str = Field(default="")
    naver_cafe_club_id: str = Field(default="")
    naver_cafe_menu_id: int = Field(default=0)

    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    dcinside_default_gallery: str = Field(default="gadget")
    enable_local_clipboard: bool = Field(default=True)

    @property
    def has_gemini_key(self) -> bool:
        key = self.gemini_api_key.strip()
        return bool(key) and not key.startswith("AIzaSyYourGoogleGeminiApiKey")

    @property
    def has_postiz_credentials(self) -> bool:
        key = self.postiz_api_key.strip()
        return bool(self.postiz_base_url.strip()) and bool(key) and key != "pz_live_..."

    @property
    def has_naver_credentials(self) -> bool:
        token = self.naver_access_token.strip()
        return bool(token) and token != "your_oauth2_access_token"

    @property
    def has_telegram_credentials(self) -> bool:
        token = self.telegram_bot_token.strip()
        chat_id = self.telegram_chat_id.strip()
        return (
            bool(token)
            and bool(chat_id)
            and not token.startswith("123456789:")
            and chat_id != "987654321"
        )


def load_app_settings(dotenv_path: Path | str | None = None) -> AppSettings:
    """Load environment variables from .env (if present) and construct AppSettings."""
    if dotenv_path is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)
    else:
        load_dotenv(override=False)

    raw_thinking = os.getenv("GEMINI_DEFAULT_THINKING_LEVEL", "low").strip().lower()
    thinking_level: ThinkingLevelType = (
        raw_thinking  # type: ignore[assignment]
        if raw_thinking in ("low", "medium", "high")
        else "low"
    )

    raw_menu_id = os.getenv("NAVER_CAFE_MENU_ID", "0").strip()
    try:
        menu_id = int(raw_menu_id)
    except ValueError:
        menu_id = 0

    raw_clipboard = os.getenv("ENABLE_LOCAL_CLIPBOARD", "true").strip().lower()
    enable_clipboard = raw_clipboard in ("1", "true", "yes", "on")

    return AppSettings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_main_model=os.getenv("GEMINI_MAIN_MODEL", "gemini-3.8-flash").strip()
        or "gemini-3.8-flash",
        gemini_lite_model=os.getenv("GEMINI_LITE_MODEL", "gemini-3.5-flash-lite").strip()
        or "gemini-3.5-flash-lite",
        gemini_default_thinking_level=thinking_level,
        postiz_base_url=os.getenv("POSTIZ_BASE_URL", "http://localhost:3000/api/v1").strip(),
        postiz_api_key=os.getenv("POSTIZ_API_KEY", "").strip(),
        postiz_instagram_channel_id=os.getenv("POSTIZ_INSTAGRAM_CHANNEL_ID", "").strip(),
        postiz_x_channel_id=os.getenv("POSTIZ_X_CHANNEL_ID", "").strip(),
        naver_client_id=os.getenv("NAVER_CLIENT_ID", "").strip(),
        naver_client_secret=os.getenv("NAVER_CLIENT_SECRET", "").strip(),
        naver_access_token=os.getenv("NAVER_ACCESS_TOKEN", "").strip(),
        naver_refresh_token=os.getenv("NAVER_REFRESH_TOKEN", "").strip(),
        naver_cafe_club_id=os.getenv("NAVER_CAFE_CLUB_ID", "").strip(),
        naver_cafe_menu_id=menu_id,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        dcinside_default_gallery=os.getenv("DCINSIDE_DEFAULT_GALLERY", "gadget").strip()
        or "gadget",
        enable_local_clipboard=enable_clipboard,
    )


def load_personas_raw(config_path: Path | str = DEFAULT_PERSONAS_PATH) -> str:
    """Read raw YAML string from config/personas.yaml."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"페르소나 설정 파일을 찾을 수 없습니다: {path}")
    return path.read_text(encoding="utf-8")


def load_personas_config(config_path: Path | str = DEFAULT_PERSONAS_PATH) -> dict[str, Any]:
    """Parse config/personas.yaml into a dictionary."""
    raw = load_personas_raw(config_path)
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"유효하지 않은 YAML 설정 형식입니다: {config_path}")
    return parsed


def resolve_enabled_channels(
    personas_config: dict[str, Any],
    cli_channels_arg: str | None = None,
) -> list[ChannelType]:
    """
    1순위: CLI `--channels` 인자 (예: "instagram,naver_cafe")
    2순위: config/personas.yaml의 `active_channels` 중 `channels.<name>.enabled == True`인 채널
    """
    if cli_channels_arg is not None and cli_channels_arg.strip():
        requested = [c.strip().lower() for c in cli_channels_arg.split(",") if c.strip()]
        invalid = [c for c in requested if c not in ALL_SUPPORTED_CHANNELS]
        if invalid:
            raise ValueError(
                f"지원하지 않는 플랫폼입니다: {invalid}. 지원 목록: {ALL_SUPPORTED_CHANNELS}"
            )
        # Preserve order while deduplicating
        deduped: list[ChannelType] = []
        for ch in requested:
            if ch not in deduped:
                deduped.append(ch)  # type: ignore[arg-type]
        if not deduped:
            msg = (
                "활성화된 플랫폼이 하나도 없습니다. "
                "personas.yaml 또는 --channels 옵션을 확인하세요."
            )
            raise ValueError(msg)
        return deduped

    active_list = personas_config.get("active_channels", list(ALL_SUPPORTED_CHANNELS))
    channels_cfg = personas_config.get("channels", {})
    resolved: list[ChannelType] = [
        ch
        for ch in active_list
        if ch in ALL_SUPPORTED_CHANNELS and channels_cfg.get(ch, {}).get("enabled", True)
    ]
    if not resolved:
        msg = (
            "활성화된 플랫폼이 하나도 없습니다. "
            "personas.yaml 또는 --channels 옵션을 확인하세요."
        )
        raise ValueError(msg)
    return resolved


def apply_naver_cafe_target(spec: Any, settings: AppSettings) -> None:
    """Override the draft's cafe/board with `.env` values (the model only knows placeholder IDs).

    No-op while `NAVER_CAFE_CLUB_ID` is unset or still the `.env.example` placeholder.
    """
    club_id = settings.naver_cafe_club_id.strip()
    if spec is None or not club_id or club_id == "12345678":
        return
    spec.club_id = club_id
    if settings.naver_cafe_menu_id > 0:
        spec.menu_id = settings.naver_cafe_menu_id

