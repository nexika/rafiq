#!/usr/bin/env bash
# Install Rafiq on Linux or macOS: a virtual environment in .venv next to this script, the dependencies (with
# the offline voices and Google), then `rafiq setup` for the keys, voices, models and the wake word.
#
#   ./install.sh               install, then run the setup wizard
#   ./install.sh --no-setup    install only (run `rafiq setup` later)
#   ./install.sh --link-only   only put `rafiq` on the PATH, as ~/.local/bin/rafiq (after moving the folder)
set -euo pipefail

# macOS before 12.3 has no readlink -f: there, the folder the script was started from.
here=$(readlink -f "$0" 2>/dev/null || echo "$0")
cd "$(dirname "$here")"
mac=0
[ "$(uname -s)" = Darwin ] && mac=1
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
      local rc=~/.bashrc
      [ "$mac" = 1 ] && rc=~/.zshrc      # macOS's shell is zsh, and ~/.local/bin isn't on its PATH
      echo "Linked $link, but $bin is not on your PATH yet. Add"
      echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
      echo "to $rc and open a new terminal. Until then, use $(pwd)/.venv/bin/rafiq" ;;
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
if [ "$mac" = 1 ]; then
  get_python="brew install python@3.12, then PYTHON=python3.12 ./install.sh (Homebrew: https://brew.sh)"
  get_ffmpeg="brew install ffmpeg"
else
  get_python="Fedora: sudo dnf install python3, Ubuntu: sudo apt install python3-venv"
  get_ffmpeg="Fedora: sudo dnf install ffmpeg    Ubuntu: sudo apt install ffmpeg"
fi
if ! command -v "$python" >/dev/null; then
  echo "Python 3.11 or newer is needed ($get_python)." >&2
  exit 1
fi
if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  # macOS's own python3 is 3.9: Homebrew's is the one to use.
  echo "Python 3.11 or newer is needed; $("$python" --version) found ($get_python)." >&2
  exit 1
fi
if ! command -v ffplay >/dev/null; then
  echo "Warning: ffmpeg (ffplay) is missing, so Rafiq cannot play its voice." >&2
  echo "  $get_ffmpeg" >&2
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv..."
  "$python" -m venv .venv
fi
echo "Installing Rafiq and its dependencies (a few minutes the first time)..."
.venv/bin/python -m pip install --quiet --upgrade pip
if [ "$mac" = 1 ]; then
  # The offline voices (Piper) may have no wheel for this Mac: Rafiq works without them, with the online voices.
  .venv/bin/python -m pip install --quiet -e ".[google]"
  .venv/bin/python -m pip install --quiet -e ".[google,offline-voice]" \
    || echo "Note: the offline voices (piper-tts) did not install here; Rafiq uses the online voices." >&2
  echo "macOS will ask to allow the microphone the first time Rafiq listens. For typing, gestures and power, allow"
  echo "your terminal in System Settings > Privacy & Security > Accessibility. rafiq doctor shows what is missing."
else
  .venv/bin/python -m pip install --quiet -e ".[google,offline-voice]"
fi

echo "Installed: $(pwd)/.venv/bin/rafiq"
link_rafiq
if [ "$setup" = 1 ]; then
  exec .venv/bin/rafiq setup
fi
echo "Next: $rafiq_cmd setup"
