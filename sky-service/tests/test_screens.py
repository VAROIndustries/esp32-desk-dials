import json

import elements as E
import screens as S
from conftest import T0, omm

DAY = 86400


def _elems(*rows):
    return E.parse_omm_json(json.dumps(list(rows)).encode())


def test_nasa_screen_picks_listed_ids_and_renames_them():
    src = {"stations": _elems(omm(25544, "ISS (ZARYA)"), omm(48274, "CSS (TIANHE)")),
           "science": _elems(omm(20580, "HST"), omm(7, "OTHER")), "resource": _elems(omm(39084, "LANDSAT 8"), omm(25544, "DUP"))}
    built, dropped = S.build_screens(src, False, False, T0)
    elems, tags = built["nasa"]
    assert dropped == {"stations": 0, "science": 0, "resource": 0}
    assert [e.name for e in elems] == ["ISS", "HUBBLE", "LANDSAT 8"]
    assert tags == ["", "", ""]


def test_spy_screen_absent_unless_enabled():
    src = {"military": _elems(omm(1, "MIL A")), "hobby": _elems(omm(2, "USA 1"))}
    assert "spy" not in S.build_screens(src, False, True, T0)[0]


def test_spy_screen_tags_and_prefers_hobby_on_duplicates():
    src = {"military": _elems(omm(1, "MIL A"), omm(2, "MIL DUP")), "hobby": _elems(omm(2, "USA 1"), omm(3, "USA 2"))}
    elems, tags = S.build_screens(src, True, True, T0)[0]["spy"]
    assert [(e.name, t) for e, t in zip(elems, tags)] == [("USA 1", "HOBBY"), ("USA 2", "HOBBY"), ("MIL A", "MIL")]
    elems, tags = S.build_screens(src, True, False, T0)[0]["spy"]
    assert tags == ["MIL", "MIL"]


def test_missing_sources_leave_screens_out():
    assert S.build_screens({}, True, True, T0) == ({}, {})


def test_objects_with_old_orbits_are_left_out_and_counted():
    src = {"starlink": _elems(omm(1, "A"), omm(2, "B")), "military": _elems(omm(3, "M")), "hobby": _elems(omm(4, "H"))}
    built, dropped = S.build_screens(src, True, True, T0 + 29 * DAY)
    assert len(built["starlink"][0]) == 2 and dropped["starlink"] == 0
    built, dropped = S.build_screens(src, True, True, T0 + 31 * DAY)        # official lists: 30 days
    assert "starlink" not in built and dropped == {"starlink": 2, "hobby": 0, "military": 1}
    assert [e.name for e in built["spy"][0]] == ["H"]                       # extra TLE file: 60 days
    built, dropped = S.build_screens(src, True, True, T0 + 61 * DAY)
    assert built == {} and dropped["hobby"] == 1


def test_objects_dated_in_the_future_are_left_out_and_counted():
    src = {"starlink": _elems(omm(1, "A"), omm(2, "B")), "hobby": _elems(omm(4, "H"))}
    built, dropped = S.build_screens(src, True, True, T0 - 12 * 3600)       # half a day ahead: clock slack, kept
    assert len(built["starlink"][0]) == 2 and dropped == {"starlink": 0, "hobby": 0}
    built, dropped = S.build_screens(src, True, True, T0 - 2 * DAY)         # two days ahead: bogus
    assert built == {} and dropped == {"starlink": 2, "hobby": 1}


# --- review follow-ups (Task 3) ---------------------------------------------------------------

EPOCH = E.parse_omm_json(json.dumps([omm(1)]).encode())[0].epoch     # the fixture's element epoch
NEWER = "2019-12-11T16:38:29.363424"                                  # two days after the fixture


def test_private_sources_never_reach_public_screens_or_dropped():
    src = {"military": _elems(omm(25544, "SECRET ISS"), omm(9, "SECRET MIL")),
           "hobby": _elems(omm(20580, "SECRET HST"), omm(10, "SECRET HOB")),
           "starlink": _elems(omm(44713, "STARLINK-1007")),
           "stations": _elems(omm(25544, "ISS (ZARYA)"))}
    for hobby_on in (False, True):
        built, dropped = S.build_screens(src, False, hobby_on, T0)
        names = [e.name for screen in ("nasa", "starlink") for e in built[screen][0]]
        assert names == ["ISS", "STARLINK-1007"] and not any("SECRET" in n for n in names)
        assert "spy" not in built and "military" not in dropped and "hobby" not in dropped
    built, dropped = S.build_screens(src, True, False, T0)
    assert "hobby" not in dropped and dropped["military"] == 0
    assert [e.name for e in built["spy"][0]] == ["SECRET ISS", "SECRET MIL"]


def test_age_limits_are_exact_at_the_boundaries():
    src = {"starlink": _elems(omm(1, "A")), "hobby": _elems(omm(4, "H"))}
    kept = lambda now: {n: len(v[0]) for n, v in S.build_screens(src, True, True, now)[0].items()}
    assert kept(EPOCH + 30 * DAY) == {"starlink": 1, "spy": 1}
    assert kept(EPOCH + 30 * DAY + 1) == {"spy": 1}
    assert kept(EPOCH - DAY) == {"starlink": 1, "spy": 1}
    assert kept(EPOCH - DAY - 1) == {}
    assert kept(EPOCH + 60 * DAY) == {"spy": 1}
    assert kept(EPOCH + 60 * DAY + 1) == {}


def test_duplicates_use_the_newer_element_set():
    src = {"hobby": _elems(omm(2, "USA 1"), omm(3, "USA 2")),
           "military": _elems(omm(1, "MIL A"), omm(2, "USA 1 NEW", EPOCH=NEWER, MEAN_ANOMALY=10.0))}
    elems, tags = S.build_screens(src, True, True, T0 + DAY)[0]["spy"]
    assert [(e.satnum, t) for e, t in zip(elems, tags)] == [(2, "HOBBY"), (3, "HOBBY"), (1, "MIL")]
    assert elems[0].epoch == EPOCH + 2 * DAY and elems[0] is src["military"][1]
    src["hobby"], src["military"] = src["military"][1:], src["hobby"][:1]        # newer copy in hobby
    elems, tags = S.build_screens(src, True, True, T0 + DAY)[0]["spy"]
    assert [(e.name, t) for e, t in zip(elems, tags)] == [("USA 1 NEW", "HOBBY")]

    src = {"stations": _elems(omm(25544, "ISS (ZARYA)")), "science": _elems(omm(20580, "HST")),
           "resource": _elems(omm(25544, "ISS", EPOCH=NEWER, MEAN_ANOMALY=10.0))}
    elems, _ = S.build_screens(src, False, False, T0 + DAY)[0]["nasa"]
    assert [e.name for e in elems] == ["ISS", "HUBBLE"]
    assert elems[0].epoch == EPOCH + 2 * DAY and elems[0].sat is src["resource"][0].sat


def test_nasa_dropped_counts_only_listed_objects():
    src = {"stations": _elems(omm(25544, "ISS"), omm(48274, "CSS", EPOCH="2019-10-01T00:00:00.000000")),
           "science": _elems(omm(7, "OTHER", EPOCH="2019-10-01T00:00:00.000000")),
           "resource": _elems(omm(39084, "LANDSAT 8", EPOCH="2019-10-01T00:00:00.000000"))}
    built, dropped = S.build_screens(src, False, False, T0)
    assert [e.name for e in built["nasa"][0]] == ["ISS"]
    assert dropped == {"stations": 0, "science": 0, "resource": 1}


def test_nasa_rename_keeps_every_other_field():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Extra(E.Elem):
        note: str = "default"
    e = _elems(omm(25544, "ISS (ZARYA)"))[0]
    src = {"stations": [Extra(e.name, e.satnum, e.sat, e.intl, e.epoch, note="kept")]}
    out = S.build_screens(src, False, False, T0)[0]["nasa"][0][0]
    assert type(out) is Extra and out.name == "ISS" and out.note == "kept"
    assert (out.satnum, out.sat, out.intl, out.epoch) == (e.satnum, e.sat, e.intl, e.epoch)
