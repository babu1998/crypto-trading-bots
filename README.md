# Binance Futures EMA-200 Trading Bot

An automated LONG-only trading system for Binance USDT-M perpetual futures. The system identifies momentum coins, detects high-quality EMA-200 pullback entries across three timeframes, executes trades with Fibonacci take-profits and a structural stop-loss, and manages risk through a full overtrading prevention layer.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How It Works](#how-it-works)
  - [Bot 1 — Watchlist Builder](#bot-1--watchlist-builder)
  - [Bot 2 — Signal Detector](#bot-2--signal-detector)
  - [Bot 3 — Order Executor](#bot-3--order-executor)
  - [Bot 4 — Backtester](#bot-4--backtester)
- [Entry Logic in Detail](#entry-logic-in-detail)
- [Stop-Loss Logic in Detail](#stop-loss-logic-in-detail)
- [Risk Management](#risk-management)
- [Overtrading Prevention](#overtrading-prevention)
- [Configuration Reference](#configuration-reference)
- [Setup](#setup)
- [Running](#running)
- [Disclaimer](#disclaimer)

---

## Overview

The strategy is built on a single core idea: **buy pullbacks to the 200-period EMA on trending coins**. The EMA-200 acts as a dynamic support level. When a coin that has been trending upward pulls back to touch this level, and all three timeframes confirm the trend is still intact, the bot enters long with a pre-defined risk/reward structure.

**Key design decisions:**

- **LONG only** — captures momentum in trending markets; no short logic.
- **Three timeframes** — 15m (trend), 3m (filter), 1m (trigger) — reduces false signals.
- **ATR-scaled deviation filter** — only enters when price made a meaningful move before the pullback, scaled to each coin's actual volatility rather than a fixed percentage.
- **Structural stop-loss** — placed below the swing low of the pullback, not a mechanical % below EMA.
- **Fibonacci take-profits** — three partial exits at 1.618R, 2.618R, and 4.236R with the SL trailing up after each TP hit.
- **R-multiple accounting** — all P&L tracked in R (1R = initial SL distance), making performance independent of position size.

---

## Architecture

```
main.py  (orchestrator — runs every SCAN_INTERVAL_SECONDS)
  │
  ├─ Bot 1  bot1_gainers.py   ← refreshed every GAINERS_REFRESH_MINUTES
  │         Scans all USDT-M perps; builds a watchlist of the top-N gainers
  │         from today's rolling 24h AND yesterday's daily candle. Union of both.
  │
  ├─ Bot 2  bot2_signals.py   ← scanned every cycle against the watchlist
  │         15m trend filter → 3m momentum filter → 1m trigger (CROSS or BOUNCE)
  │         ATR deviation check, slope filter, volume filter, 3m EMA touch skip
  │         Holds per-symbol cooldown state; receives exit feedback from Bot 3
  │
  ├─ Bot 3  bot3_executor.py  ← executes every confirmed signal
  │         Places MARKET entry + STOP_MARKET SL + three TAKE_PROFIT_MARKET orders
  │         Polls open orders each cycle; trails SL after TP1 and TP2 fill
  │         EMA-close SL: force-closes on sustained closes below EMA
  │         Calls bot2.record_exit() on every close for cooldown feedback
  │
  └─ Bot 4  bot4_backtest.py  ← standalone; no live credentials needed
            Historical replay of the full Bot 2 + Bot 3 logic on 1m data
            Walk-forward, one position per symbol, look-ahead-safe
```

`utils.py` provides three shared helpers: `get_exchange()`, `fetch_ohlcv_df()` (returns a DataFrame with `ema200` pre-computed), and `setup_logging()`.

All tunable parameters live exclusively in `config.py`.

---

## How It Works

### Bot 1 — Watchlist Builder

**File:** `bot1_gainers.py`

Bot 1 answers the question: *which coins are worth watching right now?*

It builds its watchlist by combining two scans:

1. **Today's gainers** — fetches the rolling 24-hour ticker for all USDT-M perpetuals and picks the top-N by percentage change.
2. **Yesterday's gainers** — fetches the most recent completed daily candle for every liquid symbol (volume ≥ `MIN_VOLUME_USDT`) and picks the top-N by open→close percentage.

The final watchlist is the **union** of both sets (deduplicated). This ensures the bot covers both coins that are breaking out today and coins that had a strong day yesterday and may continue.

**Why momentum coins?** The EMA-200 pullback strategy works best on coins that are already trending. A coin trading flat around its EMA-200 will generate far more false signals than a coin whose EMA-200 is rising because the coin has been making higher highs. Bot 1 pre-filters to trending coins so Bot 2 is spending its API calls on the right universe.

**Refresh cadence:** The watchlist is rebuilt every `GAINERS_REFRESH_MINUTES` (default 60). Between refreshes the same watchlist is reused.

---

### Bot 2 — Signal Detector

**File:** `bot2_signals.py`

Bot 2 is the brain of the system. It scans every symbol on the watchlist and returns a `Signal` object only when a high-confidence EMA-200 pullback entry is confirmed. It applies seven independent filters in sequence; a signal is only issued when all seven pass.

#### Three-Timeframe Structure

| Timeframe | Role | Condition |
|-----------|------|-----------|
| 15m | Trend confirmation | `close > EMA200` — the macro trend is up |
| 3m | Momentum filter | `close > EMA200` AND the 3m bar is NOT itself touching its EMA200 |
| 1m | Trigger | CROSS or BOUNCE event fires |

#### Trigger Types

**CROSS** — The previous 1m candle closed at or below EMA-200, and the current 1m candle closes above it. Price was below the EMA and just reclaimed it — a regime change.

**BOUNCE** — The 1m candle's low dips into the EMA-200 zone (`low ≤ EMA × (1 + EMA_TOUCH_BUFFER_PCT)`) but the candle closes above the EMA. Price tested the EMA as support and was rejected upward.

#### Signal Filters (applied in order)

**1. Cooldown gate** — Checks the per-symbol state machine (see [Overtrading Prevention](#overtrading-prevention)). If the symbol is in cooldown, paused, or has hit the daily trade cap, the symbol is skipped entirely before any API calls are made.

**2. 15m + 3m trend** — Both higher timeframes must close above their own EMA-200. If the intermediate or macro trend is down, no entry.

**3. 3m EMA-200 touch skip** (`SKIP_3M_EMA_TOUCH`) — If the last completed 3m bar was itself a bounce or cross of the 3m EMA-200, the entry is skipped. When both the 1m and 3m are simultaneously at their respective EMA-200 levels, the price zone is contested and prone to choppy whipsaws.

**4. BOUNCE slope filter** — For BOUNCE triggers only: the 1m EMA-200 must have a positive slope over the last `EMA_SLOPE_LOOKBACK` bars. A flat or declining EMA means the coin is in a range, not a trend. Bounces in ranging markets have no edge.

**5. ATR deviation filter** — Confirms that price made a meaningful move before the pullback:
  - **BOUNCE**: the highest high in the last `MIN_DEVIATION_BARS` bars (before the current bar) must be at least `MIN_DEVIATION_ATR_MULT × ATR(14)` above the current EMA. Price must have actually been above the EMA by a real margin — not just hovering at it.
  - **CROSS**: the lowest low among bars that closed below the EMA in the lookback window must be at least `MIN_DEVIATION_ATR_MULT × ATR(14)` below the EMA. The dip must have been real, not a 1-tick noise touch.

  Using ATR(14) instead of a fixed percentage means the threshold automatically scales to each coin's volatility. A coin with a 2% daily range needs a proportionally larger deviation than a coin with a 0.5% range.

**6. Volume filter** — The trigger candle's volume must be at least `MIN_TRIGGER_VOLUME_RATIO × 20-bar average volume`. A candle that crosses the EMA on below-average volume has less conviction.

**7. Swing low** — Not a filter, but a computation: the lowest low of the lookback window is recorded as the structural `swing_low` and attached to the Signal. Bot 3 uses this to place the stop-loss below a real price structure, not a mechanical percentage.

#### Signal Output

When all filters pass, a `Signal` dataclass is returned containing:
- `symbol` — e.g. `BTC/USDT:USDT`
- `trigger` — `'CROSS'` or `'BOUNCE'`
- `entry_price` — the current 1m close
- `ema200_1m` — the current 1m EMA-200 value
- `swing_low` — the structural low of the pullback (used for SL placement)
- `tf_checks` — the full per-timeframe analysis dict (for logging/debugging)

#### Cooldown State Machine

Bot 2 maintains a per-symbol state dictionary that Bot 3 feeds back into via `record_exit(symbol, r_achieved)`. See [Overtrading Prevention](#overtrading-prevention) for full details.

---

### Bot 3 — Order Executor

**File:** `bot3_executor.py`

Bot 3 receives confirmed signals from Bot 2 and handles everything from order placement to position management to trade closure.

#### Entry

For each signal:
1. **Check capacity** — skips if the symbol already has an open position, or if `MAX_OPEN_POSITIONS` is already reached.
2. **Set leverage** — calls `set_leverage(LEVERAGE, symbol)` and `set_margin_mode('isolated', symbol)` before any order.
3. **Calculate SL price** — uses `signal.swing_low × (1 - SL_SWING_LOW_BUFFER)`. Falls back to `EMA × (1 - SL_BUFFER_PCT)` if swing_low is not available.
4. **Calculate position size** — `risk_usdt / sl_distance` where `risk_usdt = balance × RISK_PER_TRADE_PCT / 100`. This sizes the position so that if the hard SL is hit, exactly `RISK_PER_TRADE_PCT%` of the account is lost. Capped at `MAX_POSITION_NOTIONAL_PCT × balance × LEVERAGE`.
5. **Split quantity** — the total quantity is split into three parts per `FIB_TP_SPLITS` (33% / 33% / 34%).
6. **Place orders simultaneously:**
   - `MARKET buy` — entry fill at market
   - `STOP_MARKET sell` (closePosition: True) — hard stop at the structural SL price
   - Three `TAKE_PROFIT_MARKET sell` orders — partial exits at TP1, TP2, TP3

#### Take-Profit Levels

TP prices are computed as Fibonacci extensions of the risk leg:

```
sl_distance = entry - sl_price
TP1 = entry + sl_distance × 1.618   (+1.618R)
TP2 = entry + sl_distance × 2.618   (+2.618R)
TP3 = entry + sl_distance × 4.236   (+4.236R)
```

The full position at TP3 yields approximately **+2.838R** (weighted average across the three splits).

#### Trailing Stop-Loss

`manage_positions()` is called every scan cycle. It polls open orders for each tracked position:

- **TP1 fills** (TP1 order no longer in open orders) → cancel old STOP_MARKET, place new one at `entry_price` (break-even). The trade is now risk-free.
- **TP2 fills** → trail SL up to `tp1_price`. The minimum outcome is now +0.534R on the remaining position.

State transitions: `initial` → `entry` (BE) → `tp1`

#### EMA-Close Stop-Loss

In addition to the hard price SL, `manage_positions()` monitors bar closes relative to EMA-200 and can force-close the position before the hard SL is hit. Three methods, all active simultaneously and combinable:

| Method | Config flag | Behaviour |
|--------|-------------|-----------|
| Counter | always active | Exit if `SL_CONSEC_CLOSES` consecutive 1m closes are below EMA, OR `SL_TOTAL_CLOSES` closes in the last `SL_CLOSES_WINDOW` bars are below EMA |
| Dual-TF | `SL_DUAL_TF_ENABLED` | A 1m bar only counts toward the counter when the 3m is also bearish (3m close < 3m EMA-200). Filters 1m noise from brief spikes below EMA |
| Volume confirm | `SL_VOL_CONFIRM_ENABLED` | A bar only counts if its volume exceeds `SL_VOL_CONFIRM_RATIO × 20-bar average`. Ignores low-conviction dips |
| Time stop | `SL_TIME_STOP_BARS > 0` | Exits at market after N bars with no TP1 hit. Kills stalled trades and frees capital |

When the EMA-close SL fires, the bot cancels all open orders and places a market close.

#### Cooldown Feedback

When any position closes (detected by the symbol disappearing from `fetch_positions()`), Bot 3 approximates the R outcome from the trailing SL level reached and calls `signal_bot.record_exit(symbol, approx_r)`:

| SL level at close | Approx R |
|-------------------|----------|
| `initial` (hard SL hit) | −1.0R |
| `entry` (BE SL) | +0.534R |
| `tp1` (TP1 trail) | +1.948R |

This feedback loop allows Bot 2 to update the symbol's cooldown state immediately.

---

### Bot 4 — Backtester

**File:** `bot4_backtest.py`

Bot 4 is a standalone historical replay of the complete Bot 2 + Bot 3 logic. It requires no live API credentials (read-only market data only) and is the primary tool for validating parameter changes.

#### Steps

1. **Historical gainers** — identifies which symbols would have been on the Bot 1 watchlist at the start of the backtest window, using the daily candle from the day before.
2. **Fetch 1m data** — paginates `fetch_ohlcv(1m)` from `WARMUP_DAYS` before the backtest start to now. The 3-day warmup seeds the EMA-200 accurately before trading begins.
3. **Resample and align** — resamples to 3m and 15m, computes EMA-200 on each, then forward-fills back to the 1m index. The resampled index is shifted forward by one bar-length so each 1m bar only sees the last *completed* higher-TF bar. **No look-ahead.**
4. **ATR precomputation** — computes ATR(14) once for the full 1m series before the walk-forward loop.
5. **Walk-forward signal detection** — mirrors Bot 2's logic at each bar: 15m trend, 3m momentum, 3m touch skip, BOUNCE slope, ATR deviation, volume filter. Returns `(trigger, swing_low)`.
6. **Trade simulation** — mirrors Bot 3's logic: structural SL from swing_low, Fibonacci TPs, trailing SL, EMA-close SL counter (with dual-TF and volume confirmation), time stop.
7. **Per-symbol cooldown state** — mirrors Bot 2's overtrading prevention: consecutive loss cooldown, session R threshold, 5-candle re-entry gate, daily trade cap.

#### Look-Ahead Prevention

The 3m and 15m EMAs are computed on resampled OHLCV, then the resampled index is shifted forward by one bar length before being forward-filled onto the 1m index. This means at 1m bar `T`, the system only knows about the 3m bar that completed *before* `T` — it cannot see the 3m bar that contains `T`.

#### Output Metrics

```
Total trades     : N
Win rate         : W%  (wins / losses)

Outcome breakdown:
  ALL_TPS        : N trades  +R   (all three TPs hit)
  TP1_SL         : N trades  +R   (TP1 + TP2 hit, trailing SL hit)
  BE_SL          : N trades  +R   (TP1 hit, trailing SL at entry hit)
  EMA_SL         : N trades  −R   (EMA-close SL fired before TP1)
  SL             : N trades  −R   (hard stop hit)
  OPEN           : N trades       (position open at end of window)

Total R          : +/-R
Avg R / trade    : +/-R
Profit factor    : X.XX

Simple ROI       : +XX%   (total R × risk %)
Compound ROI     : +XX%   (sequential compounding)

Per-symbol breakdown
Trade log (chronological)
```

---

## Entry Logic in Detail

A valid entry requires **all of the following** to be true simultaneously:

```
15m close  > 15m EMA-200                    ← macro uptrend
3m  close  > 3m  EMA-200                    ← intermediate uptrend
3m  last bar NOT touching its EMA-200       ← 3m trend is well-established, not at risk
1m  CROSS  or BOUNCE fires                  ← price is returning to EMA support
1m  EMA slope > 0  (BOUNCE only)            ← EMA is rising, not flat/ranging
prior high − EMA  ≥ 1 × ATR(14)  (BOUNCE)  ← price genuinely departed from EMA before pullback
EMA − prior dip   ≥ 1 × ATR(14)  (CROSS)   ← the dip below EMA was real, not 1-tick noise
trigger candle volume ≥ 0.8 × 20-bar avg   ← entry has buying conviction
```

The structural stop-loss is set at:

```
SL = min(low of last 10 bars before entry) × (1 − 0.1%)
```

This places the SL below the actual swing low of the pullback — below price structure — rather than an arbitrary fixed distance from EMA.

---

## Stop-Loss Logic in Detail

Three independent exit mechanisms are active simultaneously after entry:

### 1. Hard Price Stop-Loss
A `STOP_MARKET` order placed immediately at entry below the structural swing low. This is the last-resort backstop. Execution is guaranteed but subject to market gap risk.

### 2. Trailing Stop-Loss
Bot 3 polls open orders every scan cycle:
- After **TP1 fills**: SL moves to entry price (break-even). The trade cannot lose money.
- After **TP2 fills**: SL moves to TP1 price. The minimum outcome is guaranteed positive.

### 3. EMA-Close Stop-Loss
Monitors 1m bar closes relative to EMA-200 and force-closes before the hard SL is reached. This captures exits at better prices when the trend is clearly reversing:

- **Consecutive closes**: exit if 3 consecutive 1m bars close below EMA-200
- **Window closes**: exit if 4 of the last 7 bars close below EMA-200 (whichever fires first)
- **Dual-TF filter** (default ON): a 1m close only counts when the 3m bar is also bearish. Prevents exits on brief 1m spikes below EMA that the 3m ignores.
- **Volume filter** (default OFF): only count breakdown bars with above-average volume.

The EMA-close SL is the primary exit for losing trades in trending markets — it exits earlier than the hard SL, reducing losses from ~1R to typically ~0.5-0.7R.

---

## Risk Management

### Position Sizing

Every trade risks exactly `RISK_PER_TRADE_PCT%` of free balance regardless of the SL distance:

```
sl_distance     = entry_price − sl_price
risk_usdt       = balance × (RISK_PER_TRADE_PCT / 100)
quantity        = risk_usdt / sl_distance
```

Hard cap: `quantity × entry_price ≤ MAX_POSITION_NOTIONAL_PCT × balance × LEVERAGE`

### R-Multiples

All performance is measured in R. 1R = initial SL distance in dollars. This makes returns comparable across different position sizes and account sizes.

| Outcome | R achieved |
|---------|-----------|
| Hard SL hit | −1.0R |
| EMA-close SL (typical) | −0.5R to −0.9R |
| Break-even SL | +0.534R |
| TP1 trail SL | +1.948R |
| All TPs hit | +2.838R |

### Leverage and Margin

- Leverage: `LEVERAGE` × (default 10×)
- Margin mode: isolated per symbol (set before each entry)
- Maximum concurrent positions: `MAX_OPEN_POSITIONS` (default 3)

---

## Overtrading Prevention

Bot 2 maintains a per-symbol state dictionary. Bot 3 calls `record_exit(symbol, r_achieved)` after every position close to keep this state current.

### Consecutive Loss Cooldown
After `CONSEC_LOSS_COOLDOWN_COUNT` consecutive losing trades on a symbol, the symbol is paused for `CONSEC_LOSS_COOLDOWN_MINUTES`. The consecutive loss counter resets after the cooldown expires or after any winning trade.

### Session R Threshold
If a symbol's cumulative R in the current session drops to or below `SYMBOL_SESSION_R_THRESHOLD` (default −2.0R), the symbol is paused for `SYMBOL_SESSION_PAUSE_HOURS`. The session R resets to 0 when the pause is applied.

### Minimum Re-Entry Gap
After any trade on a symbol closes, a minimum of `MIN_REENTRY_MINUTES` (live) or `MIN_REENTRY_CANDLES` (backtest) must pass before a new entry is allowed. Prevents re-entering immediately after a stop-out on the same price level.

### Daily Trade Cap
At most `MAX_TRADES_PER_SYMBOL_PER_DAY` trades per symbol per calendar day (UTC). Resets at midnight.

---

## Configuration Reference

All parameters are in `config.py`. No values are hardcoded elsewhere.

### Bot 1 — Watchlist

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TOP_N_GAINERS` | `10` | Number of top gainers to include per scan (today + yesterday, union) |
| `MIN_VOLUME_USDT` | `5_000_000` | Minimum 24h volume in USDT to be eligible |

### Signal Detection — Bot 2

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EMA_PERIOD` | `200` | EMA period, applied on all three timeframes |
| `TIMEFRAMES` | `['1m','3m','15m']` | Trigger / filter / trend timeframes |
| `CANDLES_LIMIT` | `250` | Candles fetched per API call (must be > EMA_PERIOD) |
| `EMA_TOUCH_BUFFER_PCT` | `0.002` | Zone width around EMA that qualifies as a "touch" (0.2%) |
| `MIN_TRIGGER_VOLUME_RATIO` | `0.8` | Trigger candle volume must be ≥ 80% of 20-bar average |
| `EMA_SLOPE_LOOKBACK` | `5` | Bars back to measure EMA slope direction (BOUNCE filter) |

### Entry Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ATR_PERIOD` | `14` | Period for Average True Range (Wilder smoothing) |
| `MIN_DEVIATION_BARS` | `10` | Lookback window to find prior price deviation from EMA |
| `MIN_DEVIATION_ATR_MULT` | `1.0` | Deviation must be ≥ N × ATR(14). Raise to demand larger prior swings |
| `SKIP_3M_EMA_TOUCH` | `True` | Skip entry if the last completed 3m bar was also touching its EMA-200 |

### Structural Stop-Loss

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SL_SWING_LOW_BARS` | `10` | Lookback bars to identify the swing low of the pullback |
| `SL_SWING_LOW_BUFFER` | `0.001` | SL placed 0.1% below the identified swing low |
| `SL_BUFFER_PCT` | `0.003` | Fallback SL: 0.3% below EMA (used when swing_low is unavailable) |

### Take-Profits and Risk

| Parameter | Default | Description |
|-----------|---------|-------------|
| `RISK_PER_TRADE_PCT` | `1.0` | % of free balance risked per trade |
| `LEVERAGE` | `10` | Leverage multiplier (isolated margin) |
| `MAX_OPEN_POSITIONS` | `3` | Maximum concurrent open positions |
| `MAX_POSITION_NOTIONAL_PCT` | `0.20` | Hard cap: notional ≤ 20% of equity × leverage |
| `FIB_TP_LEVELS` | `[1.618, 2.618, 4.236]` | Fibonacci extension multiples for TP1/TP2/TP3 |
| `FIB_TP_SPLITS` | `[0.33, 0.33, 0.34]` | Fraction of position closed at each TP level |

### EMA-Close Stop-Loss Counter

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SL_CONSEC_CLOSES` | `3` | Exit after N consecutive 1m closes below EMA-200 |
| `SL_TOTAL_CLOSES` | `4` | OR: exit if N bars in the window closed below EMA-200 |
| `SL_CLOSES_WINDOW` | `7` | Rolling window size for total-closes check |
| `SL_DUAL_TF_ENABLED` | `True` | Bar only counts when 3m is also bearish (filters 1m noise) |
| `SL_VOL_CONFIRM_ENABLED` | `False` | Bar only counts with above-average volume |
| `SL_VOL_CONFIRM_RATIO` | `1.2` | Volume threshold multiplier when vol confirm is enabled |
| `SL_TIME_STOP_BARS` | `0` | Exit after N bars with no TP1 hit (0 = disabled) |

### Overtrading Prevention

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CONSEC_LOSS_COOLDOWN_COUNT` | `3` | Consecutive losses before symbol cooldown |
| `CONSEC_LOSS_COOLDOWN_MINUTES` | `30` | Duration of consecutive-loss cooldown |
| `MIN_REENTRY_MINUTES` | `5` | Minimum minutes between entries on same symbol (live) |
| `MIN_REENTRY_CANDLES` | `5` | Minimum 1m bars between entries on same symbol (backtest) |
| `MAX_TRADES_PER_SYMBOL_PER_DAY` | `10` | Hard daily cap per symbol |
| `SYMBOL_SESSION_R_THRESHOLD` | `-2.0` | Pause symbol when session R drops to this level |
| `SYMBOL_SESSION_PAUSE_HOURS` | `4` | Hours to pause after session R threshold hit |

### Timing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SCAN_INTERVAL_SECONDS` | `60` | Seconds between full watchlist scans |
| `GAINERS_REFRESH_MINUTES` | `60` | Minutes between Bot 1 watchlist rebuilds |

---

## Setup

### Requirements

```bash
pip install ccxt pandas python-dotenv
```

Python 3.10+ required (uses `list[str]` and `dict[str, ...]` type hints).

### Environment

Create a `.env` file in the project root:

```
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
TESTNET=true
```

Set `TESTNET=false` only when you are ready to trade with real funds.

To obtain Binance Futures testnet credentials: https://testnet.binancefuture.com

### Verify Setup

```bash
python -c "import ccxt, pandas, dotenv; print('Dependencies OK')"
python -c "from utils import get_exchange; e = get_exchange(); print('Exchange OK:', e.id)"
```

---

## Running

```bash
# Full live trading loop (Bots 1 → 2 → 3 orchestrated)
python main.py

# Backtest only — last 7 days, no live credentials needed beyond read access
python bot4_backtest.py

# Run any bot standalone for debugging
python bot1_gainers.py
python bot2_signals.py
python bot3_executor.py
```

Stop the live bot cleanly with `Ctrl+C` or `SIGTERM` — it completes the current scan cycle before exiting.

### Syntax Check Before Running

```bash
for f in config.py utils.py bot1_gainers.py bot2_signals.py bot3_executor.py bot4_backtest.py main.py; do
    python -c "import ast; ast.parse(open('$f').read())" && echo "$f OK"
done
```

---

## Project Structure

```
.
├── main.py              # Orchestrator loop
├── bot1_gainers.py      # Watchlist builder (top gainers)
├── bot2_signals.py      # Signal detector (EMA-200 multi-TF)
├── bot3_executor.py     # Order executor and position manager
├── bot4_backtest.py     # Historical backtester
├── utils.py             # Shared helpers (exchange, OHLCV, logging)
├── config.py            # All tunable parameters
├── requirements.txt     # Python dependencies
└── .env                 # API credentials (not committed)
```

---

## Disclaimer

This software is provided for educational purposes. Futures trading involves significant risk of loss. Using leverage amplifies both gains and losses. Backtested results do not guarantee future performance. Always test on a testnet before deploying real capital. The authors accept no responsibility for financial losses incurred through the use of this software.
