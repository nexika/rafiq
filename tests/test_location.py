import httpx

from rafiq import config, location
from rafiq.location import Place
from rafiq.tools import everyday

FORECAST = {"current": {"temperature_2m": 18, "apparent_temperature": 17, "weather_code": 3,
                        "wind_speed_10m": 9, "relative_humidity_2m": 70},
            "daily": {"time": ["2026-10-10"], "weather_code": [61], "temperature_2m_min": [12],
                      "temperature_2m_max": [19], "precipitation_probability_max": [60]}}


def ctx_with(**place):
    cfg = config.load()
    cfg["place"].update(place)
    return type("Ctx", (), {"cfg": cfg})()


def ip_reply(url, params):
    assert "ipapi.co" in url
    return {"latitude": 48.85, "longitude": 2.35, "city": "Paris", "country_name": "France"}


def test_geoclue_comes_first_and_the_ip_is_not_sent():
    def get(url, params):
        raise AssertionError("no IP lookup when GeoClue answers")

    place = location.here({"place": {"ip_lookup": True}}, get, geoclue=lambda: Place(30.04, 31.24))
    assert (place.latitude, place.longitude) == (30.04, 31.24)


def test_ip_lookup_needs_consent():
    def get(url, params):
        raise AssertionError("the IP must not leave the computer without consent")

    assert location.here({"place": {"ip_lookup": False}}, get, geoclue=lambda: None) is None
    assert location.here({"place": {}}, get, geoclue=lambda: None) is None


def test_ip_lookup_with_consent_when_geoclue_has_nothing():
    place = location.here({"place": {"ip_lookup": True}}, ip_reply, geoclue=lambda: None)
    assert place == Place(48.85, 2.35, "Paris", "France")


def test_ip_lookup_failure_is_no_location():
    def down(url, params):
        raise httpx.ConnectError("offline")

    assert location.here({"place": {"ip_lookup": True}}, down, geoclue=lambda: None) is None
    assert location.from_ip(lambda url, params: {"error": True, "reason": "RateLimited"}) is None


def test_ip_lookup_is_off_by_default():
    assert config.load()["place"]["ip_lookup"] is False


def test_weather_without_a_city_uses_your_location():
    seen = {}

    def get(url, params):
        assert "geocoding" not in url
        seen.update(params)
        return FORECAST

    text = everyday.weather("", 1, ctx=ctx_with(), get=get, here=lambda cfg, get: Place(30.04, 31.24))
    assert (seen["latitude"], seen["longitude"]) == (30.04, 31.24)
    assert text.startswith("your location (30.04, 31.24): now 18°C") and "light rain" in text


def test_weather_names_the_city_found_by_ip():
    text = everyday.weather("", 1, ctx=ctx_with(), get=lambda url, params: FORECAST,
                            here=lambda cfg, get: Place(48.85, 2.35, "Paris", "France"))
    assert text.startswith("Paris, France: now 18°C")


def test_configured_city_wins_over_location():
    def here(cfg, get):
        raise AssertionError("no location lookup when a city is set")

    def get(url, params):
        if "geocoding" in url:
            return {"results": [{"name": "Cairo", "country": "Egypt", "latitude": 30.0, "longitude": 31.2}]}
        return FORECAST

    assert everyday.weather("", 1, ctx=ctx_with(city="Cairo"), get=get, here=here).startswith("Cairo, Egypt")


def test_weather_without_city_or_location_says_how_to_fix_it():
    text = everyday.weather("", ctx=ctx_with(), here=lambda cfg, get: None)
    assert text.startswith("error") and "place.city" in text and "place.ip_lookup" in text
