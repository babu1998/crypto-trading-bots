"""
EMA-200 Discord Signal Bot — Binance Futures Top 20 Gainers
------------------------------------------------------------
Uses Binance public API only — no API key required.

Tier assignment (by 24h rank):
  Rank  1–3   →  1m  EMA-200 bounce
  Rank  4–10  →  3m + 5m EMA-200 bounce
  Rank 11–20  →  15m + 30m EMA-200 bounce

Cooldown: one alert per (symbol, timeframe) per SIGNAL_COOLDOWN_MINUTES.

.env keys required:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
"""

import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
import ccxt

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL      = os.getenv('DISCORD_WEBHOOK_URL', '')

TOP_N                    = 20
MIN_VOLUME_USDT          = 5_000_000    # skip coins with <$5M 24h volume
SCAN_INTERVAL_SECONDS    = 60
GAINERS_REFRESH_MINUTES  = 15           # re-rank top gainers every N min
SIGNAL_COOLDOWN_MINUTES  = 15           # one alert per (symbol, tf) per N min

EMA_PERIOD               = 200
EMA_TOUCH_BUFFER_PCT     = 0.002        # low must be within 0.2% of EMA to count as touch
EMA_SLOPE_LOOKBACK       = 5            # bars back to confirm EMA is rising
MIN_TRIGGER_VOLUME_RATIO = 0.8          # trigger candle volume ≥ 80% of 20-bar average
CANDLES_LIMIT            = 260          # must exceed EMA_PERIOD + warmup bars

SL_SWING_LOW_BARS        = 10           # bars to scan for the pullback swing low
SL_BUFFER_PCT            = 0.003        # fallback SL: 0.3% below EMA when no swing low
FIB_TP_LEVELS            = [1.618, 2.618, 4.236]

# Tier timeframe assignments
TIER_TF = {
    1: ['1m'],
    2: ['3m', '5m'],
    3: ['15m', '30m'],
}
TIER_LABEL = {
    1: 'TOP 3  ·  1m',
    2: 'TOP 10  ·  3m / 5m',
    3: 'TOP 20  ·  15m / 30m',
}
TIER_COLOR = {
    1: 0xFFD700,   # Gold
    2: 0x00BFFF,   # Sky-blue
    3: 0x9B59B6,   # Purple
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('EMA200-Discord')


# ── Exchange ───────────────────────────────────────────────────────────────────
def get_exchange() -> ccxt.binanceusdm:
    return ccxt.binanceusdm({
        'options':         {'defaultType': 'future'},
        'enableRateLimit': True,
    })


# ── Gainers ────────────────────────────────────────────────────────────────────
def get_top_gainers(exchange: ccxt.binanceusdm, n: int) -> list[dict]:
    """Return top-N 24h % gainers from USDT-M perps, volume-filtered."""
    tickers = exchange.fetch_tickers()
    rows = []
    for symbol, t in tickers.items():
        if not symbol.endswith('/USDT:USDT'):
            continue
        pct = t.get('percentage') or 0.0
        vol = t.get('quoteVolume') or 0.0
        if vol < MIN_VOLUME_USDT or pct <= 0:
            continue
        rows.append({
            'symbol':     symbol,
            'change_pct': round(pct, 3),
            'volume_usdt': round(vol),
            'price':      t.get('last', 0),
        })

    rows.sort(key=lambda x: x['change_pct'], reverse=True)
    for i, r in enumerate(rows[:n], 1):
        r['rank'] = i
    return rows[:n]


# ── OHLCV helpers ──────────────────────────────────────────────────────────────
def fetch_df(exchange: ccxt.binanceusdm, symbol: str, tf: str) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(symbol, tf, limit=CANDLES_LIMIT)
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    df['ema200']   = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    return df


# ── Signal detection ───────────────────────────────────────────────────────────
def check_bounce(df: pd.DataFrame) -> dict | None:
    """
    EMA-200 BOUNCE on the last completed candle (-2 index):
      1. Low ≤ EMA × (1 + touch_buffer)  — touched or pierced the EMA
      2. Close > EMA                      — recovered above EMA
      3. EMA slope positive over N bars   — uptrend confirmation
      4. Volume ≥ ratio × 20-bar average  — real buying pressure, not noise
    Returns a signal dict or None.
    """
    min_bars = EMA_PERIOD + EMA_SLOPE_LOOKBACK + 5
    if len(df) < min_bars:
        return None

    bar  = df.iloc[-2]
    prev = df.iloc[-2 - EMA_SLOPE_LOOKBACK]
    ema  = bar['ema200']

    if bar['low'] > ema * (1 + EMA_TOUCH_BUFFER_PCT):
        return None                    # price never touched EMA

    if bar['close'] <= ema:
        return None                    # closed below — not a bounce

    if ema <= prev['ema200']:
        return None                    # EMA flat or declining — skip ranging market

    vol_avg = bar['vol_ma20']
    if pd.notna(vol_avg) and bar['volume'] < MIN_TRIGGER_VOLUME_RATIO * vol_avg:
        return None                    # weak volume

    # Lower wick confirmation — wick below EMA should be visible
    body_top = max(bar['open'], bar['close'])
    lower_wick = body_top - bar['low']
    if lower_wick <= 0:
        return None

    # Structural SL: swing low of the pullback
    lookback  = df.iloc[-2 - SL_SWING_LOW_BARS : -2]
    swing_low = lookback['low'].min() if not lookback.empty else 0.0
    sl_price  = (swing_low * 0.999) if swing_low > 0 else (ema * (1 - SL_BUFFER_PCT))

    entry   = bar['close']
    sl_dist = entry - sl_price
    if sl_dist <= 0:
        return None

    tps = [round(entry + sl_dist * f, 8) for f in FIB_TP_LEVELS]
    rr  = round((tps[0] - entry) / sl_dist, 2)

    return {
        'signal':   'BOUNCE',
        'entry':    round(entry, 8),
        'ema200':   round(ema, 8),
        'sl':       round(sl_price, 8),
        'tp1':      tps[0],
        'tp2':      tps[1],
        'tp3':      tps[2],
        'rr':       rr,
        'volume':   int(bar['volume']),
        'vol_avg':  int(vol_avg) if pd.notna(vol_avg) else 0,
        'wick_pct': round(lower_wick / ema * 100, 3),
    }


# ── Discord sender ─────────────────────────────────────────────────────────────
def send_discord(symbol: str, rank: int, tier: int, tf: str,
                 change_pct: float, sig: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping alert.")
        return

    base  = symbol.replace('/USDT:USDT', '')
    color = TIER_COLOR.get(tier, 0x95A5A6)
    now   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    embed = {
        'title':       f'\U0001f4c8 EMA-200 BOUNCE  {base}  [{tf}]',
        'color':       color,
        'description': (
            f"**Rank #{rank}** · {TIER_LABEL[tier]}\n"
            f"24h Change: **{change_pct:+.2f}%**  |  Lower wick: {sig['wick_pct']}% of EMA"
        ),
        'fields': [
            {'name': '\U0001f3af Entry',          'value': f"`{sig['entry']}`",              'inline': True},
            {'name': '\U0001f4ca EMA-200',         'value': f"`{sig['ema200']}`",             'inline': True},
            {'name': '\U0001f6d1 Stop Loss',       'value': f"`{sig['sl']}`",                 'inline': True},
            {'name': '✅ TP1  (1.618R)',       'value': f"`{sig['tp1']}`",                'inline': True},
            {'name': '✅ TP2  (2.618R)',       'value': f"`{sig['tp2']}`",                'inline': True},
            {'name': '✅ TP3  (4.236R)',       'value': f"`{sig['tp3']}`",                'inline': True},
            {'name': '⚡ R:R to TP1',          'value': f"`1 : {sig['rr']}`",             'inline': True},
            {'name': '\U0001f4e6 Volume / Avg-20', 'value': f"`{sig['volume']:,}` / `{sig['vol_avg']:,}`", 'inline': True},
        ],
        'footer':    {'text': f'EMA-200 Bot  ·  {now}'},
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={'embeds': [embed]},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            log.info('Discord alert sent: %s [%s]', symbol, tf)
        else:
            log.warning('Discord returned %s: %s', resp.status_code, resp.text[:300])
    except Exception as exc:
        log.error('Discord request failed: %s', exc)


# ── Tier helper ────────────────────────────────────────────────────────────────
def tier_for_rank(rank: int) -> int:
    if rank <= 3:
        return 1
    if rank <= 10:
        return 2
    return 3


# ── Main loop ──────────────────────────────────────────────────────────────────
def main() -> None:
    log.info('Starting EMA-200 Discord Signal Bot')
    if not DISCORD_WEBHOOK_URL:
        log.warning('DISCORD_WEBHOOK_URL is not set — alerts will be logged only.')

    exchange = get_exchange()

    gainers:              list[dict]               = []
    last_gainers_ts:      float                    = 0.0
    cooldowns:            dict[tuple, float]       = {}   # (symbol, tf) → last alert time

    while True:
        now = time.time()

        # Refresh top-20 gainers list
        if now - last_gainers_ts >= GAINERS_REFRESH_MINUTES * 60:
            try:
                gainers = get_top_gainers(exchange, TOP_N)
                last_gainers_ts = now
                summary = ', '.join(f"#{r['rank']} {r['symbol'].replace('/USDT:USDT','')} ({r['change_pct']:+.1f}%)"
                                    for r in gainers[:5])
                log.info('Gainers refreshed — top 5: %s', summary)
            except Exception as exc:
                log.error('Failed to fetch gainers: %s', exc)

        # Scan each gainer for its assigned timeframes
        signals_this_cycle = 0
        for item in gainers:
            symbol     = item['symbol']
            rank       = item['rank']
            change_pct = item['change_pct']
            tier       = tier_for_rank(rank)
            tfs        = TIER_TF[tier]

            for tf in tfs:
                key       = (symbol, tf)
                last_sent = cooldowns.get(key, 0.0)
                if now - last_sent < SIGNAL_COOLDOWN_MINUTES * 60:
                    continue  # cooldown active — skip

                try:
                    df  = fetch_df(exchange, symbol, tf)
                    sig = check_bounce(df) if not df.empty else None
                    if sig:
                        log.info('SIGNAL  rank=#%d  %s  [%s]  entry=%s', rank, symbol, tf, sig['entry'])
                        send_discord(symbol, rank, tier, tf, change_pct, sig)
                        cooldowns[key] = now
                        signals_this_cycle += 1
                except Exception as exc:
                    log.error('Scan error %s [%s]: %s', symbol, tf, exc)

                time.sleep(0.15)   # gentle rate-limit buffer

        log.info('Cycle done — %d signal(s) fired. Sleeping %ds.',
                 signals_this_cycle, SCAN_INTERVAL_SECONDS)
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
