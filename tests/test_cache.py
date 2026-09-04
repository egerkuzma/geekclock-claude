"""Tests for reset-time parsing and the cache.

The cache is per-block: five_hour and seven_day carry their own fetched_at
stamps. An empty or partial HTTP 200 body does not wipe the old blocks, and
staleness is judged by the block's own stamp, not by the file mtime.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import geekclock_claude as gc


# ====== _parse_resets_at ======

def _iso_in(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def test_parse_future_timestamp_returns_minutes():
    assert gc._parse_resets_at(_iso_in(90)) == pytest.approx(89, abs=1)


def test_parse_accepts_z_suffix():
    dt = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0)
    iso_z = dt.isoformat().replace("+00:00", "Z")
    assert gc._parse_resets_at(iso_z) == pytest.approx(29, abs=1)


def test_parse_past_timestamp_is_clamped_to_zero():
    # The reset is already in the past — a negative number must not leak out.
    assert gc._parse_resets_at("2020-01-01T00:00:00Z") == 0


def test_parse_garbage_returns_none():
    assert gc._parse_resets_at("not a date") is None
    assert gc._parse_resets_at("") is None
    assert gc._parse_resets_at(None) is None


def test_parse_naive_timestamp_returns_none():
    # Without a timezone, subtracting an aware datetime raises TypeError; caught by the except.
    assert gc._parse_resets_at("2030-01-01T00:00:00") is None


# ====== _format_reset_long ======

@pytest.mark.parametrize("minutes,expected", [
    (37, "Resets in 37m"),
    (95, "Resets in 1h 35m"),
    (120, "Resets in 2h"),
    (9120, "Resets in 6d 8h"),
    (8640, "Resets in 6d"),
    (0, ""),
    (None, ""),
])
def test_format_reset_long(minutes, expected):
    assert gc._format_reset_long(minutes) == expected


# ====== _fallback_cache ======

def test_fallback_returns_data_when_fresh_enough():
    cached = {"five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
              "seven_day_pct": 10, "seven_day_resets_at": _iso_in(600)}
    out = gc._fallback_cache(cached, age=100, max_age=43200)
    assert out["five_hour_pct"] == 42
    assert out["five_hour_resets_in_min"] == pytest.approx(59, abs=1)


def test_fallback_discards_stale_cache():
    cached = {"five_hour_pct": 42}
    assert gc._fallback_cache(cached, age=43201, max_age=43200) is None
    assert gc._fallback_cache(None, None, 43200) is None


# ====== _build_result_from_api_data ======

def test_build_result_from_valid_payload():
    resets = _iso_in(60)
    data = {"five_hour": {"utilization": 42, "resets_at": resets},
            "seven_day": {"utilization": 7, "resets_at": resets}}
    assert gc._build_result_from_api_data(data) == {
        "five_hour_pct": 42, "five_hour_resets_at": resets,
        "seven_day_pct": 7, "seven_day_resets_at": resets,
        "model_weekly_pct": None, "model_weekly_resets_at": None,
        "model_weekly_label": None,
    }


def test_build_result_from_empty_payload_is_all_none():
    # An empty/changed response turns into a dict of Nones;
    # fetch_claude_limits treats a None block as "not reported" and keeps
    # the previous cached value of that block.
    assert gc._build_result_from_api_data({}) == {
        "five_hour_pct": None, "five_hour_resets_at": None,
        "seven_day_pct": None, "seven_day_resets_at": None,
        "model_weekly_pct": None, "model_weekly_resets_at": None,
        "model_weekly_label": None,
    }


# ====== fetch_claude_limits: cache poisoning ======

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def stale_good_cache(tmp_path):
    """A healthy cache with real data, but older than TTL — so a request will be made."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
        "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600),
    }))
    old = time.time() - 600  # default TTL is 300 s
    os.utime(path, (old, old))
    return str(path)


def _patch_response(monkeypatch, response):
    """Stub the network call and return a call counter.

    We count from the outside instead of raising inside: fetch_claude_limits
    is wrapped in except Exception and would swallow even an AssertionError,
    silently falling back — the check would be decorative.
    """
    calls = []

    def _get(*a, **kw):
        calls.append(kw.get("url") or (a[0] if a else None))
        return response

    monkeypatch.setattr(gc.cffi_requests, "get", _get)
    return calls


def test_http_500_falls_back_to_cache(monkeypatch, stale_good_cache):
    _patch_response(monkeypatch, _FakeResponse(500, {}))
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42


def test_http_401_falls_back_to_cache(monkeypatch, stale_good_cache):
    _patch_response(monkeypatch, _FakeResponse(401, {}))
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42


def test_http_200_updates_cache(monkeypatch, stale_good_cache):
    resets = _iso_in(120)
    _patch_response(monkeypatch, _FakeResponse(200, {
        "five_hour": {"utilization": 55, "resets_at": resets},
        "seven_day": {"utilization": 8, "resets_at": resets},
    }))
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 55
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 55


def test_http_200_with_empty_body_must_not_poison_cache(
        monkeypatch, stale_good_cache):
    _patch_response(monkeypatch, _FakeResponse(200, {}))
    gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 42


def test_next_run_after_empty_body_still_shows_data(
        monkeypatch, stale_good_cache):
    calls = _patch_response(monkeypatch, _FakeResponse(200, {}))
    gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert len(calls) == 1

    # Next run a minute later: the cache was just rewritten, so it is fresh,
    # no network call — whatever is in the file goes to the screen.
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert len(calls) == 1, "no second request expected: cache is fresh"
    assert out["five_hour_pct"] == 42


def test_partial_payload_must_not_wipe_the_other_block(
        monkeypatch, stale_good_cache):
    _patch_response(monkeypatch, _FakeResponse(200, {
        "five_hour": {"utilization": 55, "resets_at": _iso_in(120)},
    }))
    gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    cache = json.loads(open(stale_good_cache).read())
    assert cache["five_hour_pct"] == 55      # fresh block updated
    assert cache["seven_day_pct"] == 7       # old block survived


@pytest.fixture
def cache_with_old_seven_day(tmp_path):
    """A cache whose weekly block is 13 hours old while the 5-hour one is fresh.

    Timestamps are per-block: a single file mtime cannot tell these apart.
    """
    path = tmp_path / "cache.json"
    now = time.time()
    path.write_text(json.dumps({
        "five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
        "five_hour_fetched_at": now - 600,
        "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600),
        "seven_day_fetched_at": now - 13 * 3600,
    }))
    os.utime(path, (now - 600, now - 600))
    return str(path)


def test_merged_block_expires_by_its_own_timestamp(
        monkeypatch, cache_with_old_seven_day):
    _patch_response(monkeypatch, _FakeResponse(200, {
        "five_hour": {"utilization": 55, "resets_at": _iso_in(120)},
    }))
    out = gc.fetch_claude_limits(
        "key", "org", cache_with_old_seven_day, 300, 43200)
    assert out["five_hour_pct"] == 55
    # The weekly block has not arrived for 13 hours — a dash is more honest than a cheerful 7%.
    assert out["seven_day_pct"] is None

    # And the weekly block's stamp must not get younger from another block's success.
    cache = json.loads(open(cache_with_old_seven_day).read())
    assert time.time() - cache["seven_day_fetched_at"] > 12 * 3600
