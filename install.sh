#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_VER=3.10.15
VENV_PATH="$HOME/mediapipe_env"
DEPS=(make build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev
      libsqlite3-dev curl llvm libncursesw5-dev xz-utils tk-dev libxml2-dev
      libxmlsec1-dev libffi-dev liblzma-dev libncurses-dev git ffmpeg)

# ── 1. 系统依赖 ──────────────────────────────────────────────────────────────
echo "[1/4] Installing system dependencies..."
sudo apt update
sudo apt install -y "${DEPS[@]}"
sudo apt install -y libdouble-conversion3 libxcb-cursor0 || true

# ── 2. pyenv + Python ────────────────────────────────────────────────────────
echo "[2/4] Installing pyenv + Python $PY_VER (skipped if already present)..."
if [ ! -d "$HOME/.pyenv" ]; then
  git clone https://github.com/pyenv/pyenv.git "$HOME/.pyenv"
else
  echo "  pyenv already exists, skipping clone"
fi

RC="$HOME/.bashrc"
if ! grep -q 'PYENV_ROOT' "$RC" 2>/dev/null; then
  cat >> "$RC" <<'EOF'

# pyenv
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init --path)"
eval "$(pyenv init -)"
EOF
fi

export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init --path)"
eval "$(pyenv init -)"

pyenv install --skip-existing "$PY_VER"
pyenv global "$PY_VER"
echo "  Python version: $(python --version 2>&1 || python3 --version)"

# ── 3. 虚拟环境 + 依赖 ───────────────────────────────────────────────────────
echo "[3/4] Creating virtual environment at $VENV_PATH and installing dependencies..."
python -m venv "$VENV_PATH"
# shellcheck disable=SC1090
source "$VENV_PATH/bin/activate"

pip install --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
  pip install -r "$APP_DIR/requirements.txt"
else
  echo "  No requirements.txt found, skipping pip install."
fi

deactivate

# ── 4. 脚本权限 ──────────────────────────────────────────────────────────────
echo "[4/4] Granting execute permission to start.sh..."
chmod +x "$APP_DIR/start.sh"

echo ""
echo "Deployment complete."
echo "To start the project: cd $APP_DIR && ./start.sh"

