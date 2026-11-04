#!/usr/bin/env bash
# Install kiwi-dl on macOS or Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/ruizhengu/kiwi-dl/main/install.sh | bash
#
# A branch, tag or commit instead of main:  ... | bash -s -- <ref>
# pipx even though uv is installed:         ... | KIWI_DL_INSTALLER=pipx bash
# A local checkout (what CI does):          KIWI_DL_SOURCE=/path/to/kiwi-dl bash install.sh
#
# kiwi-dl is installed as an isolated tool with uv, or pipx if uv is missing,
# so it never touches your system Python. Update with `kiwi-dl update`, remove
# with `kiwi-dl remove`.
set -euo pipefail

REPO="https://github.com/ruizhengu/kiwi-dl"
REF="${1:-${KIWI_DL_REF:-main}}"
SOURCE="${KIWI_DL_SOURCE:-git+${REPO}@${REF}}"
INSTALLER="${KIWI_DL_INSTALLER:-}"

if [ -t 1 ]; then
  bold=$'\033[1m' cyan=$'\033[36m' green=$'\033[32m' red=$'\033[31m' grey=$'\033[90m' reset=$'\033[0m'
else
  bold="" cyan="" green="" red="" grey="" reset=""
fi
ok() { printf '%s✓%s %s\n' "$green" "$reset" "$*"; }
step() { printf '%s➜%s %s\n' "$cyan" "$reset" "$*"; }
note() { printf '%s  %s%s\n' "$grey" "$*" "$reset"; }
fail() {
  printf '%s✗%s %s\n' "$red" "$reset" "$1" >&2
  shift
  for line in "$@"; do printf '  %s\n' "$line" >&2; done
  exit 1
}

case "$(uname -s)" in
  Darwin) os="macOS $(sw_vers -productVersion 2>/dev/null || true)" ;;
  Linux) os="Linux" ;;
  *) fail "kiwi-dl supports macOS and Linux only." ;;
esac

printf '%s%skiwi-dl%s installer\n' "$bold" "$cyan" "$reset"
note "⚙ ${os% } · $(uname -m)"
if [ -z "${KIWI_DL_SOURCE:-}" ]; then
  if [ "$REF" = main ]; then
    note "Installing the latest main; pass a release tag, such as v0.1.0, to pin one."
  else
    note "Installing ${REF}."
  fi
fi
echo

case "$INSTALLER" in
  "" | uv | pipx) ;;
  *) fail "KIWI_DL_INSTALLER must be uv or pipx, not '${INSTALLER}'." ;;
esac
if [ -n "$INSTALLER" ] && ! command -v "$INSTALLER" >/dev/null 2>&1; then
  fail "KIWI_DL_INSTALLER=${INSTALLER}, but ${INSTALLER} is not on your PATH."
fi

if [ "$INSTALLER" != pipx ] && command -v uv >/dev/null 2>&1; then
  step "Installing with uv from ${SOURCE}"
  uv tool install --force --reinstall "$SOURCE"
  hint="uv tool update-shell"
elif command -v pipx >/dev/null 2>&1; then
  step "Installing with pipx from ${SOURCE}"
  pipx install --force "$SOURCE"
  hint="pipx ensurepath"
else
  fail "kiwi-dl installs with uv (or pipx), and neither is on your PATH." \
    "Install uv, then run this again:" \
    "  brew install uv                                   # macOS" \
    "  curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS or Linux"
fi

echo
if command -v kiwi-dl >/dev/null 2>&1; then
  ok "Installed $(kiwi-dl --version)"
else
  ok "Installed"
  note "The install folder is not on your PATH yet: run \`${hint}\`, then open a new terminal."
fi
note "Run kiwi-dl for the menu, or kdl <link> to download straight away."
