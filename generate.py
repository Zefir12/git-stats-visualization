"""
Render an animated, isometric contribution skyline as SVG for a profile README.

Data can come from GitHub, any Forgejo/Gitea instance (Codeberg included),
or several of them merged into one graph.

Examples:
    python generate.py --mock
    python generate.py --source github:octocat
    python generate.py --source forgejo:https://codeberg.org/someone --theme forgejo
    python generate.py --source github:octocat --source forgejo:https://codeberg.org/octocat
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

THEMES = {
    "github": {
        "dark": {
            "empty": "#161b22",
            "levels": ["#0e4429", "#006d32", "#26a641", "#39d353"],
            "glow": "#39d353",
        },
        "light": {
            "empty": "#ebedf0",
            "levels": ["#9be9a8", "#40c463", "#30a14e", "#216e39"],
            "glow": "#40c463",
        },
    },
    "forgejo": {
        "dark": {
            "empty": "#1d1a18",
            "levels": ["#5a2608", "#a8400c", "#ea6a12", "#ffa04d"],
            "glow": "#ff8a2b",
        },
        "light": {
            "empty": "#efebe8",
            "levels": ["#ffd3ad", "#ffa25e", "#f0700f", "#b9420a"],
            "glow": "#ff8a2b",
        },
    },
    "aurora": {
        "dark": {
            "empty": "#171a2b",
            "levels": ["#1e3a8a", "#4f46e5", "#a855f7", "#f0abfc"],
            "glow": "#c084fc",
        },
        "light": {
            "empty": "#eceef6",
            "levels": ["#c7d2fe", "#818cf8", "#8b5cf6", "#86198f"],
            "glow": "#a78bfa",
        },
    },
}

CHROME = {
    "dark": {
        "bg_top": "#0d1117",
        "bg_bottom": "#151b26",
        "border": "#30363d",
        "dots": "#8b949e",
        "primary": "#e6edf3",
        "secondary": "#8b949e",
        "card": "#161b22",
        "card_border": "#30363d",
        "slab": "#0b0f15",
        "shimmer": "#ffffff",
    },
    "light": {
        "bg_top": "#ffffff",
        "bg_bottom": "#f3f5f8",
        "border": "#d0d7de",
        "dots": "#8c959f",
        "primary": "#1f2328",
        "secondary": "#59636e",
        "card": "#ffffff",
        "card_border": "#d8dee4",
        "slab": "#d8dde3",
        "shimmer": "#ffffff",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

CELL = 12
GAP = 2
STEP = CELL + GAP
ANGLE = math.radians(20)
COS = math.cos(ANGLE)
SIN = math.sin(ANGLE)
SHADE_LEFT = 0.82
SHADE_RIGHT = 0.64
BASE_HEIGHT = 6
MAX_HEIGHT = 28
SLAB_DEPTH = 7
SLAB_MARGIN = 5


def project(x: float, y: float, z: float = 0.0) -> tuple[float, float]:
    return (x - y) * COS, (x + y) * SIN - z


def pts(*points: tuple[float, float]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def shade(hex_color: str, factor: float) -> str:
    h = hex_color.lstrip("#")
    rgb = [max(0, min(255, int(int(h[i : i + 2], 16) * factor))) for i in (0, 2, 4)]
    return "#" + "".join(f"{c:02x}" for c in rgb)


def height_from_count(count: int) -> int:
    if count <= 0:
        return 0
    # One contribution already reads as a block; more add height with
    # diminishing returns so a single huge day doesn't become a spike.
    return min(MAX_HEIGHT, int(round(BASE_HEIGHT + math.log2(count) * 4.2)))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    week: int
    day: int
    date: dt.date
    count: int
    level: int = 0


@dataclass
class Stats:
    total: int
    weekday_totals: list[int]
    longest_streak: int
    current_streak: int
    best_count: int
    best_date: dt.date | None
    active_days: int


def build_calendar(counts: dict[dt.date, int], today: dt.date) -> list[Cell]:
    """Lay out the last ~year as GitHub does: Sunday-first columns ending today."""
    days_since_sunday = (today.weekday() + 1) % 7
    start = today - dt.timedelta(days=days_since_sunday) - dt.timedelta(weeks=52)
    cells = []
    day = start
    while day <= today:
        offset = (day - start).days
        cells.append(Cell(offset // 7, offset % 7, day, counts.get(day, 0)))
        day += dt.timedelta(days=1)
    return cells


def assign_levels(cells: list[Cell], native: dict[dt.date, int] | None = None) -> None:
    if native is not None:
        for cell in cells:
            cell.level = native.get(cell.date, 0)
        return
    nonzero = sorted(c.count for c in cells if c.count > 0)
    if not nonzero:
        return

    def quantile(q: float) -> float:
        return nonzero[min(len(nonzero) - 1, int(q * len(nonzero)))]

    thresholds = [quantile(0.25), quantile(0.5), quantile(0.75)]
    for cell in cells:
        if cell.count <= 0:
            cell.level = 0
        else:
            cell.level = 1 + sum(cell.count > t for t in thresholds)


def compute_stats(cells: list[Cell]) -> Stats:
    weekday = [0] * 7
    longest = run = 0
    for cell in cells:
        weekday[cell.day] += cell.count
        run = run + 1 if cell.count > 0 else 0
        longest = max(longest, run)

    # A streak still counts as "current" if today just hasn't had activity yet.
    current = 0
    tail = list(reversed(cells))
    if tail and tail[0].count == 0:
        tail = tail[1:]
    for cell in tail:
        if cell.count <= 0:
            break
        current += 1

    best = max(cells, key=lambda c: c.count, default=None)
    return Stats(
        total=sum(c.count for c in cells),
        weekday_totals=weekday,
        longest_streak=longest,
        current_streak=current,
        best_count=best.count if best else 0,
        best_date=best.date if best and best.count else None,
        active_days=sum(1 for c in cells if c.count > 0),
    )


def mock_counts(today: dt.date) -> dict[dt.date, int]:
    rng = random.Random(42)
    counts = {}
    hot = False
    for i in range(371):
        day = today - dt.timedelta(days=i)
        if i % 7 == 0:
            hot = rng.random() < 0.2
        weekend = day.weekday() >= 5
        weights = (
            [0.04, 0.16, 0.3, 0.3, 0.2]
            if hot
            else [0.6, 0.25, 0.1, 0.04, 0.01]
            if weekend
            else [0.22, 0.32, 0.26, 0.13, 0.07]
        )
        level = rng.choices(range(5), weights)[0]
        counts[day] = [0, rng.randint(1, 3), rng.randint(4, 8), rng.randint(9, 16), rng.randint(17, 40)][level]
    return counts


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

GITHUB_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        weeks { contributionDays { contributionCount contributionLevel date } }
      }
    }
  }
}
"""

GITHUB_LEVELS = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}


def http_json(url: str, *, data: bytes | None = None, headers: dict[str, str]) -> object:
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": "contrib-skyline", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} failed ({exc.code}): {body}") from exc


def fetch_github(login: str) -> tuple[dict[dt.date, int], dict[dt.date, int]]:
    token = os.getenv("GH_README_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GitHub source needs GH_README_TOKEN or GITHUB_TOKEN.")
    body = json.dumps({"query": GITHUB_QUERY, "variables": {"login": login}}).encode()
    data = http_json(
        "https://api.github.com/graphql",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if data.get("errors"):
        raise RuntimeError(f"GitHub API errors: {json.dumps(data['errors'])}")
    user = data["data"]["user"]
    if not user:
        raise RuntimeError(f"GitHub user '{login}' not found.")
    counts, levels = {}, {}
    for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for day in week["contributionDays"]:
            date = dt.date.fromisoformat(day["date"])
            counts[date] = day["contributionCount"]
            levels[date] = GITHUB_LEVELS[day["contributionLevel"]]
    return counts, levels


def fetch_forgejo(base_url: str, login: str, tz: ZoneInfo) -> dict[dt.date, int]:
    """Forgejo/Gitea expose the profile heatmap as timestamp buckets."""
    token = os.getenv("FORGEJO_TOKEN")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"
    url = f"{base_url.rstrip('/')}/api/v1/users/{urllib.parse.quote(login)}/heatmap"
    counts: dict[dt.date, int] = {}
    for bucket in http_json(url, headers=headers) or []:
        date = dt.datetime.fromtimestamp(bucket["timestamp"], tz).date()
        counts[date] = counts.get(date, 0) + bucket["contributions"]
    return counts


def parse_source(spec: str) -> tuple[str, str, str]:
    """`github:USER` or `forgejo:https://host/USER` -> (kind, base_url, user)."""
    kind, _, rest = spec.partition(":")
    kind = kind.lower()
    if kind == "github" and rest:
        return kind, "https://github.com", rest.strip("/")
    if kind in ("forgejo", "gitea", "codeberg") and rest:
        if kind == "codeberg" and "/" not in rest:
            rest = f"https://codeberg.org/{rest}"
        parsed = urllib.parse.urlparse(rest)
        path = parsed.path.strip("/").split("/")
        if not parsed.scheme or not path[-1]:
            raise ValueError(f"Expected forgejo:https://host/USER, got '{spec}'")
        base = f"{parsed.scheme}://{parsed.netloc}"
        if len(path) > 1:
            base += "/" + "/".join(path[:-1])
        return "forgejo", base, path[-1]
    raise ValueError(f"Unknown source '{spec}'. Use github:USER or forgejo:https://host/USER")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

STYLE = """
<style>
  .cube { animation: drop 0.9s cubic-bezier(.34,1.56,.64,1) both; }
  .shine { opacity: 0; animation: shine 7s linear infinite; }
  .glow { animation: pulse 3.2s ease-in-out infinite; }
  .spark { opacity: 0; animation: float 3.6s ease-out infinite; }
  .bar { transform-box: fill-box; transform-origin: 50% 100%; animation: grow 0.9s cubic-bezier(.22,1,.36,1) both; }
  .fade { animation: fade 0.8s ease-out both; }
  .roll { animation: roll 2.4s cubic-bezier(.16,1,.3,1) both; }
  .aura { animation: breathe 6s ease-in-out infinite; }
  .ring { transform-box: fill-box; transform-origin: center; animation: ring 2.4s ease-out infinite; }
  @keyframes drop { from { transform: translateY(-46px); opacity: 0; } 55% { opacity: 1; } }
  @keyframes shine { 0% { opacity: 0; } 3% { opacity: .55; } 9% { opacity: 0; } 100% { opacity: 0; } }
  @keyframes pulse { 0%, 100% { opacity: .25; } 50% { opacity: .85; } }
  @keyframes float { 0% { transform: translateY(0); opacity: 0; } 15% { opacity: 1; } 100% { transform: translateY(-34px); opacity: 0; } }
  @keyframes grow { from { transform: scaleY(0); } }
  @keyframes fade { from { opacity: 0; transform: translateY(6px); } }
  @keyframes roll { from { transform: translateY(0); } }
  @keyframes breathe { 0%, 100% { opacity: .35; } 50% { opacity: .7; } }
  @keyframes ring { 0% { transform: scale(.4); opacity: .9; } 100% { transform: scale(2.6); opacity: 0; } }
  @media (prefers-reduced-motion: reduce) {
    .cube, .shine, .glow, .spark, .bar, .fade, .roll, .aura, .ring { animation: none !important; }
  }
</style>
"""


def text(x, y, content, *, size, fill, weight=400, anchor="start", family=FONT, extra=""):
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" {extra}>{content}</text>'
    )


def render_cube(cell: Cell, palette: dict, chrome: dict, intro_delay: float) -> list[str]:
    x0, y0, s = cell.week * STEP, cell.day * STEP, CELL
    h = height_from_count(cell.count)
    color = palette["empty"] if cell.level == 0 else palette["levels"][cell.level - 1]
    top = pts(project(x0, y0, h), project(x0 + s, y0, h), project(x0 + s, y0 + s, h), project(x0, y0 + s, h))

    diagonal = cell.week + cell.day
    delay = intro_delay + diagonal * 0.022
    out = [f'<g class="cube" style="animation-delay:{delay:.3f}s">']
    if h > 0:
        left = pts(project(x0, y0 + s, 0), project(x0, y0 + s, h), project(x0 + s, y0 + s, h), project(x0 + s, y0 + s, 0))
        right = pts(project(x0 + s, y0 + s, 0), project(x0 + s, y0 + s, h), project(x0 + s, y0, h), project(x0 + s, y0, 0))
        out.append(f'<polygon points="{left}" fill="{shade(color, SHADE_LEFT)}"/>')
        out.append(f'<polygon points="{right}" fill="{shade(color, SHADE_RIGHT)}"/>')
    out.append(f'<polygon points="{top}" fill="{color}"/>')
    if cell.level >= 4:
        out.append(f'<polygon class="glow" points="{top}" fill="{palette["glow"]}" filter="url(#blur)" style="animation-delay:{diagonal * 0.05:.2f}s"/>')
    if cell.level > 0:
        # A light sweep that travels diagonally across the skyline forever.
        sweep = 2.6 + diagonal * 0.028
        out.append(f'<polygon class="shine" points="{top}" fill="{chrome["shimmer"]}" style="animation-delay:{sweep:.3f}s"/>')
    out.append("</g>")
    return out


def render_slab(weeks: int, chrome: dict, palette: dict) -> list[str]:
    m = SLAB_MARGIN
    x0, y0, x1, y1 = -m, -m, weeks * STEP - GAP + m, 7 * STEP - GAP + m
    d = SLAB_DEPTH
    top = pts(project(x0, y0), project(x1, y0), project(x1, y1), project(x0, y1))
    left = pts(project(x0, y1), project(x1, y1), project(x1, y1, -d), project(x0, y1, -d))
    right = pts(project(x1, y1), project(x1, y0), project(x1, y0, -d), project(x1, y1, -d))
    return [
        '<g class="fade">',
        f'<polygon points="{top}" fill="{chrome["slab"]}" opacity=".85"/>',
        f'<polygon points="{left}" fill="url(#slabLeft)"/>',
        f'<polygon points="{right}" fill="url(#slabRight)"/>',
        f'<polyline points="{pts(project(x0, y1), project(x1, y1), project(x1, y0))}" fill="none" '
        f'stroke="{palette["levels"][2]}" stroke-opacity=".55" stroke-width="1"/>',
        "</g>",
    ]


def render_month_labels(cells: list[Cell], chrome: dict) -> list[str]:
    """Month names lying flat on the ground in front of the slab."""
    out = ['<g class="fade" style="animation-delay:1.2s">']
    seen_month = None
    y = 7 * STEP + SLAB_MARGIN + 4
    for cell in cells:
        if cell.day != 0:
            continue
        month = cell.date.replace(day=1)
        if month == seen_month:
            continue
        if seen_month is not None and cell.week < 52:
            ox, oy = project(cell.week * STEP, y)
            # Map text space onto the ground plane: +x along weeks, +y along days.
            out.append(
                f'<text transform="matrix({COS:.4f} {SIN:.4f} {-COS:.4f} {SIN:.4f} {ox:.2f} {oy:.2f})" '
                f'font-family="{FONT}" font-size="10" font-weight="600" letter-spacing=".6" '
                f'fill="{chrome["secondary"]}" dominant-baseline="hanging">{cell.date.strftime("%b").upper()}</text>'
            )
        seen_month = month
    out.append("</g>")
    return out


def render_sparks(cells: list[Cell], palette: dict) -> list[str]:
    rng = random.Random(7)
    hottest = sorted((c for c in cells if c.count > 0), key=lambda c: -c.count)[:8]
    out = []
    for i, cell in enumerate(hottest):
        cx, cy = project(cell.week * STEP + CELL / 2, cell.day * STEP + CELL / 2, height_from_count(cell.count))
        for j in range(2):
            dx = rng.uniform(-4, 4)
            size = rng.uniform(1.4, 2.4)
            delay = 2.8 + rng.uniform(0, 3.6) + j * 1.8
            out.append(
                f'<rect class="spark" x="{cx + dx - size / 2:.2f}" y="{cy - size / 2:.2f}" width="{size:.2f}" '
                f'height="{size:.2f}" fill="{palette["glow"]}" transform="rotate(45 {cx + dx:.2f} {cy:.2f})" '
                f'style="animation-delay:{delay:.2f}s"/>'
            )
    return out


def render_odometer(value: int, right_x: float, baseline: float, size: int, fill: str) -> list[str]:
    """A slot-machine style counter: each digit column rolls into place."""
    digits = f"{value:,}"
    advance = size * 0.62
    comma = size * 0.3
    widths = [comma if ch == "," else advance for ch in digits]
    x = right_x - sum(widths)
    line = size * 1.3
    out = [f'<clipPath id="odoClip"><rect x="{x - 4:.2f}" y="{baseline - size * 0.95:.2f}" width="{sum(widths) + 8:.2f}" height="{size * 1.2:.2f}"/></clipPath>']
    out.append('<g clip-path="url(#odoClip)">')
    column = 0
    for ch, w in zip(digits, widths):
        if ch == ",":
            out.append(text(x + w / 2, baseline, ",", size=size, fill=fill, weight=700, anchor="middle", family=MONO))
            x += w
            continue
        n = int(ch)
        # Columns further right spin through more digits, like a real counter.
        spins = 1 + (len(digits) - column) // 2
        target = n + 10 * spins
        shift = target * line
        delay = 0.3 + column * 0.06
        out.append(
            f'<g class="roll" transform="translate(0 {-shift:.2f})" '
            f'style="transform:translateY({-shift:.2f}px);animation-delay:{delay:.2f}s">'
        )
        for k in range(target + 1):
            out.append(text(x + w / 2, baseline + k * line, str(k % 10), size=size, fill=fill, weight=700, anchor="middle", family=MONO))
        out.append("</g>")
        x += w
        column += 1
    out.append("</g>")
    return out


def render_stat_cards(x: float, y: float, stats: Stats, chrome: dict, palette: dict) -> list[str]:
    best = stats.best_date.strftime("%b %-d") if stats.best_date else "—"
    cards = [
        ("LONGEST STREAK", f"{stats.longest_streak}", "days"),
        ("CURRENT STREAK", f"{stats.current_streak}", "days"),
        ("BEST DAY", f"{stats.best_count}", best),
    ]
    w, h, gap = 100, 50, 8
    out = []
    for i, (label, value, unit) in enumerate(cards):
        cx = x + i * (w + gap)
        out.append(f'<g class="fade" style="animation-delay:{0.6 + i * 0.12:.2f}s">')
        out.append(f'<rect x="{cx:.2f}" y="{y:.2f}" width="{w}" height="{h}" rx="8" fill="{chrome["card"]}" fill-opacity=".75" stroke="{chrome["card_border"]}"/>')
        out.append(f'<rect x="{cx + 10:.2f}" y="{y:.2f}" width="22" height="2" rx="1" fill="{palette["levels"][2 if i else 3]}"/>')
        out.append(text(cx + 10, y + 16, label, size=7.5, fill=chrome["secondary"], weight=600, extra='letter-spacing=".8"'))
        out.append(text(cx + 10, y + 38, value, size=18, fill=chrome["primary"], weight=700))
        vx = cx + 10 + len(value) * 10.8 + 4
        out.append(text(vx, y + 38, unit, size=9, fill=chrome["secondary"]))
        if i == 1 and stats.current_streak > 0:
            rx, ry = cx + w - 12, y + 34
            out.append(f'<circle cx="{rx}" cy="{ry}" r="3" fill="{palette["glow"]}"/>')
            out.append(f'<circle class="ring" cx="{rx}" cy="{ry}" r="3" fill="none" stroke="{palette["glow"]}" stroke-width="1.2"/>')
        out.append("</g>")
    return out


def render_weekdays(x: float, bottom: float, stats: Stats, chrome: dict, palette: dict) -> list[str]:
    labels = ["S", "M", "T", "W", "T", "F", "S"]
    peak = max(stats.weekday_totals) or 1
    bar_w, gap, max_h = 11, 5, 52
    out = [text(x, bottom - max_h - 14, "MOST ACTIVE DAYS", size=8, fill=chrome["secondary"], weight=600, extra='letter-spacing=".8" class="fade" style="animation-delay:1s"')]
    for i, total in enumerate(stats.weekday_totals):
        bx = x + i * (bar_w + gap)
        h = max(2, total / peak * max_h)
        fill = "url(#barPeak)" if total == peak else "url(#bar)"
        out.append(f'<rect x="{bx:.2f}" y="{bottom - max_h:.2f}" width="{bar_w}" height="{max_h}" rx="2" fill="{chrome["card_border"]}" fill-opacity=".35"/>')
        out.append(f'<rect class="bar" x="{bx:.2f}" y="{bottom - h:.2f}" width="{bar_w}" height="{h:.2f}" rx="2" fill="{fill}" style="animation-delay:{1.1 + i * 0.07:.2f}s"/>')
        out.append(text(bx + bar_w / 2, bottom + 12, labels[i], size=9, fill=chrome["secondary"], anchor="middle"))
    return out


def render_legend(x: float, y: float, chrome: dict, palette: dict) -> list[str]:
    out = [f'<g class="fade" style="animation-delay:1.4s">']
    out.append(text(x, y + 8, "Less", size=8.5, fill=chrome["secondary"]))
    for i, color in enumerate([palette["empty"], *palette["levels"]]):
        out.append(f'<rect x="{x + 24 + i * 12:.2f}" y="{y}" width="9" height="9" rx="2" fill="{color}" stroke="{chrome["card_border"]}" stroke-width=".5"/>')
    out.append(text(x + 24 + 5 * 12 + 3, y + 8, "More", size=8.5, fill=chrome["secondary"]))
    out.append("</g>")
    return out


def render_svg(cells: list[Cell], stats: Stats, *, mode: str, theme: str, name: str, sources: list[str], card: bool) -> str:
    palette = THEMES[theme][mode]
    chrome = CHROME[mode]
    weeks = max(c.week for c in cells) + 1
    gw, gd = weeks * STEP, 7 * STEP

    # Graph footprint in screen space (ground plane + tallest tower).
    top_x, top_y = project(0, 0, MAX_HEIGHT)
    right_x, right_y = project(gw, 0)
    left_x, left_y = project(0, gd)
    front_x, front_y = project(gw, gd, -SLAB_DEPTH)

    pad = 22
    header = 92
    min_x = left_x - pad - 8
    max_x = right_x + pad + 8
    min_y = top_y - header
    max_y = front_y + 34
    width, height = max_x - min_x, max_y - min_y

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x:.2f} {min_y:.2f} {width:.2f} {height:.2f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" aria-label="{name}: {stats.total:,} contributions in the last year">',
        f"<title>{name}: {stats.total:,} contributions in the last year</title>",
        STYLE,
        "<defs>",
        f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{chrome["bg_top"]}"/><stop offset="1" stop-color="{chrome["bg_bottom"]}"/></linearGradient>',
        f'<radialGradient id="aura" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{palette["glow"]}" stop-opacity=".22"/><stop offset="1" stop-color="{palette["glow"]}" stop-opacity="0"/></radialGradient>',
        f'<linearGradient id="accent" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{palette["levels"][3]}"/><stop offset="1" stop-color="{palette["levels"][1]}"/></linearGradient>',
        f'<linearGradient id="bar" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{palette["levels"][2]}"/><stop offset="1" stop-color="{palette["levels"][0]}"/></linearGradient>',
        f'<linearGradient id="barPeak" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{palette["glow"]}"/><stop offset="1" stop-color="{palette["levels"][1]}"/></linearGradient>',
        f'<linearGradient id="slabLeft" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{shade(palette["levels"][0], 0.9)}"/><stop offset="1" stop-color="{chrome["slab"]}"/></linearGradient>',
        f'<linearGradient id="slabRight" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{shade(palette["levels"][0], 0.7)}"/><stop offset="1" stop-color="{chrome["slab"]}"/></linearGradient>',
        '<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2"/></filter>',
        f'<pattern id="dots" width="14" height="14" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="{chrome["dots"]}" fill-opacity=".18"/></pattern>',
        "</defs>",
    ]

    if card:
        parts.append(f'<rect x="{min_x + 1:.2f}" y="{min_y + 1:.2f}" width="{width - 2:.2f}" height="{height - 2:.2f}" rx="14" fill="url(#bg)" stroke="{chrome["border"]}"/>')
        parts.append(f'<rect x="{min_x + 1:.2f}" y="{min_y + 1:.2f}" width="{width - 2:.2f}" height="{height - 2:.2f}" rx="14" fill="url(#dots)"/>')

    # Soft glow behind the busiest stretch of the year.
    busiest = max(range(weeks), key=lambda w: sum(c.count for c in cells if abs(c.week - w) <= 4))
    ax, ay = project(busiest * STEP, gd / 2)
    parts.append(f'<ellipse class="aura" cx="{ax:.2f}" cy="{ay:.2f}" rx="190" ry="90" fill="url(#aura)"/>')

    # Header: name + sources on the left, rolling total on the right.
    hx = min_x + pad
    hy = min_y + pad + 18
    parts.append('<g class="fade">')
    parts.append(text(hx, hy, name, size=20, fill=chrome["primary"], weight=700))
    parts.append(text(hx, hy + 18, f"contributions · last 12 months · {' + '.join(sources)}", size=10, fill=chrome["secondary"]))
    parts.append("</g>")
    total_right = max_x - pad
    parts += render_odometer(stats.total, total_right, hy + 14, 40, "url(#accent)")
    parts.append(text(total_right, hy + 30, "TOTAL CONTRIBUTIONS", size=8.5, fill=chrome["secondary"], weight=600, anchor="end", extra='letter-spacing="1" class="fade" style="animation-delay:.4s"'))

    parts += render_stat_cards(total_right - 3 * 100 - 2 * 8, hy + 46, stats, chrome, palette)

    parts += render_slab(weeks, chrome, palette)
    for cell in sorted(cells, key=lambda c: (c.week + c.day, c.week)):
        parts += render_cube(cell, palette, chrome, intro_delay=0.2)
    parts += render_sparks(cells, palette)
    parts += render_month_labels(cells, chrome)

    bottom = max_y - pad - 12
    parts += render_weekdays(hx, bottom - 26, stats, chrome, palette)
    parts += render_legend(hx, bottom - 4, chrome, palette)
    stamp = dt.date.today().strftime("%b %-d, %Y")
    parts.append(text(hx + 120, bottom + 4, f"{stats.active_days} active days · updated {stamp}", size=8.5, fill=chrome["secondary"], extra='class="fade" style="animation-delay:1.5s"'))

    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    env_sources = os.getenv("SOURCES", "").replace(",", " ").split()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", action="append", default=None, help="github:USER or forgejo:https://host/USER (repeatable)")
    parser.add_argument("--name", default=os.getenv("DISPLAY_NAME"), help="title shown on the card (default: first source's user)")
    parser.add_argument("--theme", default=os.getenv("THEME", "github"), choices=sorted(THEMES))
    parser.add_argument("--tz", default=os.getenv("TZ_NAME", "UTC"), help="timezone used to bucket Forgejo activity into days")
    parser.add_argument("--no-card", action="store_true", help="transparent background, no card frame")
    parser.add_argument("--mock", action="store_true", help="render with built-in mock data")
    parser.add_argument("--out", default="./output")
    args = parser.parse_args()

    tz = ZoneInfo(args.tz)
    today = dt.datetime.now(tz).date()
    sources = args.source or env_sources
    labels: list[str] = []

    if args.mock:
        cells = build_calendar(mock_counts(today), today)
        assign_levels(cells)
        name = args.name or "@you"
        labels = ["GitHub", "Forgejo"]
    else:
        if not sources:
            print("No sources. Pass --source github:USER / forgejo:https://host/USER or set SOURCES.", file=sys.stderr)
            return 1
        merged: dict[dt.date, int] = {}
        native_levels = None
        parsed = [parse_source(s) for s in sources]
        for kind, base, user in parsed:
            if kind == "github":
                counts, native_levels = fetch_github(user)
                labels.append("GitHub")
            else:
                counts = fetch_forgejo(base, user, tz)
                labels.append(urllib.parse.urlparse(base).netloc)
            for date, n in counts.items():
                merged[date] = merged.get(date, 0) + n
            print(f"{kind}:{user}@{base} -> {sum(counts.values())} contributions")
        cells = build_calendar(merged, today)
        # GitHub's own buckets only make sense when it's the only source.
        assign_levels(cells, native_levels if len(parsed) == 1 else None)
        name = args.name or f"@{parsed[0][2]}"

    stats = compute_stats(cells)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for mode in ("dark", "light"):
        svg = render_svg(cells, stats, mode=mode, theme=args.theme, name=name, sources=labels, card=not args.no_card)
        path = out / f"skyline-{mode}.svg"
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path} ({len(svg) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
