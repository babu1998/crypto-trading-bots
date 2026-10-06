import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')
TESTNET = os.getenv('TESTNET', 'true').lower() == 'true'

# ── Bot 1: Gainers scanner ─────────────────────────────────────────────────
TOP_N_GAINERS = 10
MIN_VOLUME_USDT = 5_000_000      # Ignore coins with <$5M 24h volume

# ── Bot 2: EMA 200 signal ──────────────────────────────────────────────────
EMA_PERIOD = 200
TIMEFRAMES = ['1m', '3m', '15m']  # order: trigger, filter, confirm
CANDLES_LIMIT = 250               # must be > EMA_PERIOD
# Price within this % of EMA counts as a "touch"
EMA_TOUCH_BUFFER_PCT = 0.002      # 0.2%

# Signal quality filters
MIN_TRIGGER_VOLUME_RATIO = 0.8    # trigger candle volume ≥ 80% of 20-bar average
EMA_SLOPE_LOOKBACK       = 5      # bars back to measure EMA slope for BOUNCE filter

# ── Bot 3: Risk / order execution ─────────────────────────────────────────
RISK_PER_TRADE_PCT = 1.0          # % of account risked per trade
SL_BUFFER_PCT = 0.003             # SL placed 0.3% below EMA 200 (LONG only)
LEVERAGE = 10
MAX_OPEN_POSITIONS = 3
MAX_POSITION_NOTIONAL_PCT = 0.20  # Cap: position notional ≤ 20% of equity

# Fibonacci extension levels for take profits (LONG direction, from entry)
# TP price = entry + (entry - SL) * fib_ratio
FIB_TP_LEVELS = [1.618, 2.618, 4.236]  # Higher Fib extension ratios
FIB_TP_SPLITS = [0.33, 0.33, 0.34]    # Fraction of position to close at each level

# ── EMA-close stop-loss conditions ─────────────────────────────────────────
SL_CONSEC_CLOSES = 3          # exit on N consecutive 1m closes below EMA 200 (was 2)
SL_TOTAL_CLOSES  = 4          # OR N total 1m closes below EMA 200 in window (was 3)
SL_CLOSES_WINDOW = 7          # look-back bars for total-closes check (was 5)

# New EMA SL methods (combinable with the counter above)
SL_DUAL_TF_ENABLED     = True   # a 1m bar only counts as "below EMA" when 3m is also bearish
SL_VOL_CONFIRM_ENABLED = False  # only count breakdown bars with above-average volume
SL_VOL_CONFIRM_RATIO   = 1.2    # volume must be >= N × 20-bar rolling average
SL_TIME_STOP_BARS      = 0      # 0 = disabled; exit at market after N bars with no TP1 hit

# ── Entry filters ─────────────────────────────────────────────────────────────
ATR_PERIOD             = 14     # period for Average True Range
MIN_DEVIATION_BARS     = 10     # bars to look back for prior deviation from EMA
MIN_DEVIATION_ATR_MULT = 1.0    # prior deviation must be ≥ N × ATR(14) — scales to each coin's volatility

# ── Structural SL: below swing low of the pullback ────────────────────────────
SL_SWING_LOW_BARS   = 10       # bars to scan for swing low
SL_SWING_LOW_BUFFER = 0.001    # SL placed 0.1% below identified swing low

# ── 3m EMA200 proximity skip ───────────────────────────────────────────────────
SKIP_3M_EMA_TOUCH   = True     # skip entry when 3m bar is also bouncing/crossing 3m EMA200

# ── Overtrading prevention ─────────────────────────────────────────────────
CONSEC_LOSS_COOLDOWN_COUNT   = 3     # pause symbol after N consecutive losses
CONSEC_LOSS_COOLDOWN_MINUTES = 30    # cooldown duration in minutes
MIN_REENTRY_MINUTES          = 5     # minimum gap between any two entries on same symbol (live)
MIN_REENTRY_CANDLES          = 5     # minimum 1m bars between entries (backtest)
MAX_TRADES_PER_SYMBOL_PER_DAY = 10   # hard daily cap per symbol
SYMBOL_SESSION_R_THRESHOLD   = -2.0  # pause symbol if session R drops below this (was -3.0)
SYMBOL_SESSION_PAUSE_HOURS   = 4     # hours to pause after threshold is hit

# ── Timing ─────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 60
GAINERS_REFRESH_MINUTES = 60

# ── Bot 4: Backtest dynamic watchlist ─────────────────────────────────────
POOL_SIZE = 20                   # candidate pool fetched upfront (wider than TOP_N_GAINERS)
WATCHLIST_REFRESH_HOURS = 4      # re-rank active top-N every N hours
GAINERS_LOOKBACK_HOURS = 24      # lookback window for % change when re-ranking
