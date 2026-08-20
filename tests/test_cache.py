"""Тесты на разбор времени сброса и на кеш.

Главное здесь — test_http_200_with_empty_body_must_not_poison_cache:
он падает на текущем коде и помечен xfail(strict=True). Остальные тесты
фиксируют поведение, которое сейчас корректно, чтобы его не сломать.
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
    # Сброс уже прошёл — наружу не должно уйти отрицательное число.
    assert gc._parse_resets_at("2020-01-01T00:00:00Z") == 0


def test_parse_garbage_returns_none():
    assert gc._parse_resets_at("не дата") is None
    assert gc._parse_resets_at("") is None
    assert gc._parse_resets_at(None) is None


def test_parse_naive_timestamp_returns_none():
    # Без таймзоны вычитание aware-даты бросает TypeError; ловится в except.
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
    }


def test_build_result_from_empty_payload_is_all_none():
    # Пустой/изменившийся ответ молча превращается в словарь из None —
    # исключения нет, и вызывающий код не может отличить его от данных.
    assert gc._build_result_from_api_data({}) == {
        "five_hour_pct": None, "five_hour_resets_at": None,
        "seven_day_pct": None, "seven_day_resets_at": None,
    }


# ====== fetch_claude_limits: отравление кеша ======

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def stale_good_cache(tmp_path):
    """Живой кеш с настоящими данными, но старше TTL — значит, будет запрос."""
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "five_hour_pct": 42, "five_hour_resets_at": _iso_in(60),
        "seven_day_pct": 7, "seven_day_resets_at": _iso_in(600),
    }))
    old = time.time() - 600  # TTL по умолчанию 300 с
    os.utime(path, (old, old))
    return str(path)


def _patch_response(monkeypatch, response):
    """Подменяет сетевой вызов и возвращает счётчик обращений.

    Считаем снаружи, а не бросаем исключение внутри: fetch_claude_limits
    обёрнут в except Exception и проглотил бы даже AssertionError, молча
    уйдя в фолбэк, — проверка получилась бы декоративной.
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


@pytest.mark.xfail(strict=True, reason=(
    "Известный баг: HTTP 200 с пустым или изменившимся телом даёт словарь "
    "из None, который _save_cache кладёт поверх рабочего кеша. Данные "
    "теряются безвозвратно, а не на 12 часов: свежий mtime заставляет "
    "_load_cache отдавать пустышку как валидную, и фолбэк не срабатывает."))
def test_http_200_with_empty_body_must_not_poison_cache(
        monkeypatch, stale_good_cache):
    _patch_response(monkeypatch, _FakeResponse(200, {}))
    gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert json.loads(open(stale_good_cache).read())["five_hour_pct"] == 42


@pytest.mark.xfail(strict=True, reason=(
    "Следствие того же бага: после отравления кеш выглядит свежим, "
    "и следующий запуск в пределах TTL показывает на экране прочерки."))
def test_next_run_after_empty_body_still_shows_data(
        monkeypatch, stale_good_cache):
    calls = _patch_response(monkeypatch, _FakeResponse(200, {}))
    gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert len(calls) == 1

    # Следующий запуск через минуту: кеш только что переписан, значит свежий,
    # и в сеть мы уже не идём — на экран уходит то, что лежит в файле.
    out = gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    assert len(calls) == 1, "второго запроса быть не должно: кеш свежий"
    assert out["five_hour_pct"] == 42


@pytest.mark.xfail(strict=True, reason=(
    "Схема ломается по кускам: если из ответа пропадёт только seven_day, "
    "проверка на полностью пустое тело не сработает, и в кеш ляжет "
    "половина данных с затёртой неделей."))
def test_partial_payload_must_not_wipe_the_other_block(
        monkeypatch, stale_good_cache):
    _patch_response(monkeypatch, _FakeResponse(200, {
        "five_hour": {"utilization": 55, "resets_at": _iso_in(120)},
    }))
    gc.fetch_claude_limits("key", "org", stale_good_cache, 300, 43200)
    cache = json.loads(open(stale_good_cache).read())
    assert cache["five_hour_pct"] == 55      # свежий блок обновился
    assert cache["seven_day_pct"] == 7       # старый блок уцелел


@pytest.fixture
def cache_with_old_seven_day(tmp_path):
    """Кеш, где недельный блок не обновлялся 13 часов, а пятичасовой свежий.

    Метки времени поблочные: один mtime на файл не отличает эти два случая.
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


@pytest.mark.xfail(strict=True, reason=(
    "Поблочный мерж требует поблочных меток времени. Возраст берётся из "
    "os.path.getmtime — один на весь файл, поэтому дописанный старый блок "
    "выглядит свежим, и предохранитель на 12 часов его не выбросит."))
def test_merged_block_expires_by_its_own_timestamp(
        monkeypatch, cache_with_old_seven_day):
    _patch_response(monkeypatch, _FakeResponse(200, {
        "five_hour": {"utilization": 55, "resets_at": _iso_in(120)},
    }))
    out = gc.fetch_claude_limits(
        "key", "org", cache_with_old_seven_day, 300, 43200)
    assert out["five_hour_pct"] == 55
    # Неделя не приходит 13 часов — честнее прочерк, чем бодрые 7%.
    assert out["seven_day_pct"] is None

    # И метка недельного блока не должна помолодеть от чужого успеха:
    # сейчас поля просто нет, возраст один на файл.
    cache = json.loads(open(cache_with_old_seven_day).read())
    assert time.time() - cache["seven_day_fetched_at"] > 12 * 3600
