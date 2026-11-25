"""What each satellite screen shows. Milestone 1: starlink, nasa, spy.

Edit NASA_IDS to change the NASA screen without touching the dial firmware.
"""
from __future__ import annotations

# NORAD catalogue numbers. Verify any new entry against CelesTrak's SATCAT before adding.
NASA_IDS = {
    25544: "ISS",
    20580: "HUBBLE",
    39084: "LANDSAT 8",
    49260: "LANDSAT 9",
    25994: "TERRA",
    27424: "AQUA",
    28376: "AURA",
    54754: "SWOT",
    58928: "PACE",
}

# cut: default elevation cutoff (deg). horizon_s: how far ahead to look for the next pass.
SCREENS = {
    "starlink": {"cut": 60, "horizon_s": 20 * 60, "private": False},
    "nasa":     {"cut": 20, "horizon_s": 24 * 3600, "private": False},
    "spy":      {"cut": 20, "horizon_s": 24 * 3600, "private": True},
}
CUT_MIN, CUT_MAX = 0, 85
MAX_SCREENS_PER_REQUEST = 3

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"
# source name -> CelesTrak group. One request per group, each at most once per hour.
GROUPS = {
    "starlink": "starlink",
    "stations": "stations",
    "science": "science",
    "resource": "resource",
    "military": "military",
}
HOBBY_SOURCE = "hobby"           # TLE text from SPY_HOBBY_URL, once a day, optional proxy

# An orbit measured long ago predicts badly. These are judgement calls, not a standard.
MAX_EPOCH_AGE_S = 30 * 86400          # official lists: older than this is left out
MAX_HOBBY_EPOCH_AGE_S = 60 * 86400    # hobbyist list: observed less often, kept longer
MAX_FUTURE_EPOCH_S = 86400            # an orbit dated more than a day ahead is bogus


def build_screens(sources: dict, spy_enabled: bool, hobby_enabled: bool, now: float):
    """sources: source name -> list[Elem] (missing or empty = not available).

    Returns (screens, dropped): screens is screen name -> (list[Elem], list[str] tags), with
    empty screens left out; dropped is source name -> number of objects left out as too old
    or dated in the future.
    """
    dropped = {}

    def fresh(src, limit=MAX_EPOCH_AGE_S):
        elems = sources.get(src) or []
        keep = [e for e in elems if -MAX_FUTURE_EPOCH_S <= now - e.epoch <= limit]
        if elems:
            dropped[src] = len(elems) - len(keep)
        return keep

    out = {}
    s = fresh("starlink")
    if s:
        out["starlink"] = (s, [""] * len(s))
    nasa, seen = [], set()
    for src in ("stations", "science", "resource"):
        for e in fresh(src):
            if e.satnum in NASA_IDS and e.satnum not in seen:
                seen.add(e.satnum)
                nasa.append(type(e)(NASA_IDS[e.satnum], e.satnum, e.sat, e.intl, e.epoch))
    if nasa:
        out["nasa"] = (nasa, [""] * len(nasa))
    if spy_enabled:
        spy, tags, seen = [], [], set()
        if hobby_enabled:
            for e in fresh(HOBBY_SOURCE, MAX_HOBBY_EPOCH_AGE_S):
                if e.satnum not in seen:
                    seen.add(e.satnum); spy.append(e); tags.append("HOBBY")
        for e in fresh("military"):
            if e.satnum not in seen:
                seen.add(e.satnum); spy.append(e); tags.append("MIL")
        if spy:
            out["spy"] = (spy, tags)
    return out, dropped
