"""Benchmark gate (spec 4.3): one ordinary recalculation of every tracked object must take
under 1 second on one core, and the tick that builds the look-ahead table under 2 seconds.
Run on the machine that will host the service:

    python bench.py            # exits 0 on pass, 1 on fail

Uses 16,000 synthetic objects, more than the service tracks in milestone 1.
"""
import sys
import time

from sgp4.api import Satrec

from elements import Elem
from engine import Engine

L1 = "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991"
L2 = "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482"
N, RUNS = 16000, 5
TICK_LIMIT_S, TABLE_LIMIT_S = 1.0, 2.0
T0 = 1575910800.0

elems = [Elem(f"S{i}", i, Satrec.twoline2rv(L1, L2), "", T0) for i in range(N)]
eng = Engine()
eng.set_screens({"starlink": (elems, [""] * N)}, {"starlink": T0})

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

print(f"ordinary tick, worst of {RUNS}: {worst * 1000:.0f} ms (limit {TICK_LIMIT_S * 1000:.0f} ms)")
print(f"tick that builds the look-ahead table: {table_tick * 1000:.0f} ms (limit {TABLE_LIMIT_S * 1000:.0f} ms)")
print(f"one uncached query needing a pass search over {N} objects: {query * 1000:.0f} ms")
sys.exit(0 if worst < TICK_LIMIT_S and table_tick < TABLE_LIMIT_S else 1)
