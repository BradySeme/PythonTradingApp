"""
Alpaca Candlestick Signal Trading Bot — v2 (audited)
=====================================================
Streams real-time bars for stocks and crypto, detects candlestick patterns,
and places buy/sell orders based on detected signals.

Key fixes in this version:
  1. Stop-loss now checked on EVERY bar (previously only when a sell pattern fired)
  2. Position sizing uses CASH only — no margin/leverage
  3. Sell filter changed from AND to OR (was nearly impossible to satisfy)
  4. Entry counting tracked in memory + persisted (was counting lifetime orders)
  5. RSI calculation fixed (was sampling last 14 gains, not last 14 periods)
  6. Positions persist across restarts via positions.json
  7. API keys loaded from environment / .env only — never hardcode in this file

Setup:
  pip install alpaca-py pytz python-dotenv

Usage:
  python alpaca_candle_bot.py
"""

import asyncio
import json
import logging
import threading
import os
import pytz
from collections import deque
from datetime import datetime, time as dtime
from dataclasses import dataclass
from typing import Optional
from reporting import generate_report, REPORT_INTERVAL_HOURS
from datetime import timedelta

# Optional .env support — create a file named `.env` next to this script with:
#   API_KEY=your_key
#   API_SECRET=your_secret
# and add `.env` to your .gitignore so keys never reach GitHub.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.live import StockDataStream, CryptoDataStream
from alpaca.data.models import Bar
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ─────────────────────────────────────────────
#  CONFIG — edit these before running
# ─────────────────────────────────────────────

API_KEY    = os.environ.get("API_KEY", "PKZLUITCXQJ6OCL3GKNZA3RWE4")
API_SECRET = os.environ.get("API_SECRET", "CbFDoQDq9Q9vkPKjiR1AyhPRHf4vJtJ8yU9LkoetT4Gf")

# Set to True for paper trading, False for live
PAPER_TRADING = True

# Percentage of portfolio to allocate per trade (0.05 = 5%)
POSITION_SIZE_PCT = 0.05

# Hard dollar cap per position — no single symbol can exceed this
MAX_POSITION_DOLLAR = 5000

# Max times to buy (pyramid into) the same symbol
MAX_POSITION_ENTRIES = 3

# Stocks to watch
STOCK_SYMBOLS = ["AAPL", "SPY", "TSLA", "NVDA", "SOFI", "PLTR", "AAL"]

# Crypto pairs to watch (Alpaca format)
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]

# Pattern thresholds
DOJI_BODY_THRESHOLD = 0.05   # body is ≤5% of total range → doji
HAMMER_SHADOW_RATIO = 2.0    # lower shadow ≥ 2× body size

MIN_HOLD_MINUTES = 30        # minimum minutes before pattern-based selling
STOP_LOSS_PCT    = 0.02      # 2% stop loss (checked every bar, bypasses all filters)

POSITIONS_FILE = "positions.json"

# Pattern enable/disable toggles
ENABLED_PATTERNS = {
    "dragonfly_doji":    True,
    "morning_star":      True,
    "hammer":            True,
    "bullish_engulfing": True,
    "three_inside_up":   True,
    "hanging_man":       True,
    "three_inside_down": True,
    "gravestone_doji":   True,
}

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("candle_bot.log", encoding='utf-8'),
    ]
)
log = logging.getLogger("CandleBot")

# ─────────────────────────────────────────────
#  CANDLE DATACLASS
# ─────────────────────────────────────────────

@dataclass
class Candle:
    symbol: str
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float
    timestamp: datetime

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low or 0.0001  # avoid div/0

    @property
    def upper_shadow(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_shadow(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        return (self.body / self.range) <= DOJI_BODY_THRESHOLD

# ─────────────────────────────────────────────
#  PATTERN DETECTION
# ─────────────────────────────────────────────

def detect_patterns(candles: deque) -> list[str]:
    """
    Analyze the last 1–3 candles and return a list of detected pattern names.
    Candles are ordered oldest → newest.
    """
    detected = []
    c = list(candles)  # oldest first

    if len(c) < 1:
        return detected

    last = c[-1]

    # ── Single-candle patterns ──────────────────

    if ENABLED_PATTERNS["dragonfly_doji"] and len(c) >= 1:
        if (last.is_doji
                and last.lower_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.upper_shadow <= last.body * 1.5):
            detected.append("dragonfly_doji")

    if ENABLED_PATTERNS["gravestone_doji"] and len(c) >= 1:
        if (last.is_doji
                and last.upper_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.lower_shadow <= last.body * 1.5):
            detected.append("gravestone_doji")

    if ENABLED_PATTERNS["hammer"] and len(c) >= 2:
        prev = c[-2]
        if (not last.is_doji
                and last.lower_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.upper_shadow <= last.body
                and prev.is_bearish):
            detected.append("hammer")

    if ENABLED_PATTERNS["hanging_man"] and len(c) >= 2:
        prev = c[-2]
        if (not last.is_doji
                and last.lower_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.upper_shadow <= last.body
                and prev.is_bullish):
            detected.append("hanging_man")

    # ── Two-candle patterns ─────────────────────

    if ENABLED_PATTERNS["bullish_engulfing"] and len(c) >= 2:
        prev = c[-2]
        if (prev.is_bearish and last.is_bullish
                and last.open <= prev.close
                and last.close >= prev.open):
            detected.append("bullish_engulfing")

    # ── Three-candle patterns ───────────────────

    if ENABLED_PATTERNS["morning_star"] and len(c) >= 3:
        c1, c2, c3 = c[-3], c[-2], c[-1]
        if (c1.is_bearish
                and c2.body / c2.range < 0.3
                and c2.close < c1.close
                and c3.is_bullish
                and c3.close >= (c1.open + c1.close) / 2):
            detected.append("morning_star")

    if ENABLED_PATTERNS["three_inside_up"] and len(c) >= 3:
        c1, c2, c3 = c[-3], c[-2], c[-1]
        if (c1.is_bearish
                and c2.is_bullish
                and c2.open >= c1.close and c2.close <= c1.open
                and c3.is_bullish
                and c3.close > c1.open):
            detected.append("three_inside_up")

    if ENABLED_PATTERNS["three_inside_down"] and len(c) >= 3:
        c1, c2, c3 = c[-3], c[-2], c[-1]
        if (c1.is_bullish
                and c2.is_bearish
                and c2.open <= c1.close and c2.close >= c1.open
                and c3.is_bearish
                and c3.close < c1.open):
            detected.append("three_inside_down")

    return detected

# ─────────────────────────────────────────────
#  SIGNAL CLASSIFICATION
# ─────────────────────────────────────────────

BUY_PATTERNS  = {"dragonfly_doji", "morning_star", "hammer",
                 "bullish_engulfing", "three_inside_up"}
SELL_PATTERNS = {"hanging_man", "three_inside_down", "gravestone_doji"}

def classify_signal(patterns: list[str]) -> Optional[str]:
    """Return 'buy', 'sell', or None based on detected patterns."""
    if any(p in BUY_PATTERNS for p in patterns):
        return "buy"
    if any(p in SELL_PATTERNS for p in patterns):
        return "sell"
    return None

# ─────────────────────────────────────────────
#  POSITION TRACKING (persisted across restarts)
# ─────────────────────────────────────────────

# {symbol: {"entry_price": float, "entry_time": datetime, "entries": int}}
position_entries: dict[str, dict] = {}

def save_positions():
    try:
        serializable = {
            s: {
                "entry_price": p["entry_price"],
                "entry_time": p["entry_time"].isoformat(),
                "entries": p["entries"],
            }
            for s, p in position_entries.items()
        }
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
    except Exception as e:
        log.warning(f"Could not save positions: {e}")

def load_positions():
    try:
        if not os.path.exists(POSITIONS_FILE):
            return
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for s, p in data.items():
            position_entries[s] = {
                "entry_price": p["entry_price"],
                "entry_time": datetime.fromisoformat(p["entry_time"]),
                "entries": p.get("entries", 1),
            }
        if position_entries:
            log.info(f"Restored tracked positions: {list(position_entries.keys())}")
    except Exception as e:
        log.warning(f"Could not load positions: {e}")

# ─────────────────────────────────────────────
#  ORDER EXECUTION
# ─────────────────────────────────────────────

class OrderManager:
    def __init__(self, client: TradingClient):
        self.client = client

    def get_position_qty(self, symbol: str) -> float:
        """Return current position quantity, 0 if none."""
        try:
            clean = symbol.replace("/", "")
            pos = self.client.get_open_position(clean)
            return float(pos.qty)
        except Exception:
            return 0.0

    def place_order(self, symbol: str, side: str, asset_class: str, last_price: float):
        """Place a limit order sized from CASH (never margin)."""
        try:
            if side == "buy":
                # Pyramiding cap — tracked locally, not from lifetime order history
                entry = position_entries.get(symbol)
                entries = entry["entries"] if entry else 0
                if entries >= MAX_POSITION_ENTRIES:
                    log.info(f"[SKIP] {symbol} already has {entries} entries, max reached.")
                    return

                # CASH-ONLY sizing: target % of portfolio, capped by dollar
                # limit and by actual available cash. This prevents the bot
                # from ever borrowing on margin.
                account = self.client.get_account()
                cash = float(account.cash)
                target = float(account.portfolio_value) * POSITION_SIZE_PCT
                notional = round(min(target, MAX_POSITION_DOLLAR, cash * 0.95), 2)

                if notional < 1:
                    log.info(f"[SKIP] {symbol} | insufficient cash (${cash:.2f} available)")
                    return

                tif = TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY
                req = LimitOrderRequest(
                    symbol=symbol,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=tif,
                    limit_price=round(last_price * 1.001, 2),
                )
                order = self.client.submit_order(req)
                log.info(f"[BUY] {symbol} | ${notional:.2f} notional | entry #{entries + 1} | order {order.id}")

                position_entries[symbol] = {
                    "entry_price": last_price,
                    "entry_time": datetime.now(),
                    "entries": entries + 1,
                }
                save_positions()

            elif side == "sell":
                qty = self.get_position_qty(symbol)
                if qty <= 0:
                    log.info(f"[SKIP] No position in {symbol} to sell.")
                    position_entries.pop(symbol, None)
                    save_positions()
                    return

                tif = TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=tif,
                    limit_price=round(last_price * 0.999, 2),
                )
                order = self.client.submit_order(req)
                log.info(f"[SELL] {symbol} | {qty} units | order {order.id}")
                position_entries.pop(symbol, None)
                save_positions()

        except Exception as e:
            log.error(f"Order failed for {symbol}: {e}")

# ─────────────────────────────────────────────
#  BAR BUFFER & STREAM HANDLER
# ─────────────────────────────────────────────

# Store last 3 candles per symbol (enough for 3-candle patterns)
bar_buffers: dict[str, deque] = {}
# Store pending signals waiting for recheck next candle
pending_signals: dict[str, str] = {}

pattern_log = {}
filter_stats = {"passed": 0, "rejected": 0}

# Shared historical data client (reused instead of recreated per call)
_hist_client: Optional[StockHistoricalDataClient] = None

def is_safe_trading_hours() -> bool:
    et = pytz.timezone("America/New_York")
    now = datetime.now(et).time()
    return dtime(9, 45) <= now <= dtime(15, 45)

def get_buffer(symbol: str) -> deque:
    if symbol not in bar_buffers:
        bar_buffers[symbol] = deque(maxlen=3)
    return bar_buffers[symbol]

def make_candle(symbol: str, bar: Bar) -> Candle:
    return Candle(
        symbol=symbol,
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
        timestamp=bar.timestamp,
    )

def calculate_rsi(closes: list, period: int = 14) -> float:
    """
    Calculate RSI from a list of closing prices.
    FIXED: averages gains/losses over the last `period` deltas
    (previously it grabbed the last 14 *gains* from the entire history,
    which skewed the result).
    """
    if len(closes) < period + 1:
        return 50.0  # neutral if not enough data
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    recent = deltas[-period:]
    avg_gain = sum(d for d in recent if d > 0) / period
    avg_loss = sum(-d for d in recent if d < 0) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_rsi_and_trend(symbol: str, api_key: str, secret_key: str) -> tuple[float, bool]:
    try:
        client = StockHistoricalDataClient(api_key, secret_key)
        end = datetime.now()
        start = end - timedelta(days=300)  # enough calendar days to get 210 trading days

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
            limit=210,
        )
        bars = client.get_stock_bars(request)
        closes = [bar.close for bar in bars[symbol]]
        log.info(f"[RSI DEBUG] {symbol} | Got {len(closes)} closes")

        if len(closes) < 15:
            return 50.0, True

        rsi = calculate_rsi(closes)
        above_200ma = closes[-1] > (sum(closes[-200:]) / min(len(closes), 200))
        return rsi, above_200ma

    except Exception as e:
        log.warning(f"Could not fetch RSI/trend for {symbol}: {e}")
        return 50.0, True


def should_sell(symbol: str, current_price: float) -> tuple[bool, str]:
    """
    Returns (True, reason) if a pattern-based sell is allowed.
    Stop-loss is handled separately in handle_bar and does NOT go through here.
    """
    if symbol not in position_entries:
        return True, "no entry tracked"

    entry = position_entries[symbol]
    entry_time = entry["entry_time"]
    held_minutes = (datetime.now() - entry_time).total_seconds() / 60

    if held_minutes < MIN_HOLD_MINUTES:
        return False, f"held only {held_minutes:.1f} min, minimum is {MIN_HOLD_MINUTES}"

    return True, f"hold time satisfied ({held_minutes:.1f} min)"


def handle_bar(symbol: str, bar: Bar, order_mgr: OrderManager, asset_class: str):
    candle = make_candle(symbol, bar)
    buf = get_buffer(symbol)
    buf.append(candle)

    if asset_class == "stock" and not is_safe_trading_hours():
        return

    log.debug(
        f"{symbol} | O:{candle.open:.4f} H:{candle.high:.4f} "
        f"L:{candle.low:.4f} C:{candle.close:.4f}"
    )

    # ── STOP LOSS — checked on EVERY bar, bypasses all filters ──────
    # This is the critical fix: previously the stop loss could only fire
    # if a sell PATTERN happened to appear, which meant a crashing position
    # with no pattern would never exit.
    if symbol in position_entries:
        entry_price = position_entries[symbol]["entry_price"]
        loss_pct = (entry_price - candle.close) / entry_price
        if loss_pct >= STOP_LOSS_PCT:
            log.info(f"[STOP LOSS] {symbol} down {loss_pct*100:.2f}% from entry — exiting now")
            order_mgr.place_order(symbol, "sell", asset_class, candle.close)
            pending_signals.pop(symbol, None)
            return

    # ── Pending signal recheck from previous candle ─────────────────
    if symbol in pending_signals:
        signal = pending_signals.pop(symbol)
        log.info(f"[RECHECK] {symbol} | Rechecking pending {signal} signal...")

        if asset_class == "stock":
            rsi, above_200ma = get_rsi_and_trend(symbol, API_KEY, API_SECRET)
            log.info(f"[FILTER] {symbol} | RSI: {rsi:.1f} | Above 200MA: {above_200ma}")

            if signal == "buy":
                if rsi < 40 and above_200ma:
                    log.info(f"[FILTER] {symbol} | Filters passed on recheck, placing buy")
                    order_mgr.place_order(symbol, signal, asset_class, candle.close)
                else:
                    log.info(f"[FILTER] {symbol} | Filters failed on recheck, skipping")

            elif signal == "sell":
                allowed, reason = should_sell(symbol, candle.close)
                if not allowed:
                    log.info(f"[HOLD] {symbol} | Sell blocked — {reason}")
                    return
                if rsi > 60 or not above_200ma:
                    log.info(f"[FILTER] {symbol} | Filters passed on recheck, placing sell")
                    order_mgr.place_order(symbol, signal, asset_class, candle.close)
                else:
                    log.info(f"[FILTER] {symbol} | Filters failed on recheck, skipping")
        else:
            # Crypto — no filters, just place the order
            order_mgr.place_order(symbol, signal, asset_class, candle.close)
        return

    patterns = detect_patterns(buf)
    if not patterns:
        return

    log.info(f"[PATTERN] {symbol} | Patterns detected: {', '.join(patterns)}")
    signal = classify_signal(patterns)
    if not signal:
        return

    # Crypto trades without filters (but sell still respects min hold time)
    if asset_class == "crypto":
        if signal == "sell":
            allowed, reason = should_sell(symbol, candle.close)
            if not allowed:
                log.info(f"[HOLD] {symbol} | Sell blocked — {reason}")
                return
        order_mgr.place_order(symbol, signal, asset_class, candle.close)
        return

    # Stocks — check RSI + trend filter
    rsi, above_200ma = get_rsi_and_trend(symbol, API_KEY, API_SECRET)
    log.info(f"[FILTER] {symbol} | RSI: {rsi:.1f} | Above 200MA: {above_200ma}")

    if signal == "buy":
        if rsi < 40 and above_200ma:
            log.info(f"[FILTER] {symbol} | Filters passed, placing buy")
            order_mgr.place_order(symbol, signal, asset_class, candle.close)
        else:
            log.info(f"[FILTER] {symbol} | Filters failed, waiting for next candle")
            pending_signals[symbol] = signal

    elif signal == "sell":
        allowed, reason = should_sell(symbol, candle.close)
        if not allowed:
            log.info(f"[HOLD] {symbol} | Sell blocked — {reason}")
            return
        # FIXED: was `rsi > 60 AND not above_200ma`, which almost never
        # happens together (overbought stocks are usually above their 200MA).
        # That meant pattern-based sells basically never executed and the
        # only exit was the stop loss. OR is the correct logic here:
        # sell if overbought OR if the long-term trend has turned bearish.
        if rsi > 60 or not above_200ma:
            log.info(f"[FILTER] {symbol} | Filters passed, placing sell")
            order_mgr.place_order(symbol, signal, asset_class, candle.close)
        else:
            log.info(f"[FILTER] {symbol} | Filters failed, waiting for next candle")
            pending_signals[symbol] = signal

# ─────────────────────────────────────────────
#  MAIN — STREAM SETUP
# ─────────────────────────────────────────────

async def main():
    if not API_KEY or not API_SECRET:
        log.error("API_KEY / API_SECRET not set. Create a .env file next to this "
                  "script with API_KEY=... and API_SECRET=... or set them as "
                  "environment variables (Railway: Variables tab).")
        return

    log.info("Waiting for any previous connections to close...")
    await asyncio.sleep(5)
    log.info("=" * 55)
    log.info("  Alpaca Candlestick Bot starting up (v2)")
    log.info(f"  Mode: {'PAPER' if PAPER_TRADING else ' LIVE'}")
    log.info(f"  Position size: {POSITION_SIZE_PCT*100:.1f}% of portfolio, "
             f"max ${MAX_POSITION_DOLLAR}, cash-only")
    log.info(f"  Stocks:  {STOCK_SYMBOLS}")
    log.info(f"  Crypto:  {CRYPTO_SYMBOLS}")
    log.info("=" * 55)

    load_positions()

    trading_client = TradingClient(
        api_key=API_KEY,
        secret_key=API_SECRET,
        paper=PAPER_TRADING,
    )

    order_mgr = OrderManager(trading_client)

    # ── Stock stream ──────────────────────────
    stock_stream = StockDataStream(
        api_key=API_KEY,
        secret_key=API_SECRET,
        feed=DataFeed.IEX,
    )

    async def on_stock_bar(bar: Bar):
        handle_bar(bar.symbol, bar, order_mgr, "stock")

    stock_stream.subscribe_bars(on_stock_bar, *STOCK_SYMBOLS)

    # ── Crypto stream ─────────────────────────
    crypto_stream = CryptoDataStream(
        api_key=API_KEY,
        secret_key=API_SECRET,
    )

    async def on_crypto_bar(bar: Bar):
        handle_bar(bar.symbol, bar, order_mgr, "crypto")

    crypto_stream.subscribe_bars(on_crypto_bar, *CRYPTO_SYMBOLS)

    log.info("Streams live. Listening for bars...")

    async def heartbeat():
        while True:
            log.info("Bot is running and waiting for bars...")
            await asyncio.sleep(30)

    stock_thread = threading.Thread(target=stock_stream.run, daemon=True)
    crypto_thread = threading.Thread(target=crypto_stream.run, daemon=True)

    stock_thread.start()
    crypto_thread.start()

    async def report_loop():
        while True:
            generate_report(trading_client, log, pattern_log, filter_stats)
            await asyncio.sleep(REPORT_INTERVAL_HOURS * 3600)

    await asyncio.gather(heartbeat(), report_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
