#!/usr/bin/env python3
"""Turn one `vale` run into a pull-request verdict.

The action's shell layer installs Vale, works out what to lint, and runs it.
This module owns everything after that: which alerts count, what the pinned
comment says, what the check run concludes, and what the process exits with.

The decisions below are made here and nowhere else.

**A linter that could not run has not passed.** Vale exits 1 when it finds
alerts, which is a normal verdict, and 2 or more when it failed. It also fails
by printing something that is not JSON while exiting 0 — a missing style
directory does exactly that. Both shapes end as exit code 2 and a red check
that says the linter could not run, never as a pass.

**The comment is pinned, not appended.** Every run looks for the marker below
in the pull request's existing comments and edits that one in place. A second
comment is never posted, however many times the workflow runs.

**The newest head run decides what the pinned comment says.** GitHub replays the
original event on a re-run, so a re-run of a superseded commit lints code the
pull request has already moved past. Writing its verdict would replace the one a
newer run posted, and it would set a check on a sha nobody is reviewing. A run
whose sha is not the live head, read from the API rather than from the event it
was queued with, writes to the job summary and nothing else.

**A missing write scope degrades, it does not crash.** A caller who grants only
`contents: read` still gets the verdict as an exit code and a job summary; the
comment and the check run are skipped with a warning. Failing the whole action
because it could not decorate the pull request would punish the safest config.

**Only the lines a change writes count.** Vale has to read a file whole — a
sentence is judged in its context, and a style can only see a paragraph if it
sees all of it — so a changed file is linted whole and the alerts are narrowed
afterwards to the lines this pull request adds or rewrites. An alert on a line
the change did not write is debt the pull request inherited, and a gate that
goes red over inherited debt is one people route around. Those alerts are
reported as a count, never as a verdict.

**The added lines come from the pull request's own diff.** Not from the
per-file `patch` of the files API: that field is left out for a diff GitHub
considers too large, and one 1748-line plan file was enough to lose it, which
turned that file's verdict back into a whole-file one without saying so. For a
file the diff does not carry either — a change set past the diff media type's
limit — every alert counts and the verdict names the file as counted whole.
Nothing is hidden, including that.

Exit codes:

    0  clean, or every alert is below `fail-on`
    1  at least one alert at or above `fail-on`
    2  the linter could not run, or was mis-invoked
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

MARKER = "<!-- elecnix/vale-action -->"
API = "https://api.github.com"

# Vale's three levels, weakest first. `fail-on` names one and everything at or
# above it counts.
LEVELS = ["suggestion", "warning", "error"]

# A pull request comment has a size limit and nobody reads past the first
# screen anyway. Past this many rows the table is cut and the count says so.
MAX_ROWS = 50


# --------------------------------------------------------------------------
# GitHub REST
# --------------------------------------------------------------------------


def api(method: str, path: str, token: str, body: dict | None = None) -> object:
    """One REST call. Raises urllib.error.HTTPError; callers decide what a
    403 means for them, because a missing scope is not the same as a bug."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        path if path.startswith("http") else f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "elecnix-vale-action",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def api_text(path: str, token: str, accept: str) -> str:
    """One REST call that answers with text rather than JSON.

    The diff media type is not JSON, and it is the only way to see the added
    lines of a file whose per-file `patch` the API left out."""
    req = urllib.request.Request(
        path if path.startswith("http") else f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "elecnix-vale-action",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


DIFF_MEDIA = "application/vnd.github.v3.diff"


def pull_diff(repo: str, pr: int, token: str) -> str:
    """The pull request's own unified diff, for the whole change set.

    The files API omits `patch` for a diff it considers too large, and one
    1748-line plan file was enough to lose its patch and with it the narrowing.
    The diff media type carries every file the change touches, so it is where
    the added lines are read from."""
    return api_text(f"/repos/{repo}/pulls/{pr}", token, DIFF_MEDIA)


def _unquote(name: str) -> str:
    """A path as the diff writes it, without the quotes git adds when it has to."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


def new_side_path(line: str) -> str | None:
    """The new-side path out of `diff --git a/X b/Y`.

    A mode-only change has no `+++` line at all, so the header is the only
    place its path appears. Quoted names are looked for first, because a name
    with a space in it is quoted by git and would otherwise be split."""
    rest = line[len("diff --git ") :]
    for marker in (' "b/', " b/"):
        at = rest.rfind(marker)
        if at != -1:
            return _unquote(rest[at + 1 :])[2:]
    return None


def added_lines_by_path(diff: str) -> dict[str, set[int]]:
    """The added line numbers per path, out of one unified diff.

    Paths are keyed on the new side, which is what Vale reports and what the
    pull request's file list names. A deletion has no new side and is left out."""
    out: dict[str, set[int]] = {}
    path: str | None = None
    buffer: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            if path is not None:
                out.setdefault(path, set()).update(parse_patch("\n".join(buffer)))
            path, buffer = new_side_path(raw), []
            continue
        if raw.startswith("+++ "):
            name = _unquote(raw[4:].strip())
            if name == "/dev/null":
                path = None  # a deletion: no new side to count
            else:
                path = name[2:] if name.startswith("b/") else name
            continue
        if path is not None:
            buffer.append(raw)
    if path is not None:
        out.setdefault(path, set()).update(parse_patch("\n".join(buffer)))
    return out


def paginate(path: str, token: str) -> list:
    """Walk every page. The pull-request file list defaults to 30 per page, so
    a single unpaginated call silently truncates a large change set."""
    out: list = []
    url = f"{API}{path}{'&' if '?' in path else '?'}per_page=100"
    while url:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "elecnix-vale-action",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            out.extend(json.loads(resp.read()))
            url = next_link(resp.headers.get("Link", ""))
    return out


def next_link(header: str) -> str | None:
    for part in header.split(","):
        if 'rel="next"' in part and "<" in part:
            return part[part.index("<") + 1 : part.index(">")]
    return None


def pull_head(payload: object) -> str | None:
    """The head commit out of a pull request payload, when there is one."""
    if not isinstance(payload, dict):
        return None
    head = payload.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None


def superseded(sha: str | None, head: str | None) -> bool:
    """Is this run linting a commit the pull request has moved past?

    An unknown head answers no. A read that failed is not evidence that this run
    is out of date, and the head run still has to post its verdict."""
    return bool(sha and head and sha != head)


def head_of(repo: str, pr: int | None, token: str | None) -> str | None:
    """The pull request's live head commit, or None when it cannot be read.

    The live read is the point. GitHub replays the original event on a re-run,
    so `github.event.pull_request.head.sha` names the commit that run checks
    out, which may no longer be the one under review."""
    if not (pr and repo and token):
        return None
    try:
        return pull_head(api("GET", f"/repos/{repo}/pulls/{pr}", token))
    except urllib.error.HTTPError as exc:
        warn(
            f"Could not read pull request #{pr} (HTTP {exc.code}); "
            "treating this run as the head."
        )
    except urllib.error.URLError as exc:
        warn(
            f"Could not read pull request #{pr} ({exc.reason}); "
            "treating this run as the head."
        )
    return None


# --------------------------------------------------------------------------
# Which files to lint
# --------------------------------------------------------------------------

MARKDOWN = (".md", ".markdown", ".mdx")


def changed_markdown(files: list[dict]) -> list[str]:
    """Markdown this pull request still has. A deleted file is dropped: it is
    not on disk, and Vale reports a missing path as a run failure."""
    return sorted(
        f["filename"]
        for f in files
        if f.get("status") != "removed" and f["filename"].lower().endswith(MARKDOWN)
    )


# A hunk header: `@@ -old_start,old_count +new_start,new_count @@`, where
# either count is omitted when it is 1. Only the new side matters here.
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_patch(patch: str) -> set[int]:
    """The line numbers a unified diff adds or rewrites, on the new side.

    The added lines are counted rather than the hunk's new-side span on
    purpose: a span carries three lines of context on each side, and an alert
    standing in that context is one the change did not write.

    A rewritten line is added on the new side, which is what makes the verdict
    agree with `git blame`: the change is answerable for the whole line."""
    added: set[int] = set()
    line: int | None = None
    for raw in patch.splitlines():
        header = HUNK.match(raw)
        if header:
            line = int(header.group(1))
            continue
        if line is None:
            # `diff --git`, `index`, `---`, `+++`: the file header, and the
            # only place a `+++` is a filename rather than text this change
            # adds. Inside a hunk it is added text, which is why the header
            # check comes first and stops at the first hunk.
            continue
        if raw.startswith("+"):
            added.add(line)
            line += 1
        elif raw.startswith("-"):
            continue  # gone from the new side
        elif raw.startswith("\\"):
            continue  # `\ No newline at end of file`, a note about the line above
        else:
            line += 1  # context
    return added


def changed_lines(files: list[dict]) -> dict[str, set[int]]:
    """The added line numbers per path, out of the pull request's file list.

    This is the fallback, not the source: prefer `added_lines_by_path` over the
    pull request's diff, and read this only for a path the diff did not carry.
    A path the API sent without a patch is left out instead of mapped to an
    empty set, so that "no diff information" can be told apart from "nothing
    was added"."""
    out: dict[str, set[int]] = {}
    for entry in files:
        name = entry.get("filename")
        patch = entry.get("patch")
        if isinstance(name, str) and isinstance(patch, str):
            out[name] = parse_patch(patch)
    return out


def load_files_json(path: str) -> list[dict]:
    """A pull request file list already saved to disk, as `gh api` prints it.

    This is what lets a contributor reproduce the narrowed verdict on a laptop
    without a token, and what lets CI prove the narrowing without a live pull
    request."""
    parsed = json.loads(read(path) or "null")
    if not isinstance(parsed, list):
        raise ValueError(
            f"{path} holds {type(parsed).__name__}; expected the pull request's file list"
        )
    return parsed


def write_lines(path: str, lines: dict[str, set[int]], whole: list[str]) -> None:
    """Hand the added lines to the report step, which runs in another process
    and has no way to see what this one read.

    `whole` names the files whose lines nobody could work out. They are reported
    separately rather than left to be inferred from an absence, because a
    verdict that counts one file whole has to say so."""
    payload = {
        "lines": {name: sorted(numbers) for name, numbers in sorted(lines.items())},
        "whole": sorted(whole),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def cmd_resolve_files(args: argparse.Namespace) -> int:
    if args.files_json:
        files = load_files_json(args.files_json)
        diff = read(args.diff)
    elif args.repo and args.pr and args.token:
        files = paginate(f"/repos/{args.repo}/pulls/{args.pr}/files", args.token)
        diff = pull_diff(args.repo, args.pr, args.token)
    else:
        raise ValueError(
            "resolve-files needs --files-json, or --repo, --pr and --token together"
        )
    paths = [p for p in changed_markdown(files) if os.path.exists(p)]
    if args.lines_out:
        from_diff = added_lines_by_path(diff)
        from_files = changed_lines(files)
        lines, whole = {}, []
        for name in paths:
            if name in from_diff:
                lines[name] = from_diff[name]
            elif name in from_files:
                lines[name] = from_files[name]
            else:
                whole.append(name)
        write_lines(args.lines_out, lines, whole)
        if whole:
            warn(
                "No diff for " + ", ".join(sorted(whole)) + "; their alerts will "
                "count whatever line they sit on."
            )
    print("\n".join(paths))
    return 0


# --------------------------------------------------------------------------
# Reading the verdict
# --------------------------------------------------------------------------


def parse_alerts(raw: str) -> dict:
    """Vale's JSON output, or a ValueError naming what came back instead.

    Vale prints diagnostics on stdout in some failure modes while still
    exiting 0, so parsing is the real test of whether it ran."""
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Vale did not return JSON: {text[:400]}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Vale returned {type(parsed).__name__}, not an object")

    # A configuration failure comes back as one JSON *error object* rather than
    # the path-to-alerts map. It parses fine and contains no alerts, so reading
    # it as a result is the exact shape of the false pass this action exists to
    # avoid. E201 (a StylesPath that does not exist) arrives this way.
    if "Code" in parsed and "Text" in parsed:
        raise ValueError(f"Vale reported {parsed['Code']}: {parsed['Text']}")

    for path, entries in parsed.items():
        if not isinstance(entries, list):
            raise ValueError(f"Vale returned a {type(entries).__name__} for {path!r}")
    return parsed


def describe_failure(text: str) -> str:
    """A readable one-liner out of whatever Vale left behind.

    Vale prints its own errors as a pretty JSON object. Handing that to a
    reader verbatim buries the sentence that matters under a brace, so the
    fields are unwrapped when they are there and the raw text used when they
    are not."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict) and "Text" in parsed:
        code = parsed.get("Code", "")
        where = parsed.get("Path", "")
        head = ": ".join(part for part in (code, parsed["Text"]) if part)
        return f"{head} ({where})" if where else head
    return text


def counts_by_level(alerts: dict) -> dict[str, int]:
    tally = {level: 0 for level in LEVELS}
    for entries in alerts.values():
        for entry in entries:
            level = str(entry.get("Severity", "")).lower()
            if level in tally:
                tally[level] += 1
    return tally


def failing(tally: dict[str, int], fail_on: str) -> int:
    """How many alerts sit at or above `fail-on`."""
    if fail_on not in LEVELS:
        raise ValueError(f"fail-on must be one of {', '.join(LEVELS)}; got {fail_on!r}")
    return sum(tally[level] for level in LEVELS[LEVELS.index(fail_on) :])


# --------------------------------------------------------------------------
# Narrowing the alerts to the lines this change writes
# --------------------------------------------------------------------------


def answerable(entry: dict, lines: set[int]) -> bool:
    """Is this alert standing on a line the change wrote?

    A line that cannot be read answers yes. An alert nobody can place is not
    one to drop on a guess."""
    line = entry.get("Line")
    if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
        return True
    return line in lines


def alerts_on_changed_lines(alerts: dict, changed: dict[str, set[int]]) -> tuple[dict, int]:
    """Keep the alerts the change is answerable for, and count the rest.

    A path with no diff information keeps every alert it has: that is the
    reading that cannot pass anything by accident."""
    kept: dict = {}
    ignored = 0
    for path, entries in alerts.items():
        lines = changed.get(path)
        if lines is None:
            kept[path] = list(entries)
            continue
        mine = [entry for entry in entries if answerable(entry, lines)]
        ignored += len(entries) - len(mine)
        if mine:
            kept[path] = mine
    return kept, ignored


def load_changed_lines(path: str) -> tuple[dict[str, set[int]] | None, list[str]]:
    """The added lines and the files nobody could work out, out of what
    `resolve-files --lines-out` wrote.

    The older flat map is still read: a caller with one of those gets no
    whole-file names rather than an error."""
    raw = read(path)
    if not raw.strip():
        return None, []
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} holds {type(parsed).__name__}; expected an object")
    if "lines" in parsed or "whole" in parsed:
        per_path, whole = parsed.get("lines") or {}, parsed.get("whole") or []
    else:
        per_path, whole = parsed, []
    out: dict[str, set[int]] = {}
    for name, numbers in per_path.items():
        if not isinstance(numbers, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) for n in numbers
        ):
            raise ValueError(f"{path} holds the wrong line numbers for {name!r}")
        out[name] = set(numbers)
    if not isinstance(whole, list) or not all(isinstance(p, str) for p in whole):
        raise ValueError(f"{path} holds the wrong file names for the files counted whole")
    return out, whole


def narrowed(args: argparse.Namespace) -> tuple[dict[str, set[int]] | None, list[str]]:
    """The lines this change writes, or None when every line of the linted
    files counts, plus the files whose lines nobody could work out.

    A flag that is set but unreadable is not permission to hide alerts: the run
    reports everything it found and says why."""
    if not args.changed_lines:
        return None, []
    try:
        changed, whole = load_changed_lines(args.changed_lines)
    except ValueError as exc:
        warn(f"Could not read the changed lines ({exc}); every alert counts this run.")
        return None, []
    if changed is None:
        warn(f"{args.changed_lines} holds no changed lines; every alert counts this run.")
        return None, []
    if not changed and not whole:
        # No path had a diff GitHub sent. That is not narrowing either: every
        # line counts, and the verdict must not claim otherwise.
        return None, []
    return changed, whole


def rows(alerts: dict) -> list[tuple]:
    out = []
    for path in sorted(alerts):
        for entry in alerts[path]:
            out.append(
                (
                    path,
                    entry.get("Line", 0),
                    str(entry.get("Severity", "")).lower(),
                    entry.get("Check", ""),
                    str(entry.get("Message", "")),
                )
            )
    out.sort(key=lambda r: (r[0], r[1]))
    return out


# --------------------------------------------------------------------------
# What the reader sees
# --------------------------------------------------------------------------


def escape(text: str) -> str:
    """Keep a message inside its table cell, and keep it inert.

    Vale echoes the matched prose, so a message can carry a pipe, a newline, or
    a `::` sequence the runner would read as a workflow command."""
    return text.replace("|", "\\|").replace("\n", " ").replace("::", ":\u200b:").strip()


def verdict_title(
    failed: int, fail_on: str, files_linted: int, narrow: bool, whole: list[str] | None = None
) -> str:
    """The one line the check list shows, which has to say what was counted."""
    if not narrow:
        title = (
            f"{plural(failed, 'alert')} at or above {fail_on}"
            if failed
            else f"Clean — {plural(files_linted, 'file')} linted"
        )
    else:
        title = (
            f"{plural(failed, 'alert')} on the changed lines, at or above {fail_on}"
            if failed
            else "Clean — nothing on the changed lines"
        )
    if whole:
        title += f" ({plural(len(whole), 'file')} counted whole)"
    return title


def whole_note(whole: list[str] | None) -> str:
    """Name the files whose added lines nobody could read.

    Counting one whole is the fallback that cannot pass anything by accident,
    and a reader is entitled to know which file got it."""
    if not whole:
        return ""
    named = ", ".join(f"`{p}`" for p in sorted(whole))
    return (
        f"{plural(len(whole), 'file')} counted whole, because GitHub sent no diff for "
        f"{'it' if len(whole) == 1 else 'them'}: {named}."
    )


def scope_note(ignored: int, narrow: bool, whole: list[str] | None = None) -> str:
    """What was counted, said plainly, because a narrowed verdict that reads
    like a whole-file one is the confusion this action exists to remove."""
    if not narrow:
        return ""
    counted = "Counted here: the lines this change writes."
    if ignored:
        counted = (
            f"Counted here: the lines this change writes. "
            f"{plural(ignored, 'further alert')} on untouched lines "
            f"{'is' if ignored == 1 else 'are'} left out of the verdict."
        )
    extra = whole_note(whole)
    return f"{counted} {extra}" if extra else counted


def comment_body(
    alerts: dict,
    files_linted: int,
    fail_on: str,
    version: str,
    failed: int,
    ignored: int = 0,
    narrow: bool = False,
    whole: list[str] | None = None,
) -> str:
    tally = counts_by_level(alerts)
    total = sum(tally.values())
    lines = [MARKER, ""]

    if total == 0:
        if narrow:
            lines.append("### Nothing on the lines this change writes")
        else:
            lines.append(f"### Vale is clean — {plural(files_linted, 'file')} linted")
    else:
        verdict = "fails" if failed else "passes"
        where = " on the lines this change writes" if narrow else ""
        lines.append(
            f"### Vale found {plural(total, 'alert')} in "
            f"{plural(len(alerts), 'file')}{where} — this check {verdict}"
        )
        lines.append("")
        lines.append(
            f"{plural(failed, 'alert')} at or above `{fail_on}`. "
            + ", ".join(f"{tally[level]} {level}" for level in reversed(LEVELS))
        )
        lines.append("")
        lines.append("| File | Line | Level | Rule | Message |")
        lines.append("| --- | ---: | --- | --- | --- |")
        table = rows(alerts)
        for path, line, level, check, message in table[:MAX_ROWS]:
            lines.append(
                f"| `{path}` | {line} | {level} | `{check}` | {escape(message)} |"
            )
        if len(table) > MAX_ROWS:
            lines.append("")
            lines.append(f"…and {len(table) - MAX_ROWS} more. Run Vale locally to see them all.")

    note = scope_note(ignored, narrow, whole)
    if note:
        lines.append("")
        lines.append(note)

    if narrow:
        scope = "only the lines a change writes"
        if whole:
            scope += ", and every line of a file it could not read"
    else:
        scope = "every line of every file linted"
    lines.append("")
    lines.append(
        f"<sub>vale {version} · elecnix/vale-action · {scope} · "
        "rules come from this repository</sub>"
    )
    return "\n".join(lines)


def failure_body(reason: str, version: str) -> str:
    return "\n".join(
        [
            MARKER,
            "",
            "### Vale could not run",
            "",
            "This check has not passed. It never ran.",
            "",
            "```",
            reason.strip()[:3000] or "(no output)",
            "```",
            "",
            f"<sub>vale {version} · elecnix/vale-action</sub>",
        ]
    )


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def short(sha: str | None) -> str:
    """Seven characters, which is what GitHub itself shows."""
    return (sha or "")[:7]


# --------------------------------------------------------------------------
# Decorating the pull request
# --------------------------------------------------------------------------


def upsert_comment(repo: str, pr: int, token: str, body: str) -> None:
    """Edit this action's own comment, or post the first one.

    Matching on the marker rather than on the author lets the comment survive a
    caller switching between the default token and a bot token."""
    existing = None
    for comment in paginate(f"/repos/{repo}/issues/{pr}/comments", token):
        if MARKER in (comment.get("body") or ""):
            existing = comment["id"]
            break
    if existing:
        api("PATCH", f"/repos/{repo}/issues/comments/{existing}", token, {"body": body})
    else:
        api("POST", f"/repos/{repo}/issues/{pr}/comments", token, {"body": body})


def create_check(
    repo: str, sha: str, token: str, conclusion: str, title: str, summary: str
) -> None:
    api(
        "POST",
        f"/repos/{repo}/check-runs",
        token,
        {
            "name": "vale",
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary[:60000]},
        },
    )


def warn(message: str) -> None:
    print(f"::warning::{message}", file=sys.stderr)


def summarize(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(body.replace(MARKER, "") + "\n")


def emit_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    stderr_text = read(args.stderr)

    # Whose verdict this is. A re-run replays the commit from its original
    # event, so what the pull request points at now has to be read rather than
    # assumed from the payload this run was queued with.
    head = head_of(args.repo, args.pr, args.token)

    # The linter's own failure is the first thing to rule out, and the only
    # outcome that must never read as a pass.
    stdout_text = read(args.stdout)

    # Parse first, whatever the exit code. A configuration failure leaves stdout
    # empty and puts a JSON error object on stderr, so a run that failed and a
    # run that found nothing are indistinguishable from the results file alone.
    broken = None
    alerts: dict = {}
    try:
        alerts = parse_alerts(stdout_text)
        if args.status >= 2:
            broken = describe_failure(stderr_text or stdout_text) or f"vale exited {args.status}"
    except ValueError as exc:
        broken = f"{exc}\n{describe_failure(stderr_text)}".strip()

    if broken:
        decorate(
            args, failure_body(broken, args.version), "failure", "Vale could not run", head
        )
        print(f"::error::Vale could not run. {broken.splitlines()[0][:200]}", file=sys.stderr)
        emit_output("outcome", "error")
        return 2

    # Narrow to the lines this change writes, before anything is counted. What
    # is left out is still named, and a file whose lines nobody could read is
    # named as counted whole, so a reader never mistakes a narrowed pass for a
    # clean file.
    changed, whole = narrowed(args)
    ignored = 0
    if changed is not None:
        alerts, ignored = alerts_on_changed_lines(alerts, changed)

    tally = counts_by_level(alerts)
    failed = failing(tally, args.fail_on)
    body = comment_body(
        alerts,
        args.files,
        args.fail_on,
        args.version,
        failed,
        ignored,
        changed is not None,
        whole,
    )
    conclusion = "failure" if failed else "success"
    title = verdict_title(failed, args.fail_on, args.files, changed is not None, whole)
    decorate(args, body, conclusion, title, head)
    emit_output("outcome", "failure" if failed else "success")
    emit_output("alerts", str(sum(tally.values())))
    emit_output("ignored", str(ignored))
    return 1 if failed else 0


def decorate(
    args: argparse.Namespace,
    body: str,
    conclusion: str,
    title: str,
    head: str | None = None,
) -> None:
    """Write the verdict where a reader sees it: the job summary first, then the
    pinned comment and the check run.

    `head` is the pull request's head commit as read live. A run whose sha is
    not that commit gets the job summary alone: the comment is shared by every
    run of every sha, so an outdated run writing it would replace the verdict a
    newer run posted, and its check run would set a status on a commit nobody
    is reviewing."""
    if superseded(args.sha, head):
        outdated = (
            f"This run linted {short(args.sha)}, which is no longer the head of "
            f"#{args.pr} ({short(head)}). Its verdict is below, but the pinned "
            "comment and the check run were left to the newest run."
        )
        summarize(f"> {outdated}\n\n{body}")
        print(f"::notice::{outdated}")
        return
    summarize(body)
    if not args.token or not args.repo:
        return
    if args.comment and args.pr:
        try:
            upsert_comment(args.repo, args.pr, args.token, body)
        except urllib.error.HTTPError as exc:
            warn(f"Could not post the Vale comment (HTTP {exc.code}). Needs pull-requests: write.")
    if args.check and args.sha:
        try:
            create_check(args.repo, args.sha, args.token, conclusion, title, body)
        except urllib.error.HTTPError as exc:
            warn(f"Could not create the Vale check run (HTTP {exc.code}). Needs checks: write.")


def read(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def boolean(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve-files", help="list the markdown this PR changed")
    resolve.add_argument("--repo", help="owner/name to read the pull request from")
    resolve.add_argument("--pr", type=int)
    resolve.add_argument("--token")
    resolve.add_argument(
        "--files-json",
        dest="files_json",
        help="a file list already on disk, instead of reading the API",
    )
    resolve.add_argument(
        "--lines-out",
        dest="lines_out",
        help="where to write the lines this change adds, as JSON",
    )
    resolve.add_argument(
        "--diff",
        default=None,
        help="a unified diff already on disk, instead of reading the API's",
    )
    resolve.set_defaults(func=cmd_resolve_files)

    report = sub.add_parser("report", help="turn a vale run into a verdict")
    report.add_argument("--stdout", required=True, help="file holding vale's JSON")
    report.add_argument("--stderr", default=None)
    report.add_argument("--status", required=True, type=int, help="vale's exit code")
    report.add_argument("--fail-on", default="error", dest="fail_on")
    report.add_argument("--version", default="unknown")
    report.add_argument("--files", type=int, default=0, help="how many files were linted")
    report.add_argument(
        "--changed-lines",
        dest="changed_lines",
        default=None,
        help="lines this change writes, from resolve-files --lines-out",
    )
    report.add_argument("--repo", default=None)
    report.add_argument("--pr", type=int, default=None)
    report.add_argument("--sha", default=None, help="the commit this run checked out")
    report.add_argument("--token", default=None)
    report.add_argument("--comment", type=boolean, default=True)
    report.add_argument("--check", type=boolean, default=True)
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
