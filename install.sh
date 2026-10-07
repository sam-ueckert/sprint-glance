#!/bin/sh
# Sprint Glance installer for macOS: find a Python 3.9+ with Tk, then run setup.
set -eu
cd "$(dirname "$0")"

has_tk() {
  "$1" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 9) or tkinter.TkVersion < 8.6)' 2>/dev/null
}

# /usr/bin/python3 is skipped: Apple's build ships Tk 8.5, which Apple deprecated.
for py in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
          /usr/local/bin/python3 /opt/homebrew/bin/python3 "$(command -v python3 || true)"; do
  if [ -n "$py" ] && [ "$py" != /usr/bin/python3 ] && [ -x "$py" ] && has_tk "$py"; then
    exec "$py" sprint_glance.py setup
  fi
done

if command -v uv >/dev/null 2>&1; then
  printf 'No Python with Tk found. Install one with uv (about 40 MB)? [Y/n] '
  read -r reply
  case "$reply" in [nN]*) ;; *)
    uv python install 3.12
    py="$(uv python find 3.12)"
    has_tk "$py" && exec "$py" sprint_glance.py setup
  esac
fi

cat <<'EOF'
No Python with Tk found. Install one, then run ./install.sh again:
  - the macOS installer from https://www.python.org/downloads/ (includes Tk), or
  - brew install python-tk, or
  - uv python install 3.12 (https://docs.astral.sh/uv/)
EOF
exit 1
