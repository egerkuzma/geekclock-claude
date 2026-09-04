"""Tests for the per-model weekly limit (Fable).

In the API response it is not a top-level block but an entry in the
"limits" array with kind="weekly_scoped" and scope.model.display_name.
The fixture is a real response of the usage endpoint from 2026-09-04.
"""

import copy
import json
import os
import pathlib
import time
from datetime import datetime, timedelta, timezone

import pytest

import geekclock_claude as gc

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "usage_response_2026-09-04.json"


def _iso_in(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def real_response():
    return json.loads(FIXTURE.read_text())


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


# ====== response parsing ======

def test_real_response_yields_fable_block(real_response):
    out = gc._build_result_from_api_data(real_response)
    assert out["five_hour_pct"] == 37.0
    assert out["seven_day_pct"] == 27.0
    assert out["model_weekly_pct"] == 32
    assert out["model_weekly_label"] == "Fable"
    assert out["model_weekly_resets_at"] == "2026-09-06T17:59:59.978818+00:00"


def test_no_limits_array_means_no_model_block(real_response):
    del real_response["limits"]
    out = gc._build_result_from_api_data(real_response)
    assert out["five_hour_pct"] == 37.0
    assert out["model_weekly_pct"] is None
    assert out["model_weekly_label"] is None


def test_scoped_limit_without_model_is_ignored(real_response):
    # weekly_scoped by surface, not by model — that is not a "Fable limit".
    real_response["limits"][2]["scope"] = {"model": None, "surface": "cowork"}
    assert gc._build_result_from_api_data(real_response)["model_weekly_pct"] is None


def test_garbage_in_limits_array_is_skipped():
    data = {"limits": ["x", None, {"kind": "weekly_scoped"}, {"kind": "weekly_scoped",
            "scope": {"model": {"display_name": "Fable"}}, "percent": "32"}]}
    out = gc._build_result_from_api_data(data)
    # percent came as a string — so no number, but the model is recognised
    assert out["model_weekly_pct"] is None
    assert out["model_weekly_label"] == "Fable"


def test_first_model_scoped_limit_wins():
    data = {"limits": [
        {"kind": "weekly_scoped", "percent": 10, "resets_at": None,
         "scope": {"model": {"display_name": "Fable"}}},
        {"kind": "weekly_scoped", "percent": 90, "resets_at": None,
         "scope": {"model": {"display_name": "Opus"}}},
    ]}
    out = gc._build_result_from_api_data(data)
    assert (out["model_weekly_pct"], out["model_weekly_label"]) == (10, "Fable")


# ====== cache: the model label travels with the block ======

def test_fetch_caches_and_returns_model_block(monkeypatch, tmp_path, real_response):
    path = str(tmp_path / "cache.json")
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: _FakeResponse(200, real_response))
    out = gc.fetch_claude_limits("key", "org", path, 300, 43200)
    assert out["model_weekly_pct"] == 32
    assert out["model_weekly_label"] == "Fable"
    assert "model_weekly_resets_in_min" in out

    cache = json.loads(open(path).read())
    assert cache["model_weekly_label"] == "Fable"
    assert time.time() - cache["model_weekly_fetched_at"] < 5

    # Second run within TTL — served from cache, label still there.
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: pytest.fail("no network call expected"))
    again = gc.fetch_claude_limits("key", "org", path, 300, 43200)
    assert again["model_weekly_label"] == "Fable"


def test_model_block_survives_response_without_limits(monkeypatch, tmp_path, real_response):
    """The limits array vanished from the response — the Fable block lives on from cache."""
    path = str(tmp_path / "cache.json")
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: _FakeResponse(200, real_response))
    gc.fetch_claude_limits("key", "org", path, 300, 43200)
    old = time.time() - 600
    os.utime(path, (old, old))

    without = copy.deepcopy(real_response)
    del without["limits"]
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: _FakeResponse(200, without))
    out = gc.fetch_claude_limits("key", "org", path, 300, 43200)
    assert out["model_weekly_pct"] == 32
    assert out["model_weekly_label"] == "Fable"


def test_legacy_cache_without_model_block_still_renders(tmp_path):
    """A cache from the old version: no model_weekly_* fields at all."""
    cached = {"five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
              "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600)}
    out = gc._fallback_cache(cached, age=100, max_age=43200)
    assert out["model_weekly_pct"] is None
    assert "model_weekly_label" not in out
    img = gc.create_image(out)
    assert img.size == (240, 240)


# ====== rendering ======

def test_three_blocks_use_compact_layout():
    limits = {"five_hour_pct": 37, "five_hour_resets_in_min": 90,
              "seven_day_pct": 27, "seven_day_resets_in_min": 3000,
              "model_weekly_pct": 32, "model_weekly_resets_in_min": 3000,
              "model_weekly_label": "Fable"}
    assert [b[0] for b in gc._blocks_to_draw(limits)] == ["Current", "Weekly", "Fable"]
    img = gc.create_image(limits)
    assert img.size == (240, 240)
    # The bottom block must fit entirely on screen: the last pixel row is empty.
    last_row = [img.getpixel((x, 239)) for x in range(240)]
    assert all(px == gc.COL_BG for px in last_row)


def test_two_blocks_keep_roomy_layout():
    limits = {"five_hour_pct": 37, "five_hour_resets_in_min": 90,
              "seven_day_pct": 27, "seven_day_resets_in_min": 3000,
              "model_weekly_pct": None}
    assert [b[0] for b in gc._blocks_to_draw(limits)] == ["Current", "Weekly"]
    assert gc.create_image(limits).size == (240, 240)


def test_model_block_without_label_gets_generic_title():
    limits = {"five_hour_pct": 1, "seven_day_pct": 1, "model_weekly_pct": 5}
    assert gc._blocks_to_draw(limits)[2][0] == "Model"
