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

Exit codes:

    0  clean, or every alert is below `fail-on`
    1  at least one alert at or above `fail-on`
    2  the linter could not run, or was mis-invoked
"""

from __future__ import annotations

import argparse
import json
import os
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


def cmd_resolve_files(args: argparse.Namespace) -> int:
    files = paginate(f"/repos/{args.repo}/pulls/{args.pr}/files", args.token)
    paths = [p for p in changed_markdown(files) if os.path.exists(p)]
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


def comment_body(
    alerts: dict, files_linted: int, fail_on: str, version: str, failed: int
) -> str:
    tally = counts_by_level(alerts)
    total = sum(tally.values())
    lines = [MARKER, ""]

    if total == 0:
        lines.append(f"### Vale is clean — {plural(files_linted, 'file')} linted")
    else:
        verdict = "fails" if failed else "passes"
        lines.append(
            f"### Vale found {plural(total, 'alert')} in "
            f"{plural(len(alerts), 'file')} — this check {verdict}"
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

    lines.append("")
    lines.append(f"<sub>vale {version} · elecnix/vale-action · rules come from this repository</sub>")
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

    tally = counts_by_level(alerts)
    failed = failing(tally, args.fail_on)
    body = comment_body(alerts, args.files, args.fail_on, args.version, failed)
    conclusion = "failure" if failed else "success"
    title = (
        f"{plural(failed, 'alert')} at or above {args.fail_on}"
        if failed
        else f"Clean — {plural(args.files, 'file')} linted"
    )
    decorate(args, body, conclusion, title, head)
    emit_output("outcome", "failure" if failed else "success")
    emit_output("alerts", str(sum(tally.values())))
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
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--pr", required=True, type=int)
    resolve.add_argument("--token", required=True)
    resolve.set_defaults(func=cmd_resolve_files)

    report = sub.add_parser("report", help="turn a vale run into a verdict")
    report.add_argument("--stdout", required=True, help="file holding vale's JSON")
    report.add_argument("--stderr", default=None)
    report.add_argument("--status", required=True, type=int, help="vale's exit code")
    report.add_argument("--fail-on", default="error", dest="fail_on")
    report.add_argument("--version", default="unknown")
    report.add_argument("--files", type=int, default=0, help="how many files were linted")
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
