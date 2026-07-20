#!/usr/bin/env python3
"""
geekclock-claude — push Claude.ai usage limits to a GeekMagic SmallTV clock.

On each run it fetches usage data from claude.ai/api/.../usage via curl_cffi
(Chrome TLS impersonation to bypass Cloudflare), renders a 240x240 image
and uploads it to the device.

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
from datetime import datetime, timezone

import requests
from curl_cffi import requests as cffi_requests
from PIL import Image, ImageDraw, ImageFont


# --- defaults ---
DEFAULT_CACHE_PATH = "/tmp/geekclock_claude_cache.json"
DEFAULT_CACHE_TTL = 300              # 5 minutes
DEFAULT_CACHE_MAX_FALLBACK = 43200   # 12 hours; older cache is discarded

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

def _build_result_from_api_data(data):
    """Extract the fields we care about from the API response.
    We store ISO timestamps verbatim; reset-minutes are recomputed on read
    so cached entries always show fresh countdowns."""
    return {
        "five_hour_pct": (data.get("five_hour") or {}).get("utilization"),
        "five_hour_resets_at": (data.get("five_hour") or {}).get("resets_at"),
        "seven_day_pct": (data.get("seven_day") or {}).get("utilization"),
        "seven_day_resets_at": (data.get("seven_day") or {}).get("resets_at"),
    }


def _add_computed_fields(result):
    """Add freshly computed reset-minutes from stored ISO timestamps."""
    if result is None:
        return None
    out = dict(result)
    out["five_hour_resets_in_min"] = _parse_resets_at(result.get("five_hour_resets_at"))
    out["seven_day_resets_in_min"] = _parse_resets_at(result.get("seven_day_resets_at"))
    return out


def _load_cache(path):
    """Return (data, age_seconds) or (None, None) if cache is missing/broken."""
    try:
        if not os.path.exists(path):
            return None, None
        age = int(time.time() - os.path.getmtime(path))
        with open(path, "r") as f:
            return json.load(f), age
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


def _fallback_cache(cached, age, max_age):
    """Return cached data only if it's fresh enough to be useful.
    Stale cache (>max_age seconds) is discarded so we don't display
    misleading values for days when the API is unreachable."""
    if cached is None or age is None or age > max_age:
        return None
    return _add_computed_fields(cached)


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
    """Fetch usage limits from claude.ai, with caching and graceful fallback
    on errors (401/403/429 -> serve last cached value if not too old)."""
    cached, age = _load_cache(cache_path)
    if cached is not None and age is not None and age < cache_ttl:
        return _add_computed_fields(cached)

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

        result = _build_result_from_api_data(resp.json())
        _save_cache(cache_path, result)
        return _add_computed_fields(result)
    except Exception as e:
        print(f"[claude] {type(e).__name__}: {e}", file=sys.stderr)
    return _fallback_cache(cached, age, max_fallback)


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
    min_width lets multiple pills (Current/Weekly) match in size."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    text_top_offset = bbox[1]

    w = max(tw + padding_x * 2, min_width)
    h = th + padding_y * 2
    radius = h // 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=COL_PILL_BG)
    text_x = x + (w - tw) // 2 - bbox[0]
    text_y = y + (h - th) // 2 - text_top_offset
    draw.text((text_x, text_y), text, fill=COL_PILL_TEXT, font=font)
    return w, h


def create_image(limits):
    W, H = 240, 240
    img = Image.new("RGB", (W, H), color=COL_BG)
    draw = ImageDraw.Draw(img)

    f_title = _load_first_available_font(FONT_CANDIDATES_TITLE, 32)
    f_pct = _load_first_available_font(FONT_CANDIDATES_BOLD, 44)
    f_meta = _load_first_available_font(FONT_CANDIDATES_SEMIBOLD, 15)
    f_pill = _load_first_available_font(FONT_CANDIDATES_SEMIBOLD, 13)
    f_tiny = _load_first_available_font(FONT_CANDIDATES_REGULAR, 12)

    # Header: mascot on the left, "Usage" centered, time on the right.
    _draw_pixel_monster(draw, 6, 6, scale=3)

    title = "Usage"
    bbox = draw.textbbox((0, 0), title, font=f_title)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, 2), title, fill=COL_TEXT, font=f_title)

    now = datetime.now().strftime("%H:%M")
    bbox = draw.textbbox((0, 0), now, font=f_tiny)
    draw.text((W - bbox[2] - 8, 16), now, fill=COL_DIM, font=f_tiny)

    if limits is None:
        draw.text((10, 110), "NO DATA", fill=COL_BAR_RED, font=f_title)
        return img

    # Compute a shared width for both pills so Current/Weekly match.
    pill_min_width = 0
    for txt in ("Current", "Weekly"):
        bb = draw.textbbox((0, 0), txt, font=f_pill)
        pill_min_width = max(pill_min_width, (bb[2] - bb[0]) + 20)

    # Block 1: Current (5-hour rolling window)
    block_y = 42
    fh_pct = limits.get("five_hour_pct")
    pct_text = f"{int(fh_pct)}%" if fh_pct is not None else "—"
    draw.text((10, block_y), pct_text, fill=COL_TEXT, font=f_pct)
    _draw_pill(draw, W - pill_min_width - 10, block_y + 12,
               "Current", f_pill, min_width=pill_min_width)
    _draw_rounded_bar(draw, 10, block_y + 48, W - 20, 16,
                      fh_pct, _color_for_pct(fh_pct))
    fh_reset = _format_reset_long(limits.get("five_hour_resets_in_min"))
    if fh_reset:
        draw.text((10, block_y + 68), fh_reset, fill=COL_TEXT, font=f_meta)

    # Block 2: Weekly (7-day rolling window)
    block_y = 135
    sd_pct = limits.get("seven_day_pct")
    pct_text = f"{int(sd_pct)}%" if sd_pct is not None else "—"
    draw.text((10, block_y), pct_text, fill=COL_TEXT, font=f_pct)
    _draw_pill(draw, W - pill_min_width - 10, block_y + 12,
               "Weekly", f_pill, min_width=pill_min_width)
    _draw_rounded_bar(draw, 10, block_y + 48, W - 20, 16,
                      sd_pct, _color_for_pct(sd_pct))
    sd_reset = _format_reset_long(limits.get("seven_day_resets_in_min"))
    if sd_reset:
        draw.text((10, block_y + 68), sd_reset, fill=COL_TEXT, font=f_meta)

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

    # Fetch limits (with caching/fallback) and render.
    limits = fetch_claude_limits(
        session_key, org_id,
        args.cache_path, args.cache_ttl, DEFAULT_CACHE_MAX_FALLBACK,
    )
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
        else:
            cl_str = "no data"
        print(f"[{datetime.now():%H:%M:%S}] {cl_str}, uploaded={ok}")


if __name__ == "__main__":
    main()
