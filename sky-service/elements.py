"""Parse and validate downloaded orbital elements before the service uses them.

Two element formats: CelesTrak GP data as OMM JSON, and classic TLE text (an extra TLE file, which
may arrive as a zip archive holding that text). Plus CelesTrak SATCAT JSON, used only for launch dates.
Anything that fails validation raises ElementError; the caller keeps its last good copy.
"""
from __future__ import annotations

import datetime
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass

from sgp4 import omm
from sgp4.api import Satrec

MAX_BYTES = 8 * 1024 * 1024      # largest file accepted (Starlink JSON is a few MB)
MAX_OBJECTS = 20000
MAX_ZIP_MEMBERS = 16
MAX_BAD_FRACTION = 0.10          # more broken records than this -> reject the whole file
NAME_LEN = 24
_NAME_OK = re.compile(r"[^A-Z0-9 ()+./-]")
_REV_PER_DAY = 1440.0 / (2 * math.pi)


class ElementError(Exception):
    """The downloaded file is unusable as a whole."""


@dataclass(frozen=True)
class Elem:
    name: str
    satnum: int
    sat: Satrec
    intl: str          # international designator, e.g. "1998-067A" ("" if unknown)
    epoch: float = 0.0  # unix time the orbit was measured (element set epoch)


def clean_name(raw) -> str:
    """Upper-case, safe characters only, fixed maximum length. Never empty."""
    s = _NAME_OK.sub("", re.sub(r"\s+", " ", str(raw).upper()))
    s = re.sub(r" +", " ", s).strip()[:NAME_LEN].strip()
    return s or "UNKNOWN"


def epoch_unix(sat: Satrec) -> float:
    return (sat.jdsatepoch - 2440587.5 + sat.jdsatepochF) * 86400.0


MIN_REV_DAY = 0.05               # below this the "orbit" is not a satellite orbit
_PROBE_DAYS = (0.0, 0.25)        # propagate here at load time; sgp4 flags failures only when propagating
_INTL = re.compile(r"[0-9]{4}-[0-9]{3}[A-Z]{1,3}")


def _sane(sat: Satrec) -> bool:
    rev_day = sat.no_kozai * _REV_PER_DAY
    if not (
        all(math.isfinite(v) for v in (sat.no_kozai, sat.ecco, sat.inclo, sat.nodeo, sat.argpo, sat.mo, sat.bstar))
        and MIN_REV_DAY <= rev_day <= 18.0
        and 0.0 <= sat.ecco < 1.0
        and 0.0 <= sat.inclo <= math.pi
        and sat.error == 0
        and sat.satnum > 0
        and sat.altp >= 0.0                      # perigee at or above the surface (altp is in Earth radii)
    ):
        return False
    for d in _PROBE_DAYS:
        err, r, _v = sat.sgp4(sat.jdsatepoch, sat.jdsatepochF + d)
        if err != 0 or not all(math.isfinite(x) for x in r):
            return False
    return True


def _intl(raw) -> str:
    s = str(raw or "").strip().upper()
    return s if _INTL.fullmatch(s) else ""


def _finish(good: list, bad: int, total: int) -> list:
    if total == 0 or not good:
        raise ElementError("no usable objects")
    if bad / total > MAX_BAD_FRACTION:
        raise ElementError(f"{bad} of {total} records invalid")
    return good


def parse_omm_json(raw: bytes) -> list:
    """CelesTrak `FORMAT=json` body -> list[Elem]."""
    if len(raw) > MAX_BYTES:
        raise ElementError("file too large")
    try:
        rows = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise ElementError(f"not JSON: {e}") from None
    if not isinstance(rows, list):
        raise ElementError("JSON is not a list")
    if len(rows) > MAX_OBJECTS:
        raise ElementError("too many objects")
    good, bad = [], 0
    for row in rows:
        try:
            sat = Satrec()
            omm.initialize(sat, row)
            if not _sane(sat):
                raise ValueError("out of range")
            good.append(Elem(clean_name(row.get("OBJECT_NAME", "")), int(sat.satnum), sat,
                             _intl(row.get("OBJECT_ID")), epoch_unix(sat)))
        except Exception:
            bad += 1
    return _finish(good, bad, len(rows))


SATCAT_MAX_BYTES = 1024 * 1024
SATCAT_MAX_ROWS = 2000
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_satcat_json(raw: bytes) -> dict:
    """CelesTrak SATCAT `FORMAT=json` body -> {international designator: launch day as unix time
    (midnight UTC)}. Only OBJECT_ID and LAUNCH_DATE are read; rows failing either are invalid."""
    if len(raw) > SATCAT_MAX_BYTES:
        raise ElementError("file too large")
    try:
        rows = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise ElementError(f"not JSON: {e}") from None
    if not isinstance(rows, list):
        raise ElementError("JSON is not a list")
    if len(rows) > SATCAT_MAX_ROWS:
        raise ElementError("too many objects")
    good, bad = {}, 0
    for row in rows:
        try:
            intl, day = row["OBJECT_ID"], row["LAUNCH_DATE"]
            if not (isinstance(intl, str) and _INTL.fullmatch(intl) and isinstance(day, str) and _DATE.fullmatch(day)):
                raise ValueError("bad row")
            when = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            good[intl] = when.timestamp()
        except Exception:
            bad += 1
    _finish(list(good), bad, len(rows))
    return good


def tle_checksum_ok(line: str) -> bool:
    line = line.rstrip()
    if len(line) != 69 or not line[68].isdigit():
        return False
    total = sum(int(ch) if ch.isdigit() else (1 if ch == "-" else 0) for ch in line[:68])
    return total % 10 == int(line[68])


def unzip_tle(raw: bytes) -> bytes:
    """Zip archive body -> the bytes of its one TLE member, read in memory only.

    Nothing is extracted or written to disk; member names are never used as paths and never appear in
    an error message (they are chosen by whoever made the archive). Every failure is an ElementError.
    """
    if len(raw) > MAX_BYTES:
        raise ElementError("file too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        raise ElementError("not a zip archive") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise ElementError("too many members")
        if any(i.flag_bits & 0x1 for i in infos):
            raise ElementError("encrypted member")
        files = [i for i in infos if not i.is_dir()]
        tles = [i for i in files if i.filename.lower().endswith((".tle", ".txt"))]
        if len(tles) > 1:
            raise ElementError("more than one TLE member")
        if tles:
            member = tles[0]
        elif len(files) == 1:
            member = files[0]
        else:
            raise ElementError("no TLE member")
        if member.file_size > MAX_BYTES:            # declared size: early reject only, never trusted
            raise ElementError("member too large")
        try:
            with zf.open(member) as f:
                data = f.read(MAX_BYTES + 1)
        except Exception:
            raise ElementError("unreadable zip member") from None
    if len(data) > MAX_BYTES:
        raise ElementError("member too large")
    return data


def parse_tle_text(raw: bytes) -> list:
    """TLE text (2-line sets, optionally preceded by a name line) -> list[Elem]."""
    if len(raw) > MAX_BYTES:
        raise ElementError("file too large")
    try:
        lines = [ln.rstrip() for ln in raw.decode("ascii", errors="strict").splitlines() if ln.strip()]
    except UnicodeDecodeError:
        raise ElementError("not ASCII text") from None
    good, bad, total, junk, name, i = [], 0, 0, 0, None, 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith(("1 ", "2 ")):
            total += 1
            if ln.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                l1, l2 = ln, lines[i + 1]
                i += 2
                try:
                    if total > MAX_OBJECTS:
                        raise ElementError("too many objects")
                    if not (tle_checksum_ok(l1) and tle_checksum_ok(l2)) or l1[2:7] != l2[2:7]:
                        raise ValueError("checksum")
                    sat = Satrec.twoline2rv(l1, l2)
                    if not _sane(sat):
                        raise ValueError("out of range")
                    good.append(Elem(clean_name(name or f"OBJ {sat.satnum}"), int(sat.satnum), sat, "", epoch_unix(sat)))
                except ElementError:
                    raise
                except Exception:
                    bad += 1
            else:                       # element line that is not half of an adjacent pair
                bad += 1
                junk += name is not None
                i += 1
            name = None
        else:
            junk += name is not None    # a name line already waiting was not followed by a pair
            name = ln[2:] if ln.startswith("0 ") else ln
            i += 1
    junk += name is not None
    if junk > MAX_BAD_FRACTION * len(lines):
        raise ElementError(f"{junk} of {len(lines)} lines are not element data")
    return _finish(good, bad, total)
