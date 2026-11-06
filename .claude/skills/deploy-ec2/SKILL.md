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

### 4. Register the instance IP with Angel One

**Order placement fails without this, while everything else looks fine.**
Angel One's SmartAPI only accepts `placeOrder` from IPs registered on the
account. Reads — login, holdings, order book — are not restricted, so
`/portfolio` and `/show_etfs` work perfectly on an unregistered box and the
deployment looks healthy right up to the first trade, which fails with:

```
AG7002: <ip> is not a registered IP, please check your registered IP.
```

Add the instance's public IP in the Angel One SmartAPI app settings. Use an
**Elastic IP**: a stop/start changes the public IP and silently breaks trading
again, in exactly the way that only surfaces at 15:20.

Verify from the box with a read, then confirm the registration separately —
there is no read-only call that exercises the IP check:

```bash
curl -s https://checkip.amazonaws.com    # the IP to register
```

### 5. Start under pm2

```bash
pm2 start ecosystem.config.js
```

### 6. Make it survive reboots — and verify it

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

### 7. Verify the stack (read-only)

```bash
pm2 list                      # both online, restarts=0
tail -20 logs/utils.log       # see the logging note below
./venv/bin/python -c "import src.runner, src.telegram_bot; print('imports OK')"
```

Then have the user send `/portfolio` and `/show_etfs` in Telegram. Those two
commands exercise broker TOTP login, holdings fetch, yfinance and Telegram
delivery — most of the stack, with no order placement.

### 8. Prove the scheduler fires — `smoke_test_schedule.py`

This is the sign-off step, and the stand-in for a live order test. The bundled
`smoke_test_schedule.py` (next to this file) registers a job through the exact
`getattr(schedule.every(), <weekday>).at("HH:MM")` mechanism `main.py` uses,
then only logs into the broker and reads holdings — no order-placing code path
is reachable from it. **Run it from the repo root**, under pm2, so the pm2
environment is what gets exercised:

```bash
pm2 delete schedule-smoke 2>/dev/null
pm2 start .claude/skills/deploy-ec2/smoke_test_schedule.py --name schedule-smoke \
  --interpreter ./venv/bin/python --no-autorestart -- 2
pm2 logs schedule-smoke --lines 50
```

It fires 2 minutes out and sends a Telegram confirmation ending "No orders were
placed." Pass a different integer to change the delay.

Reading the result:

- **Exit code 0** — fired on time. pm2 reports a clean exit as `stopped`.
- **`errored` in pm2** — it missed the target by 3 minutes and `sys.exit(1)`d.
  The scheduler would miss real trade times; investigate the clock and TZ.
- It also sends a Telegram warning on failure, so an absent message is itself
  a signal.

Two things that have made a passing run look broken:

- **Run it before midnight IST, on a weekday.** It registers on the target
  time's weekday, matching `main.py`'s Mon–Fri registration. Started late on a
  Friday night with a delay that rolls past midnight, the job lands on
  Saturday, which `main.py` never schedules — nothing fires until Monday.
- **An empty out log is not a failure.** Judge by exit status and the Telegram
  message. (The script now line-buffers stdout, which fixes this.)

Clean up: `pm2 delete schedule-smoke`.

Verified working on Ubuntu 26.04 at 2026-11-06 23:17:00 IST: job fired on the
target minute, broker login succeeded, Telegram message delivered.

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
11. **Never run the live tests on a deployed box.**
    `test/test_angel_api.py::test_real_buy_order` places a real BUY order, and
    it is the first test in the file; `test_trailing_profit` runs
    `sell_order_processing` live. They are now marked `live` and deselected by
    default, so a bare `pytest` is safe (23 offline tests, ~0.1s) — but never
    pass `-m live` on a box holding real credentials. A run on 2026-11-06
    attempted a 1-qty SILVERBEES-EQ buy; only the unregistered-IP rejection
    stopped it.
12. **Angel One order rejections land in `logs/<date>/app.log`**, written by
    SmartApi's own logzero handler — not in `logs/utils.log` where everything
    else goes. Check it when an order silently fails to appear.

## Bot token in logs (fixed)

`python-telegram-bot` calls the API through httpx, whose INFO request logging
wrote the full bot token in cleartext into `logs/utils.log` on every poll.
`src/telegram_utils.py` now sets the `httpx` and `httpcore` loggers to WARNING;
it is the right place because both entrypoints import it, so one setting covers
every telegram caller.

Logs written before that fix still contain the token. When bringing up a box
that has older logs, `chmod 600 logs/*.log`, and treat a token that was exposed
as worth regenerating in @BotFather.

Still strip tokens whenever echoing logs anywhere, in case another library
starts logging URLs:

```bash
sed -E 's#bot[0-9]+:[A-Za-z0-9_-]+#bot<REDACTED>#g'
```

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
