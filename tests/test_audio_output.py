import pytest

from rafiq import config
from rafiq.tools import pc

STATUS = """PipeWire 'pipewire-0' [1.6.9, me@pc, cookie:1]
 └─ Clients:
        33. WirePlumber                         [1.6.9, me@pc, pid:3800]

Audio
 ├─ Devices:
 │      44. Renoir HDMI/DP Audio Controller     [alsa]
 │      45. Ryzen HD Audio Controller           [alsa]
 │
 ├─ Sinks:
 │      49. Ryzen HD Audio Controller Analog Stereo [vol: 0.42]
 │  *   61. WH-1000XM4                          [vol: 0.70]
 │      62. Renoir HDMI / DisplayPort 1 Output  [vol: 1.00 MUTED]
 │
 ├─ Sources:
 │  *   50. Ryzen HD Audio Controller Analog Stereo [vol: 1.00 MUTED]
 │
 └─ Streams:

Video
 ├─ Sinks:
 │      70. Not audio                           [vol: 1.00]
 │
"""


def test_parse_sinks_reads_only_the_audio_outputs():
    sinks = pc.parse_sinks(STATUS)
    assert [(s.id, s.name, s.default) for s in sinks] == [
        (49, "Ryzen HD Audio Controller Analog Stereo", False), (61, "WH-1000XM4", True),
        (62, "Renoir HDMI / DisplayPort 1 Output", False)]


@pytest.mark.parametrize("spoken, kind", [
    ("headphones", "headphones"), ("the headset", "headphones"), ("le casque", "headphones"),
    ("los auriculares", "headphones"), ("Kopfhörer", "headphones"), ("سماعات الرأس", "headphones"),
    ("الهيدفون", "headphones"), ("حول على السماعات", "headphones"),
    ("speakers", "speakers"), ("les enceintes", "speakers"),
    ("los altavoces", "speakers"), ("die Lautsprecher", "speakers"), ("مكبرات الصوت", "speakers"),
    ("السبيكر", "speakers"), ("la télé", "tv"), ("Fernseher", "tv"), ("التلفزيون", "tv"), ("desk", None),
])
def test_spoken_kind_in_six_languages(spoken, kind):
    assert pc.output_kind(spoken) == kind


@pytest.mark.parametrize("spoken, aliases, sink_id", [
    ("speakers", {}, 49),                                   # "analog stereo" is the built-in speakers
    ("the TV", {}, 62),                                     # hdmi
    ("casque", {"headphones": "WH-1000XM4"}, 61),           # config alias by kind
    ("desk", {"desk": "ryzen"}, 49),                        # the user's own name
    ("wh-1000", {}, 61),                                    # part of the device's name
    ("kitchen", {}, None),
])
def test_find_sink(spoken, aliases, sink_id):
    sink = pc.find_sink(spoken, pc.parse_sinks(STATUS), aliases)
    assert (sink.id if sink else None) == sink_id


def fake_wpctl(calls):
    def run(args, timeout=10):
        calls.append(args)
        return type("Done", (), {"stdout": STATUS if args[1] == "status" else "", "stderr": "", "returncode": 0})()
    return run


def test_audio_output_switches_the_default_sink(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "_run", fake_wpctl(calls))
    ctx = type("Ctx", (), {"cfg": config.load()})()
    assert pc.audio_output("speakers", ctx=ctx) == "now playing on Ryzen HD Audio Controller Analog Stereo"
    assert calls[-1] == ["wpctl", "set-default", "49"]


def test_audio_output_without_a_name_lists_the_outputs(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "_run", fake_wpctl(calls))
    text = pc.audio_output()
    assert "WH-1000XM4 (current)" in text and "Renoir HDMI" in text
    assert all(c[1] == "status" for c in calls)


def test_audio_output_unknown_name_is_an_error_that_lists_the_outputs(monkeypatch):
    monkeypatch.setattr(pc, "_run", fake_wpctl([]))
    text = pc.audio_output("kitchen")
    assert text.startswith("error") and "WH-1000XM4" in text


def test_config_has_output_aliases():
    assert config.load()["audio"]["outputs"] == {}
