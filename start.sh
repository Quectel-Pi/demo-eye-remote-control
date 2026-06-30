#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${APP_DIR}"

echo "Activate the eye controller..."
echo "Starting application..."

# Prefer pyenv interpreter installed by install.sh so runtime modules (e.g. cv2) are available.
if [[ -x "${HOME}/.pyenv/shims/python3" ]]; then
	PYTHON_BIN="${HOME}/.pyenv/shims/python3"
elif command -v python3 >/dev/null 2>&1; then
	PYTHON_BIN="$(command -v python3)"
else
	echo "python3 not found"
	exit 1
fi

"${PYTHON_BIN}" ./src/main.py