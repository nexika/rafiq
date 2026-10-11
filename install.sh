#!/usr/bin/env bash
# Install Rafiq on Linux: a virtual environment in .venv next to this script, the dependencies (with the
# offline voices and Google), then `rafiq setup` for the keys, voices, models and the wake word.
#
#   ./install.sh               install, then run the setup wizard
#   ./install.sh --no-setup    install only (run `.venv/bin/rafiq setup` later)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
setup=1
for arg in "$@"; do
  case "$arg" in
    --no-setup) setup=0 ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

python=${PYTHON:-python3}
if ! command -v "$python" >/dev/null; then
  echo "Python 3.11 or newer is needed (Fedora: sudo dnf install python3, Ubuntu: sudo apt install python3-venv)." >&2
  exit 1
fi
if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "Python 3.11 or newer is needed; $("$python" --version) found. Set PYTHON=python3.12 to pick another." >&2
  exit 1
fi
if ! command -v ffplay >/dev/null; then
  echo "Warning: ffmpeg (ffplay) is missing, so Rafiq cannot play its voice." >&2
  echo "  Fedora: sudo dnf install ffmpeg    Ubuntu: sudo apt install ffmpeg" >&2
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv..."
  "$python" -m venv .venv
fi
echo "Installing Rafiq and its dependencies (a few minutes the first time)..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[google,offline-voice]"

echo "Installed: $(pwd)/.venv/bin/rafiq"
if [ "$setup" = 1 ]; then
  exec .venv/bin/rafiq setup
fi
echo "Next: .venv/bin/rafiq setup"
