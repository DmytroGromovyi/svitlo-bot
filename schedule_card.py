"""Render square SVG schedule cards for Telegram photo messages."""

from html import escape
import re

import resvg_py


def _intervals(schedule_text):
    if not schedule_text or schedule_text == "Відключень не заплановано":
        return []
    def to_minute(value):
        hour, minute = value.split(":")
        return int(hour) * 60 + int(minute)
    return [
        (to_minute(start), to_minute(end))
        for start, end in re.findall(r"з (\d{1,2}:\d{2}) до (\d{1,2}:\d{2})", schedule_text)
    ]


def _time(minutes):
    return "24:00" if minutes >= 1440 else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _label(text, x, y, size, color, weight=400, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-family="DejaVu Sans, Arial, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{color}" '
        f'text-anchor="{anchor}">{escape(str(text))}</text>'
    )


def _range_cards(intervals, schedule_text, y, color, compact=False):
    if not intervals:
        if schedule_text == "Відключень не заплановано":
            label, fill = "ВІДКЛЮЧЕНЬ ЗА ГРАФІКОМ НЕМАЄ", "#50d49b"
        else:
            label, fill = "ГРАФІК ЩЕ НЕ ОПУБЛІКОВАНИЙ", "#a7b2b4"
        height = 62 if compact else 82
        return [
            f'<rect x="64" y="{y}" width="952" height="{height}" rx="18" fill="{fill}" fill-opacity="0.12"/>',
            _label(label, 88, y + (39 if compact else 51), 18 if compact else 22, fill, 700),
        ]

    parts = []
    visible = intervals[:4]
    height, step = (58, 68) if compact else (82, 94)
    font_size = 22 if compact else 31
    for index, (start, end) in enumerate(visible):
        column, row = index % 2, index // 2
        x, top = 64 + column * 484, y + row * step
        parts.append(f'<rect x="{x}" y="{top}" width="464" height="{height}" rx="18" fill="{color}" fill-opacity="0.13" stroke="{color}" stroke-opacity="0.18"/>')
        parts.append(_label(f"{_time(start)} — {_time(end)}", x + 232, top + (38 if compact else 54), font_size, color, 700, "middle"))
    return parts


def build_schedule_svg(city_name, group, today_text, tomorrow_text=None, updated_at=None, now_minute=0):
    today_off = _intervals(today_text)
    tomorrow_off = _intervals(tomorrow_text)
    today_rows = max(1, min(2, (min(len(today_off), 4) + 1) // 2))
    tomorrow_rows = max(1, min(2, (min(len(tomorrow_off), 4) + 1) // 2))

    city = escape(city_name)
    group_text = escape(str(group))
    updated = escape(updated_at or "")
    # Layout adapts to the number of visible intervals so the square card stays full.
    today_top = 278
    today_bottom = today_top + (today_rows - 1) * 94 + 82
    today_more = len(today_off) > 4
    if today_more:
        today_bottom += 32
    timeline_title_y = today_bottom + 61
    bar_x, bar_y, bar_width, bar_height = 64, timeline_title_y + 31, 952, 24
    segments = [f'<rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" rx="12" fill="#35c985"/>']
    for start, end in today_off:
        x = bar_x + bar_width * start / 1440
        width = max(2, bar_width * (end - start) / 1440)
        segments.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{width:.1f}" height="{bar_height}" rx="6" fill="#ff6b60"/>')
    marker_x = bar_x + bar_width * min(max(now_minute, 0), 1440) / 1440
    segments.append(f'<path d="M {marker_x:.1f} {bar_y - 10} l -9 -11 h 18 z" fill="#f6f8f7"/>')
    segments.append(f'<rect x="{marker_x - 2:.1f}" y="{bar_y - 2}" width="4" height="{bar_height + 4}" rx="2" fill="#f6f8f7"/>')

    scale = "".join(
        _label(label, bar_x + bar_width * hour / 24, bar_y + 43, 15, "#8d9da0", 500,
               "middle" if hour not in (0, 24) else "start" if hour == 0 else "end")
        for hour, label in ((0, "00:00"), (6, "06:00"), (12, "12:00"), (18, "18:00"), (24, "24:00"))
    )

    legend_y = bar_y + 70
    divider_y = legend_y + 40
    tomorrow_title_y = divider_y + 43
    tomorrow_top = tomorrow_title_y + 20
    tomorrow_bottom = tomorrow_top + (tomorrow_rows - 1) * 68 + 62
    tomorrow_more = len(tomorrow_off) > 4
    if tomorrow_more:
        tomorrow_bottom += 27
    summary_y = max(780, tomorrow_bottom + 38)
    summary_height = 1056 - summary_y - 20
    total_y = summary_y + min(summary_height * 0.55 + 10, summary_height - 24)
    total_outage = sum(end - start for start, end in today_off)
    total_hours, total_minutes = divmod(total_outage, 60)
    known_today = bool(today_off) or today_text == "Відключень не заплановано"
    total_label = "НЕМАЄ ДАНИХ" if not known_today else (
        f"{total_hours} год {total_minutes} хв" if total_minutes else f"{total_hours} год"
    )
    total_title = "ЗАГАЛОМ ЗАПЛАНОВАНО БЕЗ СВІТЛА" if known_today else "ДАНІ ПРО ГРАФІК"

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1080" viewBox="0 0 1080 1080">',
        '<rect width="1080" height="1080" fill="#10191e"/>',
        # Inset the rounded card so Telegram's own square-photo mask never cuts it off.
        '<rect x="24" y="24" width="1032" height="1032" rx="38" fill="#172329" stroke="#26363a" stroke-width="2"/>',
        '<rect x="64" y="58" width="62" height="62" rx="20" fill="#35c985" fill-opacity="0.15"/>',
        '<path d="M 101 67 L 83 94 H 97 L 91 112 L 111 84 H 97 Z" fill="#50d49b"/>',
        _label("СВІТЛО", 146, 84, 23, "#91a3a5", 700),
        _label("ГРАФІК ВІДКЛЮЧЕНЬ", 146, 113, 17, "#64777b", 500),
        _label(city, 64, 176, 31, "#f2f6f5", 700),
        _label(f"ГРУПА {group_text}", 1016, 174, 19, "#9fb1b2", 650, "end"),
        _label("КОЛИ НЕ БУДЕ СВІТЛА", 64, 235, 23, "#ff9385", 700),
        _label("СЬОГОДНІ", 1016, 235, 18, "#f2f6f5", 700, "end"),
        *_range_cards(today_off, today_text, today_top, "#ff8175"),
        *([_label(f"ЩЕ {len(today_off) - 4} ІНТЕРВАЛІВ — У ПІДПИСІ НИЖЧЕ", 68, today_bottom, 15, "#a7b2b4", 600)] if today_more else []),
        _label("ТАЙМЛАЙН ВІДКЛЮЧЕНЬ", 64, timeline_title_y, 17, "#f2f6f5", 700),
        _label("24 ГОДИНИ", 1016, timeline_title_y, 15, "#829397", 500, "end"),
        *segments,
        scale,
        _label("■ НЕМАЄ СВІТЛА", 64, legend_y, 15, "#ff8175", 600),
        _label("ЗАРАЗ", 1016, legend_y, 15, "#f2f6f5", 600, "end"),
    ]

    parts.extend([
        f'<path d="M 64 {divider_y} H 1016" stroke="#304247" stroke-width="2"/>',
        _label("ЗАВТРА", 64, tomorrow_title_y, 18, "#f2f6f5", 700),
        *_range_cards(tomorrow_off, tomorrow_text, tomorrow_top, "#ffad70", compact=True),
    ])
    if tomorrow_more:
        parts.append(_label(f"ЩЕ {len(tomorrow_off) - 4} ІНТЕРВАЛІВ — У ПІДПИСІ", 68, tomorrow_bottom, 14, "#a7b2b4", 600))

    parts.extend([
        f'<rect x="64" y="{summary_y}" width="952" height="{summary_height}" rx="22" fill="#202f33"/>',
        _label(total_title, 92, summary_y + 36, 16, "#91a3a5", 600),
        _label(total_label, 92, total_y, 37, "#f2f6f5", 700),
        _label(f"ОНОВЛЕНО О {updated}", 992, summary_y + summary_height - 18, 14, "#829397", 500, "end"),
    ])

    parts.append("</svg>")
    return "".join(parts)


def render_schedule_png(*args, **kwargs):
    return resvg_py.svg_to_bytes(svg_string=build_schedule_svg(*args, **kwargs))
