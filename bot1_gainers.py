"""
Bot 1 – Top 10 Futures Gainers
--------------------------------
Scans all USDT-M perpetual futures on Binance and returns the top 10
gainers for:
  • today   – rolling 24-h percentage change (live ticker)
  • yesterday – yesterday's daily candle open→close change
"""

import pandas as pd
from utils import get_exchange, setup_logging
from config import TOP_N_GAINERS, MIN_VOLUME_USDT, CANDLES_LIMIT

log = setup_logging('Bot1-Gainers')


class GainersBot:
    def __init__(self):
        self.exchange = get_exchange()

    # ── helpers ────────────────────────────────────────────────────────────

    def _usdt_perp_symbols(self) -> list[str]:
        markets = self.exchange.load_markets()
        return [
            s for s, m in markets.items()
            if m.get('quote') == 'USDT'
            and m.get('settle') == 'USDT'
            and m.get('type') == 'swap'
            and m.get('active', False)
        ]

    # ── public methods ─────────────────────────────────────────────────────

    def get_top_gainers_today(self) -> pd.DataFrame:
        """Top N gainers from the rolling 24-h ticker."""
        log.info("Fetching 24-h tickers for today's gainers …")
        tickers = self.exchange.fetch_tickers()

        rows = []
        for symbol, t in tickers.items():
            pct = t.get('percentage') or 0.0
            vol = t.get('quoteVolume') or 0.0
            if vol < MIN_VOLUME_USDT:
                continue
            rows.append({
                'symbol': symbol,
                'price': t.get('last', 0),
                'change_24h_pct': round(pct, 3),
                'volume_usdt': round(vol),
            })

        df = pd.DataFrame(rows)
        if df.empty:
            log.warning("No ticker data returned.")
            return df

        top = (
            df[df['change_24h_pct'] > 0]
            .nlargest(TOP_N_GAINERS, 'change_24h_pct')
            .reset_index(drop=True)
        )
        log.info("Today's top %d gainers:\n%s", TOP_N_GAINERS, top.to_string())
        return top

    def get_top_gainers_yesterday(self) -> pd.DataFrame:
        """
        Top N gainers by yesterday's daily candle (open→close).
        Fetches daily OHLCV for every high-volume symbol.
        """
        log.info("Fetching yesterday's daily candles …")

        # First grab volume-filtered symbols from tickers to reduce API calls
        tickers = self.exchange.fetch_tickers()
        liquid_symbols = [
            s for s, t in tickers.items()
            if (t.get('quoteVolume') or 0) >= MIN_VOLUME_USDT
        ]
        log.info("Scanning %d liquid symbols for yesterday's change.", len(liquid_symbols))

        rows = []
        for symbol in liquid_symbols:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, '1d', limit=3)
                if len(ohlcv) < 2:
                    continue
                # index -2 is the completed yesterday candle
                _, o, _, _, c, v = ohlcv[-2]
                if o and o > 0:
                    pct = (c - o) / o * 100
                    rows.append({
                        'symbol': symbol,
                        'yesterday_open': o,
                        'yesterday_close': c,
                        'change_pct': round(pct, 3),
                        'volume_usdt': round(v * c),
                    })
            except Exception as exc:
                log.debug("Skip %s: %s", symbol, exc)

        df = pd.DataFrame(rows)
        if df.empty:
            log.warning("No yesterday candle data.")
            return df

        top = (
            df[df['change_pct'] > 0]
            .nlargest(TOP_N_GAINERS, 'change_pct')
            .reset_index(drop=True)
        )
        log.info("Yesterday's top %d gainers:\n%s", TOP_N_GAINERS, top.to_string())
        return top

    def get_combined_gainers(self) -> list[str]:
        """
        Returns a deduplicated list of symbols that appear in either
        today's or yesterday's top-N gainers.
        """
        today_df = self.get_top_gainers_today()
        yest_df = self.get_top_gainers_yesterday()

        today_syms = set(today_df['symbol'].tolist()) if not today_df.empty else set()
        yest_syms = set(yest_df['symbol'].tolist()) if not yest_df.empty else set()

        combined = list(today_syms | yest_syms)
        log.info("Combined watchlist (%d symbols): %s", len(combined), combined)
        return combined


if __name__ == '__main__':
    bot = GainersBot()
    bot.get_combined_gainers()
