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
    assert [e.name for e in built["spy"][0]] == ["H"]                       # hobbyist list: 60 days
    built, dropped = S.build_screens(src, True, True, T0 + 61 * DAY)
    assert built == {} and dropped["hobby"] == 1


def test_objects_dated_in_the_future_are_left_out_and_counted():
    src = {"starlink": _elems(omm(1, "A"), omm(2, "B")), "hobby": _elems(omm(4, "H"))}
    built, dropped = S.build_screens(src, True, True, T0 - 12 * 3600)       # half a day ahead: clock slack, kept
    assert len(built["starlink"][0]) == 2 and dropped == {"starlink": 0, "hobby": 0}
    built, dropped = S.build_screens(src, True, True, T0 - 2 * DAY)         # two days ahead: bogus
    assert built == {} and dropped == {"starlink": 2, "hobby": 1}
