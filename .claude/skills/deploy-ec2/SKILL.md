---
name: deploy-ec2
description: Set up or re-deploy this SIP strategy bot on an EC2 instance under pm2, one shot, from a bare instance. Use when bringing up a new instance, recovering a broken deploy, or verifying a running one. Covers provisioning and swap, the write-enabled deploy key the private repo and trade logger need, python/venv, pm2 and boot persistence, timezone, Angel One IP registration, the trade recorder, log rotation, and a read-only scheduling smoke test.
---

# Deploy the strategy bot on EC2 (pm2)

Takes a bare EC2 instance to a running, reboot-surviving deployment, then
verifies it without placing any orders. Starts from nothing: the repo is
private, so even cloning it needs a step.

## Scope

**This skill never places a live order.** It does not repoint trade times to
test the buy path, and it does not call `run_buy`, `run_trailing_sell`,
`buy_order_processing` or `sell_order_processing`. Those reach
`AngelAPI.placeOrder` and transact on a live account. If asked to test the
real order path, say that is the user's own keystroke during market hours, and
offer the read-only smoke test in step 11 instead.

## What runs

| pm2 app | Script | Role |
|---|---|---|
| `strategy-scheduler` | `main.py` | Trailing sell every 10 min 09:20–15:20, daily buy 15:20, Mon–Fri |
| `telegram-bot` | `run_bot.py` | Telegram commands (`/portfolio`, `/show_etfs`) via long polling |
| `trade-logger` | `trade_logger.py` | Records the day's trades at 16:05, regenerates `trades/REPORT.md`, pushes both |

All three must run with the **repo root as cwd** — every path in the code is
relative to it (`src/config.yaml`, `logs/*.log`). `ecosystem.config.js` already
pins `cwd` and the venv interpreter.

## Steps

### 1. Provision the instance

Ubuntu 24.04 or 26.04, `t3.micro` or larger, in a region close to NSE latency-wise
(`ap-south-1` is the obvious choice; this deployment happens to run in
`ap-southeast-2`).

**Attach an Elastic IP before going any further.** Angel One authorises order
placement per IP (step 6), so a public IP that changes on stop/start silently
breaks trading. Allocate and associate it first, then register that address.

**Give it ≥2 GB of RAM, or add swap.** Installing the pandas/numpy/matplotlib
wheels will OOM a 1 GB box part-way through, leaving a half-built venv:

```bash
free -h   # if total is under 2G, add swap:
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Security group: inbound SSH (port 22) from your IP only. Nothing else — the
Telegram bot long-polls, so no inbound ports and no webhook. Outbound HTTPS is
all the app needs (Angel One, Yahoo Finance, Telegram, GitHub).

### 2. Give the box repo access, and clone

The repo is **private**, so `git clone` needs a key on the box. The
`trade-logger` app also *pushes* (step 10), so the key must be write-enabled —
a read-only deploy key brings everything up and then fails once a day at 16:05.

On the instance:

```bash
ssh-keygen -t ed25519 -C "ec2-tradebot" -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Add that public key to the GitHub repo under **Settings → Deploy keys → Add
deploy key**, and tick **Allow write access**. Then clone to the exact
directory name the rest of this runbook uses — note the **underscore**, which
differs from the hyphenated local checkout:

```bash
git clone git@github.com:<owner>/strategy_automation.git ~/strategy_automation
cd ~/strategy_automation && git log --oneline -1
```

If the clone hangs on a host-key prompt, accept GitHub's key first:
`ssh-keyscan github.com >> ~/.ssh/known_hosts`.

### 3. Connect

```bash
ssh -i tradebot_novapr.pem ubuntu@<INSTANCE_IP>
```

This is the operator's own key for reaching the box, separate from the deploy
key generated in step 2. It lives at the root of the local checkout and must be
mode `400`. Confirm the instance IP with the user — do not assume a previous
one is still current, and note that associating an Elastic IP changes it.

### 4. Run the setup script

```bash
cd ~/strategy_automation && git pull && bash scripts/setup_ec2.sh
```

Idempotent — safe to re-run after a partial failure or a `git pull`. It handles
every trap in the next section. Read its output rather than assuming success.

### 5. Fill in credentials

`src/config.yaml` is gitignored, so a fresh clone never has it. The script
copies `src/config.example.yaml` to it at mode `600` and leaves an existing one
untouched. Sections: `trading` (Angel One — `totp_secret` is the base32 seed,
not a code), `telegram` (`bot_token`, and `chat_id` which gates the bot to one
chat), `strategy`, optional `llm`.

**Never print credential values** when inspecting this file. Redact:

```bash
sed -E 's/(^[[:space:]]*[A-Za-z_]+:[[:space:]]*).+/\1SET/' src/config.yaml
```

### 6. Register the instance IP with Angel One

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

The current deployment is Elastic IP **3.24.151.30** (associated 2026-11-06,
replacing 3.25.140.52). Associating an EIP also changes the SSH target, so
step 3 uses the same address.

Verify from the box with a read, then confirm the registration separately —
there is no read-only call that exercises the IP check:

```bash
curl -s https://checkip.amazonaws.com    # the IP to register
```

### 7. Start under pm2

```bash
pm2 start ecosystem.config.js
```

### 8. Make it survive reboots — and verify it

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

### 9. Verify the stack (read-only)

```bash
pm2 list                      # both online, restarts=0
tail -20 logs/utils.log       # see the logging note below
./venv/bin/python -c "import src.runner, src.telegram_bot; print('imports OK')"
```

Then have the user send `/portfolio` and `/show_etfs` in Telegram. Those two
commands exercise broker TOTP login, holdings fetch, yfinance and Telegram
delivery — most of the stack, with no order placement.

### 10. Set up the trade recorder

`trade-logger` needs two things the other apps do not.

**Git push access.** It commits `trades/trades.csv` and `trades/REPORT.md` and
pushes them, so the box needs a deploy key with write access:

```bash
git -C ~/strategy_automation push --dry-run origin main   # must say "Everything up-to-date"
```

No git identity is needed — `src/git_sync.py` passes one per commit rather than
writing to the box's config. If the dry run fails, add the box's
`~/.ssh/id_ed25519.pub` as a **write-enabled** deploy key on the GitHub repo.

**Confirm the repo is private** before enabling the push. The ledger is a
financial record. Unauthenticated access must be refused:

```bash
GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/<owner>/<repo>.git HEAD
```

Then verify the whole cycle once, which is read-only against the broker:

```bash
cd ~/strategy_automation
./venv/bin/python trade_logger.py --once --no-push   # merge + report, no git
./venv/bin/python trade_logger.py --once             # the real thing
pm2 start ecosystem.config.js --only trade-logger && pm2 save
```

To import historical broker statements (the only source of history beyond
today, and of charges):

```bash
mkdir -p trades/imports   # gitignored: raw statements carry the client code
./venv/bin/python trade_logger.py --import-statements trades/imports/*.xlsx trades/imports/*.csv --since 2025-10-01
```

Read the import summary. Any scrip it could not map to a ticker is listed —
add it to `SCRIP_TO_SYMBOL` in `src/trade_import.py` and re-import (merging is
idempotent, keyed on the broker's trade id). Leaving one unmapped splits an
instrument in two and corrupts the FIFO matching.

### 11. Prove the scheduler fires — `smoke_test_schedule.py`

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

### 12. Rotate the logs

`logs/utils.log` has **no rotation and grows without bound** — every module
logs there (see trap 7), and the bot polls Telegram every 10 seconds. Left
alone it will eventually fill the disk and take all three apps down.

```bash
sudo tee /etc/logrotate.d/strategy-automation >/dev/null <<'CONF'
/home/ubuntu/strategy_automation/logs/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0600 ubuntu ubuntu
}
CONF
sudo logrotate --debug /etc/logrotate.d/strategy-automation   # dry run
```

`copytruncate` matters: the Python processes hold the file open and never
reopen it, so a renamed file would leave them writing into a deleted inode and
the live log would appear frozen. `create 0600` keeps the mode tight, because
logs written before the httpx fix still contain the bot token.

### Final checklist

Everything green means done:

```bash
cd ~/strategy_automation
timedatectl | head -1                      # IST
./venv/bin/python --version                # 3.12.x
pm2 list                                   # 3 apps online, restarts 0
systemctl is-enabled pm2-ubuntu            # enabled
./venv/bin/python -m pytest -q              # 58 passed, 12 deselected
git status --porcelain --untracked-files=no # empty
git push --dry-run origin main             # Everything up-to-date
curl -s https://checkip.amazonaws.com      # matches the IP registered with Angel One
ls trades/REPORT.md trades/trades.csv      # both present
```

Then have the user send `/portfolio` in Telegram, and confirm the tap-to-sort
buttons respond.

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
11. **Never run the live tests on a deployed box.** `test_trailing_profit`
    runs `sell_order_processing` against the real account. Those modules are
    marked `live` and deselected by default, so a bare `pytest` is safe (23
    offline tests, ~0.1s) — but never pass `-m live` on a box holding real
    credentials. A run on 2026-11-06 attempted a 1-qty SILVERBEES-EQ buy from
    what was then `test_real_buy_order`; only the unregistered-IP rejection
    stopped it. That script now lives in `manual/`, outside pytest's reach.
12. **Angel One order rejections land in `logs/<date>/app.log`**, written by
    SmartApi's own logzero handler — not in `logs/utils.log` where everything
    else goes. Check it when an order silently fails to appear.
13. **`orderBook()` and `tradeBook()` are day-scoped.** They return today's
    activity only, so the ledger is an archive and a missed day is
    unrecoverable from the API. If `trade-logger` is down for a day, recover it
    from a contract-note statement, not from the API.
14. **`tradeBook()` returns `data: None`, not `[]`,** when there are no trades.
15. **Angel's `.xlsx` statements truncate some scrip names at 20 characters**
    while leaving others intact, so one ETF appears under two spellings in one
    file (`NIP IND ETF GOLD BEES` and `NIP IND ETF GOLD BEE`). `map_scrip`
    resolves a truncated name as a unique prefix of a known one. The `.csv`
    export does not truncate.
16. **Statements carry a charge-only line per order** — zero quantity, zero
    price, no Trade ID, marked `Intraday`, carrying a flat brokerage. It is not
    a trade; it is recorded with status `charges_only` so its cost counts while
    it stays out of the trade statistics.
17. **Logging in dumps `*_symbols.txt` exchange masters into the repo root.**
    Gitignored; harmless.
18. **The box pushes to the same branch you do.** Expect to `git pull --rebase`
    locally after a trade-record commit lands. `git_sync` never force-pushes.

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

## Placing a real test order

Out of scope for this skill to run — it transacts, so it is the user's own
keystroke. `manual/place_test_buy_order.py` exists for it, outside `test/` so
pytest cannot collect it and behind a typed confirmation:

```bash
cd ~/strategy_automation && ./venv/bin/python manual/place_test_buy_order.py
```

Defaults to 1 SILVERBEES as a LIMIT at ₹250, below market so it rests unfilled
rather than executing; `--symbol`, `--qty`, `--price`, `--order-type` override
that. It needs the instance IP registered (step 6), or it returns AG7002. Point
the user at it rather than running it.

## Operating

```bash
# Trade record and performance
./venv/bin/python trade_logger.py --once           # record today, report, push
./venv/bin/python trade_logger.py --report-only    # rebuild the report only
./venv/bin/python trade_logger.py --once --no-push # record without touching git
./venv/bin/python trade_logger.py --import-statements FILE... --since YYYY-MM-DD
```

```bash
pm2 status
pm2 logs strategy-scheduler --lines 100
pm2 restart ecosystem.config.js    # after a git pull
pm2 stop strategy-scheduler        # halt trading, keep the bot up
```

Only outbound HTTPS is needed (Angel One, Yahoo Finance, Telegram) — the bot
long-polls, so no inbound ports and no webhook. The box holds the account's
TOTP seed: key-only SSH, tight security group.
