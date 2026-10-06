"""
Bot 3 – Order Executor (Binance USDT-M Futures, LONG only)
-----------------------------------------------------------
Risk management:
  • Entry        : MARKET buy
  • Initial SL   : STOP_MARKET below EMA 200 (hard backstop + sizing reference)
  • Trailing SL  : After TP1 → SL moves to entry (break-even)
                   After TP2 → SL moves to TP1
  • EMA-close SL : Exit if SL_CONSEC_CLOSES consecutive 1m closes below EMA 200,
                   or SL_TOTAL_CLOSES total closes below EMA 200 in last
                   SL_CLOSES_WINDOW bars — whichever fires first.
  • Take profits : Three higher Fibonacci extension levels (partial exits)
      TP1 @ Fib 1.618 — closes FIB_TP_SPLITS[0] of position
      TP2 @ Fib 2.618 — closes FIB_TP_SPLITS[1] of position
      TP3 @ Fib 4.236 — closes remaining position
  • Position size: sized so the SL represents RISK_PER_TRADE_PCT % of balance
  • Leverage     : LEVERAGE × isolated margin (set per symbol before entry)
  • Max trades   : MAX_OPEN_POSITIONS concurrent positions

Call manage_positions() each scan cycle from main.py.
"""

import time
from typing import Optional
from utils import get_exchange, fetch_ohlcv_df, setup_logging
from bot2_signals import Signal
from config import (
    RISK_PER_TRADE_PCT, SL_BUFFER_PCT,
    SL_SWING_LOW_BUFFER,
    LEVERAGE, MAX_OPEN_POSITIONS, MAX_POSITION_NOTIONAL_PCT,
    FIB_TP_LEVELS, FIB_TP_SPLITS,
    SL_CONSEC_CLOSES, SL_TOTAL_CLOSES, SL_CLOSES_WINDOW,
    CANDLES_LIMIT, EMA_PERIOD,
)

log = setup_logging('Bot3-Executor')
EMA_COL = f'ema{EMA_PERIOD}'


class ExecutorBot:
    def __init__(self, signal_bot=None):
        self.exchange = get_exchange()
        self._signal_bot = signal_bot   # bot2_signals.SignalBot — for cooldown feedback
        self._open_symbols: set[str] = set()
        # Per-symbol trailing SL state, keyed by symbol:
        # { entry_price, tp1_price, tp2_price,
        #   sl_order_id, tp1_order_id, tp2_order_id,
        #   sl_level: 'initial' | 'entry' | 'tp1' }
        self._position_state: dict[str, dict] = {}

    # ── account helpers ────────────────────────────────────────────────────

    def get_free_balance(self) -> float:
        bal = self.exchange.fetch_balance({'type': 'future'})
        return float((bal.get('USDT') or {}).get('free', 0))

    def get_open_positions(self) -> list[dict]:
        return [
            p for p in self.exchange.fetch_positions()
            if abs(float(p.get('contracts') or 0)) > 0
        ]

    def _refresh_open_symbols(self):
        self._open_symbols = {p['symbol'] for p in self.get_open_positions()}

    # ── precision helpers ──────────────────────────────────────────────────

    def _round_price(self, symbol: str, price: float) -> float:
        prec = (self.exchange.market(symbol).get('precision') or {}).get('price', 4)
        return float(self.exchange.decimal_to_precision(price, rounding_mode=2, precision=prec))

    def _round_qty(self, symbol: str, qty: float) -> float:
        prec = (self.exchange.market(symbol).get('precision') or {}).get('amount', 3)
        return float(self.exchange.decimal_to_precision(qty, rounding_mode=2, precision=prec))

    # ── levels ─────────────────────────────────────────────────────────────

    def _sl_price(self, signal: Signal) -> float:
        if signal.swing_low > 0:
            sl = signal.swing_low * (1 - SL_SWING_LOW_BUFFER)
        else:
            sl = signal.ema200_1m * (1 - SL_BUFFER_PCT)
        return self._round_price(signal.symbol, sl)

    def _fib_tp_prices(self, entry: float, sl: float, symbol: str) -> list[float]:
        risk_leg = entry - sl
        return [
            self._round_price(symbol, entry + risk_leg * ratio)
            for ratio in FIB_TP_LEVELS
        ]

    # ── position sizing ────────────────────────────────────────────────────

    def _calculate_qty(
        self, symbol: str, entry: float, sl: float, balance: float
    ) -> Optional[float]:
        sl_dist = entry - sl
        if sl_dist <= 0:
            log.error("%s: SL is above entry — skipping.", symbol)
            return None

        risk_usdt = balance * (RISK_PER_TRADE_PCT / 100)
        qty = risk_usdt / sl_dist

        max_notional = balance * LEVERAGE * MAX_POSITION_NOTIONAL_PCT
        qty = min(qty, max_notional / entry)
        qty = self._round_qty(symbol, qty)

        if qty <= 0:
            log.error("%s: qty rounded to 0 — skipping.", symbol)
            return None

        log.info(
            "%s  balance=%.2f USDT  risk=$%.2f  SL_dist=%.5f  qty=%.5f",
            symbol, balance, risk_usdt, sl_dist, qty,
        )
        return qty

    def _split_qty(self, total: float, symbol: str) -> list[float]:
        parts: list[float] = []
        allocated = 0.0
        for i, frac in enumerate(FIB_TP_SPLITS):
            if i == len(FIB_TP_SPLITS) - 1:
                part = self._round_qty(symbol, total - allocated)
            else:
                part = self._round_qty(symbol, total * frac)
                allocated += part
            parts.append(part)
        return parts

    # ── order helpers ──────────────────────────────────────────────────────

    def _set_leverage(self, symbol: str):
        try:
            self.exchange.set_leverage(LEVERAGE, symbol)
            self.exchange.set_margin_mode('isolated', symbol)
        except Exception as exc:
            log.warning("Leverage/margin setup for %s: %s", symbol, exc)

    def _place_entry(self, symbol: str, qty: float) -> dict:
        log.info("ENTRY  BUY MARKET  %s  qty=%.5f", symbol, qty)
        return self.exchange.create_order(symbol, 'market', 'buy', qty)

    def _place_sl(self, symbol: str, sl_price: float) -> dict:
        log.info("SL     STOP_MARKET  %s  stop=%.6g", symbol, sl_price)
        return self.exchange.create_order(
            symbol, 'STOP_MARKET', 'sell', None, None,
            {
                'stopPrice': sl_price,
                'closePosition': True,
                'workingType': 'MARK_PRICE',
                'reduceOnly': True,
            }
        )

    def _place_tp_partial(
        self, symbol: str, qty: float, tp_price: float, label: str
    ) -> dict:
        log.info("TP %s  TAKE_PROFIT_MARKET  %s  stop=%.6g  qty=%.5f",
                 label, symbol, tp_price, qty)
        return self.exchange.create_order(
            symbol, 'TAKE_PROFIT_MARKET', 'sell', qty, None,
            {
                'stopPrice': tp_price,
                'workingType': 'MARK_PRICE',
                'reduceOnly': True,
            }
        )

    # ── main execute ───────────────────────────────────────────────────────

    def execute_trade(self, signal: Signal) -> bool:
        symbol = signal.symbol

        self._refresh_open_symbols()
        if symbol in self._open_symbols:
            log.info("%s: already in a position — skip.", symbol)
            return False
        if len(self._open_symbols) >= MAX_OPEN_POSITIONS:
            log.info("Max positions (%d) reached — skip %s.", MAX_OPEN_POSITIONS, symbol)
            return False

        balance = self.get_free_balance()
        if balance < 10:
            log.error("Insufficient balance: %.2f USDT", balance)
            return False

        entry = signal.entry_price
        sl    = self._sl_price(signal)
        tps   = self._fib_tp_prices(entry, sl, symbol)
        qty   = self._calculate_qty(symbol, entry, sl, balance)
        if qty is None:
            return False

        parts = self._split_qty(qty, symbol)

        log.info(
            "TRADE PLAN  %s\n"
            "  Entry      ≈ %.6g\n"
            "  Initial SL   %.6g  (%.2f%% below EMA 200)\n"
            "  TP1 [Fib %.3f]  %.6g  qty=%.5f  → SL trails to entry\n"
            "  TP2 [Fib %.3f]  %.6g  qty=%.5f  → SL trails to TP1\n"
            "  TP3 [Fib %.3f]  %.6g  qty=%.5f\n"
            "  Risk: %.1f%% of balance ($%.2f)",
            symbol, entry,
            sl, SL_BUFFER_PCT * 100,
            FIB_TP_LEVELS[0], tps[0], parts[0],
            FIB_TP_LEVELS[1], tps[1], parts[1],
            FIB_TP_LEVELS[2], tps[2], parts[2],
            RISK_PER_TRADE_PCT, balance * RISK_PER_TRADE_PCT / 100,
        )

        try:
            self._set_leverage(symbol)

            entry_order = self._place_entry(symbol, qty)
            log.info("Entry order: %s", entry_order.get('id'))

            time.sleep(1)

            sl_order = self._place_sl(symbol, sl)
            log.info("SL order: %s", sl_order.get('id'))

            fib_labels = ['1.618', '2.618', '4.236']
            tp_order_ids: list[Optional[str]] = []
            for tp_price, qty_part, label in zip(tps, parts, fib_labels):
                tp_order = self._place_tp_partial(symbol, qty_part, tp_price, label)
                tp_order_ids.append(tp_order.get('id'))
                log.info("TP%s order: %s", label, tp_order.get('id'))

            self._position_state[symbol] = {
                'entry_price':  entry,
                'tp1_price':    tps[0],
                'tp2_price':    tps[1],
                'sl_order_id':  sl_order.get('id'),
                'tp1_order_id': tp_order_ids[0],
                'tp2_order_id': tp_order_ids[1],
                'sl_level':     'initial',
            }
            self._open_symbols.add(symbol)
            log.info("Position open  %s  InitialSL=%.6g  TPs=%s", symbol, sl, tps)
            return True

        except Exception as exc:
            log.error("Order placement failed for %s: %s", symbol, exc)
            self._emergency_close(symbol)
            return False

    # ── position management (called every scan cycle from main.py) ─────────

    def manage_positions(self):
        """
        For each open position:
          1. Detect TP fills and trail the SL up accordingly.
          2. Check EMA-close SL condition; force-close if triggered.
          3. Print live position status.
        """
        current_positions = {p['symbol']: p for p in self.get_open_positions()}

        # Clean up symbols that closed since last cycle
        for sym in list(self._open_symbols):
            if sym not in current_positions:
                log.info("%s: position closed.", sym)
                state = self._position_state.get(sym)
                if state and self._signal_bot is not None:
                    # Approximate R from trailing SL level reached before close
                    sl_level = state.get('sl_level', 'initial')
                    approx_r = {'initial': -1.0, 'entry': 0.534, 'tp1': 1.948}.get(sl_level, -1.0)
                    self._signal_bot.record_exit(sym, approx_r)
                self._open_symbols.discard(sym)
                self._position_state.pop(sym, None)

        if not current_positions:
            log.info("No open positions.")
            return

        for symbol, pos in current_positions.items():
            state = self._position_state.get(symbol)
            if state is None:
                log.warning("%s: untracked position — skipping SL management.", symbol)
                self._open_symbols.add(symbol)
                continue

            try:
                self._update_trailing_sl(symbol, state)
            except Exception as exc:
                log.error("Trailing SL update failed for %s: %s", symbol, exc)

            try:
                if self._check_ema_sl(symbol, state):
                    continue   # position closed; skip status print
            except Exception as exc:
                log.error("EMA SL check failed for %s: %s", symbol, exc)

            log.info(
                "Open: %-20s  side=%-5s  size=%s  entry=%.6g  PnL=%.2f USDT  SL=%s",
                symbol,
                pos.get('side'),
                pos.get('contracts'),
                float(pos.get('entryPrice') or 0),
                float(pos.get('unrealizedPnl') or 0),
                state.get('sl_level', '?'),
            )

    def _update_trailing_sl(self, symbol: str, state: dict):
        """
        Move SL to entry after TP1 fills; move SL to TP1 after TP2 fills.
        Cancels the old STOP_MARKET and places a new one at the higher price.
        """
        if state.get('sl_level') == 'tp1':
            return  # already at maximum trail

        try:
            open_ids = {o['id'] for o in self.exchange.fetch_open_orders(symbol)}
        except Exception as exc:
            log.warning("fetch_open_orders for %s: %s", symbol, exc)
            return

        tp1_filled = (
            state.get('tp1_order_id') is not None
            and state['tp1_order_id'] not in open_ids
        )
        tp2_filled = (
            state.get('tp2_order_id') is not None
            and state['tp2_order_id'] not in open_ids
        )

        new_sl_price: Optional[float] = None
        new_level: Optional[str] = None

        if tp2_filled and state.get('sl_level') != 'tp1':
            new_sl_price = state['tp1_price']
            new_level    = 'tp1'
        elif tp1_filled and state.get('sl_level') == 'initial':
            new_sl_price = state['entry_price']
            new_level    = 'entry'

        if new_sl_price is None:
            return

        if state.get('sl_order_id'):
            try:
                self.exchange.cancel_order(state['sl_order_id'], symbol)
            except Exception as exc:
                log.warning("Cancel SL for %s: %s", symbol, exc)

        sl_order = self._place_sl(symbol, new_sl_price)
        state['sl_order_id'] = sl_order.get('id')
        state['sl_level']    = new_level
        log.info("%s: SL trailed to %s (%.6g)", symbol, new_level, new_sl_price)

    def _check_ema_sl(self, symbol: str, state: dict) -> bool:
        """
        Returns True and closes the position if the EMA-close SL fires:
          - SL_CONSEC_CLOSES consecutive 1m bars close below EMA 200, OR
          - SL_TOTAL_CLOSES bars in the last SL_CLOSES_WINDOW close below EMA 200.
        """
        df = fetch_ohlcv_df(self.exchange, symbol, '1m', CANDLES_LIMIT)
        if df is None or len(df) < SL_CLOSES_WINDOW + 1:
            return False

        recent = df.tail(SL_CLOSES_WINDOW)
        below  = (recent['close'] < recent[EMA_COL]).tolist()

        consec = 0
        for flag in reversed(below):
            if flag:
                consec += 1
            else:
                break

        if consec < SL_CONSEC_CLOSES and sum(below) < SL_TOTAL_CLOSES:
            return False

        log.info(
            "%s: EMA-close SL — consec=%d, window=%d/%d — force-closing.",
            symbol, consec, sum(below), SL_CLOSES_WINDOW,
        )
        self._emergency_close(symbol)
        self._open_symbols.discard(symbol)
        self._position_state.pop(symbol, None)
        return True

    # ── utilities ──────────────────────────────────────────────────────────

    def _emergency_close(self, symbol: str):
        try:
            self.exchange.cancel_all_orders(symbol)
            for pos in self.get_open_positions():
                if pos['symbol'] != symbol:
                    continue
                size = float(pos.get('contracts') or 0)
                if size == 0:
                    continue
                side = 'sell' if size > 0 else 'buy'
                self.exchange.create_order(
                    symbol, 'market', side, abs(size), None, {'reduceOnly': True}
                )
                log.info("Emergency close executed for %s", symbol)
        except Exception as exc:
            log.error("Emergency close failed for %s: %s", symbol, exc)
