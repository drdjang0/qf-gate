# QF-gate roadmap

The project plan. Every phase **adds** capability; nothing that works today is removed or
narrowed. Ideas taken from other projects are re-implemented in our own code.

- **freqtrade** is GPL-3 licensed: we adopt its *ideas* only and never copy its code.
- **Cryptozonescanner** gave us the zone-revisit idea, rebuilt here forward-only and to the minute.
- **TradingAgents** (Apache-2.0) gave us the multi-agent analyst / bull-bear debate idea (Phase 7). Its
  licence would allow reusing code with attribution; we still re-implement it around our own data.

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
| Phase 7 | **Intel** tab: news feed, macro / Polymarket panel, AI briefs (multi-agent, grounded, cost-capped), brief scorecard + memory, AI filter for paper / dry-run; Trade journal on result pages |
| Discovery | **Coin auto-fill** (chain, contract, icon, exchanges from the symbol), **Scanner** (any coin on 7 exchanges, 24h–90d zones / hotspots / heatmap, nothing stored), **multi-exchange Screener** (one exchange or all combined, venue shares, price-gap warning) |
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
ticker and checked by price agreement. Since then the Scanner and Screener read Gate.io, Binance, OKX, Bybit,
KuCoin, MEXC and Bitget (read-only, nothing stored; OKX is unreachable from the dev PC's network).

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

## ✅ Phase 7: AI analysis layer (ideas from TradingAgents)

Add context and readable reasoning **on top of** the existing rules-based platform. Nothing here replaces
a strategy, the engine or a check; every item is off until switched on.

**Phase 7 ground rules (in addition to the ones at the top):**
- **Advisory first.** An AI brief never places an order. It can only become a bot filter (7.7) after its
  scorecard (7.6) has shown, on decisions made live, that it helps.
- **No LLM in backtests or sweeps.** A language model was trained on data from after any historical date,
  so it can quietly "know" the future. No setting removes that, so LLM output is judged only on decisions
  made in real time and settled afterwards.
- **Point-in-time inputs.** News and sentiment are stored with the time we first saw them; nothing published
  later may reach an earlier decision (same look-ahead test as strategies).
- **Grounded numbers.** Prices, levels and stats in a brief come from our own data and are checked against it;
  the model writes reasoning, not numbers.
- **Costs capped.** Every LLM call is logged with tokens and cost, with a daily spend limit. Without an API key
  the whole phase stays hidden and the platform works exactly as today.

| # | Feature | Source | What it gives us | Done when | Effort |
|---|---|---|---|---|---|
| ✅ 7.1 | **Trade journal with reflection** | TradingAgents memory log | Every closed backtest / paper / bot trade is settled N bars after its exit: what price did next (missed profit, avoidable loss, best and worst excursion), grouped by strategy, entry tag and exit reason, with a plain-language "what worked / what didn't" summary. No LLM needed. | Settlement uses only bars after the exit; summary matches hand-computed sample; visible on backtest, paper and bot pages. | S–M |
| ✅ 7.2 | **News and sentiment feed** | TradingAgents news / sentiment analysts | Crypto headlines and social posts per coin (free sources first: RSS, Reddit, CoinGecko), stored with first-seen time, de-duplicated, shown on the Market and Scanner pages with a simple bullish / bearish / neutral count. | Items carry fetch time; a strategy-facing accessor returns only items seen before a bar's close (look-ahead test); feed survives a source being down. | M |
| ✅ 7.3 | **LLM provider layer** | TradingAgents llm_clients | One small interface for Anthropic, OpenAI and any OpenAI-compatible endpoint (incl. a local Ollama model, no cost), keys in `.env`, retries, response cache, per-call token / cost log and a daily spend cap. | Fake-provider tests; cap blocks calls once reached; nothing sent when no key is set. | S–M |
| ✅ 7.4 | **AI coin brief (multi-agent debate)** | TradingAgents analyst → researcher → trader → risk → manager graph | A **Brief** button on the Scanner and coin pages. Analysts read *our* data: technical (zones, hotspots, PDH/PDL, order flow, funding), market structure (where it trades, spreads, volume shares), news / sentiment (7.2) and macro (7.5). A bull and a bear researcher debate, three risk views (aggressive / neutral / conservative) challenge the plan, and a manager gives a 5-tier rating (Buy / Overweight / Hold / Underweight / Sell, or Review when unreadable) with reasoning and suggested entry / stop / target. Runs as a job; saved with all its inputs. | Every number in the brief is traceable to our data snapshot; unreadable output becomes Review, never Hold; runs through the job queue with progress. | L |
| ✅ 7.5 | **Macro and prediction-market panel** | TradingAgents FRED / Polymarket vendors | Context panel: US rates and dollar index (FRED, free key), crypto-related Polymarket odds, BTC dominance. Information only; also an input to 7.4. | Values dated and cached; panel degrades gracefully without keys. | S |
| ✅ 7.6 | **Brief scorecard and memory** | TradingAgents decision-quality backtest + reflection | Each saved brief is settled later against the coin's realised return and against BTC (alpha), grouped by rating, so we learn whether the briefs are any good. The latest settled briefs and lessons for the same coin are fed into its next brief. Only briefs made live count. | Scorecard per rating and per coin; settlement uses only prices after the brief; memory injection tested. | M |
| ✅ 7.7 | **AI filter for paper / dry-run bots (optional)** | our idea | A strategy option "only enter when the latest brief for this coin is at least X and no older than N hours". Paper and dry-run only, clearly marked as not backtestable. | Only offered once 7.6 shows a positive edge over enough briefs; blocked entries counted with reason "AI filter". | S |
| ✅ 7.8 | **Untrusted outside text** | Anthropic financial-services agents (untrusted-document guardrail) | Headlines and prediction-market questions reach the AI only inside a fenced, length-capped data block that every agent is told never to obey. Numbers inside that block don't count as "verified" in the grounding check. | A headline containing instructions is fenced and neutralised; a number seen only in a headline is still flagged unverified. | S |
| ✅ 7.9 | **Falsifiable thesis + invalidation** | Anthropic thesis-tracker skill | Every brief states a one-line thesis, up to 4 pillars and an **invalidation price** (where the call is proven wrong). The worker watches open briefs and records when the invalidation level is hit; the scorecard reports it. A **Re-check thesis** button asks the AI (one call) whether each pillar still holds on fresh data. | Invalidation on the wrong side of the price is dropped; hits use only 1h candles after the brief and inside its horizon; re-check stored on the brief. | M |
| ✅ 7.10 | **Bull / base / bear scenarios** | Anthropic earnings-preview skill | Each brief ends with three scenarios (trigger, price level, rough probability). The fact sheet adds the coin's typical daily move so scenarios can be judged against what is normal. | Scenario levels outside the observed range are dropped; probabilities that don't add up to ~100% are flagged. | S |
| ✅ 7.11 | **Daily morning note** | Anthropic morning-note skill | One AI call a day over your active coins: top call, what changed overnight, what is on today's macro / prediction-market calendar, ideas (each with what would make it wrong), and a review of yesterday's settled calls. "Nothing material" is a valid note. | Runs once a day from the worker (INTEL_NOTE_HOUR_UTC) or on a button; built from stored data only; numbers checked like a brief. | M |
| ✅ 7.12 | **Hit rate by idea source** | Anthropic idea-generation skill ("track which screens produce the best ideas") | Each brief records where it was started from (Intel page, Market, coin page, Scanner, Screener → Scanner, morning note); the scorecard shows edge and hit rate per source. | Source stored on every new brief; scorecard table per source. | S |
| ✅ 7.13 | **Independent AI critic (optional, off by default)** | Anthropic critic / auditor leaf workers | One extra call re-reads the final answer against the fact sheet and lists unsupported claims. If it objects, the brief's confidence is set to low and the objections are shown. | Off unless INTEL_CRITIC=true; objections shown on the brief; rating never changed by the critic. | S |

**Phase 7 status notes (honest):**
- **7.1 Trade journal:** a card on every backtest, paper and bot page. On the existing PDL-reclaim backtests it
  found 42–50% of stop-outs came back past the entry within 12 bars.
- **7.2 News:** CoinDesk, Cointelegraph and Decrypt RSS work (94 items on the first fetch). Reddit refuses anonymous
  apps (HTTP 403); that is shown and skipped. Sentiment is a word count, not a model. Items are point-in-time by
  first-seen time.
- **7.3 LLM layer:** Anthropic via the official SDK (default `claude-opus-5-5`, refusal fallback on), OpenAI,
  any OpenAI-compatible server, or a local Ollama model. It has a daily spend cap, a daily call cap, a response
  cache and a log of every call. It stays off until `INTEL_LLM_PROVIDER` is set.
- **7.4 AI brief:** 11 calls per brief (4 analysts, bull, bear, research manager, 3 risk views, portfolio manager)
  over a fact sheet built from our own data. It ran end to end on real BTC data with a stand-in model, an estimated
  ~$0.20 per brief on Opus 5.5. It has **not yet run against a real model**: no API key is set on this PC.
- **7.5 Macro panel:** live (FRED rates and dollar index, CoinGecko dominance, Polymarket crypto odds).
- **7.6 Scorecard:** briefs settle after 24 h against the coin's and BTC's price. There are no settled briefs yet.
- **7.7 AI filter:** paper and dry-run only. It stays inactive until 20 settled directional briefs show a positive
  edge and a hit rate above 50%.
- **7.8 – 7.13 (ideas from Anthropic's financial-services agent templates, Apache-2.0; ideas only, our own code
  and prompts):** headlines and Polymarket questions now sit in a fenced UNTRUSTED block and never count as
  verified numbers; every brief carries a thesis, pillars, an invalidation level and bull / base / bear scenarios
  (all checked); the worker records when an invalidation level is hit; a one-call thesis re-check; a daily morning
  note (one call, stored data only); scorecard by idea source; an optional critic (`INTEL_CRITIC`, off). Tested with
  a stand-in model (212 tests pass); the morning-note and brief fact sheets were built from live data. Like the
  rest of Phase 7, **not yet run against a real model** (no API key on this PC).

**Not adopted:** TradingAgents' stock-specific parts (SEC fundamentals, insider trades, StockTwits) and
Yahoo daily prices: our 1-minute Gate.io data is better for crypto. From Anthropic's templates: the stock-market
workflows (DCF / LBO / 3-statement models, earnings calls, pitch decks, KYC, ledger reconciliation), their paid data
connectors and the cloud Managed Agents deployment (our worker already runs briefs locally).

---

## ✅ Phase 8: Cascade orders (from the user's own gate-cascade-bot)

The buy-ladder strategy from the earlier Node.js project, rebuilt in Python as its own **Cascades** section. It
can't be a normal strategy plug-in: the engine holds one averaged position with one stop / target, while a cascade
gives every level its own buy and its own take-profit sell.

| # | Item | Status |
|---|---|---|
| 8.1 | Level math: % steps (price down, amount up, take-profit ladder) or a manual template (buy, amount, sell per line); skip-ahead with the last-level rule (`cascades/ladder.py`) | ✅ |
| 8.2 | Backtest on stored candles with the old project's rules: decision prices never from the future, no buy and sell of one level in the same bar, the next level decided on the fill bar's low, fees off what is received | ✅ |
| 8.3 | Dry-run engine: one buy resting at a time, fills on the live price **and** stored 1-minute candles since the order was placed (wicks between checks count), worker every 30 s + live-stream ticks, compare-and-set so two processes never apply a fill twice, kill switch stops new buys only | ✅ |
| 8.4 | Pages: list, create with preview, detail with hold / resume / cancel / per-level edit and withdraw, event log, backtest list and report, "start as dry-run" from a backtest | ✅ |
| 8.5 | Live mode: real Gate.io limit orders (and native trigger orders) behind the same three locks as live bots | ⏳ after a dry-run period |

**Kept from the old project:** at most one buy level rests at a time (the crash-safety rule); a take-profit sell never
gates the next level; the misplaced-decimal check on level 1 (0.2x to 3x the market). **Changed:** the old dry-run never
filled; ours fills on real prices. Backtests read our own stored candles instead of fetching from Gate (no 6000-candle cap).
**Not adopted:** the old multi-user accounts and per-user API keys (QF-gate is single-user) and its TradingView panel.

---

## Suggested order

1. **Phase 3.5** in order: 3.5.1 → 3.5.2 → 3.5.4, then 3.5.5–3.5.7 (3.5.3 alongside 3.5.2).
2. **Phase 3.6**: 3.6.3 and 3.6.1 first (cheap, strengthen every strategy), then 3.6.5, 3.6.2, 3.6.4, 3.6.6.
3. **Phase 4** once at least one strategy passes walk-forward and a paper period.
4. **Phases 5 and 6** in the order listed.
5. **Phase 7**: 7.1 first (no cost, useful on its own), then 7.2 and 7.3, then 7.4 with 7.5, then 7.6.
   7.7 only if the 7.6 scorecard earns it.
6. **Phase 8**: 8.5 (live cascades) only after a cascade has run in dry-run for a while and its fills look right.
