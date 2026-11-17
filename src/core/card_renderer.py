from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.core.schema import CardNewsBoxSpec, CardNewsSpec, CardThemeType

FONT_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgun.ttf",
]

FONT_REGULAR_CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
]

CATEGORY_HEADERS: dict[str, tuple[str, str]] = {
    "tcg_product_release": ("신상품 판매 안내", "NEW RELEASE & STOCK NOTICE"),
    "tcg_tournament": ("대회 참가 안내", "OFFICIAL TOURNAMENT NOTICE"),
    "tcg_weekly_schedule": ("주간 대회 일정", "WEEKLY TOURNAMENT SCHEDULE"),
    "tcg_tournament_result": ("대회 입상 결과", "TOURNAMENT RESULTS & DECK"),
    "tcg_store_schedule": ("매장 운영 안내", "STORE HOURS & DUEL SPACE"),
    "schedule_change": ("일정 변경 안내", "SCHEDULE CHANGE NOTICE"),
    "urgent": ("긴급 공지 안내", "URGENT STORE NOTICE"),
    "event_open": ("이벤트 진행 안내", "SPECIAL EVENT NOTICE"),
    "general": ("매장 공지 안내", "OFFICIAL STORE NOTICE"),
}

THEME_PALETTES: dict[CardThemeType, dict[str, Any]] = {
    "cyber_cyan": {
        "is_light": False,
        "bg_top": (7, 13, 27),
        "bg_mid": (14, 27, 56),
        "bg_bottom": (7, 12, 24),
        "glow_primary": (14, 116, 180, 75),
        "glow_secondary": (14, 70, 140, 55),
        "frame_outer": (30, 88, 148),
        "frame_inner": (22, 62, 108),
        "accent": (56, 189, 248),
        "banner_left": (0, 218, 245),
        "banner_right": (56, 208, 255),
        "banner_text": (7, 14, 28, 255),
        "box_bg": (9, 17, 34, 215),
        "box_header": (18, 42, 82, 235),
        "box_rim": (56, 215, 255),
        "box_border": (42, 92, 152, 255),
        "box_divider": (56, 189, 248, 170),
        "col_divider": (56, 110, 175, 190),
        "title_color": (255, 255, 255, 255),
        "title_glow": (56, 224, 255, 165),
        "label_color": (56, 224, 255, 255),
        "value_color": (248, 250, 252, 255),
        "footnote_color": (203, 213, 225, 255),
        "brand_color": (226, 232, 240, 255),
        "dot_inactive": (148, 163, 184, 175),
        "dot_active": (255, 255, 255, 255),
    },
    "crimson_red": {
        "is_light": False,
        "bg_top": (20, 7, 12),
        "bg_mid": (46, 14, 24),
        "bg_bottom": (16, 6, 10),
        "glow_primary": (190, 24, 60, 75),
        "glow_secondary": (136, 19, 55, 55),
        "frame_outer": (159, 18, 57),
        "frame_inner": (105, 16, 42),
        "accent": (251, 113, 133),
        "banner_left": (225, 29, 72),
        "banner_right": (251, 113, 133),
        "banner_text": (255, 255, 255, 255),
        "box_bg": (28, 10, 16, 220),
        "box_header": (76, 18, 34, 235),
        "box_rim": (251, 113, 133),
        "box_border": (159, 40, 68, 255),
        "box_divider": (251, 113, 133, 170),
        "col_divider": (180, 60, 88, 190),
        "title_color": (255, 255, 255, 255),
        "title_glow": (244, 63, 94, 175),
        "label_color": (253, 164, 175, 255),
        "value_color": (255, 241, 242, 255),
        "footnote_color": (226, 203, 210, 255),
        "brand_color": (241, 226, 230, 255),
        "dot_inactive": (184, 148, 158, 175),
        "dot_active": (255, 255, 255, 255),
    },
    "royal_gold": {
        "is_light": False,
        "bg_top": (15, 13, 8),
        "bg_mid": (38, 30, 14),
        "bg_bottom": (12, 10, 6),
        "glow_primary": (180, 125, 18, 70),
        "glow_secondary": (120, 82, 12, 50),
        "frame_outer": (161, 118, 28),
        "frame_inner": (108, 78, 18),
        "accent": (251, 191, 36),
        "banner_left": (217, 148, 16),
        "banner_right": (251, 191, 36),
        "banner_text": (22, 16, 4, 255),
        "box_bg": (22, 18, 10, 220),
        "box_header": (62, 46, 16, 235),
        "box_rim": (251, 191, 36),
        "box_border": (161, 118, 28, 255),
        "box_divider": (251, 191, 36, 170),
        "col_divider": (180, 138, 45, 190),
        "title_color": (255, 255, 255, 255),
        "title_glow": (245, 158, 11, 165),
        "label_color": (253, 224, 71, 255),
        "value_color": (255, 251, 235, 255),
        "footnote_color": (226, 218, 198, 255),
        "brand_color": (245, 238, 215, 255),
        "dot_inactive": (184, 168, 130, 175),
        "dot_active": (255, 255, 255, 255),
    },
    "emerald_green": {
        "is_light": False,
        "bg_top": (6, 20, 16),
        "bg_mid": (12, 46, 36),
        "bg_bottom": (5, 16, 13),
        "glow_primary": (16, 165, 115, 70),
        "glow_secondary": (10, 110, 78, 50),
        "frame_outer": (20, 130, 95),
        "frame_inner": (16, 88, 65),
        "accent": (52, 211, 153),
        "banner_left": (16, 185, 129),
        "banner_right": (52, 211, 153),
        "banner_text": (4, 24, 16, 255),
        "box_bg": (8, 26, 20, 220),
        "box_header": (16, 62, 46, 235),
        "box_rim": (52, 211, 153),
        "box_border": (32, 135, 100, 255),
        "box_divider": (52, 211, 153, 170),
        "col_divider": (45, 150, 112, 190),
        "title_color": (255, 255, 255, 255),
        "title_glow": (16, 185, 129, 165),
        "label_color": (110, 231, 183, 255),
        "value_color": (236, 253, 245, 255),
        "footnote_color": (200, 226, 216, 255),
        "brand_color": (220, 242, 232, 255),
        "dot_inactive": (140, 180, 165, 175),
        "dot_active": (255, 255, 255, 255),
    },
    "violet_purple": {
        "is_light": False,
        "bg_top": (15, 10, 28),
        "bg_mid": (36, 20, 66),
        "bg_bottom": (12, 8, 22),
        "glow_primary": (139, 92, 246, 75),
        "glow_secondary": (91, 33, 182, 55),
        "frame_outer": (109, 40, 217),
        "frame_inner": (76, 29, 149),
        "accent": (192, 132, 252),
        "banner_left": (147, 51, 234),
        "banner_right": (192, 132, 252),
        "banner_text": (255, 255, 255, 255),
        "box_bg": (18, 12, 34, 220),
        "box_header": (48, 28, 86, 235),
        "box_rim": (192, 132, 252),
        "box_border": (126, 58, 212, 255),
        "box_divider": (192, 132, 252, 170),
        "col_divider": (140, 82, 215, 190),
        "title_color": (255, 255, 255, 255),
        "title_glow": (168, 85, 247, 170),
        "label_color": (216, 180, 254, 255),
        "value_color": (250, 245, 255, 255),
        "footnote_color": (218, 206, 232, 255),
        "brand_color": (235, 226, 245, 255),
        "dot_inactive": (168, 150, 190, 175),
        "dot_active": (255, 255, 255, 255),
    },
    "minimal_light": {
        "is_light": True,
        "bg_top": (250, 251, 253),
        "bg_mid": (244, 247, 251),
        "bg_bottom": (248, 250, 252),
        "glow_primary": (219, 234, 254, 0),
        "glow_secondary": (219, 234, 254, 0),
        "frame_outer": (30, 41, 59),
        "frame_inner": (203, 213, 225),
        "accent": (15, 23, 42),
        "banner_left": (224, 242, 254),
        "banner_right": (224, 242, 254),
        "banner_text": (15, 23, 42, 255),
        "box_bg": (255, 255, 255, 255),
        "box_header": (241, 245, 249, 255),
        "box_rim": (15, 23, 42),
        "box_border": (15, 23, 42, 255),
        "box_divider": (203, 213, 225, 255),
        "col_divider": (203, 213, 225, 255),
        "title_color": (15, 23, 42, 255),
        "title_glow": (0, 0, 0, 0),
        "label_color": (30, 64, 175, 255),
        "value_color": (15, 23, 42, 255),
        "footnote_color": (71, 85, 105, 255),
        "brand_color": (51, 65, 85, 255),
        "dot_inactive": (203, 213, 225, 255),
        "dot_active": (15, 23, 42, 255),
    },
}

_EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u27bf\ufe00-\ufe0f\u200d\u20e3]"
)
_BRACKET_KV_PATTERN = re.compile(r"^\[([^\[\]]{1,18})\]\s*(.*)$")
_COLON_KV_PATTERN = re.compile(r"^([^:：|]{1,14})\s*[:：|]\s*(.+)$")
_GREETING_PATTERN = re.compile(
    r"^(안녕하세요|반갑습니다|감사합니다|고맙습니다|부탁드립니다|문의\s*부탁|.*드림\.?$|.*올림\.?$)"
)
_VERBOSE_ENDING_PATTERN = re.compile(
    r"\s*(?:예정입니다|해주시기 바랍니다|해\s*주시기 바랍니다|참고하시기 바랍니다|주시기 바랍니다|바랍니다|드립니다|입니다|됩니다|합니다|있습니다)\.?\s*$"
)


@dataclass
class NoticeBoxItem:
    """Structured HUD box entry displayed on the second notice carousel slide."""

    label: str
    value: str
    columns: list[str] | None = None


def _load_korean_font(
    size: int,
    *,
    bold: bool = True,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_REGULAR_CANDIDATES
    for path_str in candidates:
        p = Path(path_str)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _strip_emojis_and_decorations(text: str) -> str:
    """Remove emojis/variation selectors unsupported by Korean Gothic TTF fonts."""
    cleaned = _EMOJI_PATTERN.sub("", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def _simplify_box_value(text: str) -> str:
    """Shorten narrative filler/endings into concise keyword phrases for crisp cards."""
    cleaned = _strip_emojis_and_decorations(text)
    if not cleaned:
        return ""
    lines = []
    for raw_line in cleaned.splitlines():
        part = _VERBOSE_ENDING_PATTERN.sub("", raw_line.strip()).strip(" ,.;")
        if part:
            lines.append(part)
    return "\n".join(lines) if lines else cleaned


def _split_columns(value: str) -> list[str] | None:
    for sep in (" / ", " | "):
        if sep in value:
            parts = [p.strip() for p in value.split(sep) if p.strip()]
            if len(parts) == 2 and all(2 <= len(p) <= 28 for p in parts):
                return parts
    return None


def _is_footnote_line(line: str) -> bool:
    if line.startswith(("*", "※")):
        return True
    lower = line.lower()
    return lower.startswith(("당부:", "유의사항:", "참고:", "안내:", "주의:"))


def _is_generic_greeting_or_closing(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return bool(_GREETING_PATTERN.match(stripped)) and ":" not in stripped and "：" not in stripped


def _extract_items_from_lines(raw_lines: list[str]) -> tuple[list[NoticeBoxItem], list[str], list[str]]:
    items: list[NoticeBoxItem] = []
    footnotes: list[str] = []
    fallback_lines: list[str] = []
    pending_bracket_label: str | None = None

    for raw in raw_lines:
        clean = _strip_emojis_and_decorations(raw)
        if not clean:
            continue

        if _is_footnote_line(clean):
            note = clean if clean.startswith("*") else f"* {clean.lstrip('※ ')}"
            footnotes.append(note)
            pending_bracket_label = None
            continue

        stripped_bullet = re.sub(r"^[-•▪·▶►✓✔]+\s*", "", clean).strip()
        if not stripped_bullet:
            continue

        bracket_match = _BRACKET_KV_PATTERN.match(stripped_bullet)
        if bracket_match:
            lbl = bracket_match.group(1).strip()
            raw_val = bracket_match.group(2).strip()
            val = _simplify_box_value(raw_val)
            if val:
                items.append(NoticeBoxItem(label=lbl, value=val, columns=_split_columns(val)))
                pending_bracket_label = None
            else:
                pending_bracket_label = lbl
            continue

        kv_match = _COLON_KV_PATTERN.match(stripped_bullet)
        if kv_match:
            lbl = kv_match.group(1).strip()
            raw_val = kv_match.group(2).strip()
            val = _simplify_box_value(raw_val)
            if 1 <= len(lbl) <= 14 and not lbl.endswith(("다.", "요.", "니다", "습니다")) and val:
                items.append(NoticeBoxItem(label=lbl, value=val, columns=_split_columns(val)))
                pending_bracket_label = None
                continue

        if pending_bracket_label is not None:
            short_line = _simplify_box_value(stripped_bullet)
            if short_line:
                if items and items[-1].label == pending_bracket_label:
                    combined = f"{items[-1].value}\n{short_line}"
                    items[-1].value = combined
                    items[-1].columns = None
                else:
                    items.append(
                        NoticeBoxItem(
                            label=pending_bracket_label,
                            value=short_line,
                            columns=_split_columns(short_line),
                        )
                    )
            continue

        if not _is_generic_greeting_or_closing(stripped_bullet):
            short_fb = _simplify_box_value(stripped_bullet)
            if short_fb:
                fallback_lines.append(short_fb)

    return items, footnotes, fallback_lines


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", "", label).lower()


def parse_notice_card_structure(
    body: str,
    user_prompt: str = "",
    *,
    notice_category: str = "general",
) -> tuple[list[NoticeBoxItem], list[str]]:
    """Parse notice body and merge concise missing `[항목]` facts from user_prompt."""
    items, footnotes, fallback_lines = _extract_items_from_lines(body.splitlines())

    if user_prompt.strip():
        prompt_items, prompt_notes, _ = _extract_items_from_lines(user_prompt.splitlines())
        if not items and prompt_items:
            items = prompt_items
        elif prompt_items:
            existing_labels = {_norm_label(it.label) for it in items}
            missing = [pi for pi in prompt_items if _norm_label(pi.label) not in existing_labels]
            if missing:
                items = items + missing
        if not footnotes and prompt_notes:
            footnotes = prompt_notes

    if not items:
        if fallback_lines:
            concise = [ln[:45] for ln in fallback_lines[:2]]
            items = [NoticeBoxItem(label="주요 안내", value="\n".join(concise))]
        else:
            cleaned_body = _simplify_box_value(body) or "매장 공지 참고"
            items = [NoticeBoxItem(label="공지 안내", value=cleaned_body[:60])]

    max_boxes = 6 if notice_category == "tcg_weekly_schedule" else 5
    return items[:max_boxes], footnotes[:3]


def build_default_card_news_spec(
    headline: str,
    body: str,
    *,
    notice_category: str = "general",
    user_prompt: str = "",
    brand_label: str = "OFFICIAL TCG STORE",
    theme: CardThemeType = "cyber_cyan",
) -> CardNewsSpec:
    """Build an editable, simplified `CardNewsSpec` from draft copy and user prompt."""
    main_title, default_sub = CATEGORY_HEADERS.get(
        notice_category,
        ("매장 공지 안내", "OFFICIAL STORE NOTICE"),
    )
    clean_headline = _simplify_box_value(headline)
    subtitle_banner = clean_headline if clean_headline and len(clean_headline) <= 44 else default_sub

    box_items, footnotes = parse_notice_card_structure(
        body=body,
        user_prompt=user_prompt,
        notice_category=notice_category,
    )
    spec_boxes = [CardNewsBoxSpec(label=b.label, value=b.value) for b in box_items]
    clean_brand = _strip_emojis_and_decorations(brand_label) or "OFFICIAL TCG STORE"

    return CardNewsSpec(
        main_title=main_title,
        subtitle_banner=subtitle_banner,
        boxes=spec_boxes,
        footnotes=footnotes,
        footer_brand=clean_brand,
        theme=theme,
    )


def _draw_vertical_gradient(
    width: int,
    height: int,
    top_color: tuple[int, int, int],
    mid_color: tuple[int, int, int],
    bottom_color: tuple[int, int, int],
) -> Image.Image:
    base = Image.new("RGBA", (width, height))
    px = base.load()
    if px is None:
        return base
    half = max(1, height // 2)
    for y in range(height):
        if y < half:
            t = y / half
            c = tuple(int(top_color[i] + (mid_color[i] - top_color[i]) * t) for i in range(3))
        else:
            t = (y - half) / max(1, height - half)
            c = tuple(int(mid_color[i] + (bottom_color[i] - mid_color[i]) * t) for i in range(3))
        row_color = (c[0], c[1], c[2], 255)
        for x in range(width):
            px[x, y] = row_color
    return base


def _draw_ambient_glow(canvas: Image.Image, palette: dict[str, Any]) -> Image.Image:
    if palette["is_light"]:
        return canvas
    width, height = canvas.size
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse(
        [(width // 2 - 420, 40), (width // 2 + 420, 760)],
        fill=palette["glow_primary"],
    )
    gdraw.ellipse(
        [(width // 2 - 360, 520), (width // 2 + 360, 1180)],
        fill=palette["glow_secondary"],
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=95))
    return Image.alpha_composite(canvas, glow)


def _draw_outer_frame(draw: ImageDraw.ImageDraw, width: int, height: int, palette: dict[str, Any]) -> None:
    if palette["is_light"]:
        draw.rounded_rectangle(
            [(36, 36), (width - 36, height - 36)],
            radius=28,
            outline=palette["frame_outer"],
            width=4,
        )
        return

    m = 38
    cut = 28
    outer_pts = [
        (m + cut, m),
        (width - m - cut, m),
        (width - m, m + cut),
        (width - m, height - m - cut),
        (width - m - cut, height - m),
        (m + cut, height - m),
        (m, height - m - cut),
        (m, m + cut),
    ]
    draw.polygon(outer_pts, outline=palette["frame_outer"], width=2)

    m2 = m + 12
    cut2 = 20
    inner_pts = [
        (m2 + cut2, m2),
        (width - m2 - cut2, m2),
        (width - m2, m2 + cut2),
        (width - m2, height - m2 - cut2),
        (width - m2 - cut2, height - m2),
        (m2 + cut2, height - m2),
        (m2, height - m2 - cut2),
        (m2, m2 + cut2),
    ]
    draw.polygon(inner_pts, outline=palette["frame_inner"], width=1)

    accent = palette["accent"]
    arm = 54
    corners = [
        [(m, m + arm), (m, m + cut), (m + cut, m), (m + arm, m)],
        [(width - m - arm, m), (width - m - cut, m), (width - m, m + cut), (width - m, m + arm)],
        [
            (width - m, height - m - arm),
            (width - m, height - m - cut),
            (width - m - cut, height - m),
            (width - m - arm, height - m),
        ],
        [(m + arm, height - m), (m + cut, height - m), (m, height - m - cut), (m, height - m - arm)],
    ]
    for pts in corners:
        draw.line(pts, fill=accent, width=3)

    cx = width // 2
    for cy in (m, height - m):
        diamond = [(cx, cy - 7), (cx + 10, cy), (cx, cy + 7), (cx - 10, cy)]
        draw.polygon(diamond, fill=accent)


def _measure_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])


def _fit_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        para = paragraph.strip()
        if not para:
            continue
        current = ""
        for ch in para:
            candidate = current + ch
            w, _ = _measure_text(draw, candidate, font)
            if w <= max_width or not current:
                current = candidate
            else:
                lines.append(current.rstrip())
                current = ch.lstrip()
        if current:
            lines.append(current.rstrip())

    if not lines:
        return [text.strip()]
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        last = kept[-1]
        while len(last) > 1 and _measure_text(draw, f"{last}...", font)[0] > max_width:
            last = last[:-1]
        kept[-1] = f"{last.rstrip()}..."
        return kept
    return lines


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
) -> None:
    tw, _ = _measure_text(draw, text, font)
    draw.text((center_x - tw // 2, y), text, font=font, fill=fill)


def _draw_banner_strip(
    canvas: Image.Image,
    y: int,
    height: int,
    banner_text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    palette: dict[str, Any],
) -> None:
    width = canvas.width
    clean_banner = _strip_emojis_and_decorations(banner_text) or "OFFICIAL NOTICE"
    if palette["is_light"]:
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            [(140, y), (width - 140, y + height)],
            radius=16,
            fill=palette["banner_left"],
            outline=(15, 23, 42, 255),
            width=3,
        )
        fitted = _fit_text_lines(draw, clean_banner, font, max_width=width - 320, max_lines=1)[0]
        _, th = _measure_text(draw, fitted, font)
        _draw_centered_text(
            draw, width // 2, y + max(4, (height - th) // 2 - 2), fitted, font, palette["banner_text"]
        )
        return

    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    px = strip.load()
    if px is not None:
        left_pad = 96
        right_pad = width - 96
        fade_w = 130
        c1 = palette["banner_left"]
        c2 = palette["banner_right"]
        for x in range(left_pad, right_pad):
            if x < left_pad + fade_w:
                alpha = int(235 * (x - left_pad) / fade_w)
            elif x > right_pad - fade_w:
                alpha = int(235 * (right_pad - x) / fade_w)
            else:
                alpha = 235
            t = (x - left_pad) / max(1, right_pad - left_pad)
            r = int(c1[0] + (c2[0] - c1[0]) * t)
            g = int(c1[1] + (c2[1] - c1[1]) * t)
            b = int(c1[2] + (c2[2] - c1[2]) * t)
            for row in range(height):
                px[x, row] = (r, g, b, alpha)
    canvas.alpha_composite(strip, (0, y))

    draw = ImageDraw.Draw(canvas)
    fitted = _fit_text_lines(draw, clean_banner, font, max_width=width - 260, max_lines=1)[0]
    _, th = _measure_text(draw, fitted, font)
    _draw_centered_text(
        draw, width // 2, y + max(4, (height - th) // 2 - 2), fitted, font, palette["banner_text"]
    )


def _draw_centered_text_block(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    top_y: int,
    area_h: int,
    max_w: int,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_lines: int,
    fill: tuple[int, int, int, int],
) -> None:
    lines = _fit_text_lines(
        draw,
        _strip_emojis_and_decorations(text),
        font,
        max_width=max_w,
        max_lines=max_lines,
    )
    font_size = int(getattr(font, "size", 30))
    line_h = font_size + 10
    total_h = len(lines) * line_h
    cur_y = top_y + max(8, (area_h - total_h) // 2 - 2)
    for line in lines:
        _draw_centered_text(draw, center_x, cur_y, line, font, fill)
        cur_y += line_h


def _draw_info_box(
    canvas: Image.Image,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    box: NoticeBoxItem,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    value_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    palette: dict[str, Any],
) -> None:
    box_w = x2 - x1
    box_h = y2 - y1
    header_h = min(56, max(44, int(box_h * 0.34)))

    if palette["is_light"]:
        draw = ImageDraw.Draw(canvas)
        # Clean rounded card inspired by diagram boxes
        draw.rounded_rectangle([(x1 + 5, y1 + 6), (x2 + 5, y2 + 6)], radius=18, fill=(226, 232, 240, 255))
        draw.rounded_rectangle(
            [(x1, y1), (x2, y2)],
            radius=18,
            fill=palette["box_bg"],
            outline=palette["box_border"],
            width=3,
        )
        draw.line([(x1 + 16, y1 + header_h), (x2 - 16, y1 + header_h)], fill=palette["box_divider"], width=2)
    else:
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.rectangle([(x1, y1), (x2, y2)], fill=palette["box_bg"])
        odraw.rectangle([(x1, y1), (x2, y1 + header_h)], fill=palette["box_header"])

        px = overlay.load()
        rim = palette["box_rim"]
        if px is not None:
            for px_x in range(x1, x2 + 1):
                t = (px_x - x1) / max(1, box_w)
                wave = 1.0 - abs(2.0 * t - 1.0) * 0.45
                rim_color = (int(rim[0] * wave), int(rim[1] * wave), int(rim[2] * wave), 245)
                for dy in range(3):
                    if y1 + dy < canvas.height:
                        px[px_x, y1 + dy] = rim_color

        canvas.alpha_composite(overlay)
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([(x1, y1), (x2, y2)], outline=palette["box_border"], width=2)
        draw.line([(x1, y1 + header_h), (x2, y1 + header_h)], fill=palette["box_divider"], width=2)

    clean_label = _strip_emojis_and_decorations(box.label) or "안내"
    _, lh = _measure_text(draw, clean_label, label_font)
    _draw_centered_text(
        draw,
        (x1 + x2) // 2,
        y1 + max(4, (header_h - lh) // 2 - 2),
        clean_label,
        label_font,
        palette["label_color"],
    )

    body_y1 = y1 + header_h
    body_h = y2 - body_y1
    val_fill = palette["value_color"]

    if box.columns and len(box.columns) == 2:
        mid_x = (x1 + x2) // 2
        draw.line([(mid_x, body_y1 + 14), (mid_x, y2 - 14)], fill=palette["col_divider"], width=2)
        col_max_w = (box_w // 2) - 48
        centers = [(x1 + mid_x) // 2, (mid_x + x2) // 2]
        for idx, col_text in enumerate(box.columns):
            _draw_centered_text_block(
                draw, centers[idx], body_y1, body_h, col_max_w, col_text, value_font, max_lines=2, fill=val_fill
            )
    else:
        max_lines = 2 if body_h < 130 else 3
        _draw_centered_text_block(
            draw,
            (x1 + x2) // 2,
            body_y1,
            body_h,
            box_w - 72,
            box.value,
            value_font,
            max_lines=max_lines,
            fill=val_fill,
        )


def _draw_footer_and_pagination(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    brand_label: str,
    brand_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    palette: dict[str, Any],
) -> None:
    clean_brand = _strip_emojis_and_decorations(brand_label) or "OFFICIAL TCG STORE"
    footer_text = f"{clean_brand}   ×   NOTICE"
    _draw_centered_text(draw, width // 2, height - 134, footer_text, brand_font, palette["brand_color"])

    dot_y = height - 76
    cx = width // 2
    draw.ellipse([(cx - 22, dot_y - 6), (cx - 10, dot_y + 6)], fill=palette["dot_inactive"])
    draw.ellipse([(cx + 8, dot_y - 8), (cx + 24, dot_y + 8)], fill=palette["dot_active"])


def render_notice_card_png(
    headline: str,
    body: str,
    category_badge: str = "TCG STORE NOTICE",
    source_image_path: str | Path | None = None,
    width: int = 1080,
    height: int = 1350,
    *,
    notice_category: str = "general",
    user_prompt: str = "",
    brand_label: str = "OFFICIAL TCG STORE",
    include_source_thumbnail: bool = False,
    card_news: CardNewsSpec | None = None,
) -> bytes:
    """
    Render Slide 2 (4:5, 1080x1350 PNG) simplified notice card supporting custom `CardNewsSpec`
    and 6 themes (`cyber_cyan`, `crimson_red`, `royal_gold`, `emerald_green`, `violet_purple`, `minimal_light`).
    """
    if card_news is not None:
        title_text = _strip_emojis_and_decorations(card_news.main_title) or "매장 공지 안내"
        banner_str = _strip_emojis_and_decorations(card_news.subtitle_banner) or "OFFICIAL NOTICE"
        boxes = [
            NoticeBoxItem(
                label=_strip_emojis_and_decorations(b.label) or "안내",
                value=_strip_emojis_and_decorations(b.value),
                columns=_split_columns(_strip_emojis_and_decorations(b.value)),
            )
            for b in card_news.boxes
        ]
        footnotes = [_strip_emojis_and_decorations(f) for f in card_news.footnotes if f.strip()]
        active_brand = card_news.footer_brand
        theme: CardThemeType = card_news.theme
    else:
        spec = build_default_card_news_spec(
            headline=headline or category_badge,
            body=body,
            notice_category=notice_category,
            user_prompt=user_prompt,
            brand_label=brand_label,
        )
        title_text = spec.main_title
        banner_str = spec.subtitle_banner
        boxes = [
            NoticeBoxItem(label=b.label, value=b.value, columns=_split_columns(b.value))
            for b in spec.boxes
        ]
        footnotes = spec.footnotes
        active_brand = spec.footer_brand
        theme = spec.theme

    palette = THEME_PALETTES.get(theme, THEME_PALETTES["cyber_cyan"])

    canvas = _draw_vertical_gradient(
        width=width,
        height=height,
        top_color=palette["bg_top"],
        mid_color=palette["bg_mid"],
        bottom_color=palette["bg_bottom"],
    )
    canvas = _draw_ambient_glow(canvas, palette)

    draw = ImageDraw.Draw(canvas)
    _draw_outer_frame(draw, width, height, palette)

    font_title = _load_korean_font(62, bold=True)
    font_banner = _load_korean_font(28, bold=True)
    font_brand = _load_korean_font(24, bold=True)
    font_note = _load_korean_font(22, bold=False)

    if not palette["is_light"]:
        title_glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        tg_draw = ImageDraw.Draw(title_glow)
        _draw_centered_text(tg_draw, width // 2, 106, title_text, font_title, palette["title_glow"])
        title_glow = title_glow.filter(ImageFilter.GaussianBlur(radius=9))
        canvas = Image.alpha_composite(canvas, title_glow)

    draw = ImageDraw.Draw(canvas)
    _draw_centered_text(draw, width // 2, 106, title_text, font_title, palette["title_color"])

    _draw_banner_strip(canvas, y=194, height=58, banner_text=banner_str, font=font_banner, palette=palette)

    cursor_y = 282
    if include_source_thumbnail and source_image_path and Path(source_image_path).exists():
        try:
            with Image.open(source_image_path) as src_img:
                thumb = src_img.convert("RGBA")
                thumb.thumbnail((width - 220, 180))
                canvas.paste(thumb, ((width - thumb.width) // 2, cursor_y))
                cursor_y += thumb.height + 24
        except Exception:
            pass

    footer_reserve = 175
    footnote_h = (len(footnotes) * 34 + 28) if footnotes else 0
    available_h = max(240, height - footer_reserve - footnote_h - cursor_y)
    gap = 20 if len(boxes) <= 4 else 14
    box_h = min(215, max(112, (available_h - gap * max(0, len(boxes) - 1)) // max(1, len(boxes))))

    font_box_label = _load_korean_font(27 if box_h >= 150 else 23, bold=True)
    font_box_value = _load_korean_font(34 if box_h >= 165 else 27, bold=True)

    box_x1 = 96
    box_x2 = width - 96
    for idx, box in enumerate(boxes):
        y1 = cursor_y + idx * (box_h + gap)
        y2 = y1 + box_h
        if y2 > height - footer_reserve - footnote_h:
            break
        _draw_info_box(
            canvas=canvas,
            x1=box_x1,
            y1=y1,
            x2=box_x2,
            y2=y2,
            box=box,
            label_font=font_box_label,
            value_font=font_box_value,
            palette=palette,
        )

    last_box_y2 = min(
        height - footer_reserve - footnote_h,
        cursor_y + len(boxes) * box_h + max(0, len(boxes) - 1) * gap,
    )

    draw = ImageDraw.Draw(canvas)
    if footnotes:
        note_y = last_box_y2 + 22
        for note in footnotes:
            fitted_note = _fit_text_lines(draw, note, font_note, max_width=box_x2 - box_x1, max_lines=1)[0]
            draw.text((box_x1, note_y), fitted_note, font=font_note, fill=palette["footnote_color"])
            note_y += 32

    _draw_footer_and_pagination(draw, width, height, brand_label=active_brand, brand_font=font_brand, palette=palette)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
