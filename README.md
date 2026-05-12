# BTC + Metals Breakout Paper Bot

Paper-trading and research toolkit for long-only breakout strategies. The
production path tracks a weighted BTC + metals basket with public daily candles
and never sends real orders.

## Live Paper Rules

- Regime filter: symbol-specific SMA filter.
- Entry: next UTC daily open after the signal.
- Sizing: `min(0.75x, 1.50% / 20-day daily realized vol)`.
- Sizing base: current fake sleeve equity, compounded.
- Account: `$50,000` total, split by sleeve.

Tracked sleeves and fitted paper rules:

- `BTCUSD`: `$20k`, Dukascopy, 15d breakout, 100 bps buffer, 225 bps cap, 5-day hold, 10 bps costs.
- `XAUUSD`: `$15k`, Dukascopy, 30d breakout, 100 bps buffer, 225 bps cap, `sma200_95`, 15-day hold, 2 bps costs.
- `XAGUSD`: `$10k`, Dukascopy, 30d breakout, 100 bps buffer, 225 bps cap, `sma200_95`, 15-day hold, 2 bps costs.
- `XCUUSD`: `$5k`, Dukascopy, 15d breakout, 100 bps buffer, 225 bps cap, 5-day hold, 10 bps costs.

## Quick Start

```bash
python -m pip install pandas dukascopy-python
python btc_breakout_clean/run_binance_paper_daily.py --no-write --no-telegram
```

The Binance-only single-symbol bot is still available for manual crypto checks:

```bash
python btc_breakout_clean/btc_breakout_binance_paper_bot.py --symbol BTCUSDT
```

The same simulator can test Dukascopy macro instruments:

```bash
python btc_breakout_clean/btc_breakout_paper_sim.py --source dukascopy --instrument XAUUSD
python btc_breakout_clean/btc_breakout_paper_sim.py --source dukascopy --instrument XAGUSD
python btc_breakout_clean/btc_breakout_paper_sim.py --source dukascopy --instrument XCUUSD
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

- `btc_breakout_clean/paper_portfolio/<SYMBOL>/state.json`
- `btc_breakout_clean/paper_portfolio/<SYMBOL>/trades.csv`
- `btc_breakout_clean/paper_portfolio/<SYMBOL>/equity.csv`
- `btc_breakout_clean/paper_portfolio/run_log.csv`

These generated files and the Dukascopy cache are gitignored locally. The
GitHub workflow caches and uploads them as Actions artifacts.

## GitHub Actions

Workflow: `.github/workflows/btc-binance-paper-daily.yml`

- Runs daily at `00:10 UTC`, shortly after the UTC daily candle closes.
- Can be triggered manually from the Actions tab.
- Restores prior paper state and Dukascopy cache from the Actions cache.
- Uploads the paper state and cache folders as an artifact.
- Tracks `BTCUSD`, `XAUUSD`, `XAGUSD`, and `XCUUSD` by default.
- Sends one compact Telegram daily update when `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` repository secrets are configured.

`BINANCE_BASE_URL` is only used when running Binance symbols manually.

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
- `btc_breakout_clean/btc_breakout_binance_paper_bot.py` - live portfolio defaults, Binance fetcher, and paper report.
- `btc_breakout_clean/btc_breakout_paper_sim.py` - shared simulator and historical CLI.

Research scripts and local generated outputs live under `old/`, which is
gitignored.

## Disclaimer

This is research and fake-money paper trading only. It is not investment advice
and it does not place real exchange orders.
