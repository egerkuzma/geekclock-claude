"""Тесты на широкий `except Exception` в fetch_claude_limits.

Он накрывает не только сетевой вызов, но и весь разбор ответа. Поэтому
недоступность claude.ai и ошибка в нашем собственном коде выглядят
одинаково: тихий фолбэк на кеш, одна строка в stderr, exit code 0.
Первое — задуманное поведение, второе — нет.

Два теста фиксируют то, что сейчас работает правильно. Третий помечен
xfail(strict): он требует, чтобы баг в разборе не маскировался под аварию
на той стороне.
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
    """Кеш с настоящими данными, но старше TTL — значит, будет запрос."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
        "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600),
    }))
    old = time.time() - 600  # TTL по умолчанию 300 с
    os.utime(path, (old, old))
    return str(path)


@pytest.fixture
def no_cache(tmp_path):
    """Кеша нет — например, первый запуск после ребута: /tmp вычищен."""
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

def _break_our_parsing(monkeypatch):
    """Ответ от claude.ai корректный — ломается наш собственный разбор."""
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
    """Вторая половина инварианта: авария на той стороне — штатный фолбэк.

    Зелёный сейчас и обязан остаться зелёным после фикса: чинить надо так,
    чтобы недоступность claude.ai по-прежнему уходила в тихий фолбэк.
    """
    def _no_network(*a, **kw):
        raise ConnectionError("Failed to connect to claude.ai")

    monkeypatch.setattr(gc.cffi_requests, "get", _no_network)
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert out["five_hour_pct"] == 42


def test_outage_without_cache_returns_none_quietly(monkeypatch, no_cache):
    """Та же вторая половина, но после ребута: кеша нет, показываем NO DATA
    и выходим тихо. Это не наша ошибка, ронять и сигналить нечего."""
    def _no_network(*a, **kw):
        raise ConnectionError("Failed to connect to claude.ai")

    monkeypatch.setattr(gc.cffi_requests, "get", _no_network)
    assert gc.fetch_claude_limits("key", "org", no_cache, 300, 43200) is None


@pytest.mark.xfail(strict=True, reason=(
    "except Exception в fetch_claude_limits обёрнут вокруг всего блока, "
    "включая разбор ответа. Ошибка в нашем коде (TypeError, KeyError, "
    "опечатка в имени поля) неотличима от недоступности claude.ai: тихий "
    "фолбэк, одна строка в stderr и exit code 0. Ронять часы не нужно — "
    "нужно своё исключение с данными фолбэка внутри, чтобы main() "
    "нарисовал что есть и вышел с ненулевым кодом."))
def test_bug_in_our_own_code_raises_internal_error(
        monkeypatch, stale_good_cache):
    _break_our_parsing(monkeypatch)
    with pytest.raises(gc.InternalError) as ei:
        gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)

    # Фолбэк не потерян: он едет в исключении, а не в возвращаемом значении.
    assert ei.value.limits["five_hour_pct"] == 42


@pytest.mark.xfail(strict=True, reason=(
    "Признак в ключе словаря не переживает случай без кеша: функция "
    "возвращает None, и ехать ему негде. /tmp чистится при перезагрузке, "
    "так что первый утренний запуск — ровно этот случай."))
def test_our_bug_without_cache_still_reports_itself(monkeypatch, no_cache):
    _break_our_parsing(monkeypatch)
    with pytest.raises(gc.InternalError) as ei:
        gc.fetch_claude_limits("key", "org", no_cache, 300, 43200)

    # Рисовать нечего — NO DATA, — но причина названа: сломались мы.
    assert ei.value.limits is None


def test_our_bug_does_not_touch_the_cache(monkeypatch, stale_good_cache):
    """Зелёный: разбор упал до _save_cache, так что кеш цел. Фиксируем,
    чтобы фикс не начал писать в файл на пути обработки собственной ошибки.

    suppress — потому что сейчас функция молча возвращает фолбэк, а после
    фикса бросит InternalError; проверяем файл, а не способ выхода.
    """
    _break_our_parsing(monkeypatch)
    with contextlib.suppress(Exception):
        gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 42


# Следующим шагом, уже после фикса: main() ловит InternalError, рисует
# e.limits (или NO DATA, если там None) и завершается ненулевым кодом.
# Проверка на SystemExit.code — одна, тонкая, отдельным PR.

# Ключ в возвращаемом словаре на эту роль не годится: без кеша функция
# возвращает None, и признаку негде ехать — а /tmp чистится при ребуте,
# так что случай «кеша нет» наступает каждое утро.
