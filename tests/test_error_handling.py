"""Tests separating "their" errors from "ours" in fetch_claude_limits.

claude.ai being unreachable (network, 401/403/429/5xx, non-JSON body) is a
quiet fallback to the cache. A failure in our own parsing is an
InternalError carrying the fallback: main() renders what it can and exits
non-zero.
"""

import contextlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import geekclock_claude as gc


def _iso_in(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def stale_good_cache(tmp_path):
    """A cache with real data, but older than TTL — so a request will be made."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
        "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600),
    }))
    old = time.time() - 600  # default TTL is 300 s
    os.utime(path, (old, old))
    return str(path)


@pytest.fixture
def no_cache(tmp_path):
    """No cache — e.g. the first run after a reboot: /tmp was wiped."""
    return str(tmp_path / "cache.json")


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


# ====== their side: quiet fallback ======

def test_network_error_falls_back_to_cache(monkeypatch, stale_good_cache):
    """claude.ai is unreachable — show the last known numbers."""
    def _no_network(*a, **kw):
        raise ConnectionError("Failed to connect to claude.ai")

    monkeypatch.setattr(gc.cffi_requests, "get", _no_network)
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42
    assert out["seven_day_pct"] == 7


def test_non_json_body_falls_back_to_cache(monkeypatch, stale_good_cache):
    """HTTP 200 with a Cloudflare HTML page instead of JSON is their problem
    too: fallback is appropriate, and the cache must not be touched."""
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: _FakeResponse(200, None,
                                                       "<html>challenge</html>"))
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 42


# ====== our side: InternalError ======

def _break_our_parsing(monkeypatch):
    """The claude.ai response is fine — our own parsing breaks."""
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: _FakeResponse(200, {
                            "five_hour": {"utilization": 55,
                                          "resets_at": _iso_in(120)},
                            "seven_day": {"utilization": 8,
                                          "resets_at": _iso_in(600)},
                        }))

    def _our_bug(data):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(gc, "_build_result_from_api_data", _our_bug)


def test_outage_is_not_flagged_as_our_fault(monkeypatch, stale_good_cache):
    """The other half of the invariant: an outage on their side is a normal
    fallback. claude.ai being unreachable must keep going to the quiet
    fallback path.
    """
    def _no_network(*a, **kw):
        raise ConnectionError("Failed to connect to claude.ai")

    monkeypatch.setattr(gc.cffi_requests, "get", _no_network)
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42


def test_outage_without_cache_returns_none_quietly(monkeypatch, no_cache):
    """Same, but after a reboot: no cache, we show NO DATA and exit quietly.
    Not our fault, nothing to crash on or signal."""
    def _no_network(*a, **kw):
        raise ConnectionError("Failed to connect to claude.ai")

    monkeypatch.setattr(gc.cffi_requests, "get", _no_network)
    assert gc.fetch_claude_limits("key", "org", no_cache, 300, 43200) is None


def test_bug_in_our_own_code_raises_internal_error(
        monkeypatch, stale_good_cache):
    _break_our_parsing(monkeypatch)
    with pytest.raises(gc.InternalError) as ei:
        gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)

    # The fallback is not lost: it travels in the exception, not the return value.
    assert ei.value.limits["five_hour_pct"] == 42


def test_our_bug_without_cache_still_reports_itself(monkeypatch, no_cache):
    _break_our_parsing(monkeypatch)
    with pytest.raises(gc.InternalError) as ei:
        gc.fetch_claude_limits("key", "org", no_cache, 300, 43200)

    # Nothing to draw — NO DATA — but the cause is named: we broke.
    assert ei.value.limits is None


def test_our_bug_does_not_touch_the_cache(monkeypatch, stale_good_cache):
    """Parsing failed before _save_cache, so the cache is intact. Pinned so
    that handling our own error never starts writing the file."""
    _break_our_parsing(monkeypatch)
    with contextlib.suppress(Exception):
        gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 42


# ====== main(): InternalError -> render the fallback, exit non-zero ======

def _run_main(monkeypatch, tmp_path, limits_or_exc):
    out_png = tmp_path / "out.png"
    monkeypatch.setattr("sys.argv", [
        "geekclock_claude", "--session-key", "k", "--org-id", "o",
        "--dry-run", "--output", str(out_png),
        "--cache-path", str(tmp_path / "cache.json"),
    ])

    def _fetch(*a, **kw):
        if isinstance(limits_or_exc, Exception):
            raise limits_or_exc
        return limits_or_exc

    monkeypatch.setattr(gc, "fetch_claude_limits", _fetch)
    with pytest.raises(SystemExit) as ei:
        gc.main()
    return ei.value.code, out_png


def test_main_exits_nonzero_on_internal_error_but_still_renders(
        monkeypatch, tmp_path):
    fallback = {"five_hour_pct": 42, "five_hour_resets_in_min": 60,
                "seven_day_pct": 7, "seven_day_resets_in_min": 600}
    code, out_png = _run_main(
        monkeypatch, tmp_path,
        gc.InternalError(TypeError("boom"), limits=fallback))
    assert code == gc.EXIT_INTERNAL_ERROR
    assert out_png.exists(), "the fallback image must be rendered"


def test_main_exits_nonzero_on_internal_error_without_fallback(
        monkeypatch, tmp_path):
    code, out_png = _run_main(
        monkeypatch, tmp_path, gc.InternalError(TypeError("boom")))
    assert code == gc.EXIT_INTERNAL_ERROR
    assert out_png.exists(), "NO DATA must be rendered as well"


def test_main_exits_zero_on_outage_fallback(monkeypatch, tmp_path):
    """An outage on their side is not our fault: exit code is zero."""
    code, _ = _run_main(monkeypatch, tmp_path, None)
    assert code == 0
