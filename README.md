# Crypto Breakout Paper Bot

Paper-trading and research toolkit for crypto breakout strategies. The
production path uses Binance public daily candles and never sends real orders.

## Live Paper Rules

- Regime filter: only trade when close is above the 200-day SMA.
- Entry: next UTC daily open after the signal.
- Sizing: `min(0.75x, 1.50% / 20-day daily realized vol)`.
- Sizing base: initial fake equity, no compounding by default.
- Costs: 10 bps per side.

Tracked symbols and fitted paper rules:

- `BTCUSDT`: 15d breakout, 100 bps buffer, 225 bps cap, 5-day hold.
- `BNBUSDT`: 15d breakout, 100 bps buffer, 400 bps cap, 7-day hold.
- `ETHUSDT`: 10d breakout, 150 bps buffer, 300 bps cap, 10-day hold.
- `ETCUSDT`: 30d breakout, 100 bps buffer, 400 bps cap, 5-day hold.

## Quick Start

```bash
python -m pip install pandas
python btc_breakout_clean/btc_breakout_binance_paper_bot.py
```

For the historical Dukascopy/Yahoo-compatible simulator:

```bash
python -m pip install yfinance
python btc_breakout_clean/btc_breakout_paper_sim.py
```

## Daily Automation

The daily wrapper is designed for cron or GitHub Actions:

```bash
python btc_breakout_clean/run_binance_paper_daily.py
```

To test a regime-filter variant without changing the live defaults:

```bash
python btc_breakout_clean/run_binance_paper_daily.py --trend-mode sma50_slope_up --no-write --no-telegram
```

It writes:

- `btc_breakout_clean/paper_binance/<SYMBOL>/state.json`
- `btc_breakout_clean/paper_binance/<SYMBOL>/trades.csv`
- `btc_breakout_clean/paper_binance/<SYMBOL>/equity.csv`
- `btc_breakout_clean/paper_binance/run_log.csv`

These generated files are gitignored locally. The GitHub workflow uploads them
as an Actions artifact.

## GitHub Actions

Workflow: `.github/workflows/btc-binance-paper-daily.yml`

- Runs daily at `00:10 UTC`, shortly after the Binance daily candle closes.
- Can be triggered manually from the Actions tab.
- Restores prior paper state from the Actions cache.
- Uploads the paper state folder as an artifact.
- Tracks `BTCUSDT`, `BNBUSDT`, `ETHUSDT`, and `ETCUSDT` by default.
- Sends one compact Telegram daily update when `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` repository secrets are configured.

If Binance blocks the default endpoint from GitHub runners, set repository
variable `BINANCE_BASE_URL` to a compatible Binance spot API endpoint.

## Telegram Alerts

Create a Telegram bot with `@BotFather`, send it one message from your Telegram
account, then set these GitHub repository secrets:

- `TELEGRAM_BOT_TOKEN`: bot token from `@BotFather`.
- `TELEGRAM_CHAT_ID`: your chat id or group chat id.

The daily wrapper also works locally with environment variables:

```bash
TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..." \
  python btc_breakout_clean/run_binance_paper_daily.py
```

## Repository Layout

The tracked live project is intentionally small:

- `btc_breakout_clean/run_binance_paper_daily.py` - cron/GitHub wrapper with Telegram notification.
- `btc_breakout_clean/btc_breakout_binance_paper_bot.py` - Binance public candle fetcher and paper report.
- `btc_breakout_clean/btc_breakout_paper_sim.py` - shared simulator and historical CLI.

Research scripts and local generated outputs live under `old/`, which is
gitignored.

## Disclaimer

This is research and fake-money paper trading only. It is not investment advice
and it does not place real exchange orders.
