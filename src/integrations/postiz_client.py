from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from src.agents.vision_agent import inspect_image_metadata

Channel = Literal["instagram", "x_twitter"]

# Postiz integration `identifier` values that belong to each logical channel.
_IDENTIFIERS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram", "instagram-standalone"),
    "x_twitter": ("x",),
}
_PLACEHOLDER_CHANNEL_IDS = {"", "ch_insta_01", "ch_x_01"}


class PostizIntegrationError(RuntimeError):
    """Raised when Postiz credentials are missing or the API call fails."""


class PostizClient:
    """Async client for the self-hosted Postiz Public API (`/public/v1`).

    * Auth: raw API key in the `Authorization` header (no `Bearer` prefix).
    * Upload: `POST /upload` (multipart field `file`) -> `{id, path}`.
    * Posts: `POST /posts` with `type="draft"` and nested `posts[].value[].image`.
    * Channel IDs: taken from env when set, otherwise resolved from
      `GET /integrations` by platform identifier (`x`, `instagram`).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        instagram_channel_id: str = "",
        x_channel_id: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.instagram_channel_id = instagram_channel_id.strip()
        self.x_channel_id = x_channel_id.strip()
        self._http = http_client
        # media_id -> {"path": ..., "alt": ...}; `POST /posts` needs both id and path.
        self._media: dict[str, dict[str, str]] = {}
        self._integrations_cache: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------ helpers
    def _ensure_configured(self) -> None:
        if not self.base_url or not self.api_key or self.api_key == "pz_live_...":
            msg = (
                "Postiz API가 설정되지 않았습니다 (POSTIZ_BASE_URL, POSTIZ_API_KEY 환경변수를 확인하세요)."
            )
            raise PostizIntegrationError(msg)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key, "Accept": "application/json"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            if self._http is not None:
                resp = await self._http.request(method, url, headers=self._headers(), **kwargs)
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.request(method, url, headers=self._headers(), **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300]
            raise PostizIntegrationError(
                f"Postiz 요청 실패 ({method} {url}): HTTP {exc.response.status_code} {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PostizIntegrationError(f"Postiz 요청 실패 ({method} {url}): {exc}") from exc

    # --------------------------------------------------------------- integrations
    async def list_integrations(self, refresh: bool = False) -> list[dict[str, Any]]:
        """`GET /integrations` -> connected channels (`id`, `name`, `identifier`, ...)."""
        self._ensure_configured()
        if self._integrations_cache is None or refresh:
            payload = await self._request("GET", "/integrations")
            if not isinstance(payload, list):
                raise PostizIntegrationError(f"Postiz 채널 목록 응답이 올바르지 않습니다: {payload}")
            self._integrations_cache = payload
        return self._integrations_cache

    async def _resolve_integration(self, channel: Channel) -> tuple[str, str]:
        """Return `(integration_id, identifier)` for a logical channel."""
        integrations = await self.list_integrations()
        wanted = _IDENTIFIERS[channel]
        configured = self.instagram_channel_id if channel == "instagram" else self.x_channel_id

        if configured not in _PLACEHOLDER_CHANNEL_IDS:
            for item in integrations:
                if str(item.get("id")) == configured:
                    return configured, str(item.get("identifier") or wanted[0])
            raise PostizIntegrationError(
                f"설정된 Postiz 채널 ID({configured})가 연결된 채널 목록에 없습니다."
            )

        matches = [
            item
            for item in integrations
            if str(item.get("identifier")) in wanted and not item.get("disabled")
        ]
        if not matches:
            label = "인스타그램" if channel == "instagram" else "X(트위터)"
            raise PostizIntegrationError(
                f"Postiz에 연결된 {label} 채널이 없습니다. Postiz 화면에서 채널을 먼저 연결하세요."
            )
        chosen = matches[0]
        return str(chosen["id"]), str(chosen.get("identifier") or wanted[0])

    # -------------------------------------------------------------------- media
    async def upload_media(self, file_path: str, alt_text: str) -> dict[str, Any]:
        """Upload an image to `POST /upload` and return media metadata."""
        self._ensure_configured()
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"업로드할 이미지 파일이 존재하지 않습니다: {file_path}")

        meta = inspect_image_metadata(path)
        mime_type, _ = mimetypes.guess_type(str(path))
        files = {"file": (path.name, path.read_bytes(), mime_type or "image/jpeg")}

        payload = await self._request("POST", "/upload", files=files)
        if not isinstance(payload, dict):
            raise PostizIntegrationError(f"Postiz 업로드 응답이 올바르지 않습니다: {payload}")

        media_id = str(payload.get("id") or "")
        media_url = str(payload.get("path") or "")
        if not media_id:
            raise PostizIntegrationError(f"Postiz 응답에 media_id가 없습니다: {payload}")

        self._media[media_id] = {"path": media_url, "alt": alt_text}
        return {
            "media_id": media_id,
            "media_url": media_url,
            "aspect_ratio": meta.aspect_ratio_label,
        }

    # -------------------------------------------------------------------- posts
    @staticmethod
    def _settings_for(channel: Channel, identifier: str) -> dict[str, Any]:
        if channel == "instagram":
            return {
                "__type": identifier,
                "post_type": "post",
                "is_trial_reel": False,
                "collaborators": [],
            }
        return {"__type": "x", "who_can_reply_post": "everyone"}

    async def create_draft_post(
        self,
        channel: Channel,
        content: str,
        media_ids: list[str],
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a post in `draft` state on Postiz (`POST /posts`)."""
        self._ensure_configured()
        integration_id, identifier = await self._resolve_integration(channel)

        images: list[dict[str, str]] = []
        for media_id in media_ids:
            known = self._media.get(media_id, {})
            image: dict[str, str] = {"id": media_id, "path": known.get("path", "")}
            if known.get("alt"):
                image["alt"] = known["alt"]
            images.append(image)

        date = scheduled_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        body: dict[str, Any] = {
            "type": "draft",
            "date": date,
            "shortLink": False,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": integration_id},
                    "value": [{"content": content, "image": images}],
                    "settings": self._settings_for(channel, identifier),
                }
            ],
        }

        payload = await self._request("POST", "/posts", json=body)

        # Response is a list of `{postId, integration}`; tolerate a bare object too.
        first = payload[0] if isinstance(payload, list) and payload else payload
        post_id = ""
        if isinstance(first, dict):
            post_id = str(first.get("postId") or first.get("id") or first.get("post_id") or "")
        if not post_id:
            raise PostizIntegrationError(f"Postiz 응답에 post_id가 없습니다: {payload}")

        return {
            "post_id": post_id,
            "channel": channel,
            "status": "DRAFT",
            "dashboard_url": f"{self.base_url.rsplit('/api', 1)[0]}/launches",
        }
