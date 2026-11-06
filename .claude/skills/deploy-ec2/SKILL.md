---
name: deploy-ec2
description: Set up or re-deploy this SIP strategy bot on an EC2 instance under pm2, one shot. Use when bringing up a fresh instance, recovering a broken deploy, or verifying a running one. Covers python/venv, pm2, timezone, config, boot persistence and a read-only scheduling smoke test.
---

# Deploy the strategy bot on EC2 (pm2)

Brings a freshly cloned repo to a running, reboot-surviving deployment, then
verifies it without placing any orders.

## Scope

**This skill never places a live order.** It does not repoint trade times to
test the buy path, and it does not call `run_buy`, `run_trailing_sell`,
`buy_order_processing` or `sell_order_processing`. Those reach
`AngelAPI.placeOrder` and transact on a live account. If asked to test the
real order path, say that is the user's own keystroke during market hours, and
offer the read-only smoke test in step 6 instead.

## What runs

| pm2 app | Script | Role |
|---|---|---|
| `strategy-scheduler` | `main.py` | Trailing sell every 10 min 09:20–15:20, daily buy 15:20, Mon–Fri |
| `telegram-bot` | `run_bot.py` | Telegram commands (`/portfolio`, `/show_etfs`) via long polling |

Both must run with the **repo root as cwd** — every path in the code is
relative to it (`src/config.yaml`, `logs/*.log`). `ecosystem.config.js` already
pins `cwd` and the venv interpreter.

## Steps

### 1. Connect

```bash
ssh -i tradebot_novapr.pem ubuntu@<INSTANCE_IP>
```

The key lives at the repo root and must be mode `400`. Confirm the instance IP
with the user — do not assume a previous one is still current.

### 2. Run the setup script

```bash
cd ~/strategy_automation && git pull && bash scripts/setup_ec2.sh
```

Idempotent — safe to re-run after a partial failure or a `git pull`. It handles
every trap in the next section. Read its output rather than assuming success.

### 3. Fill in credentials

`src/config.yaml` is gitignored, so a fresh clone never has it. The script
copies `src/config.example.yaml` to it at mode `600` and leaves an existing one
untouched. Sections: `trading` (Angel One — `totp_secret` is the base32 seed,
not a code), `telegram` (`bot_token`, and `chat_id` which gates the bot to one
chat), `strategy`, optional `llm`.

**Never print credential values** when inspecting this file. Redact:

```bash
sed -E 's/(^[[:space:]]*[A-Za-z_]+:[[:space:]]*).+/\1SET/' src/config.yaml
```

### 4. Start under pm2

```bash
pm2 start ecosystem.config.js
```

### 5. Make it survive reboots — and verify it

```bash
pm2 save
sudo env PATH=$PATH:/usr/bin $(which pm2) startup systemd -u ubuntu --hp /home/ubuntu
```

`systemctl is-active pm2-ubuntu` reporting `inactive` right after this is
expected: the running daemon was not launched by systemd. Do not stop there —
actually prove resurrection works, during market-closed hours only:

```bash
pm2 kill && sudo systemctl start pm2-ubuntu && pm2 list
```

Both apps should come back `online`. If they don't, `pm2 start
ecosystem.config.js` recovers.

### 6. Verify (read-only)

```bash
pm2 list                      # both online, restarts=0
tail -20 logs/utils.log       # see the logging note below
./venv/bin/python -c "import src.runner, src.telegram_bot; print('imports OK')"
```

Then have the user send `/portfolio` and `/show_etfs` in Telegram. Those two
commands exercise broker TOTP login, holdings fetch, yfinance and Telegram
delivery — most of the stack, with no order placement.

To prove the `schedule` wiring fires at the right IST wall-clock time, run the
read-only smoke test. It registers a job through the exact mechanism `main.py`
uses, but only logs in and reads holdings:

```bash
pm2 delete schedule-smoke 2>/dev/null
pm2 start scripts/smoke_test_schedule.py --name schedule-smoke \
  --interpreter ./venv/bin/python --no-autorestart --no-daemon -- 2
```

Fires 2 minutes out and sends a Telegram confirmation. Exit code 0 means it
fired on time; it `sys.exit(1)`s if it misses by 3 minutes. pm2 shows a clean
exit as `stopped` and a failure as `errored`. Pass `--no-daemon` or set
`PYTHONUNBUFFERED=1`, otherwise stdout stays buffered and the out log looks
empty even on success — judge by exit status and `logs/`, not an empty log.
Clean up with `pm2 delete schedule-smoke`.

## Traps this deployment has actually hit

Each of these broke a real deploy. `setup_ec2.sh` handles the first five.

1. **Timezone.** `main.py` schedules via the `schedule` library, which fires on
   local wall-clock time with no timezone support. A default-UTC instance runs
   the 15:20 buy at 20:50 IST, after close. The box must be `Asia/Kolkata`.
2. **System Python too new.** The pins (`numpy 2.0.2`, `pandas 2.3.2`,
   `matplotlib 3.9.4`) top out at **cp312 wheels**. Ubuntu 26.04 ships Python
   3.14, so pip tries to build numpy/pandas from source and fails. The script
   provisions a standalone CPython 3.12 with `uv` and leaves the system
   interpreter alone. Prefer this over unpinning — it keeps the tested
   dependency set under a live strategy.
3. **`logzero` is an undeclared dependency.** `smartapi-python` 1.5.5 imports
   it but omits it from its metadata, so pip resolves without it and `pip
   check` reports nothing wrong. It fails only at import. Pinned in
   `requirements.txt`; if a similar `ModuleNotFoundError` appears for another
   module, it is the same class of bug — install and pin it.
4. **`logs/` is untracked.** Every module calls
   `logging.basicConfig(filename="logs/*.log")`, which fails outright if the
   directory is missing. `mkdir -p logs`.
5. **The `NorenRestApi` wheel path.** `requirements.txt` references the bundled
   wheel in `ShoonyaApi-py/dist/` by **relative** path, so
   `pip install -r requirements.txt` must run from the repo root. It is
   genuinely needed: `src/api_helper.py` imports `NorenRestApiPy`, pulled in
   transitively by `runner.py`.
6. **Scripts under `scripts/` cannot import `src`.** Python puts the script's
   own directory on `sys.path`, not the cwd — unlike root-level `main.py`. Such
   scripts need an explicit `sys.path.insert` of the repo root.
7. **Both processes log to `logs/utils.log`**, not `logs/main.log` or
   `logs/run_bot.log`, despite the `LOG_FILE` constants. `src/utils.py` calls
   `logging.basicConfig()` at import, so both entrypoints' later calls are
   no-ops. pm2 separately captures stdout/stderr and hard crashes in
   `logs/pm2-*.log`. Check both.
8. **One host at a time.** Two schedulers place duplicate buy orders; two
   pollers on one bot token make Telegram return `Conflict: terminated by other
   getUpdates`. Before starting, confirm any previous host is stopped.
9. **Memory.** Give the instance ≥2 GB RAM or a swap file — installing the
   pandas/numpy/matplotlib wheels can OOM a 1 GB box.
10. **`[I ... smartConnect:124] in pool` on stderr** is ordinary logzero
    chatter from SmartApi, not an error.

## Known issue: bot token in logs

`python-telegram-bot`'s httpx logging runs at INFO, so every request writes the
full bot token in cleartext into `logs/utils.log`, which grows unbounded. One
line in `run_bot.py` stops it:

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

Not applied yet — raise it, don't silently change it. Always strip tokens when
echoing logs: `sed -E 's#bot[0-9]+:[A-Za-z0-9_-]+#bot<REDACTED>#g'`.

## Operating

```bash
pm2 status
pm2 logs strategy-scheduler --lines 100
pm2 restart ecosystem.config.js    # after a git pull
pm2 stop strategy-scheduler        # halt trading, keep the bot up
```

Only outbound HTTPS is needed (Angel One, Yahoo Finance, Telegram) — the bot
long-polls, so no inbound ports and no webhook. The box holds the account's
TOTP seed: key-only SSH, tight security group.
