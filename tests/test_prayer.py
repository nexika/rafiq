from datetime import UTC, date, datetime, timedelta

import pytest

from rafiq import config
from rafiq.scheduler import Scheduler
from rafiq.tools import load_all, prayer

CAIRO = prayer.Place(30.0444, 31.2357, "UTC", "Cairo")
MECCA = prayer.Place(21.4225, 39.8262, "UTC", "Mecca")
ORDER = ["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]


def hm(when: datetime) -> str:
    return when.strftime("%H:%M")


def minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60


def test_the_tools_are_off_by_default():
    assert "prayer_times" not in load_all(config.load())
    tools = load_all({"prayer": {"enabled": True}})
    for name in ("prayer_times", "hijri_date", "ramadan_countdown", "quran_recite", "quran_stop"):
        assert name in tools and tools[name].action({}) is None


def test_cairo_times_match_the_egyptian_authority():
    # Egyptian General Authority of Survey times for Cairo, 10 October 2026, in UTC (as aladhan.com gives them).
    times = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian")
    assert list(times) == ORDER
    expected = {"fajr": "02:27", "sunrise": "03:54", "dhuhr": "09:42", "asr": "13:02", "maghrib": "15:30",
                "isha": "16:47"}
    for name, value in expected.items():
        got = times[name]
        want = datetime.combine(date(2026, 10, 10), datetime.strptime(value, "%H:%M").time(), UTC)
        assert abs(minutes(want, got)) <= 1, (name, hm(got), value)


def test_the_method_changes_fajr_and_isha_only():
    egypt = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian")
    mwl = prayer.prayer_times(date(2026, 10, 10), CAIRO, "mwl")
    assert egypt["fajr"] < mwl["fajr"]              # 19.5° below the horizon comes before 18°
    assert egypt["dhuhr"] == mwl["dhuhr"] and egypt["maghrib"] == mwl["maghrib"]


def test_umm_al_qura_isha_is_ninety_minutes_after_maghrib_and_two_hours_in_ramadan():
    times = prayer.prayer_times(date(2026, 10, 10), MECCA, "umm_al_qura")
    assert minutes(times["maghrib"], times["isha"]) == pytest.approx(90, abs=0.1)
    ramadan = prayer.prayer_times(date(2026, 3, 1), MECCA, "umm_al_qura")
    assert minutes(ramadan["maghrib"], ramadan["isha"]) == pytest.approx(120, abs=0.1)


def test_hanafi_asr_is_later():
    standard = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian")
    hanafi = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian", asr="hanafi")
    assert minutes(standard["asr"], hanafi["asr"]) > 30


def test_per_prayer_adjustments_in_minutes():
    plain = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian")
    moved = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian", adjust={"fajr": 2, "isha": -3})
    assert minutes(plain["fajr"], moved["fajr"]) == pytest.approx(2)
    assert minutes(plain["isha"], moved["isha"]) == pytest.approx(-3)
    assert moved["dhuhr"] == plain["dhuhr"]


def test_times_are_given_in_the_place_time_zone():
    cairo = prayer.Place(30.0444, 31.2357, "Africa/Cairo", "Cairo")
    times = prayer.prayer_times(date(2026, 10, 10), cairo, "egyptian")
    utc = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian")
    assert times["dhuhr"] == utc["dhuhr"] and str(times["dhuhr"].tzinfo) == "Africa/Cairo"


def test_far_north_summer_nights_still_have_fajr_and_isha():
    oslo = prayer.Place(59.91, 10.75, "UTC", "Oslo")
    times = prayer.prayer_times(date(2026, 6, 21), oslo, "mwl")
    assert [times[k] for k in ORDER] == sorted(times[k] for k in ORDER)


def test_mecca_times_match_umm_al_qura():
    mecca = prayer.Place(21.4225, 39.8262, "Asia/Riyadh", "Mecca")
    times = prayer.prayer_times(date(2026, 10, 10), mecca, "umm_al_qura")
    expected = ["04:59", "06:15", "12:08", "15:30", "18:01", "19:31"]   # aladhan.com, method 4
    for name, value in zip(ORDER, expected, strict=True):
        want = datetime.combine(date(2026, 10, 10), datetime.strptime(value, "%H:%M").time(), times[name].tzinfo)
        assert abs(minutes(want, times[name])) <= 1, (name, hm(times[name]), value)


def test_unknown_method_is_an_error():
    with pytest.raises(ValueError):
        prayer.prayer_times(date(2026, 10, 10), CAIRO, "nowhere")


@pytest.mark.parametrize("day, hijri", [
    (date(2023, 7, 19), (1445, 1, 1)),
    (date(2026, 2, 18), (1447, 9, 1)),
    (date(2026, 10, 10), (1448, 4, 29)),
    (date(2026, 6, 16), (1448, 1, 1)),        # the tabular calendar says 30 Dhu al-Hijjah
    (date(2050, 1, 1), (1472, 4, 7)),         # past the Umm al-Qura table: tabular,
    (date(622, 7, 19), (1, 1, 1)),
])
def test_hijri_date_follows_umm_al_qura(day, hijri):
    assert prayer.hijri(day) == hijri


def test_hijri_date_can_be_shifted_to_match_local_sighting():
    assert prayer.hijri(date(2026, 2, 18), adjust=-1) == (1447, 8, 29)


def test_hijri_date_tool_names_the_month():
    text = prayer.hijri_date("2026-10-10")
    assert "29 Rabi' al-Thani 1448" in text and "ربيع الآخر" in text


def place_cfg(**prayer_cfg):
    return {"prayer": {"enabled": True, "latitude": 30.0444, "longitude": 31.2357, "timezone": "UTC",
                       **prayer_cfg}, "place": {"city": ""}}


def ctx_for(cfg, scheduler=None):
    return type("Ctx", (), {"cfg": cfg, "scheduler": scheduler, "tag": "en"})()


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_prayer_times_tool_shows_the_method_and_the_next_prayer():
    text = prayer.prayer_times_tool("", ctx=ctx_for(place_cfg()), clock=lambda: at("2026-10-10T10:00"))
    assert "Egyptian General Authority of Survey" in text
    assert "fajr 02:2" in text and "next: asr" in text


def test_in_ramadan_prayer_times_include_the_iftar_countdown():
    text = prayer.prayer_times_tool("", ctx=ctx_for(place_cfg()), clock=lambda: at("2026-03-01T10:00"))
    assert "Ramadan 1447" in text and "iftar" in text
    text = prayer.prayer_times_tool("", ctx=ctx_for(place_cfg()), clock=lambda: at("2026-10-10T10:00"))
    assert "iftar" not in text


def test_the_place_comes_from_the_city_when_no_coordinates_are_set():
    def get(url, params):
        assert params["name"] == "Cairo"
        return {"results": [{"name": "Cairo", "latitude": 30.0444, "longitude": 31.2357, "timezone": "UTC"}]}

    cfg = {"prayer": {"enabled": True}, "place": {"city": "Cairo"}}
    place = prayer.place_from(cfg, get)
    assert place == prayer.Place(30.0444, 31.2357, "UTC", "Cairo")


def test_no_place_is_an_error():
    cfg = {"prayer": {"enabled": True}, "place": {"city": ""}}
    assert prayer.prayer_times_tool("", ctx=ctx_for(cfg)).startswith("error")


def test_ramadan_countdown_to_iftar_and_suhoor():
    clock = lambda: at("2026-03-01T12:00")      # noqa: E731  (in Ramadan 1447)
    text = prayer.ramadan_countdown(ctx=ctx_for(place_cfg()), clock=clock)
    assert "Ramadan 1447" in text and "iftar" in text and "suhoor" in text
    iftar = prayer.prayer_times(date(2026, 3, 1), CAIRO, "egyptian")["maghrib"]
    left = int(minutes(clock(), iftar))
    assert f"in {left // 60} h {left % 60} min" in text


def test_ramadan_countdown_says_when_it_is_not_ramadan():
    text = prayer.ramadan_countdown(ctx=ctx_for(place_cfg()), clock=lambda: at("2026-10-10T12:00"))
    assert "not Ramadan" in text


def test_reminders_are_off_until_the_user_sets_them(tmp_path):
    sched = Scheduler(tmp_path / "alarms.json", lambda a: None)
    assert prayer.plan_reminders(place_cfg(), sched, now=at("2026-10-10T00:00")) == []
    assert sched.pending() == []


def test_reminders_before_each_chosen_prayer(tmp_path):
    sched = Scheduler(tmp_path / "alarms.json", lambda a: None)
    cfg = place_cfg(reminders={"fajr": 10, "maghrib": 15})
    added = prayer.plan_reminders(cfg, sched, now=at("2026-10-10T03:00"))
    names = [a.label for a in added]
    # today's fajr has passed; today's maghrib, and tomorrow's fajr and maghrib remain
    assert names == ["maghrib in 15 minutes", "fajr in 10 minutes", "maghrib in 15 minutes"]
    times = prayer.prayer_times(date(2026, 10, 10), CAIRO, "egyptian")
    assert added[0].due == pytest.approx((times["maghrib"] - timedelta(minutes=15)).timestamp())
    assert prayer.plan_reminders(cfg, sched, now=at("2026-10-10T03:00")) == []     # never twice


def test_reminders_are_planned_again_each_day(tmp_path):
    sched = Scheduler(tmp_path / "alarms.json", lambda a: None)
    cfg = place_cfg(reminders={"isha": 5})
    prayer.plan_day(cfg, sched, now=at("2026-10-10T03:00"))
    prayer.plan_day(cfg, sched, now=at("2026-10-10T03:00"))
    plans = [a for a in sched.pending() if a.kind == "prayer-plan"]
    assert len(plans) == 1 and plans[0].due > at("2026-10-10T12:00").timestamp()
    assert len([a for a in sched.pending() if a.kind == "prayer"]) == 2


def test_reminders_can_be_said_in_arabic(tmp_path):
    sched = Scheduler(tmp_path / "alarms.json", lambda a: None)
    added = prayer.plan_reminders(place_cfg(reminders={"isha": 5}, language="ar"), sched, now=at("2026-10-10T03:00"))
    assert added[0].label == "العشاء بعد 5 دقائق" and added[0].tag == "ar"


def test_quran_recitation_streams_every_verse_from_everyayah():
    played = []
    ctx = ctx_for(place_cfg(reciter="husary"))
    text = prayer.quran_recite(112, ctx=ctx, player=played.extend)
    assert "Al-Ikhlas" in text and "Husary" in text
    assert played[0] == "https://everyayah.com/data/Husary_128kbps/001001.mp3"     # the basmala first
    assert played[1:] == [f"https://everyayah.com/data/Husary_128kbps/112{v:03d}.mp3" for v in range(1, 5)]


def test_al_fatiha_and_at_tawba_have_no_extra_basmala():
    played = []
    prayer.quran_recite(9, 1, 2, ctx=ctx_for(place_cfg()), player=played.extend)
    assert played == [f"https://everyayah.com/data/Alafasy_128kbps/009{v:03d}.mp3" for v in (1, 2)]


@pytest.mark.parametrize("args", [(0,), (115,), (1, 8), (2, 10, 5), (1, 1, 1, "nobody")])
def test_quran_recitation_rejects_what_does_not_exist(args):
    assert prayer.quran_recite(*args, ctx=ctx_for(place_cfg()), player=lambda urls: None).startswith("error")


def test_the_verse_table_has_every_verse():
    assert len(prayer.VERSES) == 114 and sum(prayer.VERSES) == 6236
