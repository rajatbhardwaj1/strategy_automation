#!/usr/bin/env bash
#
# One-shot setup for a freshly cloned repo on an EC2 instance.
# Idempotent: safe to re-run after a `git pull` or a partial failure.
#
#   bash scripts/setup_ec2.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# requirements.txt is pinned to a dependency set whose newest wheels are
# cp312 (numpy 2.0.2, pandas 2.3.2, matplotlib 3.9.4). On a newer interpreter
# pip falls back to building them from source, which fails. Keep the venv on a
# version the pins actually ship wheels for.
PY_TARGET="3.12"
PY_MIN_MINOR=9
PY_MAX_MINOR=12

log() { printf '\n==> %s\n' "$1"; }

# --- OS package manager ----------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  OS_FAMILY="debian"
  PKG="sudo apt-get install -y"
  sudo apt-get update -y
elif command -v dnf >/dev/null 2>&1; then
  OS_FAMILY="rhel"
  PKG="sudo dnf install -y"
  sudo dnf -y makecache
else
  echo "Unsupported distro: need apt (Ubuntu/Debian) or dnf (Amazon Linux)." >&2
  exit 1
fi
log "Detected $OS_FAMILY"

# --- Timezone --------------------------------------------------------------
# main.py uses the `schedule` library, which fires on local wall-clock time.
# NSE hours are IST, so the box must be IST or every trade time is wrong.
log "Setting system timezone to Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata
timedatectl | sed -n '1,4p'

# --- Base packages ---------------------------------------------------------
log "Installing base packages"
$PKG git curl python3
python3 --version

# --- Pick an interpreter for the venv --------------------------------------
SYS_MINOR="$(python3 -c 'import sys; print(sys.version_info[1])')"
SYS_MAJOR="$(python3 -c 'import sys; print(sys.version_info[0])')"
VENV_PY=""

if [ "$SYS_MAJOR" -eq 3 ] && [ "$SYS_MINOR" -ge "$PY_MIN_MINOR" ] && [ "$SYS_MINOR" -le "$PY_MAX_MINOR" ]; then
  log "System python3 (3.$SYS_MINOR) is compatible with the pins; using it"
  $PKG python3-pip
  if [ "$OS_FAMILY" = "debian" ]; then
    # venv ships separately on Debian/Ubuntu.
    $PKG "python3.${SYS_MINOR}-venv" || $PKG python3-venv
  fi
  [ -d venv ] || python3 -m venv venv
  ./venv/bin/python -m pip install --upgrade pip wheel
  INSTALL_CMD="./venv/bin/python -m pip install -r requirements.txt"
else
  log "System python3 is 3.$SYS_MINOR; provisioning a standalone CPython $PY_TARGET with uv"
  # uv fetches a self-contained CPython build, so the system interpreter and
  # any distro packages depending on it are left alone.
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv --version
  uv python install "$PY_TARGET"
  [ -d venv ] || uv venv --seed --python "$PY_TARGET" venv
  INSTALL_CMD="uv pip install --python ./venv/bin/python -r requirements.txt"
fi
./venv/bin/python --version

# --- Node + pm2 ------------------------------------------------------------
node_major() { node -v 2>/dev/null | sed 's/^v//; s/\..*//'; }

if [ "$(node_major)" = "" ] || [ "$(node_major)" -lt 18 ]; then
  log "Installing Node.js from distro packages"
  $PKG nodejs npm || true
fi
if [ "$(node_major)" = "" ] || [ "$(node_major)" -lt 18 ]; then
  log "Distro Node is missing or too old; using NodeSource 20"
  if [ "$OS_FAMILY" = "debian" ]; then
    NODE_SETUP_URL="https://deb.nodesource.com/setup_20.x"
  else
    NODE_SETUP_URL="https://rpm.nodesource.com/setup_20.x"
  fi
  curl -fsSL "$NODE_SETUP_URL" -o /tmp/nodesource_setup.sh
  sudo bash /tmp/nodesource_setup.sh
  $PKG nodejs
fi
node --version

if ! command -v pm2 >/dev/null 2>&1; then
  log "Installing pm2"
  sudo npm install -g pm2
fi
pm2 --version

# --- Python dependencies ---------------------------------------------------
# requirements.txt references the bundled NorenRestApi wheel by relative path,
# so this must run from the repo root (we cd'd there above).
log "Installing Python dependencies"
eval "$INSTALL_CMD"

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

log "Setup complete."
echo
echo "Next steps:"
if [ "$MISSING_CONFIG" = "1" ]; then
  echo "  1. Edit src/config.yaml and fill in the trading / telegram / strategy values."
  echo "  2. pm2 start ecosystem.config.js"
else
  echo "  1. pm2 start ecosystem.config.js"
fi
cat <<'MSG'
  3. pm2 save && pm2 startup   # survive reboots (run the command it prints)
  4. pm2 logs                  # watch both processes
MSG
