#!/usr/bin/env bash
#
# One-shot setup for a freshly cloned repo on an EC2 instance.
# Idempotent: safe to re-run after a `git pull`.
#
#   bash scripts/setup_ec2.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '\n==> %s\n' "$1"; }

# --- OS package manager ----------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
  PKG="sudo dnf install -y"
  sudo dnf -y makecache
elif command -v apt-get >/dev/null 2>&1; then
  PKG="sudo apt-get install -y"
  sudo apt-get update -y
else
  echo "Unsupported distro: need dnf (Amazon Linux) or apt (Ubuntu/Debian)." >&2
  exit 1
fi

# --- Timezone --------------------------------------------------------------
# main.py uses the `schedule` library, which fires on local wall-clock time.
# NSE hours are IST, so the box must be IST or every trade time is wrong.
log "Setting system timezone to Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata
timedatectl | sed -n '1,4p'

# --- Python ----------------------------------------------------------------
log "Installing Python toolchain"
$PKG python3 python3-pip git
# venv ships separately on Debian/Ubuntu.
if command -v apt-get >/dev/null 2>&1; then
  PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  $PKG "python3-venv" "python${PYVER}-venv" || $PKG python3-venv
fi
python3 --version

# --- Node + pm2 ------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  log "Installing Node.js 20"
  curl -fsSL https://rpm.nodesource.com/setup_20.x -o /tmp/ns.sh 2>/dev/null \
    || curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/ns.sh
  sudo -E bash /tmp/ns.sh
  $PKG nodejs
fi
node --version

if ! command -v pm2 >/dev/null 2>&1; then
  log "Installing pm2"
  sudo npm install -g pm2
fi
pm2 --version

# --- Virtualenv + dependencies --------------------------------------------
log "Creating virtualenv at $ROOT/venv"
[ -d venv ] || python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip wheel
# requirements.txt references the bundled NorenRestApi wheel by relative path,
# so pip must be invoked from the repo root (we cd'd there above).
./venv/bin/python -m pip install -r requirements.txt

# --- Runtime directories ---------------------------------------------------
# Every module calls logging.basicConfig(filename="logs/*.log"), which fails
# outright if the directory is missing. logs/ is not tracked in git.
log "Creating logs/"
mkdir -p logs

# --- Config ----------------------------------------------------------------
# src/config.yaml holds live broker credentials and is gitignored, so a fresh
# clone never has it.
if [ ! -f src/config.yaml ]; then
  cp src/config.example.yaml src/config.yaml
  chmod 600 src/config.yaml
  log "Created src/config.yaml from the template - FILL IN YOUR CREDENTIALS before starting"
  MISSING_CONFIG=1
else
  chmod 600 src/config.yaml
  log "src/config.yaml already present"
  MISSING_CONFIG=0
fi

# --- Smoke test ------------------------------------------------------------
log "Import smoke test"
./venv/bin/python -c "import src.runner, src.telegram_bot; print('imports OK')"

cat <<MSG

==> Setup complete.

Next steps:
MSG
if [ "$MISSING_CONFIG" = "1" ]; then
  cat <<'MSG'
  1. Edit src/config.yaml and fill in the trading / telegram / strategy values.
  2. pm2 start ecosystem.config.js
MSG
else
  echo "  1. pm2 start ecosystem.config.js"
fi
cat <<'MSG'
  3. pm2 save && pm2 startup   # survive reboots (run the command it prints)
  4. pm2 logs                  # watch both processes
MSG
