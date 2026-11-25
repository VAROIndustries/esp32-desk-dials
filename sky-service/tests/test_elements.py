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


# ---- fix round 1 ----------------------------------------------------------------------
def _tle_with_satnum(line, satnum):
    body = line[:2] + f"{satnum:05d}" + line[7:68]
    return body + str(sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in body) % 10)


def _omm_with_one_bad(bad_row, good=19):
    rows = [omm(i) for i in range(1, good + 1)] + [bad_row]
    return json.dumps(rows).encode()


@pytest.mark.parametrize("over", [
    {"BSTAR": 1e6},                              # sgp4 error 1 as soon as it is propagated
    {"ECCENTRICITY": 0.95},                      # perigee underground
    {"MEAN_MOTION": 0.0001},                     # implausibly low mean motion
    {"NORAD_CAT_ID": -5}, {"NORAD_CAT_ID": 0},   # non-positive catalogue number
])
def test_omm_counts_physically_broken_elements_as_invalid(over):
    got = E.parse_omm_json(_omm_with_one_bad(omm(99, **over)))
    assert len(got) == 19


def test_omm_deeply_nested_json_is_an_element_error():
    with pytest.raises(E.ElementError):
        E.parse_omm_json(b"[" * 200000 + b"]" * 200000)


@pytest.mark.parametrize("bad", [5, "text", [1, 2], None, [[]], True])
def test_omm_non_object_rows_are_invalid_records(bad):
    rows = [omm(i) for i in range(1, 20)] + [bad]
    assert len(E.parse_omm_json(json.dumps(rows).encode())) == 19


def test_omm_ten_percent_boundary():
    one_bad = [omm(i) for i in range(1, 10)] + [omm(99, ECCENTRICITY=1.5)]
    assert len(E.parse_omm_json(json.dumps(one_bad).encode())) == 9
    two_bad = [omm(i) for i in range(1, 9)] + [omm(98, ECCENTRICITY=1.5), omm(99, ECCENTRICITY=1.5)]
    with pytest.raises(E.ElementError, match="invalid"):
        E.parse_omm_json(json.dumps(two_bad).encode())


@pytest.mark.parametrize("oid,want", [("1998-067A", "1998-067A"), ("1998-067a", "1998-067A"), ("###", ""),
                                      ("garbage text", ""), ("", ""), ("1998-067ABCD", "")])
def test_omm_intl_is_valid_or_empty(oid, want):
    got = E.parse_omm_json(json.dumps([omm(1, OBJECT_ID=oid)]).encode())
    assert got[0].intl == want


def test_tle_checksum_rejects_overlong_lines():
    assert not E.tle_checksum_ok(ISS_L1 + "5")
    assert not E.tle_checksum_ok(ISS_L1 + "123456")
    assert E.tle_checksum_ok(ISS_L1 + "   ")        # trailing whitespace is ignored


def test_tle_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(E, "MAX_BYTES", 10)
    with pytest.raises(E.ElementError, match="too large"):
        E.parse_tle_text(f"{ISS_L1}\n{ISS_L2}\n".encode())


def test_tle_rejects_too_many_objects(monkeypatch):
    monkeypatch.setattr(E, "MAX_OBJECTS", 2)
    with pytest.raises(E.ElementError, match="too many"):
        E.parse_tle_text((f"{ISS_L1}\n{ISS_L2}\n" * 3).encode())


def test_tle_mismatched_catalogue_numbers_are_invalid_records():
    bad2 = _tle_with_satnum(ISS_L2, 25545)
    assert E.tle_checksum_ok(bad2)
    got = E.parse_tle_text((f"{ISS_L1}\n{ISS_L2}\n" * 10 + f"{ISS_L1}\n{bad2}\n").encode())
    assert len(got) == 10


def test_tle_returns_good_records_when_few_are_bad():
    bad = ISS_L1[:-1] + "0"
    got = E.parse_tle_text((f"N\n{ISS_L1}\n{ISS_L2}\n" * 10 + f"N\n{bad}\n{ISS_L2}\n").encode())
    assert len(got) == 10


def test_tle_broken_sets_cannot_vanish_silently():
    # 10 named sets, 5 with a corrupted second line: must be rejected, not quietly shrunk
    broken = "3 " + ISS_L2[2:]
    sets = [f"SAT {i}\n{ISS_L1}\n{ISS_L2}\n" for i in range(5)] + [f"SAT {i}\n{ISS_L1}\n{broken}\n" for i in range(5)]
    with pytest.raises(E.ElementError):
        E.parse_tle_text("".join(sets).encode())


def test_tle_garbage_flood_with_one_valid_set_is_rejected():
    with pytest.raises(E.ElementError):
        E.parse_tle_text(("<html>\n" * 1000 + f"{ISS_L1}\n{ISS_L2}\n").encode())


def test_tle_orphan_element_line_never_becomes_a_name():
    sets = f"{ISS_L1}\n{ISS_L2}\n" * 10 + f"{ISS_L1}\n" + f"{ISS_L1}\n{ISS_L2}\n"
    got = E.parse_tle_text(sets.encode())
    assert len(got) == 11 and all(e.name == "OBJ 25544" for e in got)
    orphan2 = f"{ISS_L1}\n{ISS_L2}\n" * 10 + f"{ISS_L2}\n" + f"{ISS_L1}\n{ISS_L2}\n"
    assert all(e.name == "OBJ 25544" for e in E.parse_tle_text(orphan2.encode()))


def test_tle_stray_text_within_ten_percent_is_tolerated():
    got = E.parse_tle_text((f"{ISS_L1}\n{ISS_L2}\n" * 20 + "stray words\nmore words\n").encode())
    assert len(got) == 20
