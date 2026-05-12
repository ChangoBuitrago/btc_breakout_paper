# BTC Breakout Paper Bot

Paper-trading and research toolkit for a BTC breakout strategy. The production
path uses Binance public `BTCUSDT` daily candles and never sends real orders.

## Live Paper Rule

- Signal: close above the prior 15-day close high by at least 100 bps.
- Exhaustion filter: breakout size must be `<= 225 bps`.
- Regime filter: only trade when close is above the 200-day SMA (`bull_only`).
- Entry: next UTC daily open after the signal.
- Exit: close after 5 trading days.
- Sizing: `min(0.75x, 1.50% / 20-day daily realized vol)`.
- Sizing base: initial fake equity, no compounding by default.
- Costs: 10 bps per side.

## Quick Start

```bash
python -m pip install pandas yfinance
python btc_breakout_clean/btc_breakout_binance_paper_bot.py
```

For the historical Dukascopy/Yahoo-compatible simulator:

```bash
python btc_breakout_clean/btc_breakout_paper_sim.py
```

## Daily Automation

The daily wrapper is designed for cron or GitHub Actions:

```bash
python btc_breakout_clean/run_binance_paper_daily.py
```

It writes:

- `btc_breakout_clean/paper_binance/state.json`
- `btc_breakout_clean/paper_binance/trades.csv`
- `btc_breakout_clean/paper_binance/equity.csv`
- `btc_breakout_clean/paper_binance/run_log.csv`

These generated files are gitignored locally. The GitHub workflow uploads them
as an Actions artifact.

## GitHub Actions

Workflow: `.github/workflows/btc-binance-paper-daily.yml`

- Runs daily at `00:10 UTC`, shortly after the Binance daily candle closes.
- Can be triggered manually from the Actions tab.
- Restores prior paper state from the Actions cache.
- Uploads the paper state folder as an artifact.
- Sends a Telegram daily update when `TELEGRAM_BOT_TOKEN` and
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

## Research Scripts

The repository also includes the research scripts used to arrive at the live
rule:

- `btc_breakout_clean/btc_breakout_final.py` - final validation, fixed hold logic, stress tests.
- `btc_breakout_clean/btc_breakout_extend.py` - structural hold/trend/IS-OOS exploration.
- `btc_breakout_clean/btc_breakout_relax_probe.py` - parameter relaxation and breakout cap tests.
- `btc_breakout_clean/btc_breakout_hold_decay.py` - fixed-hold decay and risk grids.
- `btc_breakout_clean/btc_breakout_position.py` - multi-day position experiments.
- `btc_breakout_clean/btc_strategy_probe.py` - initial BTC RAM/DPB probe.
- `btc_breakout_clean/btc_dpb_refine.py` - DPB refinement scans.

## Disclaimer

This is research and fake-money paper trading only. It is not investment advice
and it does not place real exchange orders.
