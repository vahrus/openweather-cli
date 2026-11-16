"""Тесты OpenWeather CLI. HTTP-запросы замоканы, реальный ключ не используется."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests
from unittest.mock import MagicMock

import weather_app
from weather_app import WeatherAppError


WEATHER = {
    "main": {"temp": 12.4, "humidity": 80},
    "weather": [{"description": "пасмурно", "main": "Clouds"}],
    "name": "Москва",
    "coord": {"lat": 55.7558, "lon": 37.6173},
}


class FakeResponse:
    def __init__(self, status_code: int, payload: object | None = None, *, broken_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._broken_json = broken_json

    def json(self):
        if self._broken_json:
            raise ValueError("invalid json")
        return self._payload


def _forbid_network(*_args, **_kwargs):
    raise AssertionError("HTTP в тесте должен быть замокан")


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(weather_app, "API_KEY", "test-api-key")
    monkeypatch.setattr(weather_app, "CACHE_FILE", tmp_path / "weather_cache.json")
    monkeypatch.setattr(weather_app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(weather_app.requests, "get", _forbid_network)


def _mock_get(monkeypatch, impl):
    monkeypatch.setattr(weather_app.requests, "get", impl)
    return impl


def test_get_coordinates_success(monkeypatch):
    response = FakeResponse(200, [{"lat": 55.7558, "lon": 37.6173, "name": "Москва"}])
    get = _mock_get(monkeypatch, MagicMock(return_value=response))

    lat, lon = weather_app.get_coordinates("Москва")

    assert (lat, lon) == (55.7558, 37.6173)
    get.assert_called_once()
    args, kwargs = get.call_args
    assert args[0] == "https://api.openweathermap.org/geo/1.0/direct"
    assert weather_app.GEO_URL == args[0]
    assert "appid" not in args[0]
    assert kwargs["timeout"] == 10
    assert kwargs["params"] == {
        "q": "Москва",
        "limit": 1,
        "lang": "ru",
        "appid": "test-api-key",
    }


def test_get_weather_success(monkeypatch):
    get = _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(200, WEATHER)))

    payload = weather_app.get_weather_by_coordinates(55.75, 37.62)

    assert payload["name"] == "Москва"
    assert payload["main"]["temp"] == 12.4
    args, kwargs = get.call_args
    assert args[0] == "https://api.openweathermap.org/data/2.5/weather"
    assert weather_app.WEATHER_URL == args[0]
    assert "appid" not in args[0]
    assert kwargs["timeout"] == 10
    assert kwargs["params"] == {
        "lat": 55.75,
        "lon": 37.62,
        "appid": "test-api-key",
        "units": "metric",
        "lang": "ru",
    }


def test_geocoding_empty(monkeypatch):
    get = _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(200, [])))

    with pytest.raises(WeatherAppError, match="не найден"):
        weather_app.get_coordinates("Атлантида")

    assert get.call_count == 1


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_api_key(value, monkeypatch, capsys):
    monkeypatch.setattr(weather_app, "API_KEY", value)

    def _no_input(_prompt=""):
        raise AssertionError("input не должен вызываться")

    monkeypatch.setattr("builtins.input", _no_input)

    with pytest.raises(WeatherAppError, match="API_KEY не найден"):
        weather_app.get_coordinates("Москва")

    with pytest.raises(SystemExit) as excinfo:
        weather_app.main()

    assert excinfo.value.code == 1
    assert capsys.readouterr().out.strip() == weather_app.MISSING_KEY_MESSAGE


def test_http_401(monkeypatch, capsys):
    weather_app.save_cache("Москва", 55.7558, 37.6173, WEATHER)
    sleeps: list[float] = []
    prompts: list[str] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(401, {"cod": 401})))
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "д")

    weather_app.run_city_request("Москва")

    output = capsys.readouterr().out
    assert "HTTP 401" in output
    assert "недействительный" in output
    assert "пасмурно" not in output
    assert get.call_count == 1
    assert sleeps == []
    assert prompts == []


def test_http_403(monkeypatch, capsys):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(403, {"cod": 403})))

    with pytest.raises(WeatherAppError, match="HTTP 403") as excinfo:
        weather_app.get_weather_by_coordinates(55.0, 37.0)

    assert "доступ запрещён" in str(excinfo.value)
    assert excinfo.value.auth is True
    assert excinfo.value.retryable is False
    assert get.call_count == 1
    assert sleeps == []


def test_http_429_retries(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(
        monkeypatch,
        MagicMock(return_value=FakeResponse(429, {"cod": 429})),
    )

    with pytest.raises(WeatherAppError, match="HTTP 429") as excinfo:
        weather_app.get_weather_by_coordinates(55.0, 37.0)

    assert excinfo.value.retryable is True
    assert get.call_count == weather_app.MAX_RETRIES + 1
    assert sleeps == [1, 2, 4]


def test_http_429_recovers(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    responses = [
        FakeResponse(429, {"cod": 429}),
        FakeResponse(429, {"cod": 429}),
        FakeResponse(200, WEATHER),
    ]
    get = _mock_get(monkeypatch, MagicMock(side_effect=responses))

    payload = weather_app.get_weather_by_coordinates(55.75, 37.62)

    assert payload["name"] == "Москва"
    assert get.call_count == 3
    assert sleeps == [1, 2]


def test_http_5xx(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(502, {"cod": 502})))

    with pytest.raises(WeatherAppError, match="временная ошибка сервера") as excinfo:
        weather_app.get_weather_by_coordinates(55.0, 37.0)

    assert "HTTP 502" in str(excinfo.value)
    assert excinfo.value.retryable is True
    assert get.call_count == 4
    assert sleeps == [1, 2, 4]


def test_timeout(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(monkeypatch, MagicMock(side_effect=requests.Timeout("slow")))

    with pytest.raises(WeatherAppError, match="время ожидания") as excinfo:
        weather_app.get_coordinates("Москва")

    assert excinfo.value.retryable is True
    assert get.call_count == 4
    assert sleeps == [1, 2, 4]


def test_connection_error(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(
        monkeypatch,
        MagicMock(side_effect=requests.ConnectionError("offline")),
    )

    with pytest.raises(WeatherAppError, match="интернет-соединение") as excinfo:
        weather_app.get_weather_by_coordinates(55.0, 37.0)

    assert excinfo.value.retryable is True
    assert get.call_count == 4
    assert sleeps == [1, 2, 4]


def test_invalid_json(monkeypatch):
    get = _mock_get(
        monkeypatch,
        MagicMock(return_value=FakeResponse(200, broken_json=True)),
    )

    with pytest.raises(WeatherAppError, match="некорректный JSON"):
        weather_app.get_weather_by_coordinates(55.0, 37.0)

    assert get.call_count == 1


def test_missing_fields(monkeypatch):
    payloads = [
        {"weather": [{"description": "пасмурно"}], "name": "Москва"},
        {"main": {"temp": 1}, "weather": [], "name": "Москва"},
        {"main": {"temp": 1}, "weather": [{"description": "ясно"}]},
        [{"name": "Москва"}],
    ]
    for payload in payloads:
        if isinstance(payload, list):
            _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(200, payload)))
            with pytest.raises(WeatherAppError, match="обязательные поля"):
                weather_app.get_coordinates("Москва")
            continue
        _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(200, payload)))
        with pytest.raises(WeatherAppError, match="обязательные поля"):
            weather_app.get_weather_by_coordinates(55.0, 37.0)


def test_format_temperature():
    assert (
        weather_app.format_weather(WEATHER)
        == "Погода в Москва: 12.4°C, пасмурно"
    )
    integer_temp = {
        "main": {"temp": 12},
        "weather": [{"description": "ясно"}],
        "name": "Сочи",
    }
    assert weather_app.format_weather(integer_temp) == "Погода в Сочи: 12.0°C, ясно"
    negative = {
        "main": {"temp": -1.04},
        "weather": [{"description": "снег"}],
        "name": "Мурманск",
    }
    assert (
        weather_app.format_weather(negative)
        == "Погода в Мурманск: -1.0°C, снег"
    )


def test_validate_coordinates():
    weather_app.validate_coordinates(-90, -180)
    weather_app.validate_coordinates(90, 180)
    weather_app.validate_coordinates(55.75, 37.62)

    with pytest.raises(WeatherAppError, match="широта должна быть в диапазоне"):
        weather_app.validate_coordinates(90.1, 0)
    with pytest.raises(WeatherAppError, match="широта должна быть в диапазоне"):
        weather_app.validate_coordinates(-90.1, 0)
    with pytest.raises(WeatherAppError, match="долгота должна быть в диапазоне"):
        weather_app.validate_coordinates(0, 180.1)
    with pytest.raises(WeatherAppError, match="долгота должна быть в диапазоне"):
        weather_app.validate_coordinates(0, -180.1)
    with pytest.raises(WeatherAppError, match="широта должна быть числом"):
        weather_app.validate_coordinates(True, 0)  # type: ignore[arg-type]

    assert weather_app.parse_latitude("55,75") == 55.75
    assert weather_app.parse_longitude("-37.5") == -37.5
    assert weather_app.parse_latitude("-90") == -90
    assert weather_app.parse_longitude("180") == 180

    with pytest.raises(WeatherAppError, match="широта должна быть числом"):
        weather_app.parse_latitude("север")
    with pytest.raises(WeatherAppError, match="долгота должна быть числом"):
        weather_app.parse_longitude("")
    with pytest.raises(WeatherAppError, match="широта должна быть в диапазоне"):
        weather_app.parse_latitude("95")
    with pytest.raises(WeatherAppError, match="долгота должна быть в диапазоне"):
        weather_app.parse_longitude("200")


def test_save_cache():
    weather_app.save_cache("Москва", 55.7558, 37.6173, WEATHER)

    raw = weather_app.CACHE_FILE.read_text(encoding="utf-8")
    data = json.loads(raw)

    assert set(data) == {"city", "lat", "lon", "fetched_at", "weather"}
    assert data["city"] == "Москва"
    assert data["lat"] == 55.7558
    assert data["lon"] == 37.6173
    moment = datetime.fromisoformat(data["fetched_at"])
    assert moment.utcoffset() == timedelta(0)
    assert data["weather"]["main"]["temp"] == 12.4
    assert data["weather"]["weather"][0]["description"] == "пасмурно"
    assert data["weather"]["name"] == "Москва"
    assert "test-api-key" not in raw
    assert "appid" not in raw
    assert "API_KEY" not in raw


def test_use_fresh_cache(monkeypatch, capsys):
    weather_app.save_cache("Москва", 55.7558, 37.6173, WEATHER)
    found = weather_app.get_cached_weather(city="москва")
    assert found is not None
    assert found["weather"]["main"]["temp"] == 12.4

    _mock_get(monkeypatch, MagicMock(side_effect=requests.ConnectionError("offline")))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "д")

    weather_app.run_city_request("Москва")

    output = capsys.readouterr().out
    assert "Погода в Москва: 12.4°C, пасмурно" in output
    assert "Показан сохранённый результат." in output


def test_reject_stale_cache(monkeypatch, capsys):
    weather_app.save_cache("Москва", 55.7558, 37.6173, WEATHER)
    data = json.loads(weather_app.CACHE_FILE.read_text(encoding="utf-8"))
    stale = datetime.now(timezone.utc) - timedelta(hours=3, seconds=5)
    data["fetched_at"] = stale.isoformat()
    weather_app.CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")

    assert weather_app.load_cache() is None
    prompts: list[str] = []
    _mock_get(monkeypatch, MagicMock(side_effect=requests.ConnectionError("offline")))
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "д")

    weather_app.run_city_request("Москва")

    output = capsys.readouterr().out
    assert "пасмурно" not in output
    assert "интернет-соединение" in output
    assert prompts == []


def test_reject_other_city_cache(monkeypatch, capsys):
    weather_app.save_cache("Москва", 55.7558, 37.6173, WEATHER)
    prompts: list[str] = []
    _mock_get(monkeypatch, MagicMock(side_effect=requests.ConnectionError("offline")))
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "д")

    weather_app.run_city_request("Казань")

    output = capsys.readouterr().out
    assert weather_app.get_cached_weather(city="Казань") is None
    assert weather_app.get_cached_weather(city="Москва") is not None
    assert "пасмурно" not in output
    assert prompts == []


def test_reject_other_coordinates_cache(monkeypatch, capsys):
    weather_app.save_cache("Москва", 55.7558, 37.6173, WEATHER)
    prompts: list[str] = []
    _mock_get(monkeypatch, MagicMock(side_effect=requests.Timeout("slow")))
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "д")

    weather_app.run_coordinates_request(59.93, 30.31)

    output = capsys.readouterr().out
    assert weather_app.get_cached_weather(lat=59.93, lon=30.31) is None
    assert weather_app.get_cached_weather(lat=55.7558, lon=37.6173) is not None
    assert "пасмурно" not in output
    assert prompts == []


def test_corrupted_cache(monkeypatch, capsys):
    samples = ["{", "", "[]", '{"city": "Москва"}', "не json"]
    for sample in samples:
        weather_app.CACHE_FILE.write_text(sample, encoding="utf-8")
        assert weather_app.load_cache() is None

    weather_app.CACHE_FILE.write_bytes(b"\xff\xfe{")
    assert weather_app.load_cache() is None

    naive = {
        "city": "Москва",
        "lat": 55.75,
        "lon": 37.62,
        "fetched_at": "2026-11-15T12:00:00",
        "weather": WEATHER,
    }
    weather_app.CACHE_FILE.write_text(json.dumps(naive), encoding="utf-8")
    assert weather_app.load_cache() is None

    weather_app.CACHE_FILE.write_text("{", encoding="utf-8")
    _mock_get(monkeypatch, MagicMock(side_effect=requests.ConnectionError("offline")))
    weather_app.run_city_request("Москва")
    assert "интернет-соединение" in capsys.readouterr().out


def test_cli_exit(monkeypatch, capsys):
    prompts: list[str] = []

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "0"

    monkeypatch.setattr("builtins.input", _input)

    weather_app.main()

    output = capsys.readouterr().out
    assert "OPENWEATHER CLI" in output
    assert "1 — Погода по городу" in output
    assert "2 — Погода по координатам" in output
    assert "0 — Выход" in output
    assert prompts == ["Выберите действие: "]
    assert "До свидания." in output


def test_cli_invalid_choice_returns_to_menu(monkeypatch, capsys):
    answers = iter(["", "9", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    weather_app.main()

    output = capsys.readouterr().out
    assert "введите номер действия" in output
    assert "неизвестный пункт меню" in output
    assert output.count("OPENWEATHER CLI") == 3
    assert "До свидания." in output


def test_cli_city_flow(monkeypatch, capsys):
    answers = iter(["1", "Москва", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    def fake_get(url, params=None, timeout=None):
        del params, timeout
        if url.endswith("/direct"):
            return FakeResponse(200, [{"lat": 55.7558, "lon": 37.6173}])
        return FakeResponse(200, WEATHER)

    _mock_get(monkeypatch, fake_get)
    weather_app.main()

    output = capsys.readouterr().out
    assert "Погода в Москва: 12.4°C, пасмурно" in output
    assert "До свидания." in output
    saved = json.loads(weather_app.CACHE_FILE.read_text(encoding="utf-8"))
    assert saved["city"] == "Москва"


def test_cli_rejects_non_numeric_coordinates(monkeypatch, capsys):
    answers = iter(["2", "север", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    weather_app.main()

    output = capsys.readouterr().out
    assert "широта должна быть числом" in output
    assert "До свидания." in output


def test_cli_keyboard_interrupt(monkeypatch, capsys):
    def _raise(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise)
    weather_app.main()
    assert "прервана" in capsys.readouterr().out


def test_http_400_and_404_are_not_retried(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(weather_app.time, "sleep", lambda seconds: sleeps.append(seconds))
    get = _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(400, {"cod": 400})))

    with pytest.raises(WeatherAppError, match="HTTP 400"):
        weather_app.get_weather_by_coordinates(55.0, 37.0)

    assert get.call_count == 1
    get.return_value = FakeResponse(404, {"cod": 404})
    with pytest.raises(WeatherAppError, match="ресурс не найден"):
        weather_app.get_coordinates("Москва")
    assert get.call_count == 2
    assert sleeps == []


def test_geocoding_not_a_list(monkeypatch):
    _mock_get(monkeypatch, MagicMock(return_value=FakeResponse(200, {"lat": 1})))
    with pytest.raises(WeatherAppError, match="некорректный ответ геокодинга"):
        weather_app.get_coordinates("Москва")
