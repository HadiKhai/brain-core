#!/bin/sh
# Runtime parity (axiom 22): every runtime, started in the same node, must see the same
# instruction chain and the same skills. Costs one short model call per runtime.
#   core/tests/parity.sh <node folder> [runtime ...]   (default: claude codex)
set -u
NODE=$(cd "${1:?usage: parity.sh <node folder> [runtime ...]}" && pwd); shift
[ $# -gt 0 ] || set -- claude codex
CORE=$(cd "$(dirname "$0")/.." && pwd)
SKILLS=$(ls "$CORE/skills")
PROMPT="Without using tools, answer from the instructions and skills already loaded in your context.
Line 1: the top-level (#) headings of every instruction file you were given, in order, joined by ' > '.
Line 2: your role, per those instructions, if this brief names none.
Line 3: the names of every skill available to you, comma-separated."
fail=0
for rt in "$@"; do
  case "$rt" in
    claude) out=$(cd "$NODE" && claude -p --model "${PARITY_CLAUDE_MODEL:-claude-sonnet-5-5}" "$PROMPT" 2>&1) ;;
    codex) out=$(cd "$NODE" && codex exec -s read-only "$PROMPT" 2>/dev/null) ;;
    *) echo "no parity runner for $rt"; fail=1; continue ;;
  esac
  echo "== $rt"; echo "$out" | tail -4
  for s in $SKILLS; do
    echo "$out" | grep -q "$s" || { echo "   ✗ $rt doesn't see skill $s"; fail=1; }
  done
done
[ $fail = 0 ] && echo "✓ parity: every runtime sees all $(echo "$SKILLS" | wc -w | tr -d ' ') skills"
exit $fail
