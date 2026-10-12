#!/usr/bin/env python3
"""Check the action metadata GitHub Marketplace refuses a release over.

GitHub validates `action.yml` when a release is published, not when the field
is written. A description of 197 characters therefore sat in this repository
until the release page was opened, and the fix cost a commit, a merge and a
fresh tag before the release could go out — the worst possible moment to
discover a one-line metadata rule.

The rules that can be settled offline are settled here, on every change. Two
are left to GitHub, because only GitHub knows the answers: whether `name` is
still unique across the Marketplace, and whether the branding icon is one of
Feather's. The release page reports both before anything is published.

Exit 0 when the metadata is publishable, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - the runner has it with yamllint
    sys.exit("PyYAML is missing, so action.yml cannot be read.")

# The eight the reference accepts, from
# https://docs.github.com/en/actions/sharing-automations/creating-actions/metadata-syntax-for-github-actions#branding
COLORS = {"white", "yellow", "blue", "green", "orange", "red", "purple", "gray-dark"}

# "Description must be less than 125 characters" is the release page's own
# wording, and the reason this file exists.
DESCRIPTION_LIMIT = 125

# Feather names are kebab-case. Membership of Feather's set is checked by the
# release page instead of by a copy of it here: the list is long and frozen, and
# a wrong icon is named on the page where it can be fixed.
ICON = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def check(root: Path) -> list[str]:
    """Return every reason the Marketplace would refuse this repository."""
    problems: list[str] = []

    metadata = root / "action.yml"
    if not metadata.is_file():
        # Metadata in a subdirectory is never listed, which is the whole
        # failure: the release page has nothing to publish.
        return ["action.yml is not at the root of the repository."]

    try:
        action = yaml.safe_load(metadata.read_text())
    except yaml.YAMLError as error:
        return [f"action.yml does not parse: {error}"]
    if not isinstance(action, dict):
        return ["action.yml does not contain a mapping."]

    if not str(action.get("name") or "").strip():
        problems.append("action.yml has no name; the Marketplace identifies the action by it.")

    description = str(action.get("description") or "").strip()
    if not description:
        problems.append("action.yml has no description.")
    elif len(description) >= DESCRIPTION_LIMIT:
        problems.append(
            f"description is {len(description)} characters, and the Marketplace refuses "
            f"{DESCRIPTION_LIMIT} or more."
        )

    # The README is the listing the Marketplace renders; without one the page
    # is a table of input names and nothing else.
    if not (root / "README.md").is_file():
        problems.append("README.md is not at the root of the repository.")

    branding = action.get("branding")
    if branding is not None:
        if not isinstance(branding, dict):
            problems.append("branding is not a mapping.")
        else:
            color = str(branding.get("color") or "")
            if color not in COLORS:
                problems.append(
                    f"branding.color {color!r} is not one of {', '.join(sorted(COLORS))}."
                )
            icon = str(branding.get("icon") or "")
            if not icon:
                problems.append("branding.icon is empty.")
            elif not ICON.match(icon):
                problems.append(f"branding.icon {icon!r} is not a Feather icon name.")

    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".")
    problems = check(root)
    for problem in problems:
        # An error annotation rather than a bare line: the check is the only
        # thing standing between a typo and a release nobody can publish.
        print(f"::error::{problem}")
    if problems:
        return 1
    print(f"{root / 'action.yml'} is publishable on the Marketplace.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
