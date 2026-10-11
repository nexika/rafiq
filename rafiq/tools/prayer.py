"""Prayer times, the Hijri date, Ramadan countdowns and Quran recitation. Off by default
(prayer.enabled in the config).

Times are calculated here, with the user's method, from the sun's position (the praytimes.org
formulas): nothing is fetched. Recitation streams verse by verse from EveryAyah, which allows it, and
nothing is kept on disk. Reminders are set only for the prayers the user names in prayer.reminders.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import tool
from .everyday import _get_json, geocode

PRAYERS = ["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]
NAMES_AR = {"fajr": "الفجر", "sunrise": "الشروق", "dhuhr": "الظهر", "asr": "العصر", "maghrib": "المغرب",
            "isha": "العشاء"}


@dataclass(frozen=True)
class Method:
    name: str
    fajr: float                 # degrees below the horizon
    isha: float = 0.0           # degrees below the horizon, or
    isha_minutes: int = 0       # minutes after maghrib (Umm al-Qura, Qatar)


METHODS = {
    "egyptian": Method("Egyptian General Authority of Survey", 19.5, 17.5),
    "umm_al_qura": Method("Umm al-Qura, Makkah", 18.5, isha_minutes=90),
    "mwl": Method("Muslim World League", 18, 17),
    "isna": Method("Islamic Society of North America", 15, 15),
    "karachi": Method("University of Islamic Sciences, Karachi", 18, 18),
    "dubai": Method("Dubai", 18.2, 18.2),
    "kuwait": Method("Kuwait", 18, 17.5),
    "qatar": Method("Qatar", 18, isha_minutes=90),
    "singapore": Method("Majlis Ugama Islam Singapura", 20, 18),
    "france": Method("Union des organisations islamiques de France", 12, 12),
}


@dataclass(frozen=True)
class Place:
    latitude: float
    longitude: float
    timezone: str = ""          # IANA name; empty for this computer's
    name: str = ""


# ---------------------------------------------------------------------- the sun

def _sin(d: float) -> float:
    return math.sin(math.radians(d))


def _cos(d: float) -> float:
    return math.cos(math.radians(d))


def _sun(jd: float) -> tuple[float, float]:
    """The sun's declination (degrees) and the equation of time (hours) on a Julian day."""
    d = jd - 2451545.0
    g = (357.529 + 0.98560028 * d) % 360
    q = (280.459 + 0.98564736 * d) % 360
    lon = (q + 1.915 * _sin(g) + 0.020 * _sin(2 * g)) % 360
    e = 23.439 - 0.00000036 * d
    ra = math.degrees(math.atan2(_cos(e) * _sin(lon), _cos(lon))) / 15
    eqt = q / 15 - ra % 24
    eqt = (eqt + 12) % 24 - 12
    decl = math.degrees(math.asin(_sin(e) * _sin(lon)))
    return decl, eqt


def _utc_hours(day: date, place: Place, method: Method, asr_factor: int, isha_minutes: int) -> dict[str, float]:
    """Each time as hours after midnight UTC."""
    jd = day.toordinal() + 1721424.5 - place.longitude / 360
    lat = place.latitude

    def noon(t: float) -> float:
        return (12 - _sun(jd + t / 24)[1]) % 24

    def at_angle(angle: float, t: float, before_noon: bool) -> float:
        decl = _sun(jd + t / 24)[0]
        cos_h = (-_sin(angle) - _sin(decl) * _sin(lat)) / (_cos(decl) * _cos(lat))
        if not -1 <= cos_h <= 1:
            return math.nan                     # the sun never gets that low (far north in summer)
        h = math.degrees(math.acos(cos_h)) / 15
        return noon(t) - h if before_noon else noon(t) + h

    def asr(t: float) -> float:
        decl = _sun(jd + t / 24)[0]
        angle = -math.degrees(math.atan(1 / (asr_factor + math.tan(math.radians(abs(lat - decl))))))
        return at_angle(angle, t, False)

    guess = {"fajr": 5, "sunrise": 6, "dhuhr": 12, "asr": 13, "maghrib": 18, "isha": 18}
    local = {
        "fajr": at_angle(method.fajr, guess["fajr"], True),
        "sunrise": at_angle(0.833, guess["sunrise"], True),
        "dhuhr": noon(guess["dhuhr"]),
        "asr": asr(guess["asr"]),
        "maghrib": at_angle(0.833, guess["maghrib"], False),
        "isha": at_angle(method.isha, guess["isha"], False) if not isha_minutes else math.nan,
    }
    if isha_minutes:
        local["isha"] = local["maghrib"] + isha_minutes / 60
    # Short nights: fajr and isha no later than a share of the night set by their angle.
    night = (local["sunrise"] - local["maghrib"]) % 24
    for name, angle, sign, edge in (("fajr", method.fajr, -1, "sunrise"), ("isha", method.isha, 1, "maghrib")):
        if name == "isha" and isha_minutes:
            continue
        limit = local[edge] + sign * angle / 60 * night
        value = local[name]
        if math.isnan(value) or abs(local[edge] - value) > angle / 60 * night:
            local[name] = limit
    return {k: v - place.longitude / 15 for k, v in local.items()}


def _zone(name: str):
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc


def prayer_times(day: date, place: Place, method: str = "egyptian", asr: str = "standard",
                 adjust: dict | None = None) -> dict[str, datetime]:
    """The day's times in the place's time zone, fajr to isha. adjust moves any of them by minutes."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; one of {', '.join(METHODS)}")
    m = METHODS[method]
    isha_minutes = m.isha_minutes
    if isha_minutes and method == "umm_al_qura" and hijri(day)[1] == 9:
        isha_minutes = 120                       # Umm al-Qura waits two hours in Ramadan
    hours = _utc_hours(day, place, m, 2 if asr == "hanafi" else 1, isha_minutes)
    midnight = datetime.combine(day, time(0), UTC)
    zone = _zone(place.timezone)
    adjust = adjust or {}
    out = {}
    for name in PRAYERS:
        when = midnight + timedelta(hours=hours[name], minutes=float(adjust.get(name, 0)), seconds=30)
        out[name] = when.replace(second=0, microsecond=0).astimezone(zone)     # to the nearest minute
    return out


# ---------------------------------------------------------------------- the Hijri calendar

MONTHS = ["Muharram", "Safar", "Rabi' al-Awwal", "Rabi' al-Thani", "Jumada al-Ula", "Jumada al-Akhirah", "Rajab",
          "Sha'ban", "Ramadan", "Shawwal", "Dhu al-Qa'dah", "Dhu al-Hijjah"]
MONTHS_AR = ["محرم", "صفر", "ربيع الأول", "ربيع الآخر", "جمادى الأولى", "جمادى الآخرة", "رجب", "شعبان", "رمضان",
             "شوال", "ذو القعدة", "ذو الحجة"]


# Umm al-Qura, the official calendar of Saudi Arabia: the length of each month from 1 Muharram 1440
# (11 September 2018) to Muharram 1461, 1 for thirty days and 0 for twenty-nine.
UMM_AL_QURA_START = (date(2018, 9, 11), 1440)
UMM_AL_QURA = ("010111010100101011011010010101011010101010101011010110010101011101001001011101100100101110101010"
               "010110110101001010110110101001010110110100101010111010010101011100101010011101010101001101011010"
               "10010101110101001001101110100100110111010010011011010101001101011010101")


def _tabular(day: date) -> tuple[int, int, int]:
    jdn = day.toordinal() + 1721425
    n = jdn - 1948440 + 10632
    cycle = (n - 1) // 10631
    n = n - 10631 * cycle + 354
    j = ((10985 - n) // 5316) * ((50 * n) // 17719) + (n // 5670) * ((43 * n) // 15238)
    n = n - ((30 - j) // 15) * ((17719 * j) // 50) - (j // 16) * ((15238 * j) // 43) + 29
    month = (24 * n) // 709
    return 30 * cycle + j - 30, month, n - (709 * month) // 24


def hijri(day: date, adjust: int = 0) -> tuple[int, int, int]:
    """(year, month, day): the Umm al-Qura calendar from 2018 to 2039, the tabular Islamic calendar
    outside it. Either can be a day off the calendar of a country that follows the moon sighting
    (Egypt, for one); adjust shifts it by whole days."""
    day += timedelta(days=adjust)
    first, year = UMM_AL_QURA_START
    offset = (day - first).days
    if offset < 0:
        return _tabular(day)
    for i, bit in enumerate(UMM_AL_QURA):
        length = 30 if bit == "1" else 29
        if offset < length:
            return year + i // 12, i % 12 + 1, offset + 1
        offset -= length
    return _tabular(day)


# ---------------------------------------------------------------------- settings

def settings(cfg: dict) -> dict:
    return cfg.get("prayer", {})


def place_from(cfg: dict, get=_get_json) -> Place | None:
    """prayer.latitude and prayer.longitude when set, else the city in place.city."""
    s = settings(cfg)
    lat, lon = s.get("latitude"), s.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return Place(float(lat), float(lon), s.get("timezone", ""), cfg.get("place", {}).get("city", ""))
    city = cfg.get("place", {}).get("city", "")
    if not city:
        return None
    found = geocode(city, get)
    if not found:
        return None
    return Place(found["latitude"], found["longitude"], s.get("timezone") or found.get("timezone", ""),
                 found.get("name", city))


def times_for(cfg: dict, day: date, place: Place) -> dict[str, datetime]:
    s = settings(cfg)
    return prayer_times(day, place, s.get("method", "egyptian"), s.get("asr", "standard"), s.get("adjust"))


def _now() -> datetime:
    return datetime.now(UTC)


def _left(start: datetime, end: datetime) -> str:
    mins = max(0, int((end - start).total_seconds() // 60))
    return f"in {mins // 60} h {mins % 60} min"


def _upcoming(cfg: dict, place: Place, current: datetime) -> list[tuple[str, datetime]]:
    """Today's and tomorrow's prayers (not sunrise) still ahead, soonest first."""
    today = current.astimezone(_zone(place.timezone)).date()
    out = []
    for day in (today, today + timedelta(days=1)):
        out += [(k, v) for k, v in times_for(cfg, day, place).items() if k != "sunrise" and v > current]
    return out


# ---------------------------------------------------------------------- tools

@tool("prayer_times", "Today's Islamic prayer times (or another day's) for the user's place, and the next prayer.",
      {"date": {"type": "string", "description": "YYYY-MM-DD; empty for today"}}, level="safe")
def prayer_times_tool(date: str = "", ctx=None, clock: Callable[[], datetime] = _now) -> str:  # noqa: A002
    place = place_from(ctx.cfg)
    if not place:
        return "error: no place set (prayer.latitude and prayer.longitude, or place.city)"
    current = clock()
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else current.astimezone(_zone(place.timezone)).date()
        times = times_for(ctx.cfg, day, place)
    except ValueError as exc:
        return f"error: {exc}"
    s = settings(ctx.cfg)
    lines = [f"{place.name or 'here'}, {day:%A %d %B %Y}, method {METHODS[s.get('method', 'egyptian')].name}"
             + (", Hanafi asr" if s.get("asr") == "hanafi" else "") + ":",
             ", ".join(f"{k} {v:%H:%M}" for k, v in times.items())]
    if not date:
        name, when = _upcoming(ctx.cfg, place, current)[0]
        lines.append(f"next: {name} at {when:%H:%M}, {_left(current, when)}")
        if hijri(day, int(s.get("hijri_adjust", 0)))[1] == 9:
            lines.append(ramadan_countdown(ctx=ctx, clock=lambda: current))
    return "\n".join(lines)


@tool("hijri_date", "The Islamic (Hijri) date, today or for a given day.",
      {"date": {"type": "string", "description": "Gregorian date YYYY-MM-DD; empty for today"}}, level="safe")
def hijri_date(date: str = "", ctx=None) -> str:  # noqa: A002
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now().date()
    except ValueError:
        return "error: give the date as YYYY-MM-DD"
    shift = int(settings(ctx.cfg).get("hijri_adjust", 0)) if ctx else 0
    y, m, d = hijri(day, shift)
    return (f"{day:%A %d %B %Y} is {d} {MONTHS[m - 1]} {y} AH ({d} {MONTHS_AR[m - 1]} {y} هـ), by the Umm al-Qura "
            "calendar; it can be a day off the local moon sighting (prayer.hijri_adjust shifts it)")


@tool("ramadan_countdown", "In Ramadan: time left until iftar (maghrib) and until suhoor ends (fajr).", level="safe")
def ramadan_countdown(ctx=None, clock: Callable[[], datetime] = _now) -> str:
    place = place_from(ctx.cfg)
    if not place:
        return "error: no place set (prayer.latitude and prayer.longitude, or place.city)"
    current = clock()
    today = current.astimezone(_zone(place.timezone)).date()
    y, m, d = hijri(today, int(settings(ctx.cfg).get("hijri_adjust", 0)))
    upcoming = _upcoming(ctx.cfg, place, current)
    iftar = next(when for name, when in upcoming if name == "maghrib")
    suhoor = next(when for name, when in upcoming if name == "fajr")
    head = f"day {d} of Ramadan {y}" if m == 9 else f"it is not Ramadan ({d} {MONTHS[m - 1]} {y})"
    return (f"{head}; iftar (maghrib) at {iftar:%H:%M}, {_left(current, iftar)}; "
            f"suhoor ends (fajr) at {suhoor:%H:%M}, {_left(current, suhoor)}")


def plan_reminders(cfg: dict, scheduler, now: datetime | None = None, get=_get_json) -> list:
    """Set a reminder before each prayer named in prayer.reminders ({"fajr": 10} is ten minutes
    before fajr), for the rest of today and tomorrow. None are set until the user asks for them; a
    reminder already set is not set again."""
    wanted = {k: int(v) for k, v in (settings(cfg).get("reminders") or {}).items() if k in PRAYERS}
    if not wanted:
        return []
    place = place_from(cfg, get)
    if not place:
        return []
    now = now or _now()
    tag = settings(cfg).get("language", "en")
    taken = {(round(a.due), a.label) for a in scheduler.pending() if a.kind == "prayer"}
    added = []
    for name, when in _upcoming(cfg, place, now):
        if name not in wanted:
            continue
        due = when - timedelta(minutes=wanted[name])
        n = wanted[name]
        label = (f"{NAMES_AR[name]} بعد {n} {'دقائق' if 3 <= n <= 10 else 'دقيقة'}" if tag.startswith("ar")
                 else f"{name} in {n} minutes")
        if due > now and (round(due.timestamp()), label) not in taken:
            added.append(scheduler.add(due.timestamp(), label, "prayer", tag))
    return added


def plan_day(cfg: dict, scheduler, now: datetime | None = None, get=_get_json) -> list:
    """Set the prayer reminders, and plan again tomorrow (a hidden "prayer-plan" alarm), so they go
    on day after day while Rafiq runs."""
    now = now or _now()
    added = plan_reminders(cfg, scheduler, now, get)
    if settings(cfg).get("reminders") and not any(a.kind == PLAN for a in scheduler.pending()):
        tomorrow = datetime.combine(now.astimezone().date() + timedelta(days=1), time(0, 5)).astimezone()
        scheduler.add(tomorrow.timestamp(), "plan prayer reminders", PLAN, "en")
    return added


PLAN = "prayer-plan"


# ---------------------------------------------------------------------- the Quran

VERSES = [7, 286, 200, 176, 120, 165, 206, 75, 129, 109, 123, 111, 43, 52, 99, 128, 111, 110, 98, 135, 112, 78, 118,
          64, 77, 227, 93, 88, 69, 60, 34, 30, 73, 54, 45, 83, 182, 88, 75, 85, 54, 53, 89, 59, 37, 35, 38, 29, 18, 45,
          60, 49, 62, 55, 78, 96, 29, 22, 24, 13, 14, 11, 11, 18, 12, 12, 30, 52, 52, 44, 28, 28, 20, 56, 40, 31, 50,
          40, 46, 42, 29, 19, 36, 25, 22, 17, 19, 26, 30, 20, 15, 21, 11, 8, 8, 19, 5, 8, 8, 11, 11, 8, 3, 9, 5, 4, 7,
          3, 6, 3, 5, 4, 5, 6]
SURAHS = ["Al-Fatiha", "Al-Baqarah", "Al Imran", "An-Nisa", "Al-Ma'idah", "Al-An'am", "Al-A'raf", "Al-Anfal",
          "At-Tawbah", "Yunus", "Hud", "Yusuf", "Ar-Ra'd", "Ibrahim", "Al-Hijr", "An-Nahl", "Al-Isra", "Al-Kahf",
          "Maryam", "Ta-Ha", "Al-Anbiya", "Al-Hajj", "Al-Mu'minun", "An-Nur", "Al-Furqan", "Ash-Shu'ara", "An-Naml",
          "Al-Qasas", "Al-Ankabut", "Ar-Rum", "Luqman", "As-Sajdah", "Al-Ahzab", "Saba", "Fatir", "Ya-Sin",
          "As-Saffat", "Sad", "Az-Zumar", "Ghafir", "Fussilat", "Ash-Shura", "Az-Zukhruf", "Ad-Dukhan", "Al-Jathiyah",
          "Al-Ahqaf", "Muhammad", "Al-Fath", "Al-Hujurat", "Qaf", "Adh-Dhariyat", "At-Tur", "An-Najm", "Al-Qamar",
          "Ar-Rahman", "Al-Waqi'ah", "Al-Hadid", "Al-Mujadilah", "Al-Hashr", "Al-Mumtahanah", "As-Saff", "Al-Jumu'ah",
          "Al-Munafiqun", "At-Taghabun", "At-Talaq", "At-Tahrim", "Al-Mulk", "Al-Qalam", "Al-Haqqah", "Al-Ma'arij",
          "Nuh", "Al-Jinn", "Al-Muzzammil", "Al-Muddaththir", "Al-Qiyamah", "Al-Insan", "Al-Mursalat", "An-Naba",
          "An-Nazi'at", "Abasa", "At-Takwir", "Al-Infitar", "Al-Mutaffifin", "Al-Inshiqaq", "Al-Buruj", "At-Tariq",
          "Al-A'la", "Al-Ghashiyah", "Al-Fajr", "Al-Balad", "Ash-Shams", "Al-Layl", "Ad-Duha", "Ash-Sharh", "At-Tin",
          "Al-Alaq", "Al-Qadr", "Al-Bayyinah", "Az-Zalzalah", "Al-Adiyat", "Al-Qari'ah", "At-Takathur", "Al-Asr",
          "Al-Humazah", "Al-Fil", "Quraysh", "Al-Ma'un", "Al-Kawthar", "Al-Kafirun", "An-Nasr", "Al-Masad",
          "Al-Ikhlas", "Al-Falaq", "An-Nas"]
# EveryAyah folders (https://everyayah.com/data/), one file per verse.
RECITERS = {
    "alafasy": ("Mishary Rashid Alafasy", "Alafasy_128kbps"),
    "abdul_basit": ("Abdul Basit Abdul Samad (murattal)", "Abdul_Basit_Murattal_192kbps"),
    "husary": ("Mahmoud Khalil Al-Husary", "Husary_128kbps"),
    "minshawi": ("Mohamed Siddiq Al-Minshawi (murattal)", "Minshawy_Murattal_128kbps"),
    "sudais": ("Abdul Rahman Al-Sudais", "Abdurrahmaan_As-Sudais_192kbps"),
    "muaiqly": ("Maher Al-Muaiqly", "MaherAlMuaiqly128kbps"),
}
EVERYAYAH = "https://everyayah.com/data/{folder}/{surah:03d}{verse:03d}.mp3"


class Player:
    """Plays a list of audio URLs one after another with ffplay, streamed, in the background. A new
    recitation or stop() ends the one playing."""

    def __init__(self):
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def __call__(self, urls: list[str]) -> None:
        player = shutil.which("ffplay")
        if not player:
            raise RuntimeError("ffplay is not installed")
        self.stop()
        stop = self._stop = threading.Event()

        def run():
            for url in urls:
                with self._lock:
                    if stop.is_set():
                        return
                    proc = self._proc = subprocess.Popen([player, "-nodisp", "-autoexit", "-loglevel", "quiet", url],
                                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc.wait()

        threading.Thread(target=run, name="rafiq-quran", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()


PLAYER = Player()


@tool("quran_recite", "Play a Quran recitation: a surah, or some of its verses, by the user's reciter.",
      {"surah": {"type": "integer", "description": "surah number, 1 to 114"},
       "from_verse": {"type": "integer", "description": "first verse; default 1"},
       "to_verse": {"type": "integer", "description": "last verse; default the end of the surah"},
       "reciter": {"type": "string", "description": "one of " + ", ".join(RECITERS) + "; empty for the user's"}},
      required=("surah",), level="act")
def quran_recite(surah: int, from_verse: int = 1, to_verse: int = 0, reciter: str = "", ctx=None,
                 player: Callable[[list[str]], None] = PLAYER) -> str:
    surah, first = int(surah), int(from_verse or 1)
    if not 1 <= surah <= 114:
        return "error: a surah is numbered 1 to 114"
    last = int(to_verse or VERSES[surah - 1])
    if not 1 <= first <= last <= VERSES[surah - 1]:
        return f"error: surah {surah} ({SURAHS[surah - 1]}) has verses 1 to {VERSES[surah - 1]}"
    key = reciter or (settings(ctx.cfg).get("reciter") if ctx else "") or "alafasy"
    if key not in RECITERS:
        return f"error: unknown reciter {key!r}; one of {', '.join(RECITERS)}"
    name, folder = RECITERS[key]
    urls = [EVERYAYAH.format(folder=folder, surah=surah, verse=v) for v in range(first, last + 1)]
    if first == 1 and surah not in (1, 9):       # the basmala opens every surah but At-Tawbah
        urls.insert(0, EVERYAYAH.format(folder=folder, surah=1, verse=1))
    try:
        player(urls)
    except (RuntimeError, OSError) as exc:
        return f"error: {exc}"
    return f"playing surah {surah} ({SURAHS[surah - 1]}), verses {first} to {last}, recited by {name}"


@tool("quran_stop", "Stop the Quran recitation that is playing.", level="act")
def quran_stop() -> str:
    PLAYER.stop()
    return "stopped"
