# Sky service

Answers "what is overhead?" for the VARO Desk Dial **Sky** source. It downloads public
orbital elements, does the orbit math once for every dial, and returns a small JSON reply.
Screens (default cutoff, how far ahead the next pass is searched):

| Screen | What it shows | Cutoff | Next pass within |
|---|---|---|---|
| `starlink` | Starlink satellites | 60 | 20 minutes |
| `nasa` | a fixed list of nine NASA satellites (`NASA_IDS` in `screens.py`) | 20 | 24 hours |
| `bright` | satellites bright enough to see with the eye, only while it is dark where you are and only those in sunlight | 20 | 24 hours |
| `gnss` | navigation satellites: GPS, Galileo, GLONASS and BeiDou, tagged `GPS`, `GAL`, `GLO`, `BDS` | 10 | 1 hour |
| `new` | objects launched in the last 30 days, with days since launch | 20 | 2 hours |
| `planets` | the Moon, Mercury, Venus, Mars, Jupiter and Saturn | 0 | next rise within 24 hours |

One more screen needs a key (see `SKY_SPY_KEY` below).

## API

    GET /v1/sky?lat=42.0&lon=-71.3&screens=starlink,nasa&cut=starlink:60,nasa:20
    GET /health

`/v1/sky` takes plain decimals for `lat` and `lon`, one to three distinct screen names in
`screens`, and an optional `cut` (minimum elevation in degrees, 0 to 85, at most two digits
per screen). Any invalid value is a 400. Unknown query parameters are ignored (more than 8
fields in all is a 400); if a parameter is repeated, the first one is used.

Reply:

    {"t": 1795631161, "poll": 50, "lag_ms": 591, "screens": {...}, "notice": "..."}

- `t`: server time (unix seconds). `poll`: seconds the dial should wait before asking again
  (always 50 in a 200 reply). `notice`: only present when `SKY_NOTICE` is set.
- `lag_ms`: how old the start of every track was when the reply was built. The dial starts
  that far into each track. Replies are never cached: every reply, including errors, carries
  `Cache-Control: private, no-store`.
- Each screen has `count` (everything above the cutoff right now), `age_h` (hours since the
  elements were downloaded, -1 if unknown), up to 60 `objects` (highest first), and `next`.
  An object is `n` (name), `tag`, and `trk`: 13 `[azimuth, elevation]` points, 5 seconds
  apart, starting at the time of the track. An object gets `"old": true` when its orbit was
  measured more than 14 days ago.
- Extra fields on some screens:
  - `gnss` has `by_tag`, the count per constellation (`{"GPS": 9, "GAL": 7, "GLO": 5, "BDS": 8}`)
    over everything above the cutoff, not only the 60 objects sent.
  - On `new`, an object has `d`, whole days since its launch, when the launch date is known
    (it is left out when the launch-date list could not be downloaded).
  - `bright` has `dark`: true when the Sun is more than 6 degrees below the horizon at the
    location. When it is not dark, `count` is 0 and `objects` is empty. When it is dark, only
    satellites in sunlight are counted and sent. Its `next` is the first pass that is above
    the cutoff, in sunlight and in the dark at the same time. The Sun's position comes from a
    low-precision formula (about 0.01 degree), and Earth's shadow is treated as a cylinder:
    the penumbra, which lasts a few seconds of a pass, is ignored.
  - `planets` objects are named `MOON`, `MERCURY`, `VENUS`, `MARS`, `JUPITER`, `SATURN` and
    tagged `MOON` or `PLANET`; `age_h` is always 0. Their tracks start at the same time as the
    satellite tracks of the same reply, so `lag_ms` applies to them too. Its `next` is the
    first of the six to rise above the cutoff within 24 hours, `from` is the compass point it
    rises at, and `max_el` is always 0. Positions are geometric (no atmospheric refraction),
    like the satellites'.
- `next` is only set when nothing is above the cutoff: `n`, `tag`, `in_s` (seconds until it
  rises), `from` (compass point it rises from), `max_el` (peak elevation within the search
  window). It is `null` when no pass rises inside the window. When no pass search could run
  right now the screen has `"next_busy": true` instead; ask again later.
- A screen with no usable data yet is `{"unavailable": true}`, and so is every screen when
  the calculation has stalled. A screen that needs a key, asked for without one, is
  `{"locked": true}`.

Errors are short JSON: 400 (bad request), 403 (missing edge header, see `ORIGIN_SECRET`),
404, 405 (only GET), 429 (per-address limit, with `Retry-After`), 500 (`"internal error"` or
`"reply too large"`), 503 (busy, with `Retry-After`). Only the 429 and 503 busy replies carry
`"poll": 300`; a 200 always has `"poll": 50`.

`/health` reports `ok`, `tick_age_s` (seconds since the last calculation), `edge_locked`,
`dropped_old` (objects left out as too old, per source) and per-source `objects`, `age_h` and
the outcome of the last download. It answers 503 when no calculation has run for 20 seconds.

## Data sources and their rules

Orbital elements are CelesTrak GP data (`celestrak.org`): groups `starlink`, `stations`,
`science`, `resource`, `visual`, `gps-ops`, `galileo`, `glo-ops`, `beidou` and
`last-30-days`. The NASA screen picks its satellites out of `stations`, `science` and
`resource`. Launch dates for the `new` screen come from CelesTrak's satellite catalogue
(`/satcat/records.php?GROUP=last-30-days&FORMAT=json`), fetched every 6 hours; only each
object's international designator and launch date are used, and a file over 1 MB or 2,000
rows, or with malformed designators or dates, is refused.

- One group per request, each group at most every 2 hours (the catalogue every 6), and at
  most one download a minute across all of them. This follows CelesTrak's usage policy; do not shorten it.
- On any answer other than HTTP 200, or any network, size or validation failure, that source
  is not asked again for 1 hour, then 2, doubling up to 24 hours, and the last good copy keeps
  being served. A non-200 answer or network failure from any `celestrak.org` address (the
  catalogue included) holds back every `celestrak.org` source for that long. The schedule is saved in `DATA_DIR`, so a restart never asks sooner.
- Downloads are HTTPS only, follow no redirects, ignore proxy settings in the environment, are
  refused above 8 MB and are abandoned after 120 seconds in total. The request's User-Agent
  names this project and `SKY_CONTACT`.
- Every file is validated (size, object count, orbital values) before use; a file that fails
  is discarded. Objects whose orbit was measured more than 30 days ago, or is dated more than
  a day ahead, are left out.
- Each source is stored in `DATA_DIR` as `<name>.dat` plus `<name>.state.json`, so the
  service answers straight after a restart, with no network, from the last good copy.

The Moon and planets come from [Skyfield](https://rhodesmill.org/skyfield/) with JPL's DE421
ephemeris (valid 1900 to 2050). The image downloads `de421.bsp` from
`https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/` while it is built and fails the build unless
the file matches the SHA-256 pinned in the `Dockerfile`; it is stored at `/app/data/de421.bsp`
(not on the volume). Nothing is downloaded while the service runs: without the file the screen
answers `unavailable`. The next rise is picked on a 10-minute table and then timed by Skyfield,
so a body that peeks above the cutoff for under 10 minutes between two samples is missed.

The calculation runs in the background every 5 seconds and the data is rebuilt after each
download and at least hourly; requests only read the result.

## Configuration

All settings are environment variables. A bad value stops the service at start-up.

| Variable | Rule |
|---|---|
| `SKY_CONTACT` | Required. Contact address put in the User-Agent sent to CelesTrak. |
| `PORT` | Whole number 1 to 65535. Default 8080. |
| `DATA_DIR` | Where the last good element files are kept; mount a volume. Default `./data` (`/data` in the image). |
| `RATE_PER_MIN` | Requests per minute per address, 1 to 10000. Default 30. |
| `CALC_PER_SEC` | Server-wide ceiling on fresh (uncached) calculations per second, 1 to 1000. Default 10. |
| `ORIGIN_SECRET` | At least 24 characters. When set, every request must carry this value in `X-Origin-Auth` (for example added by a Cloudflare rule) or gets a 403, and the caller address is read from `CF-Connecting-IP`. `/health` from the loopback address is always answered, for the container health check. When unset the service starts with a warning and accepts any caller. |
| `SKY_SPY_KEY` | At least 24 characters. Switches on a key-gated screen, answered only to callers sending `Authorization: Bearer <key>`. The key is compared in constant time. Unset: the screen is always `locked`. |
| `SKY_NOTICE` | Optional text (first 120 characters) sent to every dial as `notice`. |
| `PLANETS_EPHEMERIS` | Path of the DE421 file for the `planets` screen. Default `/app/data/de421.bsp` (baked into the image). |

## Limits

- Per address: `RATE_PER_MIN` requests a minute (IPv6 addresses are counted per /64).
  A wrong key costs six requests' worth.
- Server-wide: at most 30 wrong-key checks a minute, `CALC_PER_SEC` fresh calculations a
  second, and 2 pass searches at a time.
- At most 32 requests in progress and 64 open connections; further ones are refused. A silent
  client is dropped after 10 seconds and no connection lives past 15 seconds. One request per
  connection.
- At most 60 objects per screen, 3 screens per request, and a reply is always under 64 KB.

## Run your own

    docker build -t sky-service .
    docker run -p 8080:8080 -v skydata:/data -e SKY_CONTACT=you@example.com sky-service

The image runs as a non-root user (`dial`, uid 10001) and needs the volume writable by that
user. Put it behind your own HTTPS; set `ORIGIN_SECRET` if only a proxy such as Cloudflare
should reach it. Then point the dial's sky-service address at your server.

`python bench.py` checks that the machine is fast enough: 16,000 synthetic Starlink objects
plus every other screen at a generous size, an ordinary 5-second calculation must take
under 1 second and the first one (which also builds the look-ahead tables for pass
searches) under 2.

## Privacy

The service keeps no record of callers. Caller addresses (IPv6 by /64) are held in memory for
the current minute only, for rate limiting, and are never written to disk or logged. Query
strings and paths are never logged either: a request log line is only the status and a route
label (`/v1/sky`, `/health` or `other`). Dials send a location rounded to 0.1 degree, and the service rounds it again.

## Tests

    pip install -r requirements-dev.txt
    python -m pytest -q

The `planets` tests need `de421.bsp`: they use `$PLANETS_EPHEMERIS` if set, else download it
once to `~/.cache/esp32-desk-dials/` (checked against the pinned SHA-256), and are skipped
with the reason when neither works.
