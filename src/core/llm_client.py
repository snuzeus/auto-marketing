from __future__ import annotations

import hashlib
import io
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
ThinkingLevel = Literal["low", "medium", "high"]
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class LLMConfigurationError(RuntimeError):
    """Raised when GEMINI_API_KEY is missing or placeholder."""


class LLMGenerationError(RuntimeError):
    """Raised when Gemini API returns an empty or invalid structured response."""


MAX_VISION_IMAGE_SIDE = 2048


def load_image_bytes_for_model(path: Path, max_side: int = MAX_VISION_IMAGE_SIDE) -> tuple[bytes, str]:
    """
    모델에 보낼 이미지 바이트와 MIME 타입을 돌려준다. 긴 변이 `max_side`를 넘는 사진은
    비율을 유지한 채 줄여 JPEG로 다시 인코딩한다 (원본 파일은 건드리지 않는다).
    """
    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        mime_type = "image/jpeg"
    raw = path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if max(img.size) <= max_side:
                return raw, mime_type
            resized = img.convert("RGB")
            resized.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=90)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        # 축소에 실패하면 원본을 그대로 보낸다.
        return raw, mime_type


def _map_thinking_level(level: ThinkingLevel) -> types.ThinkingLevel:
    mapping: dict[ThinkingLevel, types.ThinkingLevel] = {
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
    }
    return mapping.get(level, types.ThinkingLevel.LOW)


class GeminiLLMClient:
    """Async wrapper around the official `google-genai` SDK (`genai.Client.aio`)."""

    def __init__(
        self,
        api_key: str,
        main_model: str = "gemini-3.8-flash",
        lite_model: str = "gemini-3.5-flash-lite",
        default_thinking_level: ThinkingLevel = "low",
        client: genai.Client | None = None,
    ) -> None:
        cleaned_key = api_key.strip()
        if not cleaned_key or cleaned_key.startswith("AIzaSyYourGoogleGeminiApiKey"):
            msg = (
                "유효한 GEMINI_API_KEY 환경변수가 설정되지 않았습니다. "
                "서버 환경변수(.env)에 실제 API 키를 설정하세요."
            )
            raise LLMConfigurationError(msg)
        self.api_key = cleaned_key
        self.main_model = main_model
        self.lite_model = lite_model
        self.default_thinking_level: ThinkingLevel = default_thinking_level
        self._client = client if client is not None else genai.Client(api_key=self.api_key)
        self._persona_cache_map: dict[str, str] = {}

    @property
    def aio(self) -> Any:
        return self._client.aio

    async def create_or_get_persona_cache(
        self,
        model: str,
        system_instruction: str,
        personas_yaml_raw: str,
        ttl: str = "3600s",
    ) -> str | None:
        """
        Create or retrieve a cached content handle for `personas.yaml`.
        If the payload is smaller than Gemini's minimum caching token threshold or caching
        is unsupported by the model/tier, gracefully returns None so callers fall back
        to inline system_instruction without failing the request.
        """
        raw_key_data = f"{model}:{system_instruction}:{personas_yaml_raw}".encode()
        cache_key = hashlib.sha256(raw_key_data).hexdigest()[:16]
        if cache_key in self._persona_cache_map:
            return self._persona_cache_map[cache_key]

        try:
            cached = await self._client.aio.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    display_name=f"personas_cache_{cache_key}",
                    system_instruction=system_instruction,
                    contents=[personas_yaml_raw],
                    ttl=ttl,
                ),
            )
            if cached and cached.name:
                self._persona_cache_map[cache_key] = cached.name
                return cached.name
        except Exception:
            # Fallback to non-cached inline system instruction when token count < minimum cache size
            return None
        return None

    async def analyze_image_structured(
        self,
        image_path: str,
        system_instruction: str,
        user_prompt: str,
        response_model: type[T],
    ) -> T:
        """Analyze an image file with `main_model` and return a validated Pydantic model."""
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"분석할 이미지 파일을 찾을 수 없습니다: {image_path}")

        image_bytes, mime_type = load_image_bytes_for_model(path)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_model,
            thinking_config=types.ThinkingConfig(
                thinking_level=_map_thinking_level(self.default_thinking_level)
            ),
        )

        user_content = types.Content(
            role="user",
            parts=[image_part, types.Part.from_text(text=user_prompt)],
        )
        response = await self._client.aio.models.generate_content(
            model=self.main_model,
            contents=user_content,
            config=config,
        )
        return self._parse_structured_response(response, response_model)

    async def generate_structured(
        self,
        model: str,
        system_instruction: str,
        user_content: str,
        response_model: type[T],
        thinking_level: ThinkingLevel = "low",
        cached_content_name: str | None = None,
    ) -> T:
        """Generate structured JSON output validated against `response_model`."""
        config = types.GenerateContentConfig(
            system_instruction=None if cached_content_name else system_instruction,
            cached_content=cached_content_name,
            response_mime_type="application/json",
            response_schema=response_model,
            thinking_config=types.ThinkingConfig(
                thinking_level=_map_thinking_level(thinking_level)
            ),
        )

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=user_content,
            config=config,
        )
        return self._parse_structured_response(response, response_model)

    async def run_mcp_tool_loop(
        self,
        model: str,
        system_instruction: str,
        prompt: str,
        tools: list[types.Tool],
        tool_executor: ToolExecutor,
        max_turns: int = 8,
    ) -> list[dict[str, Any]]:
        """Execute an async Gemini function-calling loop and return executed tool call records."""
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        executed_calls: list[dict[str, Any]] = []

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            thinking_config=types.ThinkingConfig(
                thinking_level=_map_thinking_level(self.default_thinking_level)
            ),
        )

        for _ in range(max_turns):
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

            function_calls = getattr(response, "function_calls", None)
            if not function_calls:
                break

            candidates = getattr(response, "candidates", None)
            if candidates and candidates[0].content:
                contents.append(candidates[0].content)

            response_parts: list[types.Part] = []
            for fn_call in function_calls:
                fn_name = fn_call.name or ""
                fn_args: dict[str, Any] = dict(fn_call.args) if fn_call.args else {}
                maybe_coro = tool_executor(fn_name, fn_args)
                if isinstance(maybe_coro, Awaitable):
                    result = await maybe_coro
                else:
                    result = maybe_coro

                executed_calls.append(
                    {
                        "tool_name": fn_name,
                        "arguments": fn_args,
                        "result": result,
                    }
                )
                response_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response=result,
                    )
                )

            contents.append(types.Content(role="user", parts=response_parts))

        return executed_calls

    @staticmethod
    def _parse_structured_response(response: Any, response_model: type[T]) -> T:
        """Validate Gemini response into the target Pydantic v2 model."""
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, response_model):
            return parsed
        if isinstance(parsed, dict):
            return response_model.model_validate(parsed)

        raw_text = getattr(response, "text", None)
        if not raw_text or not isinstance(raw_text, str):
            raise LLMGenerationError(
                f"모델 응답이 비어 있거나 텍스트를 추출할 수 없습니다: {response!r}"
            )
        return response_model.model_validate_json(raw_text)
