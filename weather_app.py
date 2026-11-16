"""OpenWeather CLI: текущая погода по городу или координатам."""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import os
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("API_KEY")

GEO_URL = "https://api.openweathermap.org/geo/1.0/direct"
WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"

CACHE_FILE = Path(__file__).resolve().parent / "weather_cache.json"
CACHE_TTL = timedelta(hours=3)
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAYS = (1, 2, 4)
COORD_TOLERANCE = 1e-4

MISSING_KEY_MESSAGE = (
    "Ошибка: API_KEY не найден. Добавьте ключ OpenWeather в файл .env."
)

MENU_WIDTH = 32
MENU_BANNER = (
    f"{'=' * MENU_WIDTH}\n"
    f"{'OPENWEATHER CLI'.center(MENU_WIDTH).rstrip()}\n"
    f"{'=' * MENU_WIDTH}\n"
    "\n"
    "1 — Погода по городу\n"
    "2 — Погода по координатам\n"
    "0 — Выход\n"
    "\n"
)

_CONFIRM_YES = {"д", "да", "y", "yes"}


class WeatherAppError(Exception):
    """Ожидаемая ошибка API, сети или пользовательского ввода."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        auth: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.auth = auth


def _api_key() -> str:
    if API_KEY is None or not str(API_KEY).strip():
        raise WeatherAppError(MISSING_KEY_MESSAGE)
    return str(API_KEY).strip()


def _error_for_status(status: int) -> WeatherAppError | None:
    if status < 400:
        return None
    if status == 401:
        return WeatherAppError(
            "Ошибка: недействительный или ещё не активированный API-ключ (HTTP 401).",
            auth=True,
        )
    if status == 403:
        return WeatherAppError(
            "Ошибка: доступ запрещён (HTTP 403).",
            auth=True,
        )
    if status == 404:
        return WeatherAppError("Ошибка: ресурс не найден (HTTP 404).")
    if status == 429:
        return WeatherAppError(
            "Ошибка: превышен лимит запросов (HTTP 429). Повторите попытку позже.",
            retryable=True,
        )
    if 500 <= status <= 599:
        return WeatherAppError(
            f"Ошибка: временная ошибка сервера (HTTP {status}). Повторите попытку позже.",
            retryable=True,
        )
    return WeatherAppError(f"Ошибка: некорректный запрос (HTTP {status}).")


def _request_json(url: str, params: dict[str, Any]) -> Any:
    """GET-запрос с повторами для 429, 5xx и временных сетевых сбоев."""
    _api_key()
    last_error: WeatherAppError | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.Timeout:
            last_error = WeatherAppError(
                "Ошибка: время ожидания ответа истекло.",
                retryable=True,
            )
        except requests.ConnectionError:
            last_error = WeatherAppError(
                "Ошибка: отсутствует интернет-соединение.",
                retryable=True,
            )
        except requests.RequestException:
            raise WeatherAppError(
                "Ошибка: не удалось выполнить запрос к сервису погоды."
            ) from None
        else:
            status_error = _error_for_status(response.status_code)
            if status_error is None:
                try:
                    return response.json()
                except ValueError:
                    raise WeatherAppError(
                        "Ошибка: сервер вернул некорректный JSON."
                    ) from None
            if not status_error.retryable:
                raise status_error
            last_error = status_error

        if attempt == MAX_RETRIES:
            break
        time.sleep(RETRY_DELAYS[attempt])

    if last_error is None:
        raise WeatherAppError(
            "Ошибка: не удалось получить ответ от сервиса погоды.",
            retryable=True,
        )
    raise last_error from None


def _is_real_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_coordinates(lat: float, lon: float) -> None:
    """Проверяет, что координаты — числа из допустимых диапазонов."""
    if not _is_real_number(lat):
        raise WeatherAppError("Ошибка: широта должна быть числом.")
    if not _is_real_number(lon):
        raise WeatherAppError("Ошибка: долгота должна быть числом.")
    if not -90 <= float(lat) <= 90:
        raise WeatherAppError(
            "Ошибка: широта должна быть в диапазоне от -90 до 90."
        )
    if not -180 <= float(lon) <= 180:
        raise WeatherAppError(
            "Ошибка: долгота должна быть в диапазоне от -180 до 180."
        )


def _parse_user_float(raw: str, label: str) -> float:
    text = raw.strip().replace(",", ".")
    if not text:
        raise WeatherAppError(f"Ошибка: {label} должна быть числом.")
    try:
        value = float(text)
    except ValueError:
        raise WeatherAppError(f"Ошибка: {label} должна быть числом.") from None
    if value != value or value in {float("inf"), float("-inf")}:
        raise WeatherAppError(f"Ошибка: {label} должна быть числом.")
    return value


def parse_latitude(raw: str) -> float:
    value = _parse_user_float(raw, "широта")
    if not -90 <= value <= 90:
        raise WeatherAppError(
            "Ошибка: широта должна быть в диапазоне от -90 до 90."
        )
    return value


def parse_longitude(raw: str) -> float:
    value = _parse_user_float(raw, "долгота")
    if not -180 <= value <= 180:
        raise WeatherAppError(
            "Ошибка: долгота должна быть в диапазоне от -180 до 180."
        )
    return value


def get_coordinates(city: str) -> tuple[float, float]:
    """Возвращает широту и долготу первого города из геокодинга."""
    name = city.strip()
    if not name:
        raise WeatherAppError("Ошибка: название города не может быть пустым.")

    payload = _request_json(
        GEO_URL,
        {
            "q": name,
            "limit": 1,
            "lang": "ru",
            "appid": _api_key(),
        },
    )
    if not isinstance(payload, list):
        raise WeatherAppError("Ошибка: некорректный ответ геокодинга.")
    if not payload:
        raise WeatherAppError(f"Ошибка: город «{name}» не найден.")

    place = payload[0]
    if not isinstance(place, dict):
        raise WeatherAppError("Ошибка: в ответе отсутствуют обязательные поля.")
    try:
        lat = float(place["lat"])
        lon = float(place["lon"])
    except (KeyError, TypeError, ValueError):
        raise WeatherAppError(
            "Ошибка: в ответе отсутствуют обязательные поля."
        ) from None
    validate_coordinates(lat, lon)
    return lat, lon


def _weather_parts(data: object) -> tuple[float, str, str]:
    if not isinstance(data, dict):
        raise WeatherAppError("Ошибка: в ответе отсутствуют обязательные поля.")
    try:
        temp = data["main"]["temp"]
        description = data["weather"][0]["description"]
        name = data["name"]
    except (KeyError, IndexError, TypeError):
        raise WeatherAppError(
            "Ошибка: в ответе отсутствуют обязательные поля."
        ) from None
    if not _is_real_number(temp):
        raise WeatherAppError("Ошибка: в ответе отсутствуют обязательные поля.")
    if not isinstance(description, str) or not description.strip():
        raise WeatherAppError("Ошибка: в ответе отсутствуют обязательные поля.")
    if not isinstance(name, str) or not name.strip():
        raise WeatherAppError("Ошибка: в ответе отсутствуют обязательные поля.")
    return float(temp), description, name


def get_weather_by_coordinates(lat: float, lon: float) -> dict:
    """Возвращает текущую погоду OpenWeather для координат."""
    validate_coordinates(lat, lon)
    payload = _request_json(
        WEATHER_URL,
        {
            "lat": lat,
            "lon": lon,
            "appid": _api_key(),
            "units": "metric",
            "lang": "ru",
        },
    )
    _weather_parts(payload)
    if not isinstance(payload, dict):
        raise WeatherAppError("Ошибка: в ответе отсутствуют обязательные поля.")
    return payload


def format_weather(data: dict) -> str:
    """Форматирует температуру с одним знаком после запятой."""
    temp, description, name = _weather_parts(data)
    return f"Погода в {name}: {temp:.1f}°C, {description}"


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc)


def _number_in_range(value: object, low: float, high: float) -> bool:
    return _is_real_number(value) and low <= float(value) <= high


def _read_cache() -> dict[str, Any] | None:
    try:
        text = CACHE_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    city = data.get("city")
    if not isinstance(city, str) or not city.strip():
        return None
    if _parse_utc(data.get("fetched_at")) is None:
        return None
    if not _number_in_range(data.get("lat"), -90, 90):
        return None
    if not _number_in_range(data.get("lon"), -180, 180):
        return None
    weather = data.get("weather")
    try:
        _weather_parts(weather)
    except WeatherAppError:
        return None
    return data


def load_cache() -> dict[str, Any] | None:
    """Возвращает свежий и целый кэш либо None."""
    entry = _read_cache()
    if entry is None:
        return None
    moment = _parse_utc(entry.get("fetched_at"))
    if moment is None:
        return None
    age = datetime.now(timezone.utc) - moment
    if age > CACHE_TTL:
        return None
    return entry


def _same_city(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return left.strip().casefold() == right.strip().casefold()


def _same_coords(lat1: object, lon1: object, lat2: float, lon2: float) -> bool:
    if not _is_real_number(lat1) or not _is_real_number(lon1):
        return False
    return (
        abs(float(lat1) - lat2) <= COORD_TOLERANCE
        and abs(float(lon1) - lon2) <= COORD_TOLERANCE
    )


def get_cached_weather(
    *,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any] | None:
    """Кэш подходит только для того же города или тех же координат."""
    if city is None and (lat is None or lon is None):
        return None
    entry = load_cache()
    if entry is None:
        return None
    if city is not None and not _same_city(entry.get("city"), city):
        return None
    if lat is not None or lon is not None:
        if lat is None or lon is None:
            return None
        if not _same_coords(entry.get("lat"), entry.get("lon"), lat, lon):
            return None
    return entry


def save_cache(city: str, lat: float, lon: float, weather: dict[str, Any]) -> None:
    """Сохраняет последний успешный ответ без секретов."""
    temp, description, name = _weather_parts(weather)
    label = city.strip()
    if not label:
        print("Предупреждение: не удалось сохранить кэш.")
        return
    payload = {
        "city": label,
        "lat": float(lat),
        "lon": float(lon),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "weather": {
            "main": {"temp": temp},
            "weather": [{"description": description}],
            "name": name,
        },
    }
    try:
        CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        print("Предупреждение: не удалось сохранить кэш.")


def handle_weather_error(
    exc: WeatherAppError,
    *,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    """Печатает ошибку. При сетевом сбое может показать подходящий кэш."""
    cached = None
    if exc.retryable and not exc.auth:
        cached = get_cached_weather(city=city, lat=lat, lon=lon)
    if cached is None:
        print(exc)
        return
    try:
        answer = input(
            "Не удалось получить новые данные. Показать сохранённый результат? (д/н): "
        )
    except EOFError:
        print()
        print(exc)
        return
    if answer.strip().casefold() in _CONFIRM_YES:
        try:
            line = format_weather(cached["weather"])
        except WeatherAppError as format_error:
            print(format_error)
            return
        print(line)
        print("Показан сохранённый результат.")
        return
    print(exc)


def run_city_request(city: str) -> None:
    name = city.strip()
    if not name:
        print("Ошибка: название города не может быть пустым.")
        return
    try:
        lat, lon = get_coordinates(name)
        weather = get_weather_by_coordinates(lat, lon)
    except WeatherAppError as exc:
        handle_weather_error(exc, city=name)
        return
    save_cache(name, lat, lon, weather)
    print(format_weather(weather))


def run_coordinates_request(lat: float, lon: float) -> None:
    try:
        validate_coordinates(lat, lon)
        weather = get_weather_by_coordinates(lat, lon)
    except WeatherAppError as exc:
        handle_weather_error(exc, lat=lat, lon=lon)
        return
    save_cache(str(weather["name"]), lat, lon, weather)
    print(format_weather(weather))


def _ask_city() -> None:
    city = input("Введите название города: ")
    run_city_request(city)


def _ask_coordinates() -> None:
    raw_lat = input("Введите широту: ")
    try:
        lat = parse_latitude(raw_lat)
    except WeatherAppError as exc:
        print(exc)
        return
    raw_lon = input("Введите долготу: ")
    try:
        lon = parse_longitude(raw_lon)
    except WeatherAppError as exc:
        print(exc)
        return
    run_coordinates_request(lat, lon)


def _menu_loop() -> None:
    while True:
        print(MENU_BANNER, end="")
        try:
            choice = input("Выберите действие: ").strip()
        except EOFError:
            print()
            print("До свидания.")
            return
        if choice == "0":
            print("До свидания.")
            return
        if choice == "1":
            _ask_city()
        elif choice == "2":
            _ask_coordinates()
        elif choice == "":
            print("Ошибка: введите номер действия.")
        else:
            print("Ошибка: неизвестный пункт меню. Введите 1, 2 или 0.")
        print()


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            continue


def main() -> None:
    _configure_output()
    try:
        _api_key()
    except WeatherAppError as exc:
        print(exc)
        raise SystemExit(1) from None
    try:
        _menu_loop()
    except KeyboardInterrupt:
        print("\nРабота программы прервана.")


if __name__ == "__main__":
    main()
