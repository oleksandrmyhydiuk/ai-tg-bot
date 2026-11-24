from __future__ import annotations

from dataclasses import dataclass
import io
import os
import re
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover - handled gracefully at runtime
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


@dataclass
class ParticipantStat:
    user_id: int
    display_name: str
    message_count: int
    avatar_bytes: bytes | None = None


@dataclass
class StatsCardData:
    chat_title: str
    period_label: str
    total_period_messages: int
    total_retained_messages: int
    digest_count: int
    peak_hour_label: str | None
    participants: list[ParticipantStat]


# Theme palette (modern dark slate)
BG_COLOR = (15, 23, 42)          # #0F172A
CARD_BG = (30, 41, 59)           # #1E293B
CARD_BORDER = (51, 65, 85)       # #334155
TRACK_BG = (15, 23, 42)          # #0F172A
ACCENT_CYAN = (56, 189, 248)     # #38BDF8
ACCENT_INDIGO = (129, 140, 248)  # #818CF8
TEXT_PRIMARY = (248, 250, 252)   # #F8FAFC
TEXT_SECONDARY = (148, 163, 184) # #94A3B8
TEXT_MUTED = (100, 116, 139)     # #64748B
TEXT_DARK = (15, 23, 42)         # #0F172A

RANK_BADGE_COLORS: dict[int, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    1: ((250, 204, 21), TEXT_DARK),    # #FACC15 gold
    2: ((203, 213, 225), TEXT_DARK),   # #CBD5E1 silver
    3: ((251, 146, 60), TEXT_DARK),    # #FB923C bronze
}
DEFAULT_RANK_BADGE = ((100, 116, 139), TEXT_PRIMARY)  # #64748B

BAR_COLORS: dict[int, tuple[int, int, int]] = {
    1: (250, 204, 21),   # #FACC15
    2: (56, 189, 248),   # #38BDF8
    3: (129, 140, 248),  # #818CF8
}
DEFAULT_BAR_COLOR = (56, 189, 248)  # #38BDF8

AVATAR_PALETTE: tuple[tuple[int, int, int], ...] = (
    (14, 165, 233),   # sky
    (99, 102, 241),   # indigo
    (168, 85, 247),   # purple
    (236, 72, 153),   # pink
    (244, 63, 94),    # rose
    (249, 115, 22),   # orange
    (16, 185, 129),   # emerald
    (20, 184, 166),   # teal
)

BOLD_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
)

REGULAR_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

CARD_WIDTH = 960
MAX_PARTICIPANTS = 8
AVATAR_SIZE = 56


def _load_font(size: int, *, bold: bool = False) -> Any:
    candidates = BOLD_FONT_CANDIDATES if bold else REGULAR_FONT_CANDIDATES
    for font_path in candidates:
        try:
            if os.path.isabs(font_path) and not os.path.exists(font_path):
                continue
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _get_lanczos() -> Any:
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS", 1)


def _measure_text_width(draw: Any, text: str, font: Any) -> float:
    if hasattr(draw, "textlength"):
        return float(draw.textlength(text, font=font))
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0])


def _truncate_text(draw: Any, text: str, font: Any, max_width: float) -> str:
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if not clean:
        return ""
    if _measure_text_width(draw, clean, font) <= max_width:
        return clean
    ellipsis = "..."
    low, high = 0, len(clean)
    best = ellipsis
    while low <= high:
        mid = (low + high) // 2
        candidate = clean[:mid].rstrip() + ellipsis
        if _measure_text_width(draw, candidate, font) <= max_width:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _extract_initials(display_name: str) -> str:
    clean = re.sub(r"[^\w\s-]", " ", display_name or "", flags=re.UNICODE).strip()
    parts = [p for p in re.split(r"[\s_-]+", clean) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _deterministic_avatar_color(user_id: int, display_name: str) -> tuple[int, int, int]:
    seed = abs(int(user_id)) if user_id else sum(ord(ch) for ch in (display_name or "user"))
    return AVATAR_PALETTE[seed % len(AVATAR_PALETTE)]


def _build_circular_avatar(
    participant: ParticipantStat,
    size: int = AVATAR_SIZE,
) -> Any:
    """Returns an RGBA circular avatar image (from bytes or deterministic initials badge)."""
    lanczos = _get_lanczos()
    scale = 4
    mask_hi = Image.new("L", (size * scale, size * scale), 0)
    mask_draw = ImageDraw.Draw(mask_hi)
    mask_draw.ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    mask = mask_hi.resize((size, size), resample=lanczos)

    if participant.avatar_bytes:
        try:
            with Image.open(io.BytesIO(participant.avatar_bytes)) as src:
                rgba = src.convert("RGBA")
                fitted = ImageOps.fit(rgba, (size, size), method=lanczos)
                out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                out.paste(fitted, (0, 0), mask)
                return out
        except Exception:
            pass

    # Fallback: crisp antialiased circular badge with deterministic vibrant color + initials
    bg_color = _deterministic_avatar_color(participant.user_id, participant.display_name)
    badge_hi = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    badge_draw = ImageDraw.Draw(badge_hi)
    badge_draw.ellipse(
        (0, 0, size * scale - 1, size * scale - 1),
        fill=(*bg_color, 255),
    )
    badge = badge_hi.resize((size, size), resample=lanczos)

    initials = _extract_initials(participant.display_name)
    font = _load_font(22 if len(initials) == 1 else 20, bold=True)
    draw = ImageDraw.Draw(badge)
    bbox = draw.textbbox((0, 0), initials, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size - th) / 2 - bbox[1]
    draw.text((tx, ty), initials, font=font, fill=(*TEXT_PRIMARY, 255))
    return badge


def _draw_horizontal_gradient_bar(
    img: Any,
    box: tuple[int, int, int, int],
    start_rgb: tuple[int, int, int],
    end_rgb: tuple[int, int, int],
    radius: int = 4,
) -> None:
    x0, y0, x1, y1 = box
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)

    grad = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(grad)
    for x in range(width):
        t = x / max(1, width - 1)
        r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * t)
        g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * t)
        b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * t)
        grad_draw.line([(x, 0), (x, height - 1)], fill=(r, g, b, 255))

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    img.paste(grad, (x0, y0), mask)


def render_stats_leaderboard(data: StatsCardData) -> bytes:
    """Renders a modern dark-themed PNG leaderboard card and returns PNG bytes."""
    if Image is None or ImageDraw is None or ImageFont is None or ImageOps is None:
        raise RuntimeError("Pillow is not installed")

    participants = list(data.participants[:MAX_PARTICIPANTS])

    pad_x = 36
    pad_top = 32
    pad_bottom = 28
    content_w = CARD_WIDTH - pad_x * 2

    header_h = 114
    pills_gap_top = 18
    pills_h = 80
    rows_gap_top = 22
    row_h = 84
    row_gap = 12
    footer_h = 28

    if participants:
        rows_block_h = len(participants) * row_h + (len(participants) - 1) * row_gap
    else:
        rows_block_h = 88

    total_h = (
        pad_top
        + header_h
        + pills_gap_top
        + pills_h
        + rows_gap_top
        + rows_block_h
        + 18
        + footer_h
        + pad_bottom
    )

    img = Image.new("RGBA", (CARD_WIDTH, total_h), (*BG_COLOR, 255))
    draw = ImageDraw.Draw(img)

    # Fonts
    font_title = _load_font(30, bold=True)
    font_subtitle = _load_font(17, bold=False)
    font_pill_label = _load_font(14, bold=False)
    font_pill_val = _load_font(22, bold=True)
    font_pill_val_sm = _load_font(17, bold=True)
    font_rank = _load_font(17, bold=True)
    font_name = _load_font(19, bold=True)
    font_count = _load_font(18, bold=True)
    font_pct = _load_font(15, bold=False)
    font_footer = _load_font(13, bold=False)

    # 1. Header card
    hx0, hy0 = pad_x, pad_top
    hx1, hy1 = CARD_WIDTH - pad_x, pad_top + header_h
    draw.rounded_rectangle(
        (hx0, hy0, hx1, hy1),
        radius=20,
        fill=CARD_BG,
        outline=CARD_BORDER,
        width=1,
    )
    # Accent bar at top of header card
    _draw_horizontal_gradient_bar(
        img,
        (hx0 + 20, hy0 + 14, hx1 - 20, hy0 + 20),
        ACCENT_CYAN,
        ACCENT_INDIGO,
        radius=3,
    )

    title_text = "Рейтинг активності чату"
    draw.text((hx0 + 24, hy0 + 32), title_text, font=font_title, fill=TEXT_PRIMARY)

    chat_title = (data.chat_title or "Чат").strip()
    subtitle_raw = f"{chat_title}  •  За останні {data.period_label}"
    subtitle_text = _truncate_text(draw, subtitle_raw, font_subtitle, content_w - 48)
    draw.text((hx0 + 24, hy0 + 74), subtitle_text, font=font_subtitle, fill=TEXT_SECONDARY)

    # 2. Summary metric pills row
    pills_y0 = hy1 + pills_gap_top
    pills_y1 = pills_y0 + pills_h
    pills: list[tuple[str, str, tuple[int, int, int]]] = [
        ("Повідомлень за період", str(data.total_period_messages), ACCENT_CYAN),
        ("Усього в базі", str(data.total_retained_messages), ACCENT_INDIGO),
    ]
    if data.peak_hour_label:
        pills.append(("Пік (Київ)", data.peak_hour_label, (250, 204, 21)))

    pill_gap = 16
    pill_count = len(pills)
    pill_w = (content_w - pill_gap * (pill_count - 1)) / pill_count

    for idx, (label, value, accent) in enumerate(pills):
        px0 = int(pad_x + idx * (pill_w + pill_gap))
        px1 = int(px0 + pill_w)
        draw.rounded_rectangle(
            (px0, pills_y0, px1, pills_y1),
            radius=14,
            fill=CARD_BG,
            outline=CARD_BORDER,
            width=1,
        )
        draw.text((px0 + 18, pills_y0 + 14), label, font=font_pill_label, fill=TEXT_SECONDARY)
        val_max_w = (px1 - px0) - 36
        val_font = font_pill_val
        if _measure_text_width(draw, value, val_font) > val_max_w:
            val_font = font_pill_val_sm
        val_text = _truncate_text(draw, value, val_font, val_max_w)
        draw.text((px0 + 18, pills_y0 + 40), val_text, font=val_font, fill=accent)

    # 3. Ranked participant rows
    rows_y = pills_y1 + rows_gap_top
    top_count = max((p.message_count for p in participants), default=0)
    total_for_pct = max(
        data.total_period_messages,
        sum(p.message_count for p in participants),
        1,
    )

    if not participants:
        draw.rounded_rectangle(
            (pad_x, rows_y, CARD_WIDTH - pad_x, rows_y + rows_block_h),
            radius=16,
            fill=CARD_BG,
            outline=CARD_BORDER,
            width=1,
        )
        empty_msg = "Немає активності учасників за обраний період"
        ew = _measure_text_width(draw, empty_msg, font_subtitle)
        draw.text(
            ((CARD_WIDTH - ew) / 2, rows_y + 34),
            empty_msg,
            font=font_subtitle,
            fill=TEXT_SECONDARY,
        )
        rows_end_y = rows_y + rows_block_h
    else:
        for rank, participant in enumerate(participants, start=1):
            ry0 = rows_y + (rank - 1) * (row_h + row_gap)
            ry1 = ry0 + row_h
            row_outline = (250, 204, 21) if rank == 1 else CARD_BORDER
            draw.rounded_rectangle(
                (pad_x, ry0, CARD_WIDTH - pad_x, ry1),
                radius=16,
                fill=CARD_BG,
                outline=row_outline,
                width=2 if rank == 1 else 1,
            )

            # Rank badge pill
            badge_bg, badge_fg = RANK_BADGE_COLORS.get(rank, DEFAULT_RANK_BADGE)
            bx0 = pad_x + 16
            by0 = ry0 + (row_h - 34) // 2
            bx1 = bx0 + 46
            by1 = by0 + 34
            draw.rounded_rectangle((bx0, by0, bx1, by1), radius=10, fill=badge_bg)
            rank_label = f"#{rank}"
            rbbox = draw.textbbox((0, 0), rank_label, font=font_rank)
            rw = rbbox[2] - rbbox[0]
            rh = rbbox[3] - rbbox[1]
            draw.text(
                (bx0 + (46 - rw) / 2 - rbbox[0], by0 + (34 - rh) / 2 - rbbox[1]),
                rank_label,
                font=font_rank,
                fill=badge_fg,
            )

            # Circular avatar (56x56)
            ax = bx1 + 16
            ay = ry0 + (row_h - AVATAR_SIZE) // 2
            avatar_img = _build_circular_avatar(participant, size=AVATAR_SIZE)
            img.paste(avatar_img, (ax, ay), avatar_img)

            # Right area: name + count/pct + progress bar
            info_x0 = ax + AVATAR_SIZE + 18
            info_x1 = CARD_WIDTH - pad_x - 22
            info_w = info_x1 - info_x0

            pct = int(round((participant.message_count / total_for_pct) * 100)) if total_for_pct > 0 else 0
            count_text = f"{participant.message_count} пов."
            pct_text = f" {pct}%"

            count_w = _measure_text_width(draw, count_text, font_count)
            pct_w = _measure_text_width(draw, pct_text, font_pct)
            stats_total_w = count_w + pct_w + 16

            name_max_w = max(80.0, info_w - stats_total_w)
            display_name = _truncate_text(
                draw,
                participant.display_name or f"User#{participant.user_id}",
                font_name,
                name_max_w,
            )
            draw.text((info_x0, ry0 + 16), display_name, font=font_name, fill=TEXT_PRIMARY)

            # Right-aligned count + percentage
            stats_x = info_x1 - (count_w + pct_w)
            draw.text((stats_x, ry0 + 17), count_text, font=font_count, fill=TEXT_PRIMARY)
            draw.text((stats_x + count_w, ry0 + 20), pct_text, font=font_pct, fill=TEXT_SECONDARY)

            # Progress bar
            bar_y0 = ry0 + 52
            bar_y1 = bar_y0 + 14
            draw.rounded_rectangle(
                (info_x0, bar_y0, info_x1, bar_y1),
                radius=7,
                fill=TRACK_BG,
            )
            if top_count > 0 and participant.message_count > 0:
                ratio = max(0.0, min(1.0, participant.message_count / top_count))
                fill_w = max(16, int(round(info_w * ratio)))
                bar_color = BAR_COLORS.get(rank, DEFAULT_BAR_COLOR)
                draw.rounded_rectangle(
                    (info_x0, bar_y0, info_x0 + fill_w, bar_y1),
                    radius=7,
                    fill=bar_color,
                )

        rows_end_y = rows_y + rows_block_h

    # 4. Subtle footer
    footer_y = rows_end_y + 18
    footer_left = f"Дайджестів у кеші: {data.digest_count}"
    footer_right = "Europe/Kyiv"
    draw.text((pad_x + 4, footer_y), footer_left, font=font_footer, fill=TEXT_MUTED)
    fr_w = _measure_text_width(draw, footer_right, font_footer)
    draw.text((CARD_WIDTH - pad_x - fr_w - 4, footer_y), footer_right, font=font_footer, fill=TEXT_MUTED)

    out_buf = io.BytesIO()
    img.convert("RGB").save(out_buf, format="PNG", optimize=True)
    return out_buf.getvalue()
