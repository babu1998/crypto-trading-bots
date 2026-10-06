"""
Bot 4 – Backtester
==================
Replays last week's historical 1-minute data from Binance to simulate
exactly what Bot 1 → Bot 2 → Bot 3 would have done.

Steps
-----
1. Identify the top-10 gainers at the start of the backtest window
   (mirrors Bot 1's daily scan using historical daily candles).
2. Fetch 1-minute OHLCV for each symbol:
     warmup period  : 3 days before window  (seeds 15m EMA 200 accurately)
     backtest window: 7 days
3. Resample to 3m and 15m; align EMAs back to the 1m index without
   look-ahead (bars are available only after they close).
4. Walk forward bar-by-bar detecting LONG signals (mirrors Bot 2).
5. For each signal simulate the trade: Fibonacci TP1/TP2/TP3 + SL
   (mirrors Bot 3).  Only one position per symbol at a time.
   Max MAX_OPEN_POSITIONS across all symbols simultaneously.
6. Report per-trade breakdown and aggregated R statistics.

Rate-limit compliance
---------------------
  - fetch_tickers()       : called once           (~40 weight)
  - fetch_ohlcv(1d, n)    : 1 call per symbol     (~1 weight each)
  - fetch_ohlcv(1m, 1000) : paginated, throttled  (5 weight per call)
  Total weight for 10 symbols ≈ 40 + 10 + 150 = 200/min  (well under 1200 cap)
"""

import time
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from utils import get_exchange, setup_logging
from config import (
    EMA_PERIOD, EMA_TOUCH_BUFFER_PCT, SL_BUFFER_PCT,
    SL_SWING_LOW_BARS, SL_SWING_LOW_BUFFER,
    ATR_PERIOD, MIN_DEVIATION_BARS, MIN_DEVIATION_ATR_MULT,
    SKIP_3M_EMA_TOUCH,
    FIB_TP_LEVELS, FIB_TP_SPLITS,
    TOP_N_GAINERS, MIN_VOLUME_USDT, MAX_OPEN_POSITIONS,
    POOL_SIZE, WATCHLIST_REFRESH_HOURS, GAINERS_LOOKBACK_HOURS,
    SL_CONSEC_CLOSES, SL_TOTAL_CLOSES, SL_CLOSES_WINDOW,
    SL_DUAL_TF_ENABLED, SL_VOL_CONFIRM_ENABLED, SL_VOL_CONFIRM_RATIO, SL_TIME_STOP_BARS,
    RISK_PER_TRADE_PCT,
    MIN_TRIGGER_VOLUME_RATIO, EMA_SLOPE_LOOKBACK,
    CONSEC_LOSS_COOLDOWN_COUNT, CONSEC_LOSS_COOLDOWN_MINUTES,
    MIN_REENTRY_CANDLES, MAX_TRADES_PER_SYMBOL_PER_DAY,
    SYMBOL_SESSION_R_THRESHOLD, SYMBOL_SESSION_PAUSE_HOURS,
)

log = setup_logging('Bot4-Backtest')

EMA_COL      = f'ema{EMA_PERIOD}'
BACKTEST_DAYS = 7
WARMUP_DAYS   = 3          # 15m EMA 200 needs ~2.1 days; use 3 to be safe
FETCH_LIMIT   = 1000       # candles per API call (weight = 5)
FETCH_PAUSE   = 0.15       # seconds between paginated calls  (avoids burst)
SYMBOL_PAUSE  = 0.25       # seconds between symbols


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    symbol:      str
    trigger:     str        # 'CROSS' | 'BOUNCE'
    entry_time:  pd.Timestamp
    entry_price: float
    sl_price:    float
    tp1:         float
    tp2:         float
    tp3:         float
    exit_time:   Optional[pd.Timestamp]
    exit_reason: str        # 'SL' | 'TP1+SL' | 'TP1+TP2+SL' | 'ALL_TPS' | 'OPEN'
    tps_hit:     list       # [bool, bool, bool]
    r_achieved:  float


# ─────────────────────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────────────────────

class Backtester:

    def __init__(self):
        self.exchange = get_exchange()

    # ── Step 1: historical top gainers ────────────────────────────────────────

    def find_historical_gainers(self, backtest_start: datetime, n: int = TOP_N_GAINERS) -> list[str]:
        """
        Return symbols that were top-N daily gainers on the day immediately
        before backtest_start (mirrors what Bot 1 would have shown at open).

        Uses the liquid-symbol list from fetch_tickers() to avoid scanning
        300+ low-volume pairs, then fetches 3 daily candles per symbol.
        Weight: ~40 (tickers) + 1×N_liquid (daily candles, limit=3).
        """
        log.info("Identifying historical top-%d gainers before %s …", n, backtest_start.date())

        tickers = self.exchange.fetch_tickers()
        liquid = [
            s for s, t in tickers.items()
            if (t.get('quoteVolume') or 0) >= MIN_VOLUME_USDT
        ]
        log.info("Scanning %d liquid symbols for historical daily change.", len(liquid))

        rows = []
        for sym in liquid:
            try:
                ohlcv = self.exchange.fetch_ohlcv(sym, '1d', limit=3)
                if len(ohlcv) < 2:
                    continue
                # candle at index -2 = the day before backtest_start
                ts, o, h, l, c, v = ohlcv[-2]
                if o and o > 0:
                    pct = (c - o) / o * 100
                    rows.append({'symbol': sym, 'pct': pct, 'vol': v * c})
            except Exception as exc:
                log.debug("Skip %s: %s", sym, exc)
            time.sleep(0.05)    # 50 ms throttle

        df = pd.DataFrame(rows)
        if df.empty:
            log.warning("No historical daily data returned.")
            return []

        gainers = (
            df[df['pct'] > 0]
            .nlargest(n, 'pct')[['symbol', 'pct', 'vol']]
            .reset_index(drop=True)
        )
        log.info("Historical top %d:\n%s", n, gainers.to_string())
        return gainers['symbol'].tolist()

    # ── Step 2: fetch 1-minute data (paginated) ───────────────────────────────

    def _fetch_full_1m(self, symbol: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        """
        Paginate fetch_ohlcv(1m) from since_ms to until_ms.
        Each call fetches FETCH_LIMIT candles (weight = 5).
        Pauses FETCH_PAUSE seconds between pages.
        """
        all_rows = []
        cursor = since_ms
        while cursor < until_ms:
            chunk = self.exchange.fetch_ohlcv(symbol, '1m', since=cursor, limit=FETCH_LIMIT)
            if not chunk:
                break
            all_rows.extend(chunk)
            last_ts = chunk[-1][0]
            if last_ts >= until_ms or len(chunk) < FETCH_LIMIT:
                break
            cursor = last_ts + 60_000   # advance by 1 minute
            time.sleep(FETCH_PAUSE)

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        df.set_index('ts', inplace=True)
        df = df[~df.index.duplicated(keep='last')]
        df.sort_index(inplace=True)
        # Trim to requested window
        until_ts = pd.Timestamp(until_ms, unit='ms', tz='UTC')
        df = df[df.index <= until_ts]
        return df

    # ── Step 3: resample + align EMAs to 1-minute index ──────────────────────

    def _add_ema(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df[EMA_COL] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        return df

    def _resample_and_align(
        self, df_1m: pd.DataFrame, freq: str
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        Resample df_1m to freq, compute EMA 200 on closes, then forward-fill
        back onto the 1m index.

        To avoid look-ahead, each resampled bar becomes "available" only after
        it closes: we shift the resampled index forward by one bar-length.
        At 1m timestamp T, the series therefore reflects the last *completed*
        higher-TF bar, not the currently forming one.

        Returns
        -------
        ema_aligned   : EMA 200 value of last completed bar at each 1m step
        above_aligned : whether close > EMA 200 at each 1m step
        at_ema_aligned: whether that bar was a BOUNCE or CROSS off its own EMA200
        """
        freq_map = {'3m': '3min', '15m': '15min'}
        pd_freq = freq_map[freq]
        offset = pd.tseries.frequencies.to_offset(pd_freq)

        df_r = (
            df_1m[['open', 'high', 'low', 'close', 'volume']]
            .resample(pd_freq)
            .agg({'open': 'first', 'high': 'max', 'low': 'min',
                  'close': 'last', 'volume': 'sum'})
            .dropna()
        )
        df_r[EMA_COL] = df_r['close'].ewm(span=EMA_PERIOD, adjust=False).mean()

        # Detect bounce/cross on resampled bars (before shifting)
        bounce = (
            (df_r['low'] <= df_r[EMA_COL] * (1 + EMA_TOUCH_BUFFER_PCT)) &
            (df_r['close'] > df_r[EMA_COL])
        )
        prev_above = df_r['close'].shift(1) > df_r[EMA_COL].shift(1)
        cross = (~prev_above) & (df_r['close'] > df_r[EMA_COL])
        at_ema = (bounce | cross).fillna(False)

        # Shift index forward by one bar so each row is available only after close
        df_r_shifted = df_r.copy()
        df_r_shifted.index = df_r_shifted.index + offset

        at_ema_shifted = at_ema.copy()
        at_ema_shifted.index = at_ema_shifted.index + offset

        ema_aligned    = df_r_shifted[EMA_COL].reindex(df_1m.index, method='ffill')
        above_aligned  = (
            (df_r_shifted['close'] > df_r_shifted[EMA_COL])
            .reindex(df_1m.index, method='ffill')
        )
        at_ema_aligned = at_ema_shifted.reindex(df_1m.index, method='ffill').fillna(False)
        return ema_aligned, above_aligned, at_ema_aligned

    # ── Step 4: walk-forward signal detection ────────────────────────────────

    def _check_signal_at(
        self,
        df1m:  pd.DataFrame,
        ema3m_series:    pd.Series,
        above3m_series:  pd.Series,
        at_ema3m_series: pd.Series,
        ema15m_series:   pd.Series,
        above15m_series: pd.Series,
        atr_series:      pd.Series,
        idx: int,
    ) -> Optional[tuple[str, float]]:
        """
        Replicate Bot 2 logic at bar index `idx`.
        Returns (trigger, swing_low) or None.
        All comparisons use only data ≤ idx (no look-ahead).
        """
        if idx < 1:
            return None

        # Higher-TF trend must be bullish
        if not above15m_series.iloc[idx]:
            return None
        if not above3m_series.iloc[idx]:
            return None

        ema1m_now  = float(df1m[EMA_COL].iloc[idx])
        ema1m_prev = float(df1m[EMA_COL].iloc[idx - 1])
        close_now  = float(df1m['close'].iloc[idx])
        close_prev = float(df1m['close'].iloc[idx - 1])
        low_now    = float(df1m['low'].iloc[idx])

        above_now  = close_now  > ema1m_now
        above_prev = close_prev > ema1m_prev

        # BOUNCE only: low dips into EMA zone, close above EMA
        if not (above_now and low_now <= ema1m_now * (1 + EMA_TOUCH_BUFFER_PCT)):
            return None

        trigger = 'BOUNCE'

        # EMA must be rising — flat/declining = ranging market
        if idx >= EMA_SLOPE_LOOKBACK:
            ema_slope = ema1m_now - float(df1m[EMA_COL].iloc[idx - EMA_SLOPE_LOOKBACK])
            if ema_slope <= 0:
                return None

        # ATR at current bar (already precomputed — no look-ahead)
        atr      = float(atr_series.iloc[idx])
        min_dev  = atr * MIN_DEVIATION_ATR_MULT

        # Deviation filter: swing high in lookback must be ≥ 1 ATR above EMA
        n = min(MIN_DEVIATION_BARS, SL_SWING_LOW_BARS)
        swing_low = ema1m_now * (1 - SL_BUFFER_PCT)  # fallback
        if idx >= n + 2 and min_dev > 0:
            lb_start = idx - n - 1
            lb_end   = idx - 1
            lb_high  = df1m['high'].iloc[lb_start:lb_end]
            lb_low   = df1m['low'].iloc[lb_start:lb_end]

            swing_low = float(lb_low.min())

            if (float(lb_high.max()) - ema1m_now) < min_dev:
                return None
        else:
            return None  # not enough history or ATR not ready

        # Skip 3m EMA touch: 3m bar is simultaneously bouncing/crossing its EMA200
        if SKIP_3M_EMA_TOUCH and bool(at_ema3m_series.iloc[idx]):
            return None

        # Volume filter: trigger candle must have minimum volume relative to recent average
        if idx >= 20:
            vol_avg = float(df1m['volume'].iloc[idx - 20:idx].mean())
            if vol_avg > 0 and float(df1m['volume'].iloc[idx]) < vol_avg * MIN_TRIGGER_VOLUME_RATIO:
                return None

        return trigger, swing_low

    # ── Step 5: simulate one trade ────────────────────────────────────────────

    def _simulate_trade(
        self,
        df1m: pd.DataFrame,
        entry_idx: int,
        trigger: str,
        symbol: str,
        above3m_series: Optional[pd.Series] = None,
        swing_low: float = 0.0,
    ) -> TradeResult:
        """
        Enter at close of bar `entry_idx`, scan forward bar-by-bar.

        SL logic (mirrors Bot 3):
          - Initial price SL at EMA200 * (1 - SL_BUFFER_PCT) — hard backstop
          - After TP1: trailing SL moves to entry (break-even)
          - After TP2: trailing SL moves to TP1
          - EMA-close SL: counting bars where close < EMA200 (see below)
          - Time stop: exit after SL_TIME_STOP_BARS bars with no TP1 (if enabled)

        EMA-close SL counting (all active simultaneously):
          - SL_DUAL_TF_ENABLED: a bar only counts if 3m is also bearish (filters 1m noise)
          - SL_VOL_CONFIRM_ENABLED: a bar only counts if volume > SL_VOL_CONFIRM_RATIO × avg
          - Fires when: consec >= SL_CONSEC_CLOSES OR total in window >= SL_TOTAL_CLOSES

        Per-bar priority (conservative, no tick data):
          1. Trailing price SL  (intrabar, uses bar low)
          2. TP hits            (intrabar, uses bar high)
          3. EMA-close SL       (end-of-bar, uses bar close)
          4. Time stop          (end-of-bar, no TP1 hit within SL_TIME_STOP_BARS)
        """
        entry_bar   = df1m.iloc[entry_idx]
        entry_price = float(entry_bar['close'])
        ema1m       = float(df1m[EMA_COL].iloc[entry_idx])

        if swing_low > 0:
            sl_price = swing_low * (1 - SL_SWING_LOW_BUFFER)
        else:
            sl_price = ema1m * (1 - SL_BUFFER_PCT)
        sl_dist  = entry_price - sl_price

        if sl_dist <= 0:
            return TradeResult(
                symbol=symbol, trigger=trigger,
                entry_time=entry_bar.name, entry_price=entry_price,
                sl_price=round(sl_price, 6),
                tp1=entry_price, tp2=entry_price, tp3=entry_price,
                exit_time=entry_bar.name, exit_reason='SKIP',
                tps_hit=[False, False, False], r_achieved=0.0,
            )

        tp1 = entry_price + sl_dist * FIB_TP_LEVELS[0]
        tp2 = entry_price + sl_dist * FIB_TP_LEVELS[1]
        tp3 = entry_price + sl_dist * FIB_TP_LEVELS[2]
        fib_tps = [tp1, tp2, tp3]

        trailing_sl = sl_price

        open_frac   = 1.0
        r_total     = 0.0
        tps_hit     = [False, False, False]
        exit_time   = None
        exit_reason = 'OPEN'

        consec_below = 0
        window_below: list[bool] = []

        for i in range(entry_idx + 1, len(df1m)):
            bar       = df1m.iloc[i]
            bar_low   = float(bar['low'])
            bar_high  = float(bar['high'])
            bar_close = float(bar['close'])
            bar_ema   = float(df1m[EMA_COL].iloc[i])

            # ── 1. Trailing price SL ──────────────────────────────────────
            if bar_low <= trailing_sl:
                sl_r      = (trailing_sl - entry_price) / sl_dist
                r_total  += open_frac * sl_r
                open_frac = 0.0
                exit_time = bar.name
                n_hit = sum(tps_hit)
                exit_reason = {0: 'SL', 1: 'BE_SL', 2: 'TP1_SL'}.get(n_hit, 'SL')
                break

            # ── 2. TP hits; trail SL after each ──────────────────────────
            for ti, tp_price in enumerate(fib_tps):
                if not tps_hit[ti] and bar_high >= tp_price:
                    tps_hit[ti] = True
                    r_total    += FIB_TP_SPLITS[ti] * FIB_TP_LEVELS[ti]
                    open_frac  -= FIB_TP_SPLITS[ti]
                    if ti == 0:
                        trailing_sl = max(trailing_sl, entry_price)
                    elif ti == 1:
                        trailing_sl = max(trailing_sl, tp1)

            if open_frac <= 1e-9:
                exit_time   = bar.name
                exit_reason = 'ALL_TPS'
                break

            # ── 3. EMA-close SL (end of bar) ─────────────────────────────
            is_below = bar_close < bar_ema

            # Method: dual-TF — only count bar if 3m is also bearish
            if is_below and SL_DUAL_TF_ENABLED and above3m_series is not None:
                is_below = not above3m_series.iloc[i]

            # Method: volume confirmation — only count if breakdown has conviction
            if is_below and SL_VOL_CONFIRM_ENABLED and i >= 20:
                vol_window = df1m['volume'].iloc[i - 20:i]
                vol_avg = float(vol_window.mean())
                if vol_avg > 0 and float(df1m['volume'].iloc[i]) < vol_avg * SL_VOL_CONFIRM_RATIO:
                    is_below = False

            consec_below = consec_below + 1 if is_below else 0
            window_below.append(is_below)
            if len(window_below) > SL_CLOSES_WINDOW:
                window_below.pop(0)

            if consec_below >= SL_CONSEC_CLOSES or sum(window_below) >= SL_TOTAL_CLOSES:
                exit_r    = (bar_close - entry_price) / sl_dist
                r_total  += open_frac * exit_r
                open_frac = 0.0
                exit_time = bar.name
                n_hit = sum(tps_hit)
                exit_reason = {
                    0: 'EMA_SL', 1: 'TP1+EMA_SL', 2: 'TP2+EMA_SL'
                }.get(n_hit, 'EMA_SL')
                break

            # ── 4. Time stop (no TP1 after N bars) ───────────────────────
            if SL_TIME_STOP_BARS > 0 and not tps_hit[0]:
                if (i - entry_idx) >= SL_TIME_STOP_BARS:
                    exit_r    = (bar_close - entry_price) / sl_dist
                    r_total  += open_frac * exit_r
                    open_frac = 0.0
                    exit_time = bar.name
                    exit_reason = 'TIME_SL'
                    break

        if exit_reason == 'OPEN':
            exit_time = df1m.index[-1]

        return TradeResult(
            symbol=symbol,
            trigger=trigger,
            entry_time=entry_bar.name,
            entry_price=entry_price,
            sl_price=round(sl_price, 6),
            tp1=round(tp1, 6),
            tp2=round(tp2, 6),
            tp3=round(tp3, 6),
            exit_time=exit_time,
            exit_reason=exit_reason,
            tps_hit=tps_hit,
            r_achieved=round(r_total, 4),
        )

    # ── Step 6: per-symbol walk-forward ──────────────────────────────────────

    # ── Step 3b: dynamic 4-hour watchlist windows ─────────────────────────────

    def _compute_window_gainers(
        self,
        pool_dfs: dict,
        backtest_start: pd.Timestamp,
        backtest_end:   pd.Timestamp,
    ) -> dict:
        """
        At each WATCHLIST_REFRESH_HOURS boundary, rank the pool by
        GAINERS_LOOKBACK_HOURS % change and return the top TOP_N_GAINERS.
        Returns {window_start_ts: set_of_active_symbols}.
        """
        windows: dict[pd.Timestamp, set] = {}
        current = backtest_start

        while current < backtest_end:
            lookback_ts = current - pd.Timedelta(hours=GAINERS_LOOKBACK_HOURS)
            scores: dict[str, float] = {}

            for sym, df in pool_dfs.items():
                i_now  = min(int(df.index.searchsorted(current)),    len(df) - 1)
                i_then = min(int(df.index.searchsorted(lookback_ts)), len(df) - 1)
                p_now  = float(df['close'].iloc[i_now])
                p_then = float(df['close'].iloc[i_then])
                if p_then > 0:
                    scores[sym] = (p_now - p_then) / p_then * 100

            if scores:
                top_n = sorted(scores, key=lambda s: scores[s], reverse=True)[:TOP_N_GAINERS]
                windows[current] = set(top_n)
                log.debug("Window %s → top-%d: %s",
                          str(current)[:16], TOP_N_GAINERS,
                          [(s, f"{scores[s]:.1f}%") for s in top_n])

            current += pd.Timedelta(hours=WATCHLIST_REFRESH_HOURS)

        return windows

    # ── Step 6: per-symbol walk-forward ──────────────────────────────────────

    def _run_symbol(
        self,
        symbol: str,
        df_full: pd.DataFrame,
        backtest_start_ts: pd.Timestamp,
        window_gainers: Optional[dict] = None,
    ) -> list[TradeResult]:
        """
        Run the full backtest for one symbol.
        df_full includes warmup + backtest window.
        window_gainers: optional {window_ts: set_of_symbols} — when provided,
        new entries are only allowed while this symbol is in the active top-N.
        Returns list of TradeResult.
        """
        df_full = self._add_ema(df_full)

        # Precompute ATR(14) once — reused by every bar's signal check
        _tr = pd.concat([
            df_full['high'] - df_full['low'],
            (df_full['high'] - df_full['close'].shift(1)).abs(),
            (df_full['low']  - df_full['close'].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr_series = _tr.ewm(span=ATR_PERIOD, adjust=False).mean()

        ema3m,  above3m,  at_ema3m  = self._resample_and_align(df_full, '3m')
        ema15m, above15m, _         = self._resample_and_align(df_full, '15m')

        backtest_mask = df_full.index >= backtest_start_ts
        backtest_indices = [i for i, v in enumerate(backtest_mask) if v]
        if not backtest_indices:
            return []

        results     = []
        in_position = False
        pos_exit_idx: Optional[int] = None   # bar index where current position exits

        # Per-symbol overtrading prevention state
        consec_losses         = 0
        session_r             = 0.0
        last_entry_idx: Optional[int] = None  # bar-count re-entry gate
        trades_per_day: dict  = {}
        pause_until: Optional[pd.Timestamp] = None

        for idx in backtest_indices:
            ts = df_full.index[idx]

            if in_position and pos_exit_idx is not None and idx > pos_exit_idx:
                in_position = False

            if in_position:
                continue

            # Dynamic watchlist gate: only enter when symbol is in current 4h window
            if window_gainers is not None:
                elapsed_h = (ts - backtest_start_ts).total_seconds() / 3600
                win_idx   = int(elapsed_h / WATCHLIST_REFRESH_HOURS)
                win_ts    = backtest_start_ts + pd.Timedelta(hours=win_idx * WATCHLIST_REFRESH_HOURS)
                if symbol not in window_gainers.get(win_ts, set()):
                    continue

            # ── Overtrading checks ────────────────────────────────────────
            if pause_until is not None and ts < pause_until:
                continue

            # 5-candle re-entry gate (bar-count based)
            if last_entry_idx is not None and (idx - last_entry_idx) < MIN_REENTRY_CANDLES:
                continue

            trade_date = ts.date()
            if trades_per_day.get(trade_date, 0) >= MAX_TRADES_PER_SYMBOL_PER_DAY:
                continue

            result = self._check_signal_at(
                df_full, ema3m, above3m, at_ema3m, ema15m, above15m, atr_series, idx
            )
            if result is None:
                continue
            trigger, swing_low = result

            trade = self._simulate_trade(df_full, idx, trigger, symbol, above3m, swing_low)
            if trade.exit_reason == 'SKIP':
                continue

            results.append(trade)
            in_position   = True
            last_entry_idx = idx

            # Map exit timestamp back to a bar index for the candle-count gate
            if trade.exit_time is not None:
                exit_locs = df_full.index.searchsorted(trade.exit_time)
                pos_exit_idx = int(exit_locs)
            else:
                pos_exit_idx = len(df_full) - 1

            # ── Update cooldown state ─────────────────────────────────────
            is_loss = trade.r_achieved <= 0
            consec_losses = consec_losses + 1 if is_loss else 0
            session_r += trade.r_achieved
            exit_date = trade.exit_time.date() if trade.exit_time else trade_date
            trades_per_day[exit_date] = trades_per_day.get(exit_date, 0) + 1

            if consec_losses >= CONSEC_LOSS_COOLDOWN_COUNT:
                if trade.exit_time is not None:
                    pause_until = trade.exit_time + pd.Timedelta(minutes=CONSEC_LOSS_COOLDOWN_MINUTES)
                consec_losses = 0

            if session_r <= SYMBOL_SESSION_R_THRESHOLD:
                if trade.exit_time is not None:
                    pause_until = trade.exit_time + pd.Timedelta(hours=SYMBOL_SESSION_PAUSE_HOURS)
                session_r = 0.0

        return results

    # ── Public run method ─────────────────────────────────────────────────────

    def run(self, days: int = BACKTEST_DAYS) -> list[TradeResult]:
        """
        Full backtest over the past `days` days.

        Two-phase approach:
          Phase 1 — Fetch 1m data for a POOL_SIZE candidate pool.
          Phase 2 — Walk-forward per symbol, gated by a dynamic top-N
                     watchlist that is re-ranked every WATCHLIST_REFRESH_HOURS
                     using GAINERS_LOOKBACK_HOURS % change.
        """
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        backtest_start   = now_utc - timedelta(days=days)
        data_fetch_start = backtest_start - timedelta(days=WARMUP_DAYS)

        since_ms = int(data_fetch_start.timestamp() * 1000)
        until_ms = int(now_utc.timestamp() * 1000)
        backtest_start_ts = pd.Timestamp(backtest_start)
        backtest_end_ts   = pd.Timestamp(now_utc)

        log.info("=" * 60)
        log.info("Backtest window  : %s → %s", backtest_start.date(), now_utc.date())
        log.info("Data fetch from  : %s (incl. %dd warmup)", data_fetch_start.date(), WARMUP_DAYS)
        log.info("Watchlist refresh: every %dh  |  pool=%d  |  active=%d",
                 WATCHLIST_REFRESH_HOURS, POOL_SIZE, TOP_N_GAINERS)

        # ── Phase 1: build candidate pool and fetch all data ──────────────
        pool = self.find_historical_gainers(backtest_start, n=POOL_SIZE)
        if not pool:
            log.error("No symbols found — aborting.")
            return []
        log.info("Candidate pool (%d): %s", len(pool), pool)

        pool_dfs:      dict[str, pd.DataFrame] = {}
        df_per_symbol: dict[str, pd.DataFrame] = {}

        for sym in pool:
            log.info("─── Fetching: %s", sym)
            try:
                df_1m = self._fetch_full_1m(sym, since_ms, until_ms)
                if df_1m.empty or len(df_1m) < EMA_PERIOD + 10:
                    log.warning("%s: insufficient data (%d bars)", sym, len(df_1m))
                    continue
                log.info("%s: %d 1m bars", sym, len(df_1m))
                pool_dfs[sym]      = df_1m
                df_per_symbol[sym] = df_1m
            except Exception as exc:
                log.error("Fetch error %s: %s", sym, exc)
            time.sleep(SYMBOL_PAUSE)

        if not pool_dfs:
            log.error("No data fetched — aborting.")
            return []

        # ── Phase 2: compute dynamic windows, then simulate per symbol ────
        window_gainers = self._compute_window_gainers(
            pool_dfs, backtest_start_ts, backtest_end_ts
        )
        log.info("Computed %d 4h windows", len(window_gainers))

        all_results: list[TradeResult] = []

        for sym, df_1m in pool_dfs.items():
            log.info("─── Simulating: %s", sym)
            try:
                sym_results = self._run_symbol(sym, df_1m, backtest_start_ts, window_gainers)
                log.info("%s: %d trades", sym, len(sym_results))
                all_results.extend(sym_results)
            except Exception as exc:
                log.error("Simulate error %s: %s", sym, exc)

        self.print_summary(all_results, backtest_start, now_utc, list(pool_dfs.keys()))
        self.plot_trades(all_results, df_per_symbol)
        return all_results

    # ── Summary printer ───────────────────────────────────────────────────────

    @staticmethod
    def print_summary(
        results: list[TradeResult],
        start: datetime,
        end: datetime,
        symbols: list[str],
    ):
        SEP = "═" * 68

        print(f"\n{SEP}")
        print(f"  BACKTEST RESULTS  |  {start.date()} → {end.date()}  |  {len(symbols)} symbols")
        print(SEP)

        if not results:
            print("  No trades taken.\n")
            return

        # Exclude degenerate SKIP entries from stats
        closed = [r for r in results if r.exit_reason != 'SKIP']
        total   = len(closed)
        if total == 0:
            print("  No valid trades.\n")
            return

        wins    = [r for r in closed if r.r_achieved > 0]
        losses  = [r for r in closed if r.r_achieved <= 0]
        total_r = sum(r.r_achieved for r in closed)
        avg_r   = total_r / total

        gross_win  = sum(r.r_achieved for r in wins)
        gross_loss = abs(sum(r.r_achieved for r in losses))
        pf = (gross_win / gross_loss) if gross_loss else float('inf')

        # ROI — sequential compounding (assumes trades are taken one at a time)
        simple_roi   = total_r * RISK_PER_TRADE_PCT
        compound_bal = 1.0
        for r in sorted(closed, key=lambda x: x.entry_time):
            compound_bal *= (1 + r.r_achieved * RISK_PER_TRADE_PCT / 100)
        compound_roi = (compound_bal - 1) * 100

        # Categorise outcomes (ordered from best to worst)
        exit_order = [
            'ALL_TPS',
            'TP2+EMA_SL', 'TP1_SL',
            'TP1+EMA_SL', 'BE_SL',
            'EMA_SL', 'TIME_SL', 'SL',
            'OPEN',
        ]
        cats: dict[str, list] = {}
        for r in closed:
            cats.setdefault(r.exit_reason, []).append(r)

        print(f"  Total trades     : {total}")
        print(f"  Win rate         : {len(wins)/total*100:.1f}%  "
              f"({len(wins)} wins / {len(losses)} losses)")
        print()
        print("  Outcome breakdown:")
        for label in exit_order:
            bucket = cats.get(label, [])
            if not bucket:
                continue
            bucket_r = sum(b.r_achieved for b in bucket)
            print(f"    {label:<16}: {len(bucket):>3} trades   {bucket_r:+.3f}R")
        for label, bucket in cats.items():   # any unlisted reasons
            if label not in exit_order:
                bucket_r = sum(b.r_achieved for b in bucket)
                print(f"    {label:<16}: {len(bucket):>3} trades   {bucket_r:+.3f}R")
        print()
        print(f"  Total R          : {total_r:+.3f}R")
        print(f"  Avg R / trade    : {avg_r:+.3f}R")
        print(f"  Profit factor    : {pf:.2f}")
        print()
        print(f"  ── ROI  (risk {RISK_PER_TRADE_PCT:.1f}% of balance per trade) ──────────")
        print(f"  Simple ROI       : {simple_roi:+.2f}%  (total R × risk %)")
        print(f"  Compound ROI     : {compound_roi:+.2f}%  (sequential compounding)")
        print()

        # Per-symbol breakdown
        print("  Per-symbol breakdown:")
        sym_groups: dict[str, list[TradeResult]] = {}
        for r in results:
            sym_groups.setdefault(r.symbol, []).append(r)

        for sym, sym_res in sorted(sym_groups.items(), key=lambda x: -sum(r.r_achieved for r in x[1])):
            sym_r = sum(r.r_achieved for r in sym_res)
            sym_wins = sum(1 for r in sym_res if r.r_achieved > 0)
            print(f"    {sym:<24} {len(sym_res):>2} trades | {sym_r:+.3f}R | "
                  f"{sym_wins}/{len(sym_res)} wins")

        print()

        # Per-trade table
        print("  Trade log:")
        header = f"  {'Symbol':<22} {'Trigger':<8} {'Entry Time':<20} "
        header += f"{'Entry':>10} {'SL':>10} {'TP1':>10} {'TP2':>10} {'TP3':>10} "
        header += f"{'Exit':<16} {'R':>7}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in sorted(results, key=lambda x: x.entry_time):
            tps_str = ''.join('✓' if h else '✗' for h in r.tps_hit)
            print(
                f"  {r.symbol:<22} {r.trigger:<8} "
                f"{str(r.entry_time)[:19]:<20} "
                f"{r.entry_price:>10.4f} {r.sl_price:>10.4f} "
                f"{r.tp1:>10.4f} {r.tp2:>10.4f} {r.tp3:>10.4f} "
                f"{r.exit_reason:<16} {r.r_achieved:>+7.3f}R  [{tps_str}]"
            )

        print(f"\n{SEP}\n")


    # ── Chart generation ──────────────────────────────────────────────────────

    def _plot_trade_chart(
        self,
        trade: TradeResult,
        df_full: pd.DataFrame,
    ):
        """
        Render one TradingView-style candlestick chart for a single trade.
        Shows: candles, EMA 200, SL / TP1-3 levels, entry & exit markers,
               risk/reward zone fills, right-side price labels.
        Returns a matplotlib Figure, or None if data is insufficient.
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import mplfinance as mpf

        # Locate entry and exit rows
        entry_loc = int(df_full.index.searchsorted(trade.entry_time))
        if entry_loc >= len(df_full):
            return None
        if trade.exit_time is not None:
            exit_loc = min(int(df_full.index.searchsorted(trade.exit_time)), len(df_full) - 1)
        else:
            exit_loc = len(df_full) - 1

        # Context window: 50 bars pre-entry, 25 bars post-exit
        start    = max(0, entry_loc - 50)
        end      = min(len(df_full), exit_loc + 26)
        df_plot  = df_full.iloc[start:end].copy()
        if len(df_plot) < 10:
            return None

        entry_pos = entry_loc - start
        exit_pos  = min(exit_loc - start, len(df_plot) - 1)

        # ── Addplots ──────────────────────────────────────────────────────────

        def _flat(price: float, color: str, ls: str = '--', lw: float = 0.9):
            return mpf.make_addplot(
                pd.Series(price, index=df_plot.index),
                color=color, linestyle=ls, width=lw,
            )

        # Entry marker: green triangle below the entry bar's low
        entry_mark = pd.Series(float('nan'), index=df_plot.index, dtype=float)
        if 0 <= entry_pos < len(df_plot):
            entry_mark.iloc[entry_pos] = float(df_plot['low'].iloc[entry_pos]) * 0.9985

        # Exit marker: triangle above the exit bar's high (color = profit/loss)
        exit_mark  = pd.Series(float('nan'), index=df_plot.index, dtype=float)
        exit_color = '#26a69a' if trade.r_achieved > 0 else '#ef5350'
        if 0 <= exit_pos < len(df_plot):
            exit_mark.iloc[exit_pos] = float(df_plot['high'].iloc[exit_pos]) * 1.0015

        addplots = [
            mpf.make_addplot(df_plot[EMA_COL], color='#f0a500', width=1.8, label='EMA 200'),
            _flat(trade.sl_price, '#ef5350'),
            _flat(trade.tp1, '#00e676'),
            _flat(trade.tp2, '#00bcd4'),
            _flat(trade.tp3, '#ab47bc'),
            mpf.make_addplot(entry_mark, type='scatter', markersize=120,
                             marker='^', color='#26a69a'),
            mpf.make_addplot(exit_mark,  type='scatter', markersize=120,
                             marker='v', color=exit_color),
        ]

        # ── TradingView dark style ─────────────────────────────────────────────
        mc = mpf.make_marketcolors(
            up='#26a69a', down='#ef5350',
            wick={'up': '#26a69a', 'down': '#ef5350'},
            edge='inherit',
            volume={'up': '#26a69a', 'down': '#ef5350'},
        )
        style = mpf.make_mpf_style(
            base_mpf_style='nightclouds',
            marketcolors=mc,
            facecolor='#131722',
            figcolor='#131722',
            gridstyle='--',
            gridcolor='#2a2e3955',
            rc={
                'axes.labelcolor': '#d1d4dc',
                'xtick.color':     '#787b86',
                'ytick.color':     '#787b86',
                'text.color':      '#d1d4dc',
            },
        )

        # ── Title ─────────────────────────────────────────────────────────────
        tps_str = ''.join('✓' if h else '✗' for h in trade.tps_hit)
        sign    = '+' if trade.r_achieved >= 0 else ''
        title   = (
            f"{trade.symbol}  ·  {trade.trigger}  ·  "
            f"{str(trade.entry_time)[:16]}  ·  "
            f"Entry {trade.entry_price:.6g}  ·  "
            f"{trade.exit_reason} ({sign}{trade.r_achieved:.3f}R)  [{tps_str}]"
        )

        fig, axes = mpf.plot(
            df_plot,
            type='candle',
            style=style,
            title='\n' + title,
            addplot=addplots,
            volume=True,
            returnfig=True,
            figsize=(16, 9),
        )

        ax = axes[0]   # main price axes (volume is axes[2])

        # ── Post-render: zone fills + right-side price labels ─────────────────
        y_lo, y_hi = ax.get_ylim()

        def _label(price: float, text: str, color: str):
            if y_lo <= price <= y_hi:
                ax.annotate(
                    text, xy=(1.0, price),
                    xycoords=('axes fraction', 'data'),
                    xytext=(4, 0), textcoords='offset points',
                    fontsize=7.5, color=color, va='center',
                )

        _label(trade.sl_price, f'SL  {trade.sl_price:.6g}', '#ef5350')
        _label(trade.tp1, f'TP1 {trade.tp1:.6g}{"  ✓" if trade.tps_hit[0] else ""}', '#00e676')
        _label(trade.tp2, f'TP2 {trade.tp2:.6g}{"  ✓" if trade.tps_hit[1] else ""}', '#00bcd4')
        _label(trade.tp3, f'TP3 {trade.tp3:.6g}{"  ✓" if trade.tps_hit[2] else ""}', '#ab47bc')

        # Risk zone (SL → entry): red fill
        ax.axhspan(trade.sl_price, trade.entry_price, alpha=0.07, color='#ef5350', zorder=0)
        # Reward zone (entry → TP1): green fill
        ax.axhspan(trade.entry_price, trade.tp1,      alpha=0.07, color='#26a69a', zorder=0)

        # Vertical dotted lines at entry and exit
        for xpos, color in [(entry_pos, '#26a69a'), (exit_pos, exit_color)]:
            if 0 <= xpos < len(df_plot):
                ax.axvline(x=xpos, color=color, linewidth=0.7, linestyle=':', alpha=0.7)

        fig.patch.set_facecolor('#131722')
        for a in axes:
            if a is not None:
                a.set_facecolor('#131722')

        return fig

    def plot_trades(
        self,
        results: list[TradeResult],
        df_per_symbol: dict[str, pd.DataFrame],
        out_dir: str = 'charts',
    ):
        """
        For every trade in results, draw a candlestick chart and save to
        out_dir/<SYMBOL>.pdf — one page per trade, grouped by symbol.
        """
        import os
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages

        if not results:
            return

        os.makedirs(out_dir, exist_ok=True)

        # Add EMA 200 to each symbol's DataFrame
        dfs_ema: dict[str, pd.DataFrame] = {
            sym: self._add_ema(df) for sym, df in df_per_symbol.items()
        }

        # Group trades by symbol, preserving entry-time order
        by_sym: dict[str, list[TradeResult]] = {}
        for r in results:
            if r.exit_reason != 'SKIP':
                by_sym.setdefault(r.symbol, []).append(r)

        total_charts = 0
        for sym, trades in sorted(by_sym.items()):
            df_full = dfs_ema.get(sym)
            if df_full is None:
                continue

            trades_sorted = sorted(trades, key=lambda t: t.entry_time)
            pdf_path = os.path.join(out_dir, f"{sym.replace('/', '_')}.pdf")

            with PdfPages(pdf_path) as pdf:
                # Summary page
                fig_s, ax_s = plt.subplots(figsize=(16, 4), facecolor='#131722')
                ax_s.set_facecolor('#131722')
                ax_s.axis('off')
                sym_r     = sum(t.r_achieved for t in trades_sorted)
                sym_wins  = sum(1 for t in trades_sorted if t.r_achieved > 0)
                header    = (
                    f"{sym}  |  {len(trades_sorted)} trades  |  "
                    f"Win rate {sym_wins/len(trades_sorted)*100:.0f}%  |  "
                    f"Total R {sym_r:+.3f}R"
                )
                ax_s.text(0.5, 0.7, header, ha='center', va='center',
                          fontsize=13, color='#d1d4dc', transform=ax_s.transAxes)
                rows = [
                    [str(t.entry_time)[:16], t.trigger, f"{t.entry_price:.6g}",
                     f"{t.sl_price:.6g}", t.exit_reason, f"{t.r_achieved:+.3f}R",
                     ''.join('✓' if h else '✗' for h in t.tps_hit)]
                    for t in trades_sorted
                ]
                cols = ['Entry Time', 'Trigger', 'Entry', 'SL', 'Exit Reason', 'R', 'TPs']
                tbl = ax_s.table(cellText=rows, colLabels=cols, loc='center',
                                 cellLoc='center', bbox=[0, 0, 1, 0.6])
                tbl.auto_set_font_size(False)
                tbl.set_fontsize(7)
                for (row, col), cell in tbl.get_celld().items():
                    cell.set_facecolor('#1e222d' if row % 2 == 0 else '#131722')
                    cell.set_text_props(color='#d1d4dc')
                    cell.set_edgecolor('#2a2e39')
                pdf.savefig(fig_s, bbox_inches='tight', facecolor='#131722')
                plt.close(fig_s)

                # One page per trade
                for trade in trades_sorted:
                    try:
                        fig = self._plot_trade_chart(trade, df_full)
                        if fig is not None:
                            pdf.savefig(fig, bbox_inches='tight', facecolor='#131722')
                            plt.close(fig)
                            total_charts += 1
                    except Exception as exc:
                        log.warning("Chart failed for %s @ %s: %s",
                                    sym, trade.entry_time, exc)

            log.info("Saved %d-trade PDF → %s", len(trades_sorted), pdf_path)

        print(f"\n  Charts saved to ./{out_dir}/  "
              f"({total_charts} charts across {len(by_sym)} symbols)\n")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    bt = Backtester()
    results = bt.run(days=7)
