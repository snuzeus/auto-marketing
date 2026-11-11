from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Literal

import httpx

from src.agents.vision_agent import inspect_image_metadata


class PostizIntegrationError(RuntimeError):
    """Raised when Postiz credentials are missing or the API call fails."""


class PostizClient:
    """Async REST client for self-hosted Postiz (Instagram & X DRAFT creation)."""

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

    def _ensure_configured(self, channel: Literal["instagram", "x_twitter"] | None = None) -> None:
        if not self.base_url or not self.api_key or self.api_key == "pz_live_...":
            msg = (
                "Postiz API가 설정되지 않았습니다 (POSTIZ_BASE_URL, POSTIZ_API_KEY 환경변수를 확인하세요)."
            )
            raise PostizIntegrationError(msg)

        if channel == "instagram" and (
            not self.instagram_channel_id or self.instagram_channel_id == "ch_insta_01"
        ):
            msg = "POSTIZ_INSTAGRAM_CHANNEL_ID 환경변수가 실제 채널 ID로 설정되지 않았습니다."
            raise PostizIntegrationError(msg)

        if channel == "x_twitter" and (
            not self.x_channel_id or self.x_channel_id == "ch_x_01"
        ):
            msg = "POSTIZ_X_CHANNEL_ID 환경변수가 실제 채널 ID로 설정되지 않았습니다."
            raise PostizIntegrationError(msg)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def upload_media(self, file_path: str, alt_text: str) -> dict[str, Any]:
        """Upload media binary to `POST /media` and return media metadata."""
        self._ensure_configured()
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"업로드할 이미지 파일이 존재하지 않습니다: {file_path}")

        meta = inspect_image_metadata(path)
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "image/jpeg"

        files = {"file": (path.name, path.read_bytes(), mime_type)}
        data = {"alt": alt_text}

        try:
            if self._http is not None:
                resp = await self._http.post(
                    f"{self.base_url}/media",
                    headers=self._headers(),
                    files=files,
                    data=data,
                )
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self.base_url}/media",
                        headers=self._headers(),
                        files=files,
                        data=data,
                    )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            msg = f"Postiz 미디어 업로드 실패 ({self.base_url}/media): {exc}"
            raise PostizIntegrationError(msg) from exc

        media_id = str(payload.get("id") or payload.get("media_id") or "")
        media_url = str(payload.get("path") or payload.get("media_url") or "")
        if not media_id:
            raise PostizIntegrationError(f"Postiz 응답에 media_id가 없습니다: {payload}")

        return {
            "media_id": media_id,
            "media_url": media_url,
            "aspect_ratio": meta.aspect_ratio_label,
        }

    async def create_draft_post(
        self,
        channel: Literal["instagram", "x_twitter"],
        content: str,
        media_ids: list[str],
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a post in strict `DRAFT` status on Postiz (`POST /posts`)."""
        self._ensure_configured(channel=channel)
        channel_id = (
            self.instagram_channel_id if channel == "instagram" else self.x_channel_id
        )

        body: dict[str, Any] = {
            "type": "draft",
            "status": "DRAFT",
            "channelId": channel_id,
            "platform": channel,
            "content": content,
            "mediaIds": media_ids,
        }
        if scheduled_at:
            body["scheduledAt"] = scheduled_at

        try:
            if self._http is not None:
                resp = await self._http.post(
                    f"{self.base_url}/posts",
                    headers=self._headers(),
                    json=body,
                )
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self.base_url}/posts",
                        headers=self._headers(),
                        json=body,
                    )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            msg = f"Postiz DRAFT 생성 실패 ({channel}): {exc}"
            raise PostizIntegrationError(msg) from exc

        post_id = str(payload.get("id") or payload.get("post_id") or "")
        if not post_id:
            raise PostizIntegrationError(f"Postiz 응답에 post_id가 없습니다: {payload}")

        dashboard_url = str(
            payload.get("dashboard_url") or f"{self.base_url.rsplit('/api', 1)[0]}/launches"
        )
        return {
            "post_id": post_id,
            "channel": channel,
            "status": "DRAFT",
            "dashboard_url": dashboard_url,
        }
