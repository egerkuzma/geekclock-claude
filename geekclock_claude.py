#!/usr/bin/env python3
"""
geekclock-claude — push Claude.ai usage limits to a GeekMagic SmallTV clock.

On each run it fetches usage data from claude.ai/api/.../usage via curl_cffi
(Safari TLS impersonation to bypass Cloudflare), renders a 240x240 image
with the 5-hour, weekly and (when reported) per-model weekly limits, and
uploads it to the device.

Configuration is read from environment variables (see .env.example) or
command-line arguments.

Usage:
    geekclock_claude.py
    geekclock_claude.py --session-key <key> --device-ip <ip>

Cron example (update every minute):
    * * * * * /path/to/geekclock_claude.py >> /tmp/geekclock.log 2>&1
"""

import argparse
import io
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from curl_cffi import requests as cffi_requests
from PIL import Image, ImageDraw, ImageFont


# --- defaults ---
DEFAULT_CACHE_PATH = "/tmp/geekclock_claude_cache.json"
DEFAULT_CACHE_TTL = 300              # 5 minutes
DEFAULT_CACHE_MAX_FALLBACK = 43200   # 12 hours; older cache is discarded
EXIT_INTERNAL_ERROR = 2              # exit code when our own parsing failed

# --- palette ---
COL_BG = (0, 0, 0)
COL_TEXT = (245, 245, 245)
COL_DIM = (150, 150, 160)
COL_BAR_BG = (45, 45, 55)
COL_BAR_GREEN = (0, 240, 60)
COL_BAR_YELLOW = (255, 210, 40)
COL_BAR_RED = (240, 70, 80)
COL_PILL_BG = (50, 45, 70)
COL_PILL_TEXT = (245, 245, 245)

# --- font candidates (tried in order, first existing one is used) ---
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Helvetica.ttc",
]
FONT_CANDIDATES_SEMIBOLD = [
    "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_CANDIDATES_TITLE = [
    "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf",
    "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
    "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


# ====== time helpers ======

def _parse_resets_at(resets_at):
    """ISO 8601 timestamp -> minutes until reset, or None on failure."""
    if not resets_at:
        return None
    try:
        cleaned = resets_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        delta = dt - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() / 60))
    except Exception:
        return None


def _format_reset_long(minutes):
    """Minutes -> human string: 37 -> 'Resets in 37m',
    95 -> 'Resets in 1h 35m', 9120 -> 'Resets in 6d 8h'."""
    if minutes is None or minutes <= 0:
        return ""
    days = minutes // (24 * 60)
    rem = minutes % (24 * 60)
    hours = rem // 60
    mins = rem % 60
    if days >= 1:
        return f"Resets in {days}d {hours}h" if hours else f"Resets in {days}d"
    if hours >= 1:
        return f"Resets in {hours}h {mins}m" if mins else f"Resets in {hours}h"
    return f"Resets in {mins}m"


# ====== cache ======
#
# Cache file layout (per-block timestamps):
#   {
#     "five_hour_pct": 42, "five_hour_resets_at": "...", "five_hour_fetched_at": 1.7e9,
#     "seven_day_pct": 7,  "seven_day_resets_at": "...", "seven_day_fetched_at": 1.7e9,
#     "model_weekly_pct": 32, "model_weekly_resets_at": "...",
#     "model_weekly_fetched_at": 1.7e9, "model_weekly_label": "Fable"
#   }
# A block missing from an API response keeps its previous value and its
# own fetched_at, so a partial or empty payload never wipes good data.
# Blocks older than max_fallback are hidden on read, independently.
#
# "model_weekly" is the per-model weekly limit (e.g. Fable). It is not a
# top-level key in the API response; it lives in the "limits" array as an
# entry with kind="weekly_scoped" and scope.model.display_name.

BLOCKS = ("five_hour", "seven_day", "model_weekly")
BLOCK_EXTRA_FIELDS = ("label",)   # optional per-block fields carried through the cache


class InternalError(Exception):
    """A bug in our own response parsing (as opposed to claude.ai being
    unreachable). Carries the best available fallback data in `limits`
    (or None) so the caller can still render something, while signalling
    the failure via a non-zero exit code."""

    def __init__(self, cause, limits=None):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.limits = limits


def _as_pct(value):
    """Return value if it's a real number (not bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _find_model_weekly_limit(limits):
    """Pick the per-model weekly limit out of the "limits" array.

    Looks for the first entry with kind="weekly_scoped" whose scope names a
    model (scope.model.display_name), e.g. the Fable weekly quota.
    Returns {"pct", "resets_at", "label"} or None."""
    if not isinstance(limits, list):
        return None
    for item in limits:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        label = model.get("display_name") if isinstance(model, dict) else None
        if not label:
            continue
        return {
            "pct": _as_pct(item.get("percent")),
            "resets_at": item.get("resets_at"),
            "label": str(label),
        }
    return None


def _build_result_from_api_data(data):
    """Extract the fields we care about from the API response.
    We store ISO timestamps verbatim; reset-minutes are recomputed on read
    so cached entries always show fresh countdowns."""
    if not isinstance(data, dict):
        data = {}
    result = {}
    for block in ("five_hour", "seven_day"):
        entry = data.get(block) or {}
        if not isinstance(entry, dict):
            entry = {}
        result[f"{block}_pct"] = _as_pct(entry.get("utilization"))
        result[f"{block}_resets_at"] = entry.get("resets_at")

    scoped = _find_model_weekly_limit(data.get("limits")) or {}
    result["model_weekly_pct"] = scoped.get("pct")
    result["model_weekly_resets_at"] = scoped.get("resets_at")
    result["model_weekly_label"] = scoped.get("label")
    return result


def _merge_cache(cached, age, fresh, now=None):
    """Combine a fresh API result with the previous cache, block by block.
    A block present in `fresh` gets fetched_at=now; a block absent from
    `fresh` is carried over from `cached` with its own fetched_at (falling
    back to the file mtime for legacy caches without per-block stamps)."""
    now = time.time() if now is None else now
    cached = cached or {}
    legacy_stamp = now - age if age is not None else now
    merged = {}
    for block in BLOCKS:
        if fresh.get(f"{block}_pct") is not None:
            src = fresh
            merged[f"{block}_fetched_at"] = now
        elif cached.get(f"{block}_pct") is not None:
            src = cached
            merged[f"{block}_fetched_at"] = cached.get(
                f"{block}_fetched_at", legacy_stamp)
        else:
            merged[f"{block}_pct"] = None
            merged[f"{block}_resets_at"] = None
            continue
        merged[f"{block}_pct"] = src[f"{block}_pct"]
        merged[f"{block}_resets_at"] = src.get(f"{block}_resets_at")
        for extra in BLOCK_EXTRA_FIELDS:
            if src.get(f"{block}_{extra}") is not None:
                merged[f"{block}_{extra}"] = src[f"{block}_{extra}"]
    return merged


def _load_cache(path):
    """Return (data, age_seconds) or (None, None) if cache is missing/broken."""
    try:
        if not os.path.exists(path):
            return None, None
        age = int(time.time() - os.path.getmtime(path))
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, None
        return data, age
    except Exception:
        return None, None


def _save_cache(path, result):
    """Write cache atomically via temp file + rename."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result, f)
        os.rename(tmp, path)
    except Exception as e:
        print(f"[cache] save failed: {e}", file=sys.stderr)


def _fallback_cache(cached, age, max_age, now=None):
    """Turn a cache dict into display data, honouring per-block age.

    Each block is shown only if it is younger than max_age seconds
    (by its own fetched_at, or the file age for legacy caches); older
    blocks come out as None so we don't display misleading values for
    days when the API is unreachable. Reset-minutes are recomputed from
    the stored ISO timestamps so countdowns stay accurate.
    Returns None when no block has usable data."""
    if cached is None or age is None:
        return None
    now = time.time() if now is None else now
    out = {}
    any_data = False
    for block in BLOCKS:
        pct = cached.get(f"{block}_pct")
        fetched_at = cached.get(f"{block}_fetched_at")
        block_age = (now - fetched_at) if fetched_at is not None else age
        if pct is None or block_age > max_age:
            out[f"{block}_pct"] = None
            out[f"{block}_resets_at"] = None
            out[f"{block}_resets_in_min"] = None
            continue
        any_data = True
        resets_at = cached.get(f"{block}_resets_at")
        out[f"{block}_pct"] = pct
        out[f"{block}_resets_at"] = resets_at
        out[f"{block}_resets_in_min"] = _parse_resets_at(resets_at)
        for extra in BLOCK_EXTRA_FIELDS:
            if cached.get(f"{block}_{extra}") is not None:
                out[f"{block}_{extra}"] = cached[f"{block}_{extra}"]
    return out if any_data else None


# ====== claude.ai API ======

def fetch_org_id(session_key):
    """One-time call to discover the organization UUID for the account."""
    resp = cffi_requests.get(
        "https://claude.ai/api/organizations",
        cookies={"sessionKey": session_key},
        impersonate="safari18_0",
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"failed to fetch org id: HTTP {resp.status_code}")
    orgs = resp.json()
    if not orgs:
        raise RuntimeError("no organizations in response")
    return orgs[0]["uuid"]


def fetch_claude_limits(session_key, org_id, cache_path, cache_ttl, max_fallback):
    """Fetch usage limits from claude.ai, with caching and graceful fallback.

    Anything that goes wrong on the network / on claude.ai's side
    (connection errors, 401/403/429/5xx, non-JSON body) falls back to the
    last cached value if it is not too old. A failure inside our own
    parsing raises InternalError (with the same fallback attached) so it
    is not mistaken for an outage."""
    cached, age = _load_cache(cache_path)
    if cached is not None and age is not None and age < cache_ttl:
        return _fallback_cache(cached, age, max_fallback)

    # --- their side: network + HTTP status + JSON decoding ---
    try:
        resp = cffi_requests.get(
            f"https://claude.ai/api/organizations/{org_id}/usage",
            cookies={"sessionKey": session_key},
            impersonate="safari18_0",
            timeout=15,
        )
        if resp.status_code == 401:
            print("[claude] HTTP 401 — sessionKey expired", file=sys.stderr)
            return _fallback_cache(cached, age, max_fallback)
        if resp.status_code == 403:
            print("[claude] HTTP 403 — Cloudflare challenge", file=sys.stderr)
            return _fallback_cache(cached, age, max_fallback)
        if resp.status_code == 429:
            print(f"[claude] HTTP 429 rate limited, cache age={age}s",
                  file=sys.stderr)
            return _fallback_cache(cached, age, max_fallback)
        if resp.status_code != 200:
            print(f"[claude] HTTP {resp.status_code}: {resp.text[:200]}",
                  file=sys.stderr)
            return _fallback_cache(cached, age, max_fallback)
        data = resp.json()
    except Exception as e:
        print(f"[claude] {type(e).__name__}: {e}", file=sys.stderr)
        return _fallback_cache(cached, age, max_fallback)

    # --- our side: parsing and merging. A bug here must not look like
    # an outage, so it is reported via InternalError instead of a silent
    # fallback. The cache is not touched on this path.
    try:
        fresh = _build_result_from_api_data(data)
        merged = _merge_cache(cached, age, fresh)
    except Exception as e:
        raise InternalError(e, limits=_fallback_cache(cached, age, max_fallback)) from e

    missing = [b for b in BLOCKS if fresh.get(f"{b}_pct") is None]
    if missing:
        print(f"[claude] HTTP 200 but no usable data for: {', '.join(missing)}; "
              f"keeping cached values", file=sys.stderr)

    _save_cache(cache_path, merged)
    return _fallback_cache(merged, 0, max_fallback)


# ====== rendering ======

def _load_first_available_font(candidates, size, default=None):
    """Try the candidates in order, return the first font that loads."""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return default or ImageFont.load_default()


def _color_for_pct(pct):
    """Bar colour by utilization: <50% green, 50-80% yellow, >=80% red."""
    if pct is None:
        return COL_DIM
    if pct >= 80:
        return COL_BAR_RED
    if pct >= 50:
        return COL_BAR_YELLOW
    return COL_BAR_GREEN


def _draw_pixel_monster(draw, x, y, scale=3):
    """Pixel-art mascot: orange square body, two eyes, two side arms,
    four legs. Sprite grid is 15x10."""
    sprite = [
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1],
        [1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0],
        [0, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 0],
        [0, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 0],
    ]
    col_main = (255, 95, 50)
    col_dark = (20, 15, 18)
    for ry, row in enumerate(sprite):
        for rx, v in enumerate(row):
            if v == 0:
                continue
            c = col_main if v == 1 else col_dark
            px = x + rx * scale
            py = y + ry * scale
            draw.rectangle([px, py, px + scale - 1, py + scale - 1], fill=c)


def _draw_rounded_bar(draw, x, y, w, h, pct, color):
    """Pill-shaped progress bar."""
    radius = h // 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=COL_BAR_BG)
    if pct is None or pct <= 0:
        return
    fill_w = int(w * min(100, pct) / 100)
    # Ensure the fill is at least round-cap-wide so it doesn't collapse.
    fill_w = max(fill_w, 2 * radius + 1)
    fill_w = min(fill_w, w)
    draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=radius, fill=color)


def _draw_pill(draw, x, y, text, font, padding_x=10, padding_y=5, min_width=0):
    """Rounded rectangle with centered text inside.

    Height is derived from the font's cap height (not from the word's own
    ink box), so a word with a descender ("Weekly") gets the same pill as
    one without ("Current") and its letters sit visually centred.
    min_width lets multiple pills match in size."""
    cap = draw.textbbox((0, 0), "H", font=font, anchor="ls")
    cap_h = cap[3] - cap[1]            # baseline-relative: bbox[1] is negative
    ink = draw.textbbox((0, 0), text, font=font, anchor="ls")
    tw = ink[2] - ink[0]

    w = max(tw + padding_x * 2, min_width)
    h = cap_h + padding_y * 2
    radius = h // 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=COL_PILL_BG)
    text_x = x + (w - tw) // 2 - ink[0]
    baseline = y + padding_y + cap_h
    draw.text((text_x, baseline), text, fill=COL_PILL_TEXT, font=font, anchor="ls")
    return w, h


# Layout presets. Blocks are flowed top-to-bottom; whatever vertical space
# is left after all blocks is spread evenly between them, so the screen is
# filled edge to edge regardless of how many blocks / reset lines there are.
#   f_pct/f_pill/f_meta: font sizes; pill_dy: pill offset from block top;
#   pct_to_bar: gap between the percent text and the bar; bar_h: bar height;
#   bar_to_reset: gap between the bar and the "Resets in" line.
LAYOUT_ROOMY = dict(f_pct=44, f_pill=13, f_meta=15, pill_dy=12,
                    pct_to_bar=12, bar_h=16, bar_to_reset=6)
LAYOUT_COMPACT = dict(f_pct=30, f_pill=12, f_meta=12, pill_dy=4,
                      pct_to_bar=10, bar_h=12, bar_to_reset=5)
BLOCKS_TOP = 40          # first block y (below the header)
BLOCKS_BOTTOM = 236      # last block must end above this line
SIDE = 10                # left/right margin


def _blocks_to_draw(limits):
    """Blocks in display order: dicts with title, pct, reset_min, group.
    The per-model weekly block is only included when the API reported one.
    The "Resets in" line is drawn once per reset group, under the last
    block of that group: weekly limits (all models / Fable) reset together."""
    blocks = [
        dict(title="Current", group="session",
             pct=limits.get("five_hour_pct"),
             reset_min=limits.get("five_hour_resets_in_min")),
        dict(title="Weekly", group="weekly",
             pct=limits.get("seven_day_pct"),
             reset_min=limits.get("seven_day_resets_in_min")),
    ]
    if limits.get("model_weekly_pct") is not None:
        blocks.append(dict(title=limits.get("model_weekly_label") or "Model",
                           group="weekly",
                           pct=limits.get("model_weekly_pct"),
                           reset_min=limits.get("model_weekly_resets_in_min")))
    last_in_group = {b["group"]: i for i, b in enumerate(blocks)}
    for i, b in enumerate(blocks):
        b["show_reset"] = (last_in_group[b["group"]] == i
                           and bool(_format_reset_long(b["reset_min"])))
    return blocks


def _text_height(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font, anchor="ls")
    return bb[3] - bb[1]


def create_image(limits):
    W, H = 240, 240
    img = Image.new("RGB", (W, H), color=COL_BG)
    draw = ImageDraw.Draw(img)

    f_title = _load_first_available_font(FONT_CANDIDATES_TITLE, 24)
    f_tiny = _load_first_available_font(FONT_CANDIDATES_REGULAR, 12)

    # Header: mascot on the left, "Usage" centered, time on the right.
    _draw_pixel_monster(draw, 8, 8, scale=2)

    title = "Usage"
    bbox = draw.textbbox((0, 0), title, font=f_title)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, 6), title, fill=COL_TEXT, font=f_title)

    now = datetime.now().strftime("%H:%M")
    bbox = draw.textbbox((0, 0), now, font=f_tiny)
    draw.text((W - bbox[2] - 8, 12), now, fill=COL_DIM, font=f_tiny)

    if limits is None:
        draw.text((10, 110), "NO DATA", fill=COL_BAR_RED, font=f_title)
        return img

    blocks = _blocks_to_draw(limits)
    L = LAYOUT_COMPACT if len(blocks) > 2 else LAYOUT_ROOMY
    f_pct = _load_first_available_font(FONT_CANDIDATES_BOLD, L["f_pct"])
    f_meta = _load_first_available_font(FONT_CANDIDATES_SEMIBOLD, L["f_meta"])
    f_pill = _load_first_available_font(FONT_CANDIDATES_SEMIBOLD, L["f_pill"])

    # Shared pill width so all labels (Current/Weekly/Fable) match.
    pill_min_width = 0
    for b in blocks:
        bb = draw.textbbox((0, 0), b["title"], font=f_pill, anchor="ls")
        pill_min_width = max(pill_min_width, (bb[2] - bb[0]) + 20)

    # Vertical flow: measure every block, spread the leftover evenly.
    pct_h = _text_height(draw, "100%", f_pct)
    meta_h = _text_height(draw, "Resets in 0d 0h", f_meta)
    bar_dy = pct_h + L["pct_to_bar"]
    heights = []
    for b in blocks:
        h = bar_dy + L["bar_h"]
        if b["show_reset"]:
            h += L["bar_to_reset"] + meta_h
        heights.append(h)
    leftover = BLOCKS_BOTTOM - BLOCKS_TOP - sum(heights)
    gap = leftover / (len(blocks) - 1) if len(blocks) > 1 else 0

    y = BLOCKS_TOP
    for b, h in zip(blocks, heights):
        yi = int(round(y))
        pct = b["pct"]
        pct_text = f"{int(pct)}%" if pct is not None else "—"
        draw.text((SIDE, yi + pct_h), pct_text, fill=COL_TEXT,
                  font=f_pct, anchor="ls")
        _draw_pill(draw, W - pill_min_width - SIDE, yi + L["pill_dy"],
                   b["title"], f_pill, min_width=pill_min_width)
        _draw_rounded_bar(draw, SIDE, yi + bar_dy, W - 2 * SIDE, L["bar_h"],
                          pct, _color_for_pct(pct))
        if b["show_reset"]:
            reset_y = yi + bar_dy + L["bar_h"] + L["bar_to_reset"]
            draw.text((SIDE, reset_y + meta_h), _format_reset_long(b["reset_min"]),
                      fill=COL_TEXT, font=f_meta, anchor="ls")
        y += h + gap

    return img


# ====== device upload ======

def upload_to_geekclock(img, device_ip):
    """Upload the rendered image to a GeekMagic SmallTV over HTTP."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    url = f"http://{device_ip}/doUpload?dir=/image/"
    try:
        r = requests.post(
            url,
            files={"file": ("claude.jpg", buf.getvalue(), "image/jpeg")},
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[geekclock] upload failed: {e}", file=sys.stderr)
        return False


# ====== entrypoint ======

def load_session_key(path):
    """Read sessionKey from file, trimming whitespace/newlines."""
    with open(os.path.expanduser(path), "r") as f:
        return f.read().strip()


def main():
    parser = argparse.ArgumentParser(
        description="Push Claude.ai usage limits to GeekMagic SmallTV clock"
    )
    parser.add_argument(
        "--session-key", "-k",
        default=os.environ.get("CLAUDE_SESSION_KEY"),
        help="sessionKey cookie value from claude.ai (or $CLAUDE_SESSION_KEY)",
    )
    parser.add_argument(
        "--session-key-file",
        default=os.environ.get("CLAUDE_SESSION_KEY_FILE", "~/.claude_session_key"),
        help="path to file with sessionKey (used if --session-key not set)",
    )
    parser.add_argument(
        "--org-id",
        default=os.environ.get("CLAUDE_ORG_ID"),
        help="organization uuid (auto-detected if not provided)",
    )
    parser.add_argument(
        "--device-ip",
        default=os.environ.get("GEEKCLOCK_IP"),
        help="GeekMagic device IP address (or $GEEKCLOCK_IP)",
    )
    parser.add_argument(
        "--cache-path",
        default=os.environ.get("GEEKCLOCK_CACHE_PATH", DEFAULT_CACHE_PATH),
    )
    parser.add_argument(
        "--cache-ttl", type=int,
        default=int(os.environ.get("GEEKCLOCK_CACHE_TTL", DEFAULT_CACHE_TTL)),
        help="seconds between API calls (default: 300)",
    )
    parser.add_argument(
        "--output", "-o",
        help="save image to this path instead of (or in addition to) uploading",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="don't upload, just render image",
    )
    args = parser.parse_args()

    # Resolve sessionKey from arg, env, or file.
    session_key = args.session_key
    if not session_key:
        try:
            session_key = load_session_key(args.session_key_file)
        except FileNotFoundError:
            print(f"error: session key not provided. Set --session-key or "
                  f"$CLAUDE_SESSION_KEY, or create {args.session_key_file}",
                  file=sys.stderr)
            sys.exit(1)

    # Resolve orgId from arg/env, or discover it via API.
    org_id = args.org_id
    if not org_id:
        try:
            org_id = fetch_org_id(session_key)
            print(f"[info] detected org_id={org_id} "
                  f"(set CLAUDE_ORG_ID to skip this step)", file=sys.stderr)
        except Exception as e:
            print(f"error: cannot detect org id: {e}", file=sys.stderr)
            sys.exit(1)

    # Fetch limits (with caching/fallback) and render. A bug in our own
    # parsing still renders whatever fallback data is available, but the
    # run exits non-zero so it is visible in cron logs / monitoring.
    exit_code = 0
    try:
        limits = fetch_claude_limits(
            session_key, org_id,
            args.cache_path, args.cache_ttl, DEFAULT_CACHE_MAX_FALLBACK,
        )
    except InternalError as e:
        print(f"error: internal failure while parsing usage response: {e}",
              file=sys.stderr)
        traceback.print_exception(
            type(e.cause), e.cause, e.cause.__traceback__, file=sys.stderr)
        limits = e.limits
        exit_code = EXIT_INTERNAL_ERROR
    img = create_image(limits)

    if args.output:
        img.save(args.output)
        print(f"[ok] saved to {args.output}")

    if not args.dry_run:
        if not args.device_ip:
            print("error: device IP not provided. Set --device-ip or "
                  "$GEEKCLOCK_IP", file=sys.stderr)
            sys.exit(1)
        ok = upload_to_geekclock(img, args.device_ip)
        if limits:
            cl_str = (f"5h={limits.get('five_hour_pct')}% "
                      f"7d={limits.get('seven_day_pct')}%")
            if limits.get("model_weekly_pct") is not None:
                cl_str += (f" {limits.get('model_weekly_label') or 'model'}="
                           f"{limits.get('model_weekly_pct')}%")
        else:
            cl_str = "no data"
        print(f"[{datetime.now():%H:%M:%S}] {cl_str}, uploaded={ok}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
