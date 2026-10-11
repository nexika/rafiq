"""Where the user is, for "what's the weather?" when no city is set.

GNOME's location service (GeoClue) first: it stays on the computer and follows the system's
location setting. Then, only when the user turned on place.ip_lookup, an IP lookup at ipapi.co,
since that sends their IP address to a third party.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GEOCLUE = "org.freedesktop.GeoClue2"
CITY_ACCURACY = 4          # GClueAccuracyLevel: the city is enough for the weather
IP_LOOKUP_URL = "https://ipapi.co/json/"


@dataclass
class Place:
    latitude: float
    longitude: float
    name: str = ""         # GeoClue gives coordinates only
    country: str = ""


def from_geoclue(timeout: float = 5.0) -> Place | None:
    """Ask GeoClue over the system D-Bus. None when it is missing, off, refused or too slow."""
    try:
        from jeepney import DBusAddress, Properties, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import DBusErrorResponse, unwrap_msg
    except ImportError:
        return None
    try:
        with open_dbus_connection(bus="SYSTEM") as conn:
            # The client lives as long as this connection, so every call goes through it.
            def call(msg):
                return unwrap_msg(conn.send_and_get_reply(msg, timeout=timeout))   # raises on a D-Bus error

            manager = DBusAddress("/org/freedesktop/GeoClue2/Manager", GEOCLUE, f"{GEOCLUE}.Manager")
            client = DBusAddress(call(new_method_call(manager, "GetClient"))[0], GEOCLUE, f"{GEOCLUE}.Client")
            call(Properties(client).set("DesktopId", "s", "rafiq"))
            call(Properties(client).set("RequestedAccuracyLevel", "u", CITY_ACCURACY))
            call(new_method_call(client, "Start"))
            try:
                deadline = time.monotonic() + timeout
                found = call(Properties(client).get("Location"))[0][1]
                while found == "/" and time.monotonic() < deadline:   # "/" until the first fix
                    time.sleep(0.2)
                    found = call(Properties(client).get("Location"))[0][1]
                if found == "/":
                    return None
                fix = Properties(DBusAddress(found, GEOCLUE, f"{GEOCLUE}.Location"))
                return Place(float(call(fix.get("Latitude"))[0][1]), float(call(fix.get("Longitude"))[0][1]))
            finally:
                call(new_method_call(client, "Stop"))
    except (OSError, DBusErrorResponse, ValueError, IndexError) as exc:   # no bus, no GeoClue, refused, slow
        logger.info("GeoClue gave no location: %s", exc)
        return None


def from_ip(get: Callable) -> Place | None:
    import httpx

    try:
        data = get(IP_LOOKUP_URL, params={})
        if data.get("error"):
            logger.info("IP lookup refused: %s", data.get("reason"))
            return None
        return Place(float(data["latitude"]), float(data["longitude"]), data.get("city") or "",
                     data.get("country_name") or "")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.info("IP lookup failed: %s", exc)
        return None


def here(cfg: dict, get: Callable, geoclue: Callable[[], Place | None] = from_geoclue) -> Place | None:
    place = geoclue()
    if place is None and cfg.get("place", {}).get("ip_lookup"):
        place = from_ip(get)
    return place
