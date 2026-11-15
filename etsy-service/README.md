# Etsy dial service

Feeds the desk dials with one Etsy shop's numbers for **Today**, **this month** and **this
year**: orders, gross income, Etsy fees and postage labels. A single Python file, no framework.

- `GET /summary` — `Authorization: Bearer <SERVICE_KEY>` — what the dials read
- `GET /health` — sync status, no money
- `/etsy/connect` — one-time Etsy authorization by the shop owner (asks for the service key)

## What the numbers mean

| field   | meaning |
|---------|---------|
| gross   | item prices − discounts + shipping + gift wrap, for orders not canceled or fully refunded, minus partial refunds. Sales tax, VAT and the small state delivery fees Etsy collects and remits are not income, so they're left out. |
| fees    | everything Etsy charged that period per the payment-account ledger: transaction, processing, offsite ads, Etsy Ads, listing/renewal and subscription fees, minus fee refunds and credits |
| labels  | postage labels bought on Etsy that period (kept separate — the dial has its own checkbox) |
| net     | gross − fees |

Periods are the calendar day / month / year in `TZ_NAME` (default America/New_York). Orders count
by the day they were placed; fees by the day Etsy charged them. Ledger entry types the service
doesn't recognise are left out of fees and listed by `Store.state["unknown_types"]`.

## Running it

Docker image from this folder: port 8080, persistent volume at `/data` (holds the service's own Etsy
token and the cached year of orders and ledger entries). It needs the keystring and shared secret of
the Etsy app the shop owner uses, read-only scope `transactions_r`.

| Variable | |
|----------|---|
| `ETSY_KEYSTRING`, `ETSY_SHARED_SECRET` | the shop owner's Etsy app |
| `ETSY_SHOP_ID` | numeric shop id |
| `SERVICE_KEY` | long random string; also typed into each dial |
| `PUBLIC_URL` | where this service is reachable over https (its `/etsy/callback` must be a callback URL on the Etsy app) |
| `SHOP_NAME` | optional, echoed in `/summary` |
| `TZ_NAME`, `SYNC_SECONDS` | optional |

Then open `<PUBLIC_URL>/etsy/connect`, enter the service key and approve on Etsy. Etsy rotates refresh
tokens on every use, so this authorization must not be shared with any other script — it gets its
own.

Load: the first sync reads the year's receipts and the payment ledger (Etsy caps ledger queries at 31
days, so it walks the year in 30-day windows); after that it fetches only what changed every
5 minutes, and re-reads receipts in full every 6 hours to catch late refunds. That is roughly 600
requests a day at no more than 4 a second — well inside a personal Etsy app's 5/second and
5,000/day, which the app shares with any other tool using the same keystring.

*The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or
certified by Etsy, Inc.*
