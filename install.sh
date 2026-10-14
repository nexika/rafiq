#!/usr/bin/env bash
# Install Rafiq on Linux: a virtual environment in .venv next to this script, the dependencies (with the
# offline voices and Google), then `rafiq setup` for the keys, voices, models and the wake word.
#
#   ./install.sh               install, then run the setup wizard
#   ./install.sh --no-setup    install only (run `rafiq setup` later)
#   ./install.sh --link-only   only put `rafiq` on the PATH, as ~/.local/bin/rafiq (after moving the folder)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
setup=1
link_only=0
for arg in "$@"; do
  case "$arg" in
    --no-setup) setup=0 ;;
    --link-only) link_only=1 ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# The guides say `rafiq doctor`, not `.venv/bin/rafiq doctor` (#171): link it into ~/.local/bin, which
# Fedora and Ubuntu put on the PATH. A link from an older checkout is replaced; a file of your own is not.
rafiq_cmd=.venv/bin/rafiq
link_rafiq() {
  local bin="$HOME/.local/bin"
  local link="$bin/rafiq"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "$link already exists and is not a link, so I left it; use $(pwd)/.venv/bin/rafiq"
    return
  fi
  mkdir -p "$bin"
  ln -sfn "$(pwd)/.venv/bin/rafiq" "$link"
  case ":$PATH:" in
    *":$bin:"*)
      rafiq_cmd=rafiq
      echo "Linked $link: \`rafiq doctor\`, \`rafiq run\` and the rest work from any folder." ;;
    *)
      echo "Linked $link, but $bin is not on your PATH yet. Log out and in again, or add"
      echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
      echo "to ~/.bashrc. Until then, use $(pwd)/.venv/bin/rafiq" ;;
  esac
}

if [ "$link_only" = 1 ]; then
  if [ ! -x .venv/bin/rafiq ]; then
    echo "Rafiq is not installed here yet: run ./install.sh first." >&2
    exit 1
  fi
  link_rafiq
  exit 0
fi

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
link_rafiq
if [ "$setup" = 1 ]; then
  exec .venv/bin/rafiq setup
fi
echo "Next: $rafiq_cmd setup"
