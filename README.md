# QF-gate — personal Gate.io market data platform (Phase 1)

Django app that keeps a list of coins **identified by chain + contract address**, pulls
**1-minute candles** from Gate.io (60 days of history plus a live stream), and computes each
day's **high and low with the time each was hit**, with Excel export.

## Quick start

```bash
python -m pip install -r requirements.txt
copy .env.example .env          # optional; defaults work for local use
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver      # http://127.0.0.1:8000  (log in, then "Add coin")
```

Then run the two background services, each in its own terminal:

```bash
python manage.py run_worker       # job queue + schedule (sync every 15 min, daily identity check)
python manage.py stream_candles   # live 1m candles over WebSocket
```

The header shows a green dot for **worker** and **live** while each service is running.

## What you get in the browser

- **Market page** (header → *Market*) — an exchange-style view of one coin, picked from the
  dropdown:
  - top: live 24h ticker (last price, change, high, low, volume);
  - left: the candle chart (below);
  - right: Gate.io's live **order book** (asks/bids with running totals and depth bars, price
    grouping and depth choice, spread) and **market trades**, refreshed every second. These are
    read live from Gate.io's public API and not stored;
  - below: the **day-by-day history**: open, high + time, low + time, close, change, range,
    volume, PDH/PDL and whether the day broke them, with totals on top. Click a day to zoom the
    chart to it.
- **Zones page** (Market page → *Zones*) — every stored day's high and low becomes a zone
  (±1% by default, adjustable 0.1–5%), and the page shows how often price came back to it:
  - **forward-only** (no hindsight): only days *after* a zone formed can touch it;
  - a day touches a zone when its range overlaps it, so trading straight through counts;
  - each touch has the **first minute** price entered the zone, and a result: *held* or
    *broke* (that day's close against where price came from), *inside* or *live*;
  - **hotspots**: up to 8 non-overlapping bands where price returned on the most distinct
    days, drawn on the chart; a **heatmap** of touches per day and price band; Excel export
    with Zones, Touches and Hotspots sheets. Code: `market/services/zones.py`.

- **Coin page chart** — candles at 1m / 5m / 15m / 1h / 4h / 1d, built from the stored 1m data.
  Dashed lines show the previous day's high/low (PDH/PDL), solid lines show the current day's
  running high/low, and H/L markers sit on the bar where each day's high/low was hit. The
  chart updates every 5 s without reloading.
- **Sync button** — queues a job; the worker runs it. If the worker is down, the page warns you.
- **Jobs page** — every scheduled or manual run with its status, duration and result.

## Commands

| Command | What it does |
|---|---|
| `run_worker [--once] [--no-schedule]` | Runs queued jobs and the schedule (`--once` drains the queue and exits) |
| `verify_coins [coin...]` | Checks chain + contract against Gate.io, fills in Gate's currency code |
| `sync_candles [coin...] [--days 60] [--no-archive]` | History: 1m REST, trade archive, fallbacks, then daily levels |
| `stream_candles` | Live 1m candles over WebSocket, REST gap-fill on reconnect, levels updated each minute |
| `compute_levels [--tz Asia/Karachi]` | Recompute daily high/low from stored candles |
| `export_levels [coin...] --out file.xlsx` | Excel: date, high + time, low + time, PDH, PDL, precision |

A `coin` can be its id, symbol, Gate pair or contract address.

## Coin identity (why the contract address)

Tickers collide (Gate.io itself lists different tokens with clashing names). A coin is
therefore stored as **chain + contract address**. On save, the app looks the contract up in
Gate.io's currency list and takes Gate's own currency code from that match. If the contract
is listed under a different chain, or the code you typed doesn't own that contract, the save
is rejected. Native coins (BTC, ETH, SOL…) have no contract; their chain identifies them.

## Where the candles come from (and how accurate they are)

Gate.io's candle API only returns the **latest 10,000 candles per interval** (1m ≈ 6.9 days).
To get 60 days at 1-minute accuracy:

1. **Last ~6.9 days** — 1m candles from the REST API.
2. **Older, completed months** — 1m candles rebuilt from Gate.io's official monthly
   **trade archive** (every individual trade, so exact). Cached in `var/archive/`.
3. **Gap** — the current month before the 1m REST window (its archive isn't published until
   the month ends) is filled with 5m candles. The high/low **price is still exact**; only the
   timestamp is to 5 minutes, and is refined to 1 minute where 1m data exists. The `Precision`
   column shows which applies. This gap disappears once `stream_candles` has been running.
4. **Live** — `stream_candles` gets Gate's pushed 1m candle every ~2–4 s.

Every daily high/low is cross-checked against Gate.io's own 1d candle, and any mismatch is flagged.

**Day boundary:** UTC by default (matches Gate's 1d candles and `btc historical pdh pdl.xlsx`,
whose values are each UTC day's own high/low). Set `MARKET_DAY_TIMEZONE` to change it, then
run `compute_levels`.

## Layout

```
market/exchanges/   exchange adapters (base interface + Gate.io) <- add trading/other exchanges here
market/services/    verify, sync, archive, levels, export      <- no web code, reusable by bots
market/management/  CLI commands (sync, stream, verify, export)
market/tests/       unit + integration tests (no network)
trading/strategies/ strategy plug-ins (PDH breakout, PDL sweep & reclaim)
trading/engine.py   bar-by-bar engine shared by backtests, paper and (phase 4) live trading
trading/            backtest.py, paper.py, models, views          tests: python manage.py test market trading
```

## Strategies, backtests and paper trading (Phase 3)

Open **Strategies** in the header.

1. **New strategy** — pick one, choose the coin and timeframe (1m–4h), and set its parameters
   plus risk and costs (starting cash, risk % per trade, max position %, fee rate, slippage).
2. **Run backtest** — replays the stored candles. The page shows return vs buy-and-hold, max
   drawdown, win rate, profit factor, average R, fees, time in market and Sharpe, plus an equity
   curve, a price chart with entry/exit markers, and every trade with the reason it was opened.
3. **Start paper trading** — runs the same strategy on live bars with simulated fills (no
   orders are sent). The live stream steps it within seconds of each minute closing, and the
   worker steps it every minute as a backup.

4. **Parameter sweep** (strategy page → *New sweep*) — backtests every combination of the values
   you enter, up to 1000. Enter values as `1, 2, 3`, a range `start:stop:step` (e.g. `0.2:1:0.2`),
   `all` for a choice, or `both` for yes/no. Any strategy or risk/cost setting can vary,
   including the fee rate, which shows how much costs matter. The candles are loaded once, so
   120 combinations on 57 days of 15m bars take about 15 seconds.
   - **In-sample / out-of-sample:** the last part of the period (30% by default) is held back.
     Combinations are ranked on the first part only, then re-tested on the held-back part. If the
     top combinations fail there, the "best" settings were luck (curve-fitting).
   - Results: a summary of how the top 10 did out-of-sample, a heatmap of the first two settings
     you varied, and a ranking table. **Save as strategy** copies a row into a new strategy; the
     original is never changed.
   - Sweeps run on the worker when it's running; otherwise they run inside the web server.
   - The timeframe can't be varied within one sweep, because each timeframe needs its own bars.
     Create one strategy per timeframe and sweep each.

**Accuracy and safety tools (Phase 3.5):**
- **1m intra-bar detail:** when a bar touches both the stop and the target, its stored 1-minute
  candles are replayed to see which was reached first (instead of assuming the stop). The
  backtest page shows how many bars this decided.
- **Strategy checks** (strategy page → *Run checks*): *look-ahead* (no database access while
  deciding; replays with the future cut off must give identical signals) and *warm-up* (signals
  must not change with more history than the strategy asks for).
- **Protections** (risk settings, all off by default, sweepable): cooldown after an exit,
  stoploss guard, max-drawdown pause, low-profit pause. Blocked entries are counted per reason.
- **Breakdowns:** results by month / week / day / exit reason / entry type on backtest and paper
  pages, with an Excel export.
- **Data quality** (coin page): per-day 1m coverage, with a *Repair gaps* button.
  Set `MARKET_HISTORY_DAYS=365` to keep a rolling year of 1m history.

**Strategy toolkit (Phase 3.6)** — all are risk/execution settings on every strategy (off by
default) and can be swept:
- **Exits:** trailing stop (optionally from +N R), break-even at +N R, partial take-profit,
  ROI target that can decay to break-even over N bars, time exit after N bars.
- **Orders:** limit (maker) entries that fill only if price trades through, with expiry; target
  exits at the maker fee; your own maker fee rate.
- **Sizing:** risk-based (default), fixed amount or % of equity, and scaling in at fixed steps,
  always capped by max position % and cash.
- **Sweeps:** *Smart search* (sample, then refine around the best: explores spaces far larger than
  the 1000-combination grid), *walk-forward* folds (pick on the past, test on the next window),
  and ranking by Sortino, Calmar or a combined score.

**Engine rules (the same for backtests and paper):** a strategy only sees closed bars, and
orders fill at the next bar's open (no look-ahead). Stop and target are checked against each
bar's high/low, using the bar's 1-minute path to see which came first; if both are hit inside the
same minute the stop counts first, and a gap fills at the open. Position size is the smallest of risk-based size, the max position % and the available
cash. Fees and slippage are charged on both fills. A test proves paper trading in small steps
produces exactly the same trades as one backtest over the same bars.

**Built-in strategies** (long-only spot) — `trading/strategies/pdh_pdl.py`:

| Strategy | Entry | Stop | Exit |
|---|---|---|---|
| PDH breakout | a bar closes above the previous day's high after closing below it | under PDH or under the breakout bar | target at R multiple, or day end |
| PDL sweep & reclaim | price trades below the previous day's low, then a bar closes back above it within N bars | under the sweep low | R multiple, mid-range or PDH; or day end |
| Zone hotspot (bounce / breakout) | price dips into a hotspot band from above and closes back above it, or closes above a band | under the band (or the dip low) | next band above, or R multiple |

The zone strategy rebuilds the Zones-page hotspots each day from the previous `lookback_days` completed days only (no look-ahead), so it needs that many days of history before it can trade: backtests, sweeps and paper trading load the extra warm-up automatically.

**Adding your own strategy:** create a class in `trading/strategies/`, subclass `Strategy`,
declare `params` (the form is generated from them), implement `on_bar(ctx)` returning
`Signal("buy", stop=..., target=...)` or `Signal("sell")`, decorate it with `@register` and
import it in `strategies/base.py::_load_builtins`. `ctx` gives the bar, recent bars, the current
day so far, the previous day (`ctx.pdh`, `ctx.pdl`), the open position and a `state` dict that
persists between bars.

## Bots: dry-run and live (Phase 4)

Header → **Bots** opens the Control page; a strategy page has **Start dry-run bot**.

- **Dry-run bot:** the same engine decides on every closed bar, but the order is "sent" the moment it
  decides and filled against Gate.io's **live order book** (walking the price levels, so slippage is
  real), with the taker fee. Nothing is sent to the exchange and no API key is needed.
- **Between bars** every live price tick from `stream_candles` is checked against the bot's stop and
  target (which follow the engine's trailing / break-even moves), so exits are not a bar late.
- **Bot page:** position, actual fills vs decision price (slippage in bps), fees, realised P&L from the
  fills next to the engine's model P&L, and Force exit / Stop.
- **Control page:** halt entries / resume, **kill switch** (sell every bot position and stop all bots),
  risk limits (max daily loss % — auto-halts —, max open exposure, max orders per hour), alerts test.
  Limits only block entries; exits always go through.
- **Live** needs all of: `LIVE_TRADING=true` and `GATE_API_KEY`/`GATE_API_SECRET` in the environment
  (trade-only key, IP-whitelisted, no withdrawals), the live switch on the Control page (type LIVE), and
  the strategy's promotion checklist (N days of dry-run, strategy checks passed).
- **Alerts:** set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` and/or `WEBHOOK_URLS`; every fill, block,
  error, halt and stream outage is sent. `python manage.py telegram_bot` adds commands:
  /status /profit /daily /balance /stop /start /kill confirm /forceexit &lt;id&gt; (your chat only).
- **Server:** see [deploy/README.md](deploy/README.md) for the VPS setup (systemd, PostgreSQL, Caddy, backups).

## More data and markets (Phase 5)

- **Order flow:** every 1m candle stores taker buy and sell volume (from Gate's trade archive and the
  live trade feed). The chart's **Order flow** box shows delta bars and the cumulative delta (CVD);
  the day history shows daily delta. Strategies can require buyers to dominate the signal bar
  (`min_entry_delta_pct`). Fill older months with `python manage.py backfill_orderflow`.
- **Backtest charts** draw the strategy's own levels (PDH/PDL, zone support/resistance), mark signals
  that were blocked or ignored, and **Export signals (CSV)** lists every signal and what became of it.
- **Screener** (header): every Gate.io pair filtered by 24h volume, spread, range, price and change,
  with a blacklist; *Add coin* still verifies the coin by contract.
- **Portfolio backtest** (strategy page): one strategy on several coins with one shared balance and a
  max number of open trades.
- **Compare exchanges** (coin page): Gate.io vs Binance daily closes, volumes and Binance's order flow
  (read-only; matched by ticker and flagged if the prices disagree).

## Advanced (Phase 6)

- **Futures backtests:** set the risk setting *Market* to *futures* to trade long **and short** with
  leverage. Each position ties up margin, has a liquidation price (checked before the stop; it costs the
  whole margin) and pays / receives funding every 8 hours from Gate's real history (`sync` keeps 180 days;
  older periods use *Funding default*). Prices come from spot candles as a stand-in for the perpetual.
- **Short strategies:** every long strategy has a mirrored twin (`..._short`), made by running the long
  logic on inverted prices: PDH breakout becomes PDL breakdown, zone bounce becomes zone rejection.
- **Adaptive ML:** *Adaptive ML: logistic regression* retrains on a rolling window of finished examples
  only (verified with the look-ahead check). On real BTC data its defaults lose money: research tool.
- **Signal sharing:** set `SIGNAL_API_TOKEN` to publish this instance's live signals at
  `/trading/api/v1/signals` (or accept pushes at `.../ingest`); on another instance run
  `python manage.py follow_signals --url https://this-host/ --name mybot` (token in `REMOTE_SIGNAL_TOKEN`)
  and start a bot with the *Follow remote signals* strategy.
- Bots (dry-run / live) trade **spot only**; futures and short strategies run in backtests, sweeps and paper.

## Schedule

`run_worker` queues these jobs itself (all settings are in `.env`, see `.env.example`):

| Job | When | Setting |
|---|---|---|
| Incremental sync of every active, verified coin | every 15 min | `MARKET_SYNC_EVERY_MINUTES` (0 = off) |
| Identity re-check on Gate.io (delistings, contract changes) | daily 00:05 UTC | `MARKET_DAILY_AT_UTC` |
| Delete job records older than 14 days | daily | `MARKET_KEEP_JOBS_DAYS` |

The incremental sync also upgrades the start-of-month 5m days to exact 1m automatically, once
Gate.io publishes that month's trade archive.

## Production: PostgreSQL + Docker

SQLite is fine on one PC. For an always-on server, use PostgreSQL. The bundled stack runs
Postgres, the web app, the worker and the stream, and restarts them automatically:

```bash
copy .env.example .env    # set DJANGO_SECRET_KEY, POSTGRES_DB=qfgate, POSTGRES_PASSWORD, DJANGO_ALLOWED_HOSTS
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

- The web app listens on `127.0.0.1:8000` only. For access from other devices, put a TLS reverse
  proxy (Caddy or nginx) in front, and set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Postgres for local development only: `docker compose up -d db`, then set `POSTGRES_DB`,
  `POSTGRES_PASSWORD` and `POSTGRES_HOST=localhost` in `.env` and run
  `pip install -r requirements.txt` (adds the `psycopg` driver).

**Moving existing SQLite data to Postgres:**

```bash
python manage.py dumpdata market auth --natural-foreign --natural-primary -o backup.json   # with POSTGRES_DB empty
python manage.py migrate                                                                     # with POSTGRES_* set
python manage.py loaddata backup.json
```

**Without Docker on Windows:** install the requirements (they include `waitress`) and run each
service as a Windows service with NSSM: `waitress-serve --listen=127.0.0.1:8000 qfgate.wsgi:application`,
`python manage.py run_worker` and `python manage.py stream_candles`.

## Roadmap

The full project plan, with every adopted feature, its source, acceptance criteria and effort,
is in **[ROADMAP.md](ROADMAP.md)**. In short:

- **Phases 1–3 (done):** coins by contract, accurate 1m data, daily levels, job queue, live
  charts, Market and Zones pages, strategy plug-ins, backtests, sweeps, paper trading.
- **Phase 3.5 (next): backtest accuracy and safety:** intra-bar (1m) detail, automatic
  look-ahead and warm-up checks, protections, result breakdowns, 365-day history, gap report.
- **Phase 3.6: strategy toolkit:** trailing / break-even / time exits, smart optimiser, more
  ranking goals, walk-forward, limit (maker) orders, sizing modes.
- **Phase 4: live trading:** order execution, risk limits and kill switch, Telegram and
  webhooks, dry-run → live promotion, VPS deployment.
- **Phase 5: more data and markets:** order-flow, multi-coin portfolios, coin filters, other
  exchanges (read-only), signal plotting.
- **Phase 6: advanced:** futures and shorts, adaptive ML models, signal sharing between instances.
