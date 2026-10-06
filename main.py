"""
Main Orchestrator
-----------------
Loop:
  1. Every GAINERS_REFRESH_MINUTES → Bot 1 rebuilds the top-gainers watchlist
  2. Every SCAN_INTERVAL_SECONDS   → Bot 2 scans for LONG EMA-200 signals
  3. For each confirmed signal      → Bot 3 executes LONG with Fibonacci TPs + SL

Run:  python main.py
"""

import time
import sys
import signal as _signal
from datetime import datetime, timedelta

from utils import setup_logging
from bot1_gainers import GainersBot
from bot2_signals import SignalBot
from bot3_executor import ExecutorBot
from config import SCAN_INTERVAL_SECONDS, GAINERS_REFRESH_MINUTES

log = setup_logging('Main')

# ── graceful shutdown ──────────────────────────────────────────────────────
_running = True

def _handle_shutdown(sig, frame):
    global _running
    log.info("Shutdown signal received — stopping after current cycle.")
    _running = False

_signal.signal(_signal.SIGINT, _handle_shutdown)
_signal.signal(_signal.SIGTERM, _handle_shutdown)


def main():
    log.info("=" * 60)
    log.info("Binance Futures EMA-200 Trading Bot  |  Starting up")
    log.info("=" * 60)

    bot1 = GainersBot()
    bot2 = SignalBot()
    bot3 = ExecutorBot(signal_bot=bot2)

    watchlist: list[str] = []
    next_gainers_refresh = datetime.utcnow()   # refresh immediately on start

    while _running:
        now = datetime.utcnow()

        # ── Bot 1: refresh watchlist ────────────────────────────────────
        if now >= next_gainers_refresh:
            log.info("─── Refreshing gainers watchlist ───")
            try:
                watchlist = bot1.get_combined_gainers()
                log.info("Watchlist (%d symbols): %s", len(watchlist), watchlist)
            except Exception as exc:
                log.error("Gainers refresh failed: %s", exc)
            next_gainers_refresh = now + timedelta(minutes=GAINERS_REFRESH_MINUTES)

        if not watchlist:
            log.warning("Watchlist is empty — waiting for next refresh.")
            time.sleep(SCAN_INTERVAL_SECONDS)
            continue

        # ── Bot 2: scan for signals ─────────────────────────────────────
        log.info("─── Scanning %d symbols for EMA-200 signals ───", len(watchlist))
        try:
            signals = bot2.scan_watchlist(watchlist)
        except Exception as exc:
            log.error("Signal scan failed: %s", exc)
            signals = []

        # ── Bot 3: execute trades ───────────────────────────────────────
        if signals:
            log.info("Found %d signal(s) this cycle.", len(signals))
            for sig in signals:
                if not _running:
                    break
                try:
                    bot3.execute_trade(sig)
                except Exception as exc:
                    log.error("Executor error for %s: %s", sig.symbol, exc)
        else:
            log.info("No signals found this cycle.")

        # ── trailing SL management + status snapshot ────────────────────
        try:
            bot3.manage_positions()
        except Exception:
            pass

        log.info("Sleeping %ds until next scan …\n", SCAN_INTERVAL_SECONDS)
        time.sleep(SCAN_INTERVAL_SECONDS)

    log.info("Bot stopped cleanly.")
    sys.exit(0)


if __name__ == '__main__':
    main()
