"""Benchmark gate (spec 4.3): one ordinary recalculation of every tracked object must take
under 1 second on one core, and the tick that builds the look-ahead table under 2 seconds.
Run on the machine that will host the service:

    python bench.py            # exits 0 on pass, 1 on fail

Uses 16,000 synthetic Starlink objects (more than the real list) plus the other screens at
generous sizes, each with its own look-ahead horizon from screens.SCREENS.
"""
import sys
import time

from sgp4.api import Satrec

from elements import Elem
from engine import Engine

L1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
L2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"
N, RUNS = 16000, 5
OTHER = {"nasa": 9, "spy": 1500, "bright": 300, "gnss": 200, "new": 1000}   # generous sizes
TICK_LIMIT_S, TABLE_LIMIT_S = 1.0, 2.0
T0 = 1575910800.0

def _elems(n, base):
    return [Elem(f"S{i}", base + i, Satrec.twoline2rv(L1, L2), "", T0) for i in range(n)]


built = {"starlink": (_elems(N, 0), [""] * N)}
for k, (name, n) in enumerate(OTHER.items()):
    built[name] = (_elems(n, 100000 * (k + 1)), [""] * n) + (([T0] * n,) if name == "new" else ())
eng = Engine()
eng.set_screens(built, {name: T0 for name in built})

t = time.perf_counter()
eng.tick(T0)                      # first tick: also builds the look-ahead table
table_tick = time.perf_counter() - t

worst = 0.0
for k in range(1, RUNS + 1):      # ordinary ticks: the table is reused
    t = time.perf_counter()
    eng.tick(T0 + 5 * k)
    worst = max(worst, time.perf_counter() - t)

t = time.perf_counter()
eng.query("starlink", 42.0, -71.3, 85)      # cutoff 85: nothing is up, so a pass search runs
query = time.perf_counter() - t
t = time.perf_counter()
eng.query("bright", 42.0, -71.3, 85)        # the 24 h search with the sunlit / dark mask
bright = time.perf_counter() - t

total = N + sum(OTHER.values())
print(f"{total} objects in all ({N} starlink, " + ", ".join(f"{n} {k}" for k, n in OTHER.items()) + ")")
print(f"ordinary tick, worst of {RUNS}: {worst * 1000:.0f} ms (limit {TICK_LIMIT_S * 1000:.0f} ms)")
print(f"tick that builds the look-ahead table: {table_tick * 1000:.0f} ms (limit {TABLE_LIMIT_S * 1000:.0f} ms)")
print(f"one uncached query needing a pass search over {N} objects: {query * 1000:.0f} ms")
print(f"one uncached bright query needing a 24 h masked pass search over {OTHER['bright']} objects: {bright * 1000:.0f} ms")
sys.exit(0 if worst < TICK_LIMIT_S and table_tick < TABLE_LIMIT_S else 1)
