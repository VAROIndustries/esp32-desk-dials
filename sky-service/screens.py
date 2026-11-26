"""What each screen shows: satellites (starlink, nasa, spy, bright, gnss, new) and the Moon
and planets (planets, calculated by planets.py, not from orbital elements).

Edit NASA_IDS to change the NASA screen without touching the dial firmware.
"""
from __future__ import annotations

from dataclasses import replace

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
    "bright":   {"cut": 20, "horizon_s": 24 * 3600, "private": False},   # only in the dark, only sunlit
    "gnss":     {"cut": 10, "horizon_s": 60 * 60, "private": False},     # navigation satellites, tagged
    "new":      {"cut": 20, "horizon_s": 2 * 3600, "private": False},    # launched in the last 30 days
    "planets":  {"cut": 0, "horizon_s": 24 * 3600, "private": False},    # Moon and planets (planets.py)
}
PLANETS_SCREEN = "planets"
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
    "visual": "visual",
    "gps-ops": "gps-ops",
    "galileo": "galileo",
    "glo-ops": "glo-ops",
    "beidou": "beidou",
    "last-30-days": "last-30-days",
}
# gnss screen: source -> tag
GNSS_TAGS = {"gps-ops": "GPS", "galileo": "GAL", "glo-ops": "GLO", "beidou": "BDS"}
# Launch dates for the new screen (CelesTrak SATCAT records). Parsed to {intl designator: unix}.
SATCAT_SOURCE = "satcat"
SATCAT_URL = "https://celestrak.org/satcat/records.php?GROUP=last-30-days&FORMAT=json"
HOBBY_SOURCE = "hobby"           # extra TLE file (SPY_HOBBY_URL), once a day

# An orbit measured long ago predicts badly. These are judgement calls, not a standard.
MAX_EPOCH_AGE_S = 30 * 86400          # official lists: older than this is left out
MAX_HOBBY_EPOCH_AGE_S = 60 * 86400    # extra TLE file: updated less often, kept longer
MAX_FUTURE_EPOCH_S = 86400            # an orbit dated more than a day ahead is bogus


def build_screens(sources: dict, spy_enabled: bool, hobby_enabled: bool, now: float):
    """sources: source name -> list[Elem] (missing or empty = not available).

    sources[SATCAT_SOURCE], if present, is {international designator: launch unix time}.

    Returns (screens, dropped): screens is screen name -> (list[Elem], list[str] tags), with
    empty screens left out; the new screen has a third item, the launch time of each object
    (unix, or None when unknown); dropped is source name -> number of objects left out as too old
    or dated in the future (for the NASA sources, only objects listed in NASA_IDS count).
    An object found in more than one source of a screen appears once, at its first place and
    with its first tag, using whichever copy has the newer element set.
    """
    dropped = {}

    def fresh(src, limit=MAX_EPOCH_AGE_S, wanted=None):
        elems = sources.get(src) or []
        if elems:
            dropped[src] = 0
        if wanted is not None:
            elems = [e for e in elems if e.satnum in wanted]
        keep = [e for e in elems if -MAX_FUTURE_EPOCH_S <= now - e.epoch <= limit]
        if elems:
            dropped[src] = len(elems) - len(keep)
        return keep

    def merge(groups):
        """[(elems, tag), ...] -> (elems, tags): one entry per object, the newer copy wins."""
        pos, out, tags = {}, [], []
        for elems, tag in groups:
            for e in elems:
                i = pos.get(e.satnum)
                if i is None:
                    pos[e.satnum] = len(out)
                    out.append(e)
                    tags.append(tag)
                elif e.epoch > out[i].epoch:
                    out[i] = e
        return out, tags

    out = {}
    s = fresh("starlink")
    if s:
        out["starlink"] = (s, [""] * len(s))
    nasa, _ = merge((fresh(src, wanted=NASA_IDS), "") for src in ("stations", "science", "resource"))
    nasa = [replace(e, name=NASA_IDS[e.satnum]) for e in nasa]
    if nasa:
        out["nasa"] = (nasa, [""] * len(nasa))
    if spy_enabled:
        groups = [(fresh(HOBBY_SOURCE, MAX_HOBBY_EPOCH_AGE_S), "HOBBY")] if hobby_enabled else []
        groups.append((fresh("military"), "MIL"))
        spy, tags = merge(groups)
        if spy:
            out["spy"] = (spy, tags)
    v = fresh("visual")
    if v:
        out["bright"] = (v, [""] * len(v))
    gnss, tags = merge((fresh(src), tag) for src, tag in GNSS_TAGS.items())
    if gnss:
        out["gnss"] = (gnss, tags)
    new = fresh("last-30-days")
    if new:
        launches = sources.get(SATCAT_SOURCE) or {}
        out["new"] = (new, [""] * len(new), [launches.get(e.intl) if e.intl else None for e in new])
    return out, dropped
