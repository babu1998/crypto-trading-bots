"""
Bot 2 – EMA 200 Multi-Timeframe Signal Detector  (LONG only)
-------------------------------------------------------------
Strategy:
  Enter LONG when the price pulls back close to the EMA 200
  and all three timeframes confirm the uptrend.

  Conditions (all must be true):
    15m  close > EMA 200   — higher-timeframe trend is up
    3m   close > EMA 200   — intermediate trend is up
    1m   trigger fires     — one of:
          CROSS   : previous close ≤ EMA 200, current close > EMA 200
          BOUNCE  : price pulled back into EMA 200 zone from above
                    (candle low ≤ EMA * (1 + buffer)) and close > EMA 200

  The "EMA 200 zone" on the 1m is defined as:
    EMA * (1 - EMA_TOUCH_BUFFER_PCT) ≤ price ≤ EMA * (1 + EMA_TOUCH_BUFFER_PCT)

Signal fields:
    symbol, trigger ('CROSS'|'BOUNCE'), entry_price, ema200_1m, tf_checks
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
from utils import get_exchange, fetch_ohlcv_df, setup_logging
from config import (
    EMA_PERIOD, TIMEFRAMES, CANDLES_LIMIT, EMA_TOUCH_BUFFER_PCT,
    MIN_TRIGGER_VOLUME_RATIO, EMA_SLOPE_LOOKBACK,
    ATR_PERIOD, MIN_DEVIATION_BARS, MIN_DEVIATION_ATR_MULT,
    SL_SWING_LOW_BARS, SL_SWING_LOW_BUFFER, SL_BUFFER_PCT,
    SKIP_3M_EMA_TOUCH,
    CONSEC_LOSS_COOLDOWN_COUNT, CONSEC_LOSS_COOLDOWN_MINUTES,
    MIN_REENTRY_MINUTES, MAX_TRADES_PER_SYMBOL_PER_DAY,
    SYMBOL_SESSION_R_THRESHOLD, SYMBOL_SESSION_PAUSE_HOURS,
)

log = setup_logging('Bot2-Signals')
EMA_COL = f'ema{EMA_PERIOD}'


@dataclass
class Signal:
    symbol: str
    trigger: str           # 'CROSS' or 'BOUNCE'
    entry_price: float
    ema200_1m: float
    swing_low: float = 0.0  # structural low of the pullback — used for SL placement
    tf_checks: dict = field(default_factory=dict)

    def __str__(self):
        return (
            f"[LONG {self.trigger}] {self.symbol} "
            f"entry={self.entry_price:.6g}  EMA200_1m={self.ema200_1m:.6g}  "
            f"swing_low={self.swing_low:.6g}"
        )


class SignalBot:
    def __init__(self):
        self.exchange = get_exchange()
        # Per-symbol cooldown state
        self._symbol_state: dict[str, dict] = {}

    # ── per-symbol cooldown state ──────────────────────────────────────────

    def _get_state(self, symbol: str) -> dict:
        if symbol not in self._symbol_state:
            self._symbol_state[symbol] = {
                'consec_losses': 0,
                'pause_until': None,        # datetime | None
                'last_exit_time': None,     # datetime | None
                'trades_today': {},         # date -> int
                'session_r': 0.0,
            }
        return self._symbol_state[symbol]

    def _is_symbol_allowed(self, symbol: str) -> bool:
        state = self._get_state(symbol)
        now = datetime.utcnow()

        if state['pause_until'] and now < state['pause_until']:
            log.debug("%s: cooldown active until %s", symbol, state['pause_until'].strftime('%H:%M'))
            return False

        if state['last_exit_time']:
            gap_min = (now - state['last_exit_time']).total_seconds() / 60
            if gap_min < MIN_REENTRY_MINUTES:
                log.debug("%s: re-entry too soon (%.1f min)", symbol, gap_min)
                return False

        today = now.date()
        if state['trades_today'].get(today, 0) >= MAX_TRADES_PER_SYMBOL_PER_DAY:
            log.debug("%s: daily trade cap reached", symbol)
            return False

        return True

    def record_exit(self, symbol: str, r_achieved: float,
                    exit_time: Optional[datetime] = None):
        """Called by bot3 executor when a position on `symbol` closes."""
        state = self._get_state(symbol)
        now = exit_time or datetime.utcnow()
        today = now.date()

        is_loss = r_achieved <= 0
        state['consec_losses'] = state['consec_losses'] + 1 if is_loss else 0
        state['session_r'] += r_achieved
        state['last_exit_time'] = now
        state['trades_today'][today] = state['trades_today'].get(today, 0) + 1

        if state['consec_losses'] >= CONSEC_LOSS_COOLDOWN_COUNT:
            state['pause_until'] = datetime.utcnow() + timedelta(minutes=CONSEC_LOSS_COOLDOWN_MINUTES)
            state['consec_losses'] = 0
            log.info("%s: %d consecutive losses — cooling down for %d min",
                     symbol, CONSEC_LOSS_COOLDOWN_COUNT, CONSEC_LOSS_COOLDOWN_MINUTES)

        if state['session_r'] <= SYMBOL_SESSION_R_THRESHOLD:
            state['pause_until'] = datetime.utcnow() + timedelta(hours=SYMBOL_SESSION_PAUSE_HOURS)
            state['session_r'] = 0.0
            log.info("%s: session R hit %.1fR threshold — pausing %dh",
                     symbol, SYMBOL_SESSION_R_THRESHOLD, SYMBOL_SESSION_PAUSE_HOURS)

    # ── per-timeframe analysis ─────────────────────────────────────────────

    def _analyze_tf(self, symbol: str, timeframe: str) -> Optional[dict]:
        df = fetch_ohlcv_df(self.exchange, symbol, timeframe, CANDLES_LIMIT)
        if df is None or len(df) < EMA_PERIOD + 5:
            log.debug("%s %s: not enough candles", symbol, timeframe)
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema   = last[EMA_COL]
        close = last['close']
        low   = last['low']

        above_ema      = close > ema
        prev_above_ema = prev['close'] > prev[EMA_COL]

        crossed_above = (not prev_above_ema) and above_ema
        in_ema_zone   = low <= ema * (1 + EMA_TOUCH_BUFFER_PCT)
        bounced_up    = above_ema and in_ema_zone

        # Volume ratio: current bar vs 20-bar rolling average (excluding current)
        if len(df) > 21:
            vol_avg = df['volume'].iloc[-21:-1].mean()
            vol_ratio = last['volume'] / vol_avg if vol_avg > 0 else 1.0
        else:
            vol_ratio = 1.0

        # EMA slope over lookback bars (positive = rising EMA)
        if len(df) > EMA_SLOPE_LOOKBACK:
            ema_slope = ema - df[EMA_COL].iloc[-1 - EMA_SLOPE_LOOKBACK]
        else:
            ema_slope = 0.0

        # ATR(14) — Wilder smoothing, use bar before current to avoid look-ahead
        if len(df) > ATR_PERIOD + 1:
            tr = pd.concat([
                df['high'] - df['low'],
                (df['high'] - df['close'].shift(1)).abs(),
                (df['low']  - df['close'].shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr = float(tr.ewm(span=ATR_PERIOD, adjust=False).mean().iloc[-2])
        else:
            atr = 0.0

        # Deviation filter and swing low (lookback excludes current bar)
        n = min(MIN_DEVIATION_BARS, SL_SWING_LOW_BARS)
        deviation_bounce_ok = False
        deviation_cross_ok  = False
        swing_low           = ema * (1 - SL_BUFFER_PCT)  # fallback
        min_dev             = atr * MIN_DEVIATION_ATR_MULT

        if len(df) > n + 2 and min_dev > 0:
            lb = df.iloc[-n - 2:-2]   # N bars before the trigger bar
            max_high = float(lb['high'].max())
            deviation_bounce_ok = (max_high - ema) >= min_dev

            below = lb[lb['close'] < lb[EMA_COL]]
            if not below.empty:
                min_low_below = float(below['low'].min())
                deviation_cross_ok = (ema - min_low_below) >= min_dev

            swing_low = float(lb['low'].min())

        return {
            'tf': timeframe,
            'close': close,
            'ema200': round(ema, 8),
            'above_ema': above_ema,
            'crossed_above': crossed_above,
            'bounced_up': bounced_up,
            'vol_ratio': vol_ratio,
            'ema_slope': ema_slope,
            'deviation_bounce_ok': deviation_bounce_ok,
            'deviation_cross_ok':  deviation_cross_ok,
            'swing_low': swing_low,
        }

    # ── multi-TF signal ────────────────────────────────────────────────────

    def check_signal(self, symbol: str) -> Optional[Signal]:
        """
        Returns a LONG Signal when all three TF conditions are met, else None.
        TIMEFRAMES order: ['1m', '3m', '15m']
          1m  = trigger
          3m  = intermediate filter
          15m = trend confirmation
        """
        if not self._is_symbol_allowed(symbol):
            return None

        tf_data: dict[str, dict] = {}
        for tf in TIMEFRAMES:
            result = self._analyze_tf(symbol, tf)
            if result is None:
                return None
            tf_data[tf] = result

        tf_1m  = tf_data['1m']
        tf_3m  = tf_data['3m']
        tf_15m = tf_data['15m']

        if not (tf_15m['above_ema'] and tf_3m['above_ema']):
            return None

        if not tf_1m['bounced_up']:
            return None

        trigger = 'BOUNCE'

        # Rising EMA required — flat/declining = ranging market
        if tf_1m['ema_slope'] <= 0:
            log.debug("%s: BOUNCE skipped — EMA slope flat/negative (%.6f)",
                      symbol, tf_1m['ema_slope'])
            return None

        # Prior deviation: swing high must be ≥ MIN_DEVIATION_ATR_MULT × ATR above EMA
        if not tf_1m['deviation_bounce_ok']:
            log.debug("%s: BOUNCE skipped — no prior deviation above EMA (< %.1f× ATR)",
                      symbol, MIN_DEVIATION_ATR_MULT)
            return None

        # Skip when 3m is simultaneously bouncing/crossing its own EMA200
        if SKIP_3M_EMA_TOUCH and (tf_3m['bounced_up'] or tf_3m['crossed_above']):
            log.debug("%s: %s skipped — 3m is also at its EMA200", symbol, trigger)
            return None

        # Volume confirmation: trigger candle must meet minimum volume threshold
        if tf_1m['vol_ratio'] < MIN_TRIGGER_VOLUME_RATIO:
            log.debug("%s: %s skipped — low trigger volume (ratio=%.2f)",
                      symbol, trigger, tf_1m['vol_ratio'])
            return None

        swing_low = tf_1m['swing_low']

        sig = Signal(
            symbol=symbol,
            trigger=trigger,
            entry_price=tf_1m['close'],
            ema200_1m=tf_1m['ema200'],
            swing_low=swing_low,
            tf_checks=tf_data,
        )
        log.info("SIGNAL: %s", sig)
        return sig

    def scan_watchlist(self, symbols: list[str]) -> list[Signal]:
        signals = []
        for sym in symbols:
            try:
                sig = self.check_signal(sym)
                if sig:
                    signals.append(sig)
            except Exception as exc:
                log.warning("Error scanning %s: %s", sym, exc)
        return signals


if __name__ == '__main__':
    from bot1_gainers import GainersBot
    watchlist = GainersBot().get_combined_gainers()
    bot = SignalBot()
    for s in bot.scan_watchlist(watchlist):
        print(s)
