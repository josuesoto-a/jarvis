import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


WEATHER_CODES = {
    0: "cielo despejado",
    1: "principalmente despejado",
    2: "parcialmente nublado",
    3: "nublado",
    45: "niebla",
    48: "niebla con escarcha",
    51: "llovizna ligera",
    53: "llovizna moderada",
    55: "llovizna intensa",
    56: "llovizna helada ligera",
    57: "llovizna helada intensa",
    61: "lluvia ligera",
    63: "lluvia moderada",
    65: "lluvia fuerte",
    66: "lluvia helada ligera",
    67: "lluvia helada fuerte",
    71: "nevada ligera",
    73: "nevada moderada",
    75: "nevada fuerte",
    77: "granos de nieve",
    80: "chubascos ligeros",
    81: "chubascos moderados",
    82: "chubascos fuertes",
    85: "chubascos de nieve ligeros",
    86: "chubascos de nieve fuertes",
    95: "tormenta eléctrica",
    96: "tormenta eléctrica con granizo ligero",
    99: "tormenta eléctrica con granizo fuerte",
}


def fetch_json(url: str, params: dict) -> dict:
    query = urlencode(params)

    request = Request(
        f"{url}?{query}",
        headers={
            "User-Agent": "Jarvis/0.3"
        },
    )

    with urlopen(
        request,
        timeout=10,
    ) as response:

        return json.loads(
            response.read().decode("utf-8")
        )


def geocode_location(location: str) -> dict:
    data = fetch_json(
        GEOCODING_URL,
        {
            "name": location,
            "count": 5,
            "language": "es",
            "format": "json",
        },
    )

    results = data.get("results", [])

    if not results:
        raise ValueError(
            f"No encontré la ubicación: {location}"
        )

    # Por ahora usamos el resultado mejor clasificado
    # por el servicio de geocodificación.
    place = results[0]

    return {
        "name": place.get("name"),
        "admin1": place.get("admin1"),
        "country": place.get("country"),
        "country_code": place.get("country_code"),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone": place.get("timezone"),
    }


def weather_description(code: int | None) -> str:
    if code is None:
        return "condición desconocida"

    return WEATHER_CODES.get(
        int(code),
        f"código meteorológico {code}",
    )


def get_current_weather(location: str) -> dict:
    place = geocode_location(location)

    data = fetch_json(
        WEATHER_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],

            "current": ",".join(
                [
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "precipitation",
                    "weather_code",
                    "cloud_cover",
                    "wind_speed_10m",
                    "wind_direction_10m",
                ]
            ),

            "timezone": "auto",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        },
    )

    current = data.get("current")

    if not current:
        raise RuntimeError(
            "Open-Meteo no devolvió condiciones actuales."
        )

    code = current.get("weather_code")

    result = {
        "location": place,
        "time": current.get("time"),
        "condition": weather_description(code),
        "weather_code": code,
        "temperature_c": current.get("temperature_2m"),
        "feels_like_c": current.get("apparent_temperature"),
        "humidity_percent": current.get("relative_humidity_2m"),
        "precipitation_mm": current.get("precipitation"),
        "cloud_cover_percent": current.get("cloud_cover"),
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "wind_direction_degrees": current.get("wind_direction_10m"),
    }

    return result


def weather_for_jarvis(location: str) -> str:
    weather = get_current_weather(location)

    place = weather["location"]

    place_name = place["name"]

    if place.get("admin1"):
        place_name += f", {place['admin1']}"

    if place.get("country"):
        place_name += f", {place['country']}"

    return (
        f"Condiciones meteorológicas actuales para {place_name}. "
        f"Hora de los datos: {weather['time']}. "
        f"Condición: {weather['condition']}. "
        f"Temperatura: {weather['temperature_c']} °C. "
        f"Sensación térmica: {weather['feels_like_c']} °C. "
        f"Humedad relativa: {weather['humidity_percent']} %. "
        f"Precipitación reciente: {weather['precipitation_mm']} mm. "
        f"Nubosidad: {weather['cloud_cover_percent']} %. "
        f"Viento: {weather['wind_speed_kmh']} km/h. "
        f"Dirección del viento: {weather['wind_direction_degrees']} grados."
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(
            'Uso: python weather_tool.py "Cartago, Costa Rica"'
        )
        raise SystemExit(1)

    location = " ".join(sys.argv[1:])

    try:
        print(
            weather_for_jarvis(location)
        )

    except Exception as error:
        print(
            f"[WEATHER ERROR] {error}"
        )

        raise SystemExit(1)


if __name__ == "__main__":
    main()
