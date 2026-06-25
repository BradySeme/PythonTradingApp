"""
Alpaca Candlestick Signal Trading Bot
======================================
Streams real-time bars for stocks and crypto, detects candlestick patterns,
and places buy/sell orders based on detected signals.

Supports all 8 patterns:
  BUY:  Dragonfly Doji, Morning Star, Hammer, Bullish Engulfing, Three Inside Up
  SELL: Hanging Man, Three Inside Down, Gravestone Doji

Setup:
  pip install alpaca-py pandas

Usage:
  python alpaca_candle_bot.py
"""

import asyncio
import logging
import threading
import os
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.data.live import StockDataStream, CryptoDataStream
from alpaca.data.models import Bar
from alpaca.data.enums import DataFeed
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

# ─────────────────────────────────────────────
#  CONFIG — edit these before running
# ─────────────────────────────────────────────

API_KEY    = os.environ.get("API_KEY", "PKIYR5XC6TUM3C6Z57EP5FUQIA")
API_SECRET = os.environ.get("API_SECRET", "9PLmFbtf1kZuevCQGhUcmaSbv8rwMzvAcN4RS8zS5j17")

# Set to True for paper trading, False for live
PAPER_TRADING = True

# Percentage of portfolio to allocate per trade (0.05 = 5%)
POSITION_SIZE_PCT = 0.05

# max times to buy the same symbol
MAX_POSITION_ENTRIES = 3  

# Stocks to watch
STOCK_SYMBOLS = ["AAPL", "SPY", "TSLA", "NVDA", "SOFI","PLTR", "AAL"]

# Crypto pairs to watch (Alpaca format)
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]

# Minimum body-to-range ratio for pattern confirmation (0.0–1.0)
DOJI_BODY_THRESHOLD   = 0.05   # body is ≤5% of total range → doji
HAMMER_SHADOW_RATIO   = 2.0    # lower shadow ≥ 2× body size
ENGULF_CONFIRM        = True   # require full engulfing (strict)

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
        logging.FileHandler("candle_bot.log"),
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
        # Doji body at the top, long lower shadow, minimal upper shadow
        if (last.is_doji
                and last.lower_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.upper_shadow <= last.body * 1.5):
            detected.append("dragonfly_doji")

    if ENABLED_PATTERNS["gravestone_doji"] and len(c) >= 1:
        # Doji body at the bottom, long upper shadow, minimal lower shadow
        if (last.is_doji
                and last.upper_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.lower_shadow <= last.body * 1.5):
            detected.append("gravestone_doji")

    if ENABLED_PATTERNS["hammer"] and len(c) >= 2:
        prev = c[-2]
        # Small body near top, long lower shadow ≥ 2× body, appears after downtrend
        if (not last.is_doji
                and last.lower_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.upper_shadow <= last.body
                and prev.is_bearish):  # simple downtrend confirmation
            detected.append("hammer")

    if ENABLED_PATTERNS["hanging_man"] and len(c) >= 2:
        prev = c[-2]
        # Same shape as hammer but appears after uptrend
        if (not last.is_doji
                and last.lower_shadow >= HAMMER_SHADOW_RATIO * max(last.body, 0.001)
                and last.upper_shadow <= last.body
                and prev.is_bullish):  # simple uptrend confirmation
            detected.append("hanging_man")

    # ── Two-candle patterns ─────────────────────

    if ENABLED_PATTERNS["bullish_engulfing"] and len(c) >= 2:
        prev = c[-2]
        # Bearish candle followed by bullish candle that fully engulfs it
        if (prev.is_bearish and last.is_bullish
                and last.open <= prev.close
                and last.close >= prev.open):
            detected.append("bullish_engulfing")

    # ── Three-candle patterns ───────────────────

    if ENABLED_PATTERNS["morning_star"] and len(c) >= 3:
        c1, c2, c3 = c[-3], c[-2], c[-1]
        # Large bearish → small body (gap down) → large bullish closing into c1
        if (c1.is_bearish
                and c2.body / c2.range < 0.3        # small middle candle
                and c2.close < c1.close              # gap or step down
                and c3.is_bullish
                and c3.close >= (c1.open + c1.close) / 2):  # closes into bearish body
            detected.append("morning_star")

    if ENABLED_PATTERNS["three_inside_up"] and len(c) >= 3:
        c1, c2, c3 = c[-3], c[-2], c[-1]
        # Large bearish, small bullish inside, large bullish closing above c1 open
        if (c1.is_bearish
                and c2.is_bullish
                and c2.open >= c1.close and c2.close <= c1.open  # inside c1
                and c3.is_bullish
                and c3.close > c1.open):
            detected.append("three_inside_up")

    if ENABLED_PATTERNS["three_inside_down"] and len(c) >= 3:
        c1, c2, c3 = c[-3], c[-2], c[-1]
        # Large bullish, small bearish inside, large bearish closing below c1 open
        if (c1.is_bullish
                and c2.is_bearish
                and c2.open <= c1.close and c2.close >= c1.open  # inside c1
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
#  ORDER EXECUTION
# ─────────────────────────────────────────────

class OrderManager:
    def __init__(self, client: TradingClient):
        self.client = client

    def get_portfolio_value(self) -> float:
        account = self.client.get_account()
        return float(account.portfolio_value)

    def get_position_qty(self, symbol: str) -> float:
        """Return current position quantity, 0 if none."""
        try:
            # Alpaca uses '/' in crypto symbols but not in position lookup
            clean = symbol.replace("/", "")
            pos = self.client.get_open_position(clean)
            return float(pos.qty)
        except Exception:
            return 0.0
        
    def get_entry_count(self, symbol: str) -> int:
        """Count how many times we've entered this symbol."""
        try:
            clean = symbol.replace("/", "")
            activities = self.client.get_portfolio_history()
            orders = self.client.get_orders(filter=GetOrdersRequest(
                symbol=clean,
                side=OrderSide.BUY,
                status=QueryOrderStatus.FILLED,
            ))
            # Count filled buy orders that are part of current open position
            pos_qty = self.get_position_qty(symbol)
            if pos_qty <= 0:
                return 0
            return min(len(orders), MAX_POSITION_ENTRIES)
        except Exception:
            return 0

    def place_order(self, symbol: str, side: str, asset_class: str, last_price: float):
        """Place a market order sized at POSITION_SIZE_PCT of portfolio."""
        try:
            portfolio_value = self.get_portfolio_value()
            notional = round(portfolio_value * POSITION_SIZE_PCT, 2)

            # Alpaca crypto uses '/' format; stocks don't
            order_symbol = symbol if asset_class == "crypto" else symbol

            if side == "buy":
                # Check we don't already have a position
                entry_count = self.get_entry_count(symbol)
                if entry_count >= MAX_POSITION_ENTRIES:
                    log.info(f"[SKIP] {symbol} already has {entry_count} entries, max reached.")
                    return

                tif = TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY
                req = LimitOrderRequest(
                    symbol=order_symbol,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=tif,
                    limit_price=round(last_price * 1.001, 2),
                    extended_hours=True,
                )

                order = self.client.submit_order(req)
                log.info(f" BUY  {symbol} | ${notional:.2f} notional | order {order.id}")

            elif side == "sell":
                qty = self.get_position_qty(symbol)
                if qty <= 0:
                    log.info(f"  No position in {symbol} to sell.")
                    return

                tif = TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY
                req = LimitOrderRequest(
                    symbol=order_symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=tif,
                    limit_price=round(last_price * 0.999, 2),
                    extended_hours=True,
                )
                order = self.client.submit_order(req)
                log.info(f" SELL {symbol} | {qty} units | order {order.id}")

        except Exception as e:
            log.error(f"Order failed for {symbol}: {e}")

# ─────────────────────────────────────────────
#  BAR BUFFER & STREAM HANDLER
# ─────────────────────────────────────────────

# Store last 3 candles per symbol (enough for 3-candle patterns)
bar_buffers: dict[str, deque] = {}

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

def handle_bar(symbol: str, bar: Bar, order_mgr: OrderManager, asset_class: str):
    candle = make_candle(symbol, bar)
    buf = get_buffer(symbol)
    buf.append(candle)

    log.debug(
        f"{symbol} | O:{candle.open:.4f} H:{candle.high:.4f} "
        f"L:{candle.low:.4f} C:{candle.close:.4f}"
    )

    if len(buf) < 1:
        return

    patterns = detect_patterns(buf)
    if patterns:
        log.info(f" {symbol} | Patterns detected: {', '.join(patterns)}")
        signal = classify_signal(patterns)
        if signal:
            order_mgr.place_order(symbol, signal, asset_class, candle.close)

# ─────────────────────────────────────────────
#  MAIN — STREAM SETUP
# ─────────────────────────────────────────────

async def main():
    log.info("Waiting for any previous connections to close...")
    await asyncio.sleep(5)  # 5 second buffer on startup
    log.info("=" * 55)
    log.info("  Alpaca Candlestick Bot starting up")
    log.info(f"  Mode: {'PAPER' if PAPER_TRADING else ' LIVE'}")
    log.info(f"  Position size: {POSITION_SIZE_PCT*100:.1f}% of portfolio per trade")
    log.info(f"  Stocks:  {STOCK_SYMBOLS}")
    log.info(f"  Crypto:  {CRYPTO_SYMBOLS}")
    log.info("=" * 55)

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

    # Run both streams concurrently
    log.info("Streams live. Listening for bars...")

    async def heartbeat():
        while True:
            log.info("Bot is running and waiting for bars...")
            await asyncio.sleep(30)

    stock_thread = threading.Thread(target=stock_stream.run, daemon=True)
    crypto_thread = threading.Thread(target=crypto_stream.run, daemon=True)

    stock_thread.start()
    crypto_thread.start()

    await heartbeat()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
