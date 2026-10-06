# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Start the signal bot
python discord_signal_bot.py

# Syntax check
python -c "import ast; ast.parse(open('discord_signal_bot.py').read()) or print('OK')"

# Verify public API connectivity (no keys needed)
python -c "import ccxt; e = ccxt.binanceusdm({'enableRateLimit': True}); e.load_markets(); print('OK')"
```

## Environment

Only one `.env` key is required:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/TOKEN
```

No Binance API keys — the bot uses public endpoints only (`fetch_tickers`, `fetch_ohlcv`).

Dependencies are inherited from the parent repo's `requirements.txt` (`ccxt`, `pandas`, `python-dotenv`). `requests` comes in as a ccxt transitive dependency.

## Architecture

Single file: `discord_signal_bot.py`. All tunable parameters are module-level constants at the top — no separate config file.

**Main loop cadence** (`main()`):
1. Every `GAINERS_REFRESH_MINUTES` (15 min) — re-rank top 20 USDT-M perps by 24h % change via `get_top_gainers()`.
2. Every `SCAN_INTERVAL_SECONDS` (60 s) — iterate gainers and call `fetch_df()` + `check_bounce()` for each assigned timeframe.
3. `cooldowns` dict (`(symbol, tf) → float`) enforces `SIGNAL_COOLDOWN_MINUTES` (15 min) between alerts on the same (symbol, timeframe) pair.

**Tier assignment** (rank → timeframes scanned):
| Rank | Timeframes |
|------|------------|
| 1–3 | `1m` |
| 4–10 | `3m`, `5m` |
| 11–20 | `15m`, `30m` |

**Signal logic** (`check_bounce()`): uses the last *completed* candle (`df.iloc[-2]`). Four conditions must all pass:
- `low ≤ EMA200 × (1 + EMA_TOUCH_BUFFER_PCT)` — touched the EMA
- `close > EMA200` — bounced above
- `EMA200[now] > EMA200[N bars ago]` — EMA is rising (uptrend)
- `volume ≥ MIN_TRIGGER_VOLUME_RATIO × 20-bar average` — real buying pressure
- Lower wick must exist (price genuinely rejected below EMA)

SL is placed below the swing low of the last `SL_SWING_LOW_BARS` bars (fallback: `EMA × (1 − SL_BUFFER_PCT)`). TPs are Fibonacci extensions from entry using SL distance as 1R.

**Discord delivery** (`send_discord()`): posts a single rich embed per signal. Tier colours: Gold (top 3), Sky-blue (top 10), Purple (top 20).

## Key invariants

- `CANDLES_LIMIT` (260) must stay above `EMA_PERIOD` (200) + warmup bars or EMA will be seeded on too few bars.
- `df.iloc[-2]` is always the last *closed* candle; `df.iloc[-1]` is the live in-progress bar and is never used for signals.
- `get_top_gainers()` filters symbols to those ending in `/USDT:USDT` (ccxt USDM perp format) and drops any with volume below `MIN_VOLUME_USDT`.
