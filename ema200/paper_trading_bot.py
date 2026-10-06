"""
EMA-200 Paper Trading Bot — runs parallel to discord_signal_bot.py
-------------------------------------------------------------------
Reuses the exact same signal detection (get_top_gainers / fetch_df /
check_bounce / tier_for_rank) from discord_signal_bot.py so both bots
always agree on what counts as a signal. Instead of posting to Discord,
this bot simulates the full trade lifecycle and tracks cumulative R.

Rules:
  - At most MAX_OPEN_TRADES simulated trades open at once.
  - At most one open trade per symbol at a time (no repeated/duplicate
    entries on a symbol that's already in a trade).
  - TP1 hit  -> close 25% of the original size, SL -> breakeven (entry).
  - TP2 hit  -> close another 25% (50% total closed), SL -> TP1 price.
  - TP3 hit  -> close the remaining 50% (100% total), trade fully closed.
  - SL is checked before TPs each cycle (conservative: assume the worst
    if both were touched within the same candle).

State (open trades + cumulative R) is persisted to paper_trades_state.json
so a restart resumes cleanly. Every partial close / full close is appended
to paper_trades_log.csv.

.env keys required: none beyond what discord_signal_bot.py already uses.
"""

import os
import csv
import json
import time
import signal
import logging
from datetime import datetime, timezone

from discord_signal_bot import (
    get_exchange,
    get_top_gainers,
    fetch_df,
    check_bounce,
    tier_for_rank,
    TIER_TF,
    TOP_N,
    SCAN_INTERVAL_SECONDS,
    GAINERS_REFRESH_MINUTES,
    SIGNAL_COOLDOWN_MINUTES,
)

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_OPEN_TRADES     = 3
TP1_CLOSE_FRACTION  = 0.25   # of original size, closed at TP1
TP2_CLOSE_FRACTION  = 0.25   # additional, closed at TP2 (cumulative 0.50)
TP3_CLOSE_FRACTION  = 0.50   # remainder, closed at TP3   (cumulative 1.00)

_HERE       = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(_HERE, 'paper_trades_state.json')
LOG_FILE    = os.path.join(_HERE, 'paper_trades_log.csv')
LOG_FIELDS  = [
    'timestamp', 'symbol', 'tf', 'rank', 'tier', 'event', 'price',
    'fraction_closed', 'r_contribution', 'trade_total_r', 'cumulative_rr',
]

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('EMA200-PaperTrader')

_stop = False


def _handle_stop(signum, _frame):
    global _stop
    log.info('Signal %s received — finishing current cycle then exiting.', signum)
    _stop = True


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


# ── State persistence ──────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as exc:
            log.error('Failed to load state file, starting fresh: %s', exc)
    return {'open_trades': {}, 'cumulative_rr': 0.0}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        log.error('Failed to save state file: %s', exc)


def log_event(row: dict) -> None:
    new_file = not os.path.exists(LOG_FILE)
    try:
        with open(LOG_FILE, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        log.error('Failed to write trade log: %s', exc)


# ── Trade helpers ──────────────────────────────────────────────────────────────
def open_trade(symbol: str, tf: str, rank: int, tier: int, sig: dict) -> dict:
    sl_dist = sig['entry'] - sig['sl']
    return {
        'symbol':            symbol,
        'tf':                tf,
        'rank':              rank,
        'tier':              tier,
        'entry_time':        datetime.now(timezone.utc).isoformat(),
        'entry':             sig['entry'],
        'initial_sl':        sig['sl'],
        'current_sl':        sig['sl'],
        'sl_dist':           sl_dist,
        'tp1':               sig['tp1'],
        'tp2':               sig['tp2'],
        'tp3':               sig['tp3'],
        'stage':             'initial',    # initial -> tp1 -> tp2 -> closed
        'remaining_fraction': 1.0,
        'realized_r':        0.0,
    }


def r_multiple(trade: dict, price: float) -> float:
    return (price - trade['entry']) / trade['sl_dist']


def manage_trade(exchange, trade: dict, state: dict) -> bool:
    """Check one open trade against the latest 1m price action.
    Returns True if the trade fully closed (caller should remove it)."""
    symbol = trade['symbol']
    try:
        bars = exchange.fetch_ohlcv(symbol, '1m', limit=2)
    except Exception as exc:
        log.error('Monitor fetch failed %s: %s', symbol, exc)
        return False
    if not bars:
        return False

    high = max(b[2] for b in bars)
    low  = min(b[3] for b in bars)
    now  = datetime.now(timezone.utc).isoformat()

    # 1. Stop-loss check first (conservative — assume worst case within the bar)
    if low <= trade['current_sl']:
        price   = trade['current_sl']
        r_mult  = r_multiple(trade, price)
        contrib = trade['remaining_fraction'] * r_mult
        trade['realized_r'] += contrib

        event = 'SL' if trade['stage'] == 'initial' else (
            'BE_SL' if trade['stage'] == 'tp1' else 'TP1_SL'
        )
        state['cumulative_rr'] = round(state['cumulative_rr'] + contrib, 4)
        log_event({
            'timestamp': now, 'symbol': symbol, 'tf': trade['tf'],
            'rank': trade['rank'], 'tier': trade['tier'], 'event': event,
            'price': price, 'fraction_closed': round(trade['remaining_fraction'], 4),
            'r_contribution': round(contrib, 4),
            'trade_total_r': round(trade['realized_r'], 4),
            'cumulative_rr': state['cumulative_rr'],
        })
        log.info('%s  %-8s  %-6s  total_r=%.3f  cumulative_rr=%.3f',
                  symbol, event, trade['tf'], trade['realized_r'], state['cumulative_rr'])
        return True

    # 2. Take-profit checks, one level per cycle (1m/60s resolution is enough here)
    if trade['stage'] == 'initial' and high >= trade['tp1']:
        r_mult  = r_multiple(trade, trade['tp1'])
        contrib = TP1_CLOSE_FRACTION * r_mult
        trade['realized_r'] += contrib
        trade['remaining_fraction'] -= TP1_CLOSE_FRACTION
        trade['current_sl'] = trade['entry']          # SL -> breakeven
        trade['stage'] = 'tp1'
        log_event({
            'timestamp': now, 'symbol': symbol, 'tf': trade['tf'],
            'rank': trade['rank'], 'tier': trade['tier'], 'event': 'TP1',
            'price': trade['tp1'], 'fraction_closed': TP1_CLOSE_FRACTION,
            'r_contribution': round(contrib, 4), 'trade_total_r': '',
            'cumulative_rr': state['cumulative_rr'],
        })
        log.info('%s  TP1 hit  %-6s  closed 25%%  SL -> breakeven', symbol, trade['tf'])
        return False

    if trade['stage'] == 'tp1' and high >= trade['tp2']:
        r_mult  = r_multiple(trade, trade['tp2'])
        contrib = TP2_CLOSE_FRACTION * r_mult
        trade['realized_r'] += contrib
        trade['remaining_fraction'] -= TP2_CLOSE_FRACTION
        trade['current_sl'] = trade['tp1']             # SL -> TP1 price
        trade['stage'] = 'tp2'
        log_event({
            'timestamp': now, 'symbol': symbol, 'tf': trade['tf'],
            'rank': trade['rank'], 'tier': trade['tier'], 'event': 'TP2',
            'price': trade['tp2'], 'fraction_closed': TP2_CLOSE_FRACTION,
            'r_contribution': round(contrib, 4), 'trade_total_r': '',
            'cumulative_rr': state['cumulative_rr'],
        })
        log.info('%s  TP2 hit  %-6s  closed 25%% more (50%% total)  SL -> TP1', symbol, trade['tf'])
        return False

    if trade['stage'] == 'tp2' and high >= trade['tp3']:
        contrib = trade['remaining_fraction'] * r_multiple(trade, trade['tp3'])
        trade['realized_r'] += contrib
        trade['remaining_fraction'] = 0.0
        state['cumulative_rr'] = round(state['cumulative_rr'] + contrib, 4)
        log_event({
            'timestamp': now, 'symbol': symbol, 'tf': trade['tf'],
            'rank': trade['rank'], 'tier': trade['tier'], 'event': 'TP3',
            'price': trade['tp3'], 'fraction_closed': TP3_CLOSE_FRACTION,
            'r_contribution': round(contrib, 4),
            'trade_total_r': round(trade['realized_r'], 4),
            'cumulative_rr': state['cumulative_rr'],
        })
        log.info('%s  TP3 hit  %-6s  trade closed  total_r=%.3f  cumulative_rr=%.3f',
                  symbol, trade['tf'], trade['realized_r'], state['cumulative_rr'])
        return True

    return False


# ── Main loop ──────────────────────────────────────────────────────────────────
def main() -> None:
    log.info('Starting EMA-200 Paper Trading Bot (max %d concurrent trades)', MAX_OPEN_TRADES)

    exchange = get_exchange()
    state    = load_state()
    open_trades: dict = state.setdefault('open_trades', {})
    state.setdefault('cumulative_rr', 0.0)
    if open_trades:
        log.info('Resumed %d open trade(s) from state file. cumulative_rr=%.3f',
                  len(open_trades), state['cumulative_rr'])

    gainers:         list[dict]         = []
    last_gainers_ts: float              = 0.0
    cooldowns:       dict[tuple, float] = {}

    while not _stop:
        now = time.time()

        if now - last_gainers_ts >= GAINERS_REFRESH_MINUTES * 60:
            try:
                gainers = get_top_gainers(exchange, TOP_N)
                last_gainers_ts = now
                log.info('Gainers refreshed — %d symbols', len(gainers))
            except Exception as exc:
                log.error('Failed to fetch gainers: %s', exc)

        # 1. Manage existing open trades (independent of current gainers list —
        #    a trade stays open even if the symbol drops out of the top 20).
        for symbol in list(open_trades.keys()):
            trade = open_trades[symbol]
            if manage_trade(exchange, trade, state):
                del open_trades[symbol]
            time.sleep(0.15)
        save_state(state)

        # 2. Look for new entries, respecting the open-trade cap and
        #    one-trade-per-symbol rule.
        for item in gainers:
            if len(open_trades) >= MAX_OPEN_TRADES:
                break

            symbol = item['symbol']
            if symbol in open_trades:
                continue  # already have a live trade on this symbol

            rank = item['rank']
            tier = tier_for_rank(rank)

            for tf in TIER_TF[tier]:
                key       = (symbol, tf)
                last_sent = cooldowns.get(key, 0.0)
                if now - last_sent < SIGNAL_COOLDOWN_MINUTES * 60:
                    continue

                try:
                    df  = fetch_df(exchange, symbol, tf)
                    sig = check_bounce(df) if not df.empty else None
                except Exception as exc:
                    log.error('Scan error %s [%s]: %s', symbol, tf, exc)
                    sig = None

                cooldowns[key] = now
                time.sleep(0.15)

                if sig:
                    open_trades[symbol] = open_trade(symbol, tf, rank, tier, sig)
                    log.info('OPEN  %s  [%s]  rank=#%d  entry=%s  sl=%s  tp1/2/3=%s/%s/%s  (%d/%d slots used)',
                              symbol, tf, rank, sig['entry'], sig['sl'],
                              sig['tp1'], sig['tp2'], sig['tp3'],
                              len(open_trades), MAX_OPEN_TRADES)
                    break  # one trade per symbol — stop scanning its other tf

        save_state(state)
        log.info('Cycle done — %d/%d trades open. cumulative_rr=%.3f. Sleeping %ds.',
                  len(open_trades), MAX_OPEN_TRADES, state['cumulative_rr'], SCAN_INTERVAL_SECONDS)

        for _ in range(SCAN_INTERVAL_SECONDS):
            if _stop:
                break
            time.sleep(1)

    save_state(state)
    log.info('Stopped cleanly. Final cumulative_rr=%.3f', state['cumulative_rr'])


if __name__ == '__main__':
    main()
