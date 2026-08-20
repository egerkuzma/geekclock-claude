"""Тесты на широкий `except Exception` в fetch_claude_limits.

Он накрывает не только сетевой вызов, но и весь разбор ответа. Поэтому
недоступность claude.ai и ошибка в нашем собственном коде выглядят
одинаково: тихий фолбэк на кеш, одна строка в stderr, exit code 0.
Первое — задуманное поведение, второе — нет.

Два теста фиксируют то, что сейчас работает правильно. Третий помечен
xfail(strict): он требует, чтобы баг в разборе не маскировался под аварию
на той стороне.
"""

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
    """Кеш с настоящими данными, но старше TTL — значит, будет запрос."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
        "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600),
    }))
    old = time.time() - 600  # TTL по умолчанию 300 с
    os.utime(path, (old, old))
    return str(path)


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


# ====== то, что сейчас работает правильно ======

def test_network_error_falls_back_to_cache(monkeypatch, stale_good_cache):
    """claude.ai недоступен — показываем последние известные цифры."""
    def _no_network(*a, **kw):
        raise ConnectionError("Failed to connect to claude.ai")

    monkeypatch.setattr(gc.cffi_requests, "get", _no_network)
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42
    assert out["seven_day_pct"] == 7


def test_non_json_body_falls_back_to_cache(monkeypatch, stale_good_cache):
    """HTTP 200 с HTML-страницей Cloudflare вместо JSON — тоже их проблема,
    фолбэк уместен, и кеш при этом трогать нельзя."""
    monkeypatch.setattr(gc.cffi_requests, "get",
                        lambda *a, **kw: _FakeResponse(200, None,
                                                       "<html>challenge</html>"))
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 42


# ====== то, что не работает ======

@pytest.mark.xfail(strict=True, reason=(
    "except Exception в fetch_claude_limits обёрнут вокруг всего блока, "
    "включая разбор ответа. Ошибка в нашем коде (TypeError, KeyError, "
    "опечатка в имени поля) неотличима от недоступности claude.ai: тихий "
    "фолбэк, одна строка в stderr и exit code 0. При запуске раз в минуту "
    "такая строка тонет в логе, а на экране висят вчерашние проценты — "
    "ровно то, чего фолбэк и не должен допускать."))
def test_bug_in_our_own_code_is_not_disguised_as_outage(
        monkeypatch, stale_good_cache):
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

    # Ответ от claude.ai корректный — сломались мы. Это должно быть видно,
    # а не превращаться в «показываем старое, как при 500».
    with pytest.raises(TypeError):
        gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
