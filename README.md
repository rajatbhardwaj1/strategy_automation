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

Python logging writes to `logs/main.log`, `logs/run_bot.log` and
`logs/utils.log`; pm2 only captures stdout/stderr and tracebacks, in
`logs/pm2-*.log`. Check both when debugging.

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
