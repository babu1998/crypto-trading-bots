import logging
import ccxt
import pandas as pd
from config import BINANCE_API_KEY, BINANCE_API_SECRET, TESTNET


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-18s  %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)


def get_exchange() -> ccxt.binanceusdm:
    params = {
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'options': {'defaultType': 'future'},
        'enableRateLimit': True,
    }
    exchange = ccxt.binanceusdm(params)
    if TESTNET:
        exchange.set_sandbox_mode(True)
    return exchange


def fetch_ohlcv_df(exchange: ccxt.binanceusdm, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """Fetch OHLCV and return a tidy DataFrame with ema_{EMA_PERIOD} column."""
    from config import EMA_PERIOD
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('ts', inplace=True)
    df[f'ema{EMA_PERIOD}'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df


def normalize_symbol(raw: str) -> str:
    """Ensure symbol is in ccxt USDM format: BTC/USDT:USDT"""
    if ':' not in raw:
        base = raw.replace('USDT', '').replace('/', '')
        return f"{base}/USDT:USDT"
    return raw
