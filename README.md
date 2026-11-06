# SIP Strategy Automation

Automate systematic ETF investments using the **SIP strategy** with Angel One APIs.  
This project allows you to automatically execute trades based on predefined rules, track your portfolio, and calculate key metrics like XIRR and CAGR in real-time.

---

## **Features**

- **Automated ETF Trading**: Execute buy/sell orders automatically via Angel One API.
- **SIP Execution**: Monthly systematic investments with optional annual increment.
- **Dynamic Strategy**:
  - Buy ETFs that have fallen the most compared to moving averages.
  - Average down positions when price drops below a threshold.
  - Trailing stop-loss to secure profits.
- **Portfolio Management**: Track holdings, cash, invested value, and trades in real-time.
- **Performance Metrics**: Calculate XIRR, CAGR, and track overall and yearly portfolio performance.
- **Configurable**: Set thresholds, SIP amount, number of daily buys, trailing stop-loss, and more in a config file.

---

## **Project Structure**


---

## **Trade record and performance reporting**

A third process records every trade and publishes a readable P&L report.

**👉 [`trades/REPORT.md`](trades/REPORT.md) — what this strategy has actually made.**
Regenerated after every market close: XIRR, CAGR, realised and unrealised P&L,
charges paid, and a per-ETF breakdown. Start there.

[`trades/trades.csv`](trades/trades.csv) is the underlying ledger — one row per
order with full detail: price, quantity, fill price, value, status, charges,
broker order and trade ids.

### Why it reads the broker's books

The account is traded by hand as well as by the strategy, and Angel One's order
book covers **every** order regardless of who placed it — the only way the
record is complete. Each row is attributed in the `order_source` column:

| `order_source` | Meaning |
|---|---|
| `STRATEGY_BUY` | Placed by the daily buy |
| `STRATEGY_SELL` | Placed by the trailing-profit sell |
| `STRATEGY_SELL_STOPLOSS` | Placed by the stop-loss sell |
| `MANUAL_OR_UNTAGGED` | Placed by hand in the Angel One app |
| `HISTORICAL_IMPORT` | Imported from a contract-note statement |

Attribution comes from Angel's `ordertag`, which the strategy stamps on
everything it places (`ORDER_TAGS` in [src/angel_api.py](src/angel_api.py)). An
untagged order is one this code did not place — stated as such rather than
guessed at from the order's shape.

The ledger is **append-only** because `orderBook()` and `tradeBook()` are
day-scoped: they return today and nothing else, so a day that is never
recorded is gone for good.

### Commands

Everything runs from the repo root, through the venv.

```bash
./venv/bin/python trade_logger.py --once
```
Record today now, regenerate the report, commit and push. This is what the
schedule runs.

```bash
./venv/bin/python trade_logger.py --report-only
```
Rebuild the report from the existing ledger. No broker write, no git.

```bash
./venv/bin/python trade_logger.py --once --no-push
```
Record but leave git alone — useful when testing.

```bash
./venv/bin/python trade_logger.py --import-statements trades/imports/FY2025-26.xlsx trades/imports/FY2026-27.csv --since 2025-10-01
```
Import Angel One **TradeBook And Charges** exports (`.csv` or `.xlsx`), which
are the only source of history beyond today *and* of charges — the live API
reports neither. Re-running is safe: rows are keyed on the broker's trade id,
so overlapping statements merge rather than duplicate.

```bash
pm2 logs trade-logger
pm2 restart trade-logger
```

### How it runs unattended

The `trade-logger` pm2 app fires at **16:05 IST**, Mon–Fri — after the 15:30
close and well clear of the 15:20 buy. Each run logs in read-only, merges new
orders, regenerates the report, commits both files and pushes, then sends a
Telegram summary. It never force-pushes: a conflicted rebase is abandoned and
retried next run, because losing a day of report regeneration is recoverable
and overwriting a commit is not.

### Reading the numbers

- **XIRR** is the headline. It is money-weighted, so a rupee added last week
  is not credited with a year of growth — the only fair way to state a SIP's
  return. CAGR is shown beside it for comparison and ignores timing.
- **Net P&L** needs no lot matching: everything sold, plus the market value of
  what is still held, minus everything bought, minus charges. Splitting it into
  realised and unrealised does need matching, which is done **FIFO** (aligning
  with Indian capital-gains treatment).
- **Charges** — brokerage, GST, STT, SEBI fees, stamp duty — come off the P&L
  and out of the XIRR cashflows. They are only known for trades imported from
  contract notes, so recent automated trades show zero charges and the true
  cost is slightly higher than reported.
- A sell with no matching buy lot (because the ledger starts mid-history) is
  reported as `unmatched_qty` rather than booked as pure profit.

Raw statements live in `trades/imports/` and are **gitignored** — they carry
the client code and duplicate what the ledger already holds.

---

## **Deploying on EC2 (pm2)**

Two long-running processes make up the deployment:

| pm2 app | Script | What it does |
|---|---|---|
| `strategy-scheduler` | `main.py` | Trailing sell every 10 min (09:20–15:20) and the daily buy at 15:20, Mon–Fri |
| `telegram-bot` | `run_bot.py` | Telegram command bot (`/portfolio`, `/show_etfs`, …) via long polling |

### Quick start

```bash
cd ~/strategy-automation
bash scripts/setup_ec2.sh          # installs python, node, pm2, venv, deps
vi src/config.yaml                 # fill in credentials (created from the template)
pm2 start ecosystem.config.js
pm2 save && pm2 startup            # then run the command pm2 prints
```

### What the setup script handles

- **Timezone → `Asia/Kolkata`.** `main.py` schedules with the `schedule`
  library, which fires on local wall-clock time and has no timezone support. A
  default UTC instance would run the 15:20 buy at 20:50 IST, after market close.
- **`logs/` directory.** Not tracked in git, and every module calls
  `logging.basicConfig(filename="logs/*.log")`, which fails if it is missing.
- **`src/config.yaml`.** Gitignored, so a fresh clone never has it. Copied from
  `src/config.example.yaml` and chmod'd to `600`.
- **The bundled `NorenRestApi` wheel**, installed from
  `ShoonyaApi-py/dist/` via the relative path in `requirements.txt` — so `pip
  install -r requirements.txt` must be run from the repo root.

### Operating

```bash
pm2 status
pm2 logs strategy-scheduler --lines 100
pm2 restart ecosystem.config.js    # after a git pull
pm2 stop strategy-scheduler        # halt trading without killing the bot
```

**Both processes log to `logs/utils.log`** — not to `logs/main.log` or
`logs/run_bot.log`, despite what the `LOG_FILE` constants in `main.py` and
`run_bot.py` suggest. `src/utils.py` calls `logging.basicConfig()` at import
time, and since both entrypoints import it before configuring their own
handler, the later `basicConfig()` calls are no-ops. pm2 separately captures
stdout/stderr and hard crashes in `logs/pm2-*.log`; check both when debugging.

### Notes

- The pinned dependency set (`numpy 2.0.2`, `pandas 2.3.2`, `matplotlib 3.9.4`)
  tops out at **cp312 wheels**. If the instance's system Python is newer —
  Ubuntu 26.04 ships 3.14 — `setup_ec2.sh` provisions a standalone CPython
  3.12 with `uv` for the venv and leaves the system interpreter alone, since
  pip would otherwise try to build numpy and pandas from source.
- A `t3.micro`/`t4g.small` is plenty, but give it **at least 2 GB of RAM or a
  swap file** — installing the `pandas`/`numpy`/`matplotlib` wheels can OOM a
  1 GB box.
- Only **outbound** HTTPS is needed (Angel One, Yahoo Finance, Telegram). The
  bot uses long polling, so no inbound ports and no webhook URL.
- Run the bot on **one host at a time**. Two pollers on the same token fight
  over updates and Telegram returns `Conflict: terminated by other getUpdates`.
- The instance needs the Angel One account's TOTP seed, so treat the box as
  sensitive: key-only SSH, locked-down security group.
