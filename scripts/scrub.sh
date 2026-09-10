#!/usr/bin/env bash
# Fail the build if a name that must not be public reached this repository.
#
# This repository is public and its author also works on a private codebase.
# A fixture copied across, a sample sentence lifted from an internal writing
# guide, or a work address on a commit is a disclosure that cannot be undone
# once it is pushed. This check runs before every merge and blocks one.
#
# The patterns below are written with a bracketed letter — `pri[z]mal` — so
# that the words they hunt for never appear in this file in a form the check
# would find. The bracket matches one literal character, so the pattern is
# otherwise identical to the plain word.
set -uo pipefail

root="${1:-.}"
status=0

banned=(
  'pri[z]mal'
  'confi[g][ _-]?api'
  'switc[h][ _-]key'
  'provide[r][ _-]key'
  'routin[g][ _-]event'
  'managemen[t][ _-]key'
  'bifros[t]'
)

echo "Scrubbing $root for names that must not be public."

for pattern in "${banned[@]}"; do
  # grep exits 1 when it finds nothing, which is the outcome we want, so the
  # status is captured rather than chained.
  hits="$(grep -rniE --exclude-dir=.git --exclude-dir=node_modules "$pattern" "$root" 2>/dev/null)"
  if [ -n "$hits" ]; then
    echo "::error::Found a name that must not be public, matching /$pattern/:"
    printf '%s\n' "$hits"
    status=1
  fi
done

# The posting identity is the other half of the same disclosure. An address at
# the private company on a public commit names the company.
if git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
  authors="$(git -C "$root" log --format='%ae %ce' 2>/dev/null | tr ' ' '\n' | sort -u)"
  bad="$(printf '%s\n' "$authors" | grep -iE 'pri[z]mal\.ai$')"
  if [ -n "$bad" ]; then
    echo "::error::A commit carries an address that names the private company."
    status=1
  fi
fi

if [ "$status" -eq 0 ]; then
  echo "Clean."
fi
exit "$status"
