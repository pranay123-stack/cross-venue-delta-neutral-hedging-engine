# Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
docker compose up --build
```

| Service | Host port | URL |
|---|---|---|
| Dashboard (nginx) | 5174 | <http://localhost:5174> |
| Backend API | 8001 | <http://localhost:8001/docs> |
| PostgreSQL | 5468 | |
| Redis | 6479 | |

Ports are deliberately non-default so the stack runs alongside other projects.
Every one is a variable — override in `.env` rather than editing the compose
file.

The frontend's nginx proxies `/api` and `/ws` to the backend, so the browser
sees a single origin and CORS never applies.

The backend needs **outbound HTTPS** to `api.india.delta.exchange`,
`api.hyperliquid.xyz` and `api.kraken.com` in the default `live` mode. All
three are public, read-only market-data endpoints — no key, no account. Offline,
or behind a firewall, run the bundled recording instead:

```bash
HEDGELAB_MARKET_DATA_SOURCE=replay docker compose up --build
```

The image ships `/app/recordings/live_frames.jsonl.gz` (500 frames of a live
session) and `/app/recordings/history.json.gz` (30 days of hourly and 3 days
of 5-minute candles plus settled funding). Compose points
`HEDGELAB_MARKET_REPLAY_PATH` at the first. In replay mode, when
`HEDGELAB_MARKET_HISTORY_PATH` is unset, the statistics estimator is seeded
from the `history.json.gz` that sits beside the recording, so the objectives use
measured statistics from the first request.

### What starts, in order

1. `postgres` and `redis` come up and pass their health checks.
2. `backend` waits for the database (40 attempts, 2s apart), runs
   `alembic upgrade head`, then serves. On startup it loads the instrument
   catalogue, takes a first poll of both venues (live) or the first frame
   (replay), and seeds the statistics estimator from real 5-minute candles. A
   failed first poll is logged and the API still comes up and says why; the
   affected instruments return 503 until a quote arrives.
3. `frontend` serves the built bundle.

The backend entrypoint **refuses to start** if `HEDGELAB_TRADING_MODE` is
anything but `PAPER`, exiting 78 (`EX_CONFIG`).

## Local development

```bash
# backend
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
export HEDGELAB_DATABASE_URL="sqlite+aiosqlite:///$PWD/hedgelab.db"
.venv/bin/hedgelab serve                 # :8000, creates the SQLite schema itself

# frontend
cd frontend
npm install && npm run dev               # :5173, proxies to :8000
```

Against PostgreSQL instead:

```bash
export HEDGELAB_DATABASE_URL="postgresql+asyncpg://hedgelab:hedgelab@localhost:5468/hedgelab"
.venv/bin/alembic upgrade head
.venv/bin/hedgelab serve
```

The API creates the schema directly only for SQLite. On PostgreSQL, Alembic
owns it.

## Configuration

Every setting is an environment variable prefixed `HEDGELAB_`. The ones that
matter:

| Variable | Default | Notes |
|---|---|---|
| `HEDGELAB_TRADING_MODE` | `PAPER` | Anything else is refused |
| `HEDGELAB_ENVIRONMENT` | `local` | Outside `local`/`test`, API keys are mandatory |
| `HEDGELAB_DATABASE_URL` | PostgreSQL on 5468 | `sqlite+aiosqlite:///…` also supported |
| `HEDGELAB_REDIS_URL` | Redis on 6479 | Degrades to in-process if unreachable |
| `HEDGELAB_API_KEYS` | *(unset)* | `user:role:key` triples, comma separated |
| `HEDGELAB_ACCOUNT_CURRENCY` | `USD` | Reporting currency for every aggregate |
| `HEDGELAB_PAPER_STARTING_BALANCE` | `250000` | Per venue |
| `HEDGELAB_MARKET_DATA_SOURCE` | `live` | `live` polls the venues; `replay` steps a recording. Nothing else is accepted |
| `HEDGELAB_MARKET_POLL_SECONDS` | `3` | Live poll interval. One poll of four coins costs 28 of Hyperliquid's 1200 request-weight per minute per IP, so 3s uses about half |
| `HEDGELAB_MARKET_REPLAY_PATH` | *(unset)* | Recording to replay; required when the source is `replay` |
| `HEDGELAB_MARKET_HISTORY_PATH` | *(unset)* | Candle history that seeds the statistics; when unset, live mode fetches 5-minute candles and replay uses the `history.json.gz` beside the recording |
| `HEDGELAB_MARKET_DEPTH` | `20` | Book levels kept per side from each venue |
| `HEDGELAB_STALE_DATA_THRESHOLD_SECONDS` | `15` | A quote older than this, relative to the newest poll, is stale and blocks execution |
| `HEDGELAB_RANDOM_SEED` | `20260908` | Seeds the fault injector's probabilistic draws — never a price |
| `HEDGELAB_STATS_SAMPLE_SECONDS` | `300` | Minimum spacing between statistics samples |
| `HEDGELAB_STATS_WINDOW` / `_MIN_SAMPLES` | `500` / `30` | Estimator window and the floor below which it refuses to answer |
| `HEDGELAB_LOG_LEVEL` / `_LOG_JSON` | `INFO` / `true` | |
| `HEDGELAB_MAX_SPREAD_BPS_FOR_EXECUTION` | `50` | Execution quality gate |

There is deliberately **no setting for an exchange credential** — the platform
has no live path and nothing to authenticate with. The market data it reads is
public.

The simulator-era settings (`SIMULATOR_SEED`, `SIMULATOR_TICK_MS`,
`SIMULATOR_SECONDS_PER_TICK`) are gone. An old `.env` that still sets them is
ignored, not honoured — remove them.

## Security

* Container runs as an unprivileged user (uid 10001).
* No secret is committed; `.env` is gitignored and `.env.example` holds only
  placeholders.
* API keys are stored as SHA-256 hashes and compared in constant time.
* Irreversible actions require `ADMIN` plus `X-Confirm-Action: CONFIRM`.
* Unauthenticated operation is refused outside `local`/`test`.
* Every mutating action writes to `audit_logs`.

## Observability

* **Structured JSON logs** on stdout with `correlation_id`, `cycle_id` and
  `order_id` bound in scope.
* **`GET /api/health`** — liveness. **`GET /api/ready`** — readiness, including
  per-venue connectivity, so a disconnected venue shows as not-ready.
* Docker health checks on all four services.
* Six append-only audit tables; see [DATABASE.md](DATABASE.md).

To trace one hedge end to end:

```bash
curl -H 'X-Correlation-ID: my-trace-1' -X POST localhost:8001/api/hedge/execute \
  -H 'Content-Type: application/json' \
  -d '{"mapping_name":"BTC Delta perp -> Hyperliquid perp","source_quantity":"5000"}'

curl 'localhost:8001/api/audit?correlation_id=my-trace-1'
docker compose logs backend | grep my-trace-1
```

## Operations

```bash
docker compose logs -f backend
docker compose exec backend hedgelab status
docker compose exec backend hedgelab scenario run-all
docker compose exec backend hedgelab instruments sync          # print the venues' current specs (dry run)
docker compose exec backend alembic upgrade head
docker compose exec postgres pg_dump -U hedgelab hedgelab > backup.sql

docker compose down          # keeps the volume
docker compose down -v       # deletes the database
```

## Refreshing the real data

The catalogue and the committed recordings are snapshots of the venues. To
regenerate them:

```bash
hedgelab instruments sync --write          # specs from both venues' product metadata
hedgelab market record --out rec.jsonl.gz --frames 500 --interval 2 --depth 20
hedgelab market history --out history.json.gz --hourly-days 30 --five-minute-days 3
```

Keep recordings well inside Hyperliquid's per-IP budget, and do not record
while anything else on the same IP polls it: HTTP 429s put gaps in a recording.
Hyperliquid's tick is five significant figures, so it depends on the price
level — re-sync if a price crosses a power of ten.

## Scaling notes

The system is **single-process by design**: one process owns the book. Running
two would need leader election and a per-pair lock, because two coordinators
hedging the same pair would both see the same residual and both trade it.

If you did need to scale, the split that makes sense is read replicas for the
dashboard's polling endpoints, which are the only genuinely read-heavy path.
The execution path is inherently serial per pair.

## Hard limits and the watchdog

| Setting | Default | Meaning |
|---|---|---|
| `HEDGELAB_HARD_MAX_ORDER_NOTIONAL` | `3500000` | Largest single order, account currency |
| `HEDGELAB_HARD_MAX_POSITION_NOTIONAL` | `5000000` | Largest position an order may leave |
| `HEDGELAB_HARD_MAX_DAILY_LOSS_PER_VENUE` | `100000` | Last-resort backstop; above a normal one-leg move of a hedged book |
| `HEDGELAB_HARD_MAX_ORDERS_PER_MINUTE` | `60` | Stops a runaway order loop |
| `HEDGELAB_DRY_RUN_PAYLOADS` | `true` | Build and check the venue-native payload of every order |

The `watchdog` service runs `hedgelab watchdog --timeout 30 --interval 5`. It
reads the backend's heartbeat from the database and, if it stops for 30 seconds,
flattens every position and leaves the kill switch engaged. Clear it with
`DELETE /api/emergency/kill-switch` once the cause is understood. An operator
can trigger it independently of the API with
`docker compose exec watchdog hedgelab watchdog --kill-now`.
