# QF-gate roadmap

The project plan. Every phase **adds** capability; nothing that works today is removed or
narrowed. Ideas taken from other projects are re-implemented in our own code.

- **freqtrade** is GPL-3 licensed: we adopt its *ideas* only and never copy its code.
- **Cryptozonescanner** gave us the zone-revisit idea, rebuilt here forward-only and to the minute.

Status: ✅ done · 🔜 next · ⏳ planned

---

## Ground rules (apply to every item)

1. **No look-ahead.** A strategy may only see closed bars and completed days. Every new feature
   that feeds strategies needs a test proving the future can't change past decisions.
2. **Backtest = paper = live.** The single `Engine` runs all three. Any new engine feature (exits,
   protections, order types) must keep the "paper in small steps equals one backtest" test green.
3. **Additive.** Existing pages, strategies, saved results and settings keep working.
4. **Tested before shipped.** Each item lists acceptance criteria; the test suite must pass.
5. **Local = SQLite, live = PostgreSQL on a VPS, no Docker on the dev PC.**

---

## ✅ Done so far

| Area | What exists |
|---|---|
| Coins | Chain + contract identity, verified against Gate.io |
| Data | 1m candles for 60+ days: REST, trade-archive rebuild, 5m fallback, live WebSocket, gap back-fill |
| Daily levels | Daily high/low with the minute each was hit, PDH/PDL, cross-check vs Gate's 1d candle, Excel export |
| Services | Job queue, scheduler, worker, live stream, heartbeats, SQLite lock-safe |
| Pages | Coin page chart, **Market page** (chart + live order book + trades + day-by-day history), **Zones page** (forward-only zones, hotspots, heatmap, export) |
| Strategies | Plug-in API; PDH breakout, PDL sweep & reclaim, **Zone hotspot bounce/breakout**; per-strategy warm-up |
| Testing tools | Backtests with metrics, **parameter sweeps** with out-of-sample check and heatmap, **paper trading** on live bars |
| Phase 6 | Futures backtests (shorts, leverage, liquidation, real funding), mirrored short strategies, adaptive ML strategy, signal sharing between instances |
| Phase 5 | Order flow (buy/sell volume, delta, CVD, entry filter), signal + indicator plots and CSV, coin screener, portfolio backtests, Binance comparison |
| Phase 4 | Dry-run / live bots (order-book fills, real-time stops), order log with slippage, risk limits, kill switch, promotion gate, Telegram + webhooks, VPS kit |
| Phase 3.6 | Trailing / break-even / partial / ROI / time exits, smart search, walk-forward, Sortino / Calmar / combined ranking, limit (maker) orders, sizing modes and scaling in |
| Phase 3.5 | 1m intra-bar detail, look-ahead + warm-up checks (strategy page), protections, breakdowns + Excel export, 365-day history, data-quality report with repair |

---

## ✅ Phase 3.5: Backtest accuracy and safety

Make every result we already produce more trustworthy, before adding more strategies.

| # | Feature | Source | What it gives us | Done when | Effort |
|---|---|---|---|---|---|
| ✅ 3.5.1 | **Intra-bar detail backtesting** | freqtrade `--timeframe-detail` | When a 15m/1h bar hits both stop and target, replay its stored 1m candles to know which came first, instead of assuming "stop first". On by default; the result page states the detail interval used. | A test bar that hits target before stop at minute level is booked as a target; sweeps, paper and backtest stay identical. | S–M |
| ✅ 3.5.2 | **Automatic look-ahead check for every strategy** | freqtrade `lookahead-analysis` | A generic test (and a button on the strategy page) that reruns each signal with the future cut off and flags any decision that changes. Covers future custom strategies automatically. | Every registered strategy passes; a deliberately cheating test strategy is caught. | S |
| ✅ 3.5.3 | **Warm-up / recursive check** | freqtrade `recursive-analysis` | Shows whether a strategy's signals still change with more warm-up history, so `warmup_days` can be set correctly. | Report on the strategy page; zone strategy confirms `lookback + 1` is enough. | S |
| ✅ 3.5.4 | **Protections** | freqtrade protections | Engine-level guards, configurable per strategy and sweepable: *Stoploss guard* (pause after N stops in a window), *Max drawdown* (pause past X% drawdown), *Cooldown* (wait N bars after an exit), *Low-profit lock* (pause a coin whose recent trades lose). These are also the base of Phase 4's kill switch. | Each protection has a test; skipped entries are counted and shown with the reason. | M |
| ✅ 3.5.5 | **Backtest breakdowns** | freqtrade report tables | Results split by month / week / day, by exit reason and by entry reason ("tag"), plus rejected-signal counts. Shows *where* a strategy wins or loses. | New sections on backtest and paper pages; CSV/Excel export. | S |
| ✅ 3.5.6 | **Longer history** | our own finding | Sync 365 days (the archive supports it) so long-lookback strategies like zones get a fair sample; schedule keeps it rolling. | BTC has 365 days of 1m candles; zone sweep reruns with enough trades. | S |
| ✅ 3.5.7 | **Missing-candle report** | Cryptozonescanner gap report | Per-coin, per-day count of expected vs stored candles on the coin page, with a "repair" button that re-syncs gaps. | Report matches a test with injected gaps. | S |

## ✅ Phase 3.6: Strategy toolkit

| # | Feature | Source | What it gives us | Done when | Effort |
|---|---|---|---|---|---|
| ✅ 3.6.1 | **More exit types** | freqtrade stoploss / callbacks | Trailing stop (with activation offset), break-even after +N R, time-based exit (N bars), partial take-profit, and a profit-ladder ("ROI table") exit. Available to every strategy as risk settings, so sweeps can vary them. | Each exit tested on synthetic bars; paper = backtest still holds. | M |
| ✅ 3.6.2 | **Smart optimiser** | freqtrade hyperopt (idea; our own random + refine search, no new dependency) | Next to the grid sweep: a guided search for large spaces (hundreds of settings) that finds good regions in far fewer runs, still with the in-/out-of-sample split. | Finds the known optimum of a test strategy in far fewer runs than the grid. | M |
| ✅ 3.6.3 | **More ranking goals** | freqtrade loss functions | Rank sweeps by Sortino, Calmar, profit ÷ drawdown, daily Sharpe, or a combined score, plus a minimum-trades rule (exists). | New choices in the sweep form, each unit-tested. | S |
| ✅ 3.6.4 | **Walk-forward testing** | standard practice (extends our OOS split) | Repeated train → test windows rolled through the history, so a setting must keep working period after period. | Walk-forward report per sweep with per-window results. | M |
| ✅ 3.6.5 | **Limit (maker) orders** | freqtrade order types | Entries/exits as limit orders that fill only if price trades through, paying the lower maker fee; unfilled orders expire. Our sweeps showed fees are what removes the edge. | Fill rules tested; fee impact visible in a sweep (market vs limit). | M |
| ✅ 3.6.6 | **Position sizing modes and scaling in** | freqtrade stake / position adjustment (DCA) | Fixed amount, % of equity, risk-based (exists), and optional adding to a position at better prices, all with hard caps. | Each mode tested; caps can't be exceeded. | M |

## ✅ Phase 4: Live trading (built; running in dry-run)

| # | Feature | Source | What it gives us | Done when | Effort |
|---|---|---|---|---|---|
| ✅ 4.1 | **Order execution** | original plan | Gate.io spot orders through `ExchangeClient` (trade-only, IP-whitelisted key, never withdrawal rights); the same Engine drives it. | Dry-run first; orders reconciled with exchange fills. | L |
| ✅ 4.2 | **Risk limits and kill switch** | original plan + protections (3.5.4) | Account-level max daily loss, max open exposure, max orders per hour; one click (or one command) stops all trading and cancels open orders. | Each limit tested; kill switch works from the web UI and Telegram. | M |
| ✅ 4.3 | **Telegram bot: alerts and control** | freqtrade Telegram | Alerts on entries, exits, errors and service outages; commands `/status`, `/profit`, `/daily`, `/balance`, `/stop`, `/start`, `/forceexit`; only your own chat ID is accepted. | Commands tested against a fake bot API. | M |
| ✅ 4.4 | **Webhooks** | freqtrade webhooks | The same events sent to any URL (Discord, Slack, your own service). | Configurable per event; retries on failure. | S |
| ✅ 4.5 | **Dry-run → live promotion** | freqtrade dry-run | A strategy runs in paper mode for a set period and must meet its backtest expectations before it can be switched to live. | Promotion gate enforced in the UI. | S |
| ✅ 4.6 | **VPS deployment** | our plan | Ubuntu VPS: PostgreSQL, systemd services (web, worker, stream), Caddy with TLS, daily DB backups, no Docker needed. | Runbook in the README; restore from backup tested. | M |

**Phase 4 status notes (honest):** dry-run bots are running and fully tested. The live Gate.io order
path (signed REST, market orders) is built and unit-tested against a fake exchange, but has **not yet
sent a real order**; it stays locked until LIVE_TRADING=true, API keys and the Control-page switch are all
set, and a config passes its promotion checklist. The VPS kit (deploy/) is written but not yet run on a
server. Telegram and webhooks work as soon as their tokens/URLs are set.

## ✅ Phase 5: More data, more markets

| # | Feature | Source | What it gives us | Done when | Effort |
|---|---|---|---|---|---|
| ✅ 5.1 | **Order-flow data** | freqtrade orderflow | From the trades we already download: buy vs sell volume and delta per bar, cumulative delta (CVD), footprint (volume per price level), imbalances. Shown on the chart and available to strategies. | Values match a hand-computed sample; no look-ahead. | M–L |
| ✅ 5.2 | **Multiple coins per strategy (portfolio)** | freqtrade multi-pair bot | One strategy runs over a list of coins with shared cash and a max number of open trades. | Portfolio backtest = sum of its trades; paper = backtest holds. | L |
| ✅ 5.3 | **Coin filters ("pair lists")** | freqtrade pairlists | Pick coins automatically by volume, spread, volatility, price, age, recent performance; blacklist. Coins still verified by contract. | Filters tested on fixture data. | M |
| ✅ 5.4 | **Other exchanges, read-only data** | Cryptozonescanner / freqtrade (CCXT) | Candles from other exchanges for analysis and comparison. Coin identity still comes from contract addresses, so these coins must map to a verified coin. | One extra exchange working end to end. | M |
| ✅ 5.5 | **Plotting and analysis exports** | freqtrade plotting | Strategy indicators and signals drawn on the chart in backtest/paper pages; CSV export of every signal. | Visible on backtest pages. | S–M |

**Phase 5 status notes (honest):** order flow = taker buy / sell volume, delta and CVD per candle
(from the trade archives and the live trade feed; Gate's archive side code verified against the REST API).
A per-price-level **footprint is not built yet**: it needs raw trades stored per bar, which we don't keep.
Portfolio runs are backtests only (no multi-coin paper/bot sessions yet). The age filter of the screener
is not available (Gate gives no reliable listing date). Other exchanges: Binance, read-only, matched by
ticker and checked by price agreement.

## ✅ Phase 6: Advanced

| # | Feature | Source | What it gives us | Done when | Effort |
|---|---|---|---|---|---|
| ✅ 6.1 | **Futures, shorts and leverage** | freqtrade futures | Gate.io futures: short strategies, leverage limits, funding fees and liquidation price in backtests. | Short versions of the PDH/PDL/zone strategies backtested with funding. | L |
| ✅ 6.2 | **Adaptive machine-learning models** | freqtrade FreqAI | Models retrained on a rolling window that feed predictions into strategies, with the same no-look-ahead guarantees. Needs the longer history from 3.5.6 first. | A model strategy that passes walk-forward (3.6.4). | L |
| ✅ 6.3 | **Signal sharing between instances** | freqtrade producer/consumer | One instance computes signals or indicators and others subscribe, for when we run several bots or VPSs. | Two local instances exchanging signals. | M |

---

**Phase 6 status notes (honest):** futures/shorts/leverage/liquidation/funding work in backtests, sweeps,
portfolios and paper trading (perpetual priced from spot candles, real Gate funding for the last 180 days).
Bots stay spot-only: futures order execution is not built. Every long strategy has a mirrored short twin.
The ML strategy is built and proven free of look-ahead, but on real BTC data it loses badly with its
defaults (300 days, 1h: 424 trades, -84%): treat it as a framework to research with, not a strategy.
Signal sharing works between instances (token API, push or poll); follow bots copy spot buys and sells.

---

## Suggested order

1. **Phase 3.5** in order: 3.5.1 → 3.5.2 → 3.5.4, then 3.5.5–3.5.7 (3.5.3 alongside 3.5.2).
2. **Phase 3.6**: 3.6.3 and 3.6.1 first (cheap, strengthen every strategy), then 3.6.5, 3.6.2, 3.6.4, 3.6.6.
3. **Phase 4** once at least one strategy passes walk-forward and a paper period.
4. **Phases 5 and 6** in the order listed.
