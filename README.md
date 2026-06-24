# Alpaca Candlestick Signal Bot

Real-time candlestick pattern trading bot using the Alpaca API.
Streams live bars for stocks and crypto, detects patterns, and places orders automatically.

---

## Supported Patterns

| Pattern           | Signal | Candles |
|-------------------|--------|---------|
| Dragonfly Doji    | BUY    | 1       |
| Morning Star      | BUY    | 3       |
| Hammer            | BUY    | 2       |
| Bullish Engulfing | BUY    | 2       |
| Three Inside Up   | BUY    | 3       |
| Hanging Man       | SELL   | 2       |
| Three Inside Down | SELL   | 3       |
| Gravestone Doji   | SELL   | 1       |

---

## Setup

### 1. Install dependencies

```bash
pip install alpaca-py pandas
```

### 2. Get your Alpaca API keys

1. Go to https://alpaca.markets and sign up
2. Navigate to **Paper Trading** → API Keys → Generate
3. Copy your **API Key** and **Secret Key**

### 3. Configure the bot

Open `alpaca_candle_bot.py` and edit the CONFIG section at the top:

```python
API_KEY    = "YOUR_ALPACA_API_KEY"
API_SECRET = "YOUR_ALPACA_SECRET_KEY"

PAPER_TRADING     = True        # ← Start here. Set False only for live trading.
POSITION_SIZE_PCT = 0.05        # 5% of portfolio per trade

STOCK_SYMBOLS  = ["AAPL", "SPY", "TSLA", "NVDA"]   # stocks to watch
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]             # crypto pairs to watch
```

### 4. Run

```bash
python alpaca_candle_bot.py
```

Logs are written to both console and `candle_bot.log`.

---

## Switching from Paper to Live

When you're ready to go live:

1. Generate a **Live Trading** API key from your Alpaca dashboard
2. Replace `API_KEY` and `API_SECRET` with your live keys
3. Set `PAPER_TRADING = False`

> ⚠️ Live trading uses real money. Test thoroughly on paper first.

---

## Tuning Parameters

| Parameter             | Default | Description                                      |
|-----------------------|---------|--------------------------------------------------|
| `POSITION_SIZE_PCT`   | `0.05`  | % of portfolio per trade (0.05 = 5%)             |
| `DOJI_BODY_THRESHOLD` | `0.05`  | Max body/range ratio to qualify as a doji        |
| `HAMMER_SHADOW_RATIO` | `2.0`   | Lower shadow must be ≥ this multiple of body     |
| `ENABLED_PATTERNS`    | all on  | Toggle individual patterns on/off                |

---

## Important Notes

- **Stocks only trade during market hours** (9:30 AM – 4:00 PM ET). Bars won't stream outside hours.
- **Crypto trades 24/7** on Alpaca.
- The bot uses **market orders** — fills are fast but price isn't guaranteed.
- The `iex` data feed is free. Upgrade to `sip` (paid) for full market data.
- Candlestick signals are probabilistic — not guaranteed to be profitable. Always backtest before risking real capital.

---

## File Structure

```
alpaca_candle_bot.py   ← main bot script
candle_bot.log         ← auto-created log file when bot runs
README.md              ← this file
```
