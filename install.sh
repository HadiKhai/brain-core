#!/bin/sh
# Install a Brain: a new one, or this machine's copy of an existing one (--from).
#
#   curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/HadiKhai/brain-core/main/install.sh | sh -s -- --from git@github.com:<you>/<your-brain>.git
#
# Options:
#   --dir PATH          where the Brain lives (default ~/Brain)
#   --from URL          clone an existing Brain instead of creating one
#   --operator ID       your principal id (new Brain)
#   --title TEXT        the root's title (new Brain)
#   --runtimes LIST     agent runtimes, e.g. claude,codex (default: the ones installed)
#   --host NAME         host adapter (default t3)
#   --tz AREA/CITY      timezone (default: this machine's)
#   --core-url URL      where the core comes from (default $BRAIN_CORE_URL or the published core)
#   --home ID=PATH      this machine's path for a sealed node's home (repeatable, --from only)
#   --bin-dir PATH      where the `brain` command goes (default ~/.local/bin)
#   --no-modify-path    don't add --bin-dir to PATH in your shell's startup file
#   --yes               don't ask; use defaults
set -eu

CORE_URL="${BRAIN_CORE_URL:-https://github.com/HadiKhai/brain-core.git}"
DIR="$HOME/Brain"; FROM=""; OPERATOR=""; TITLE=""; RUNTIMES=""; HOST="t3"; TZONE=""
BIN_DIR="$HOME/.local/bin"; YES=0; HOMES=""; MODIFY_PATH=1

die() { echo "install: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "needs $1"; }
expand() { case "$1" in "~") echo "$HOME" ;; "~/"*) echo "$HOME/${1#\~/}" ;; *) echo "$1" ;; esac; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR=$(expand "$2"); shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --operator) OPERATOR="$2"; shift 2 ;;
    --title) TITLE="$2"; shift 2 ;;
    --runtimes) RUNTIMES="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --tz) TZONE="$2"; shift 2 ;;
    --core-url) CORE_URL="$2"; shift 2 ;;
    --home) HOMES="$HOMES
$2"; shift 2 ;;
    --bin-dir) BIN_DIR=$(expand "$2"); shift 2 ;;
    --no-modify-path) MODIFY_PATH=0; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,21p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (try --help)" ;;
  esac
done

need git
need python3
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "needs python3 3.9 or newer"
GIT="git -c protocol.file.allow=always"

interactive() { [ "$YES" = 0 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; }

# ask VAR "question" "default": keeps VAR if already set
ask() {
  eval "cur=\${$1}"
  [ -n "$cur" ] && return 0
  ans=""
  if interactive; then
    printf '%s [%s]: ' "$2" "$3" > /dev/tty
    read -r ans < /dev/tty || ans=""
  fi
  [ -n "$ans" ] || ans="$3"
  eval "$1=\$ans"
}

brain() { python3 "$DIR/core/bin/brain" "$@"; }

if [ -n "$FROM" ]; then
  # ---- this machine's copy of an existing Brain ----
  [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && die "$DIR exists and isn't empty"
  echo "Cloning $FROM -> $DIR"
  $GIT clone -q --recurse-submodules "$FROM" "$DIR"
  cd "$DIR"
  brain link >/dev/null
  echo "$HOMES" | while IFS= read -r pair; do
    [ -n "$pair" ] || continue
    brain set "${pair%%=*}" home="$(expand "${pair#*=}")" >/dev/null
  done
  brain machine --missing-homes | while IFS="$(printf '\t')" read -r id url; do
    [ -n "$id" ] || continue
    if ! interactive; then
      echo "  $id: home not set (later: brain set $id home=<path>)"
      continue
    fi
    printf '%s: path of its home on this machine (blank = later) [%s]: ' "$id" "$url" > /dev/tty
    read -r path < /dev/tty || path=""
    [ -n "$path" ] || continue
    path=$(expand "$path")
    if [ ! -e "$path" ] && [ "$url" != "none" ]; then
      printf 'Clone %s there? [y/N]: ' "$url" > /dev/tty
      read -r yn < /dev/tty || yn=""
      case "$yn" in y|Y|yes) git clone -q "$url" "$path" ;; esac
    fi
    brain set "$id" home="$path" >/dev/null
  done
else
  # ---- a new Brain ----
  [ -e "$DIR/instance.md" ] && die "$DIR is already a Brain (use --from on a new machine)"
  [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && die "$DIR exists and isn't empty"
  detected=""
  command -v claude >/dev/null 2>&1 && detected="claude"
  command -v codex >/dev/null 2>&1 && detected="${detected:+$detected,}codex"
  zone=$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##')
  me=$(id -un | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-\n' '-')
  ask OPERATOR "Your id (lowercase)" "$me"
  ask TITLE "Name of your Brain" "Brain"
  ask RUNTIMES "Agent runtimes" "${detected:-claude}"
  ask TZONE "Timezone" "${zone:-UTC}"
  mkdir -p "$DIR"
  git init -q -b main "$DIR"
  cd "$DIR"
  echo "Adding the core from $CORE_URL"
  $GIT submodule add -q -b main "$CORE_URL" core
  brain init "$DIR" --operator "$OPERATOR" --title "$TITLE" --runtimes "$RUNTIMES" --host "$HOST" --tz "$TZONE"
fi

brain link --command --bin-dir "$BIN_DIR"

# So `brain` works in every new terminal: put BIN_DIR on PATH in the shell's startup file.
on_path() { case ":$PATH:" in *":$1:"*) return 0 ;; *) return 1 ;; esac; }
NEW_SHELL=""
if [ "$MODIFY_PATH" = 1 ] && ! on_path "$BIN_DIR"; then
  case "$(basename "${SHELL:-sh}")" in
    zsh) rc="$HOME/.zshrc"; line="export PATH=\"$BIN_DIR:\$PATH\"" ;;
    bash) rc="$HOME/.bashrc"; [ "$(uname)" = Darwin ] && rc="$HOME/.bash_profile"
          line="export PATH=\"$BIN_DIR:\$PATH\"" ;;
    fish) rc="$HOME/.config/fish/config.fish"; line="fish_add_path \"$BIN_DIR\"" ;;
    *) rc="$HOME/.profile"; line="export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
  if ! grep -qsF "$line" "$rc"; then
    mkdir -p "$(dirname "$rc")"
    printf '\n# brain\n%s\n' "$line" >> "$rc"
    echo "Added $BIN_DIR to PATH in $rc"
  fi
  NEW_SHELL=1
fi
echo
if brain check >/dev/null 2>&1; then echo "✓ checks pass"; else echo "! run 'brain check' to see what's left"; fi
cat <<EOF

Done: $DIR
Next:
EOF
[ -z "$NEW_SHELL" ] || echo "  0. Open a new terminal so the \`brain\` command is found."
cat <<EOF
  1. In your host ($HOST), add a project for $DIR and start a thread in it.
  2. Tell it: "set up this machine" (it registers the projects and coordinators).
EOF
[ -n "$FROM" ] || echo "  3. Fill in \"About the operator\" in $DIR/AGENTS.md."
