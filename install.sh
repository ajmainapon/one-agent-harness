#!/usr/bin/env sh
# One Agent Harness — Universal Installer for Linux & macOS (POSIX sh / bash / zsh)
set -e

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
else
    echo "Error: Python 3.9+ is required. Please install Python 3 and re-run ./install.sh" >&2
    exit 1
fi

echo "==> Installing One Agent Harness (Linux / macOS) using $PY_BIN..."
PYTHONPATH="$REPO_ROOT:${PYTHONPATH}" "$PY_BIN" -m harness install

echo "==> Starting Auto-Sync Watcher daemon..."
PYTHONPATH="$REPO_ROOT:${PYTHONPATH}" "$PY_BIN" -m harness watch --daemon || true

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo ""
        echo "NOTE: Add ~/.local/bin to your PATH so you can run 'harness' from anywhere:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "Done! Run '~/.local/bin/harness' or 'harness pick' in your terminal."
