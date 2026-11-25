import json

import pytest

import elements as E
from conftest import ISS_L1, ISS_L2, omm


def test_omm_good_file():
    got = E.parse_omm_json(json.dumps([omm(25544, "ISS (ZARYA)"), omm(2, "b")]).encode())
    assert abs(got[0].epoch - 1575909509.36) < 0.01          # 2019-12-09T16:38:29.36 UTC
    assert [(e.name, e.satnum, e.intl) for e in got] == [("ISS (ZARYA)", 25544, "1998-067A"), ("B", 2, "1998-067A")]


@pytest.mark.parametrize("raw", [b"<html>blocked</html>", b'{"a":1}', b"[]", b"\xff\xfe"])
def test_omm_rejects_non_lists_and_empty(raw):
    with pytest.raises(E.ElementError):
        E.parse_omm_json(raw)


def test_omm_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(E, "MAX_BYTES", 10)
    with pytest.raises(E.ElementError, match="too large"):
        E.parse_omm_json(json.dumps([omm(1)]).encode())


def test_omm_rejects_too_many_objects(monkeypatch):
    monkeypatch.setattr(E, "MAX_OBJECTS", 2)
    with pytest.raises(E.ElementError, match="too many"):
        E.parse_omm_json(json.dumps([omm(i) for i in range(1, 4)]).encode())


def test_omm_skips_a_few_bad_records_but_rejects_many():
    rows = [omm(i) for i in range(1, 20)] + [omm(99, ECCENTRICITY=1.5)]
    assert len(E.parse_omm_json(json.dumps(rows).encode())) == 19
    rows = [omm(i) for i in range(1, 5)] + [omm(98, MEAN_MOTION=0), omm(99, INCLINATION=200)]
    with pytest.raises(E.ElementError, match="invalid"):
        E.parse_omm_json(json.dumps(rows).encode())


@pytest.mark.parametrize("raw,want", [("usa 245 #ff0000 <b>", "USA 245 FF0000 B"), ("", "UNKNOWN"), ("x" * 80, "X" * 24),
                                      ("Starlink-1234 (v2)", "STARLINK-1234 (V2)"), ("a\n\tb", "A B")])
def test_clean_name(raw, want):
    assert E.clean_name(raw) == want


def test_tle_checksum():
    assert E.tle_checksum_ok(ISS_L1) and E.tle_checksum_ok(ISS_L2)
    assert not E.tle_checksum_ok(ISS_L1[:-1] + "0")
    assert not E.tle_checksum_ok("1 short")


def test_tle_text_with_and_without_name_lines():
    raw = f"USA 999\n{ISS_L1}\n{ISS_L2}\n{ISS_L1}\n{ISS_L2}\n0 NAMED\n{ISS_L1}\n{ISS_L2}\n".encode()
    got = E.parse_tle_text(raw)
    assert [e.name for e in got] == ["USA 999", "OBJ 25544", "NAMED"]
    assert abs(got[0].epoch - 1575909509.36) < 0.01


def test_tle_text_rejects_file_with_mostly_bad_checksums():
    bad = ISS_L1[:-1] + "0"
    with pytest.raises(E.ElementError):
        E.parse_tle_text(f"{bad}\n{ISS_L2}\n".encode())
    with pytest.raises(E.ElementError):
        E.parse_tle_text(b"<html>not found</html>")
    with pytest.raises(E.ElementError):
        E.parse_tle_text("café".encode("utf-8"))
