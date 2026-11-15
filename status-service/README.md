# Server status service

Feeds the Status view of the desk dials: every Hetzner Cloud server, every resource on one or
more Coolify instances, and every public domain, rolled up into one small JSON.

- `GET /status` — `Authorization: Bearer <SERVICE_KEY>` — what the dials read
- `GET /health` — is the monitor alive (no auth, no details)

## States

| state | when |
|-------|------|
| **down** | Hetzner server not running; Coolify server unreachable; a resource degraded, restarting or unhealthy; a domain that fails its HTTPS check (no connection, certificate error, HTTP 5xx); a `PORT_CHECKS` service that doesn't answer, fails TLS or greets wrongly. A stopped resource that has a domain is down, because its site is. |
| **warn** | a checked TLS certificate close to expiry; starting; a server's 5-minute CPU at or above `CPU_WARN`; an API the monitor could not read; Coolify says stopped but the site still answers |
| **stopped** | exited and no domain — something you turned off. Grey, not an alert. |
| **up** | running (healthy, or no health check configured) and its domain answers |

Coolify reports status as `state:health` (`running:healthy`, `running:unknown`,
`degraded:unhealthy`, `restarting:unknown`, …) or `exited`; see `classify_coolify()`.

## Configuration

| Variable | |
|----------|---|
| `SERVICE_KEY` | bearer key the dials send — start it with `st_` (e.g. `st_` + `openssl rand -hex 24`) so a dial set up from its hotspot knows it is a status key |
| `HETZNER_TOKEN` | Hetzner Cloud API token — **Read** is enough |
| `COOLIFY_INSTANCES` | `NAME=URL,NAME=URL`, e.g. `VAROAPPS-1=http://89.167.57.129:8000,VAROAPPS-2=http://coolify:8080` (an instance's own Coolify is reachable from its apps at `http://coolify:8080` on the `coolify` Docker network) |
| `COOLIFY_TOKEN_<NAME>` | one API token per instance (upper-case, `-` → `_`), permission **read** |
| `EXTRA_URLS` | optional `Label=https://…,…` to check as well |
| `PORT_CHECKS` | optional `Label=host:port,…` TCP services to check, e.g. mail: `Mail SMTP=mail.example.com:25,Mail IMAPS=mail.example.com:993`. 465/993/995 are checked over TLS with the certificate verified; SMTP ports must greet with `220`, IMAP with `* OK`. |
| `CERT_WARN_DAYS` | warn when a checked TLS certificate expires within this many days (default 14) |
| `IGNORE` | optional comma list of resource names to leave out |
| `POLL_SECONDS` | default 60 |
| `CPU_WARN` | default 90 |

Run it on a different server from the ones that matter most, or accept that when its own host
dies the dial shows "can't reach the status service" — which is itself the alert.

Load: per poll, one Hetzner list call plus one metrics call per running server; per Coolify
instance, `/servers`, `/resources` and one `/servers/{uuid}/resources` per server (plus one call per
service without a top-level domain); one HTTPS GET per domain, eight at a time.
