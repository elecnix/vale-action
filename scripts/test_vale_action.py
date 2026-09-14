#!/usr/bin/env python3
"""Unit tests for the verdict logic. Stdlib unittest, no install step.

The end-to-end proof lives in CI, which runs the composite action against the
three fixture repositories. These tests pin the decisions that are cheap to get
wrong and expensive to notice: what counts as a failed run, what `fail-on`
includes, and that a message cannot escape its table cell.
"""

import argparse, io, json, os, sys, tempfile, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vale_action as va


def alert(severity="error", check="demo.Filler", line=1, message="Cut 'very'."):
    return {"Severity": severity, "Check": check, "Line": line, "Message": message}


class ParseAlerts(unittest.TestCase):
    def test_empty_output_is_a_clean_run(self):
        self.assertEqual(va.parse_alerts(""), {})
        self.assertEqual(va.parse_alerts("{}"), {})

    def test_path_map_parses(self):
        raw = json.dumps({"a.md": [alert()]})
        self.assertEqual(list(va.parse_alerts(raw)), ["a.md"])

    def test_error_object_is_a_failed_run_not_a_clean_one(self):
        # The regression that matters. Vale answers a bad StylesPath with this
        # shape, which is valid JSON holding zero alerts.
        raw = json.dumps(
            {"Line": 0, "Path": ".vale.ini", "Text": "The path 'styles' does not exist.", "Code": "E201", "Span": 1}
        )
        with self.assertRaises(ValueError) as caught:
            va.parse_alerts(raw)
        self.assertIn("E201", str(caught.exception))

    def test_non_json_is_a_failed_run(self):
        with self.assertRaises(ValueError):
            va.parse_alerts("panic: runtime error")

    def test_json_that_is_not_an_object_is_a_failed_run(self):
        with self.assertRaises(ValueError):
            va.parse_alerts("[1, 2]")

    def test_entry_that_is_not_a_list_is_a_failed_run(self):
        with self.assertRaises(ValueError):
            va.parse_alerts(json.dumps({"a.md": "oops"}))


class DescribeFailure(unittest.TestCase):
    def test_a_vale_error_object_becomes_one_readable_line(self):
        raw = json.dumps(
            {"Line": 0, "Path": ".vale.ini", "Text": "The path 'styles' does not exist.", "Code": "E201", "Span": 1}
        )
        self.assertEqual(
            va.describe_failure(raw),
            "E201: The path 'styles' does not exist. (.vale.ini)",
        )

    def test_plain_text_passes_through(self):
        self.assertEqual(va.describe_failure(" boom \n"), "boom")

    def test_empty_stays_empty(self):
        self.assertEqual(va.describe_failure(""), "")


class FailOn(unittest.TestCase):
    def setUp(self):
        self.alerts = {
            "a.md": [alert("error"), alert("warning")],
            "b.md": [alert("suggestion"), alert("suggestion")],
        }
        self.tally = va.counts_by_level(self.alerts)

    def test_tally(self):
        self.assertEqual(self.tally, {"error": 1, "warning": 1, "suggestion": 2})

    def test_error_counts_only_errors(self):
        self.assertEqual(va.failing(self.tally, "error"), 1)

    def test_warning_counts_warnings_and_above(self):
        self.assertEqual(va.failing(self.tally, "warning"), 2)

    def test_suggestion_counts_everything(self):
        self.assertEqual(va.failing(self.tally, "suggestion"), 4)

    def test_unknown_level_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            va.failing(self.tally, "fatal")

    def test_unknown_severity_in_an_alert_is_not_counted(self):
        tally = va.counts_by_level({"a.md": [alert("catastrophe")]})
        self.assertEqual(sum(tally.values()), 0)


class CommentBody(unittest.TestCase):
    def test_clean_run_says_so_and_carries_the_marker(self):
        body = va.comment_body({}, 4, "error", "3.20.0", 0)
        self.assertTrue(body.startswith(va.MARKER))
        self.assertIn("clean", body)
        self.assertIn("4 files", body)
        self.assertIn("3.20.0", body)

    def test_alerts_render_as_one_row_each(self):
        body = va.comment_body({"a.md": [alert(line=7)]}, 1, "error", "3.20.0", 1)
        self.assertIn("| `a.md` | 7 | error | `demo.Filler` |", body)
        self.assertIn("this check fails", body)

    def test_alerts_below_fail_on_pass(self):
        body = va.comment_body({"a.md": [alert("suggestion")]}, 1, "error", "3.20.0", 0)
        self.assertIn("this check passes", body)

    def test_long_run_is_truncated_with_a_count(self):
        many = {"a.md": [alert(line=n) for n in range(va.MAX_ROWS + 5)]}
        body = va.comment_body(many, 1, "error", "3.20.0", len(many["a.md"]))
        self.assertIn("and 5 more", body)

    def test_a_message_cannot_break_the_table_or_the_runner(self):
        body = va.comment_body(
            {"a.md": [alert(message="a | b\nc ::error::owned")]}, 1, "error", "3.20.0", 1
        )
        row = [ln for ln in body.splitlines() if ln.startswith("| `a.md`")][0]
        self.assertEqual(row.count("|"), 7)  # six cell walls, one escaped pipe
        self.assertNotIn("::error::", row)

    def test_failure_body_never_reads_as_a_pass(self):
        body = va.failure_body("E201: the path 'styles' does not exist", "3.20.0")
        self.assertTrue(body.startswith(va.MARKER))
        self.assertIn("could not run", body)
        self.assertIn("has not passed", body)
        self.assertIn("E201", body)


class ChangedFiles(unittest.TestCase):
    def test_only_markdown_survives(self):
        files = [
            {"filename": "README.md", "status": "modified"},
            {"filename": "docs/a.markdown", "status": "added"},
            {"filename": "docs/b.mdx", "status": "added"},
            {"filename": "main.go", "status": "modified"},
        ]
        self.assertEqual(
            va.changed_markdown(files), ["README.md", "docs/a.markdown", "docs/b.mdx"]
        )

    def test_a_deleted_file_is_dropped(self):
        # Vale reports a missing path as a run failure, so a PR that only
        # deletes prose must not turn the check red.
        files = [{"filename": "gone.md", "status": "removed"}]
        self.assertEqual(va.changed_markdown(files), [])


OLD = "a" * 40
NEW = "b" * 40


class Superseded(unittest.TestCase):
    """Whether the commit a run linted is still the pull request's head."""

    def test_pull_head_reads_the_commit(self):
        self.assertEqual(va.pull_head({"head": {"sha": "abc"}}), "abc")

    def test_a_payload_without_a_head_is_unknown(self):
        payloads = (None, {}, {"head": {}}, {"head": {"sha": ""}}, {"head": "abc"}, [])
        for payload in payloads:
            self.assertIsNone(va.pull_head(payload), payload)

    def test_the_head_commit_is_not_superseded(self):
        self.assertFalse(va.superseded(OLD, OLD))

    def test_an_older_commit_is_superseded(self):
        self.assertTrue(va.superseded(OLD, NEW))

    def test_an_unknown_head_never_supersedes(self):
        # A read that failed is not evidence that this run is out of date. The
        # head run still has to post its verdict.
        self.assertFalse(va.superseded(OLD, None))
        self.assertFalse(va.superseded(None, NEW))
        self.assertFalse(va.superseded(None, None))


class HeadOf(unittest.TestCase):
    """Reading the live head rather than trusting the event payload."""

    def test_no_pull_request_means_no_read(self):
        with mock.patch.object(va, "api") as api:
            self.assertIsNone(va.head_of("o/r", None, "t"))
        api.assert_not_called()

    def test_a_read_that_fails_is_not_evidence_of_staleness(self):
        failure = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(va, "api", side_effect=failure), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as err:
            self.assertIsNone(va.head_of("o/r", 2, "t"))
        self.assertIn("403", err.getvalue())


class SummaryCase(unittest.TestCase):
    """Plumbing for the tests that write a job summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.summary = os.path.join(self.tmp.name, "summary.md")
        os.environ["GITHUB_STEP_SUMMARY"] = self.summary
        self.addCleanup(os.environ.pop, "GITHUB_STEP_SUMMARY", None)

    def summary_text(self):
        return va.read(self.summary)


class Decoration(SummaryCase):
    """The shared comment and the check run belong to the newest head run."""

    def run_decorate(self, head=None):
        args = argparse.Namespace(
            repo="o/r", pr=2, sha=OLD, token="t", comment=True, check=True
        )
        with mock.patch.object(va, "upsert_comment") as comment, mock.patch.object(
            va, "create_check"
        ) as check, mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            va.decorate(args, "### Vale is clean", "success", "Clean", head=head)
        return comment, check, out

    def test_the_head_run_writes_the_comment_and_the_check(self):
        comment, check, _ = self.run_decorate(head=OLD)
        self.assertEqual(comment.call_count, 1)
        self.assertEqual(check.call_count, 1)
        self.assertIn("Vale is clean", self.summary_text())

    def test_an_unknown_head_decorates_as_before(self):
        comment, check, _ = self.run_decorate(head=None)
        self.assertEqual(comment.call_count, 1)
        self.assertEqual(check.call_count, 1)

    def test_a_superseded_run_writes_neither(self):
        # Repainting the comment here overwrites the verdict a newer run
        # already corrected, and the check run lands on a sha nobody is
        # reviewing.
        comment, check, _ = self.run_decorate(head=NEW)
        comment.assert_not_called()
        check.assert_not_called()

    def test_a_superseded_run_says_why_in_the_step_summary(self):
        self.run_decorate(head=NEW)
        summary = self.summary_text()
        self.assertIn(OLD[:7], summary)
        self.assertIn(NEW[:7], summary)
        self.assertIn("no longer the head", summary)
        self.assertIn("Vale is clean", summary)

    def test_a_superseded_run_announces_itself_in_the_log(self):
        _, _, out = self.run_decorate(head=NEW)
        self.assertIn("::notice::", out.getvalue())


class Report(SummaryCase):
    """One run end to end, with the head read stubbed."""

    def setUp(self):
        super().setUp()
        self.stdout = os.path.join(self.tmp.name, "out.json")
        self.stderr = os.path.join(self.tmp.name, "err.txt")
        write(self.stdout, json.dumps({"a.md": [alert()]}))
        write(self.stderr, "")

    def args(self, status=1, **over):
        base = dict(
            stdout=self.stdout,
            stderr=self.stderr,
            status=status,
            fail_on="error",
            version="3.20.0",
            files=1,
            repo="o/r",
            pr=2,
            sha=OLD,
            token="t",
            comment=True,
            check=True,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def report(self, head, **over):
        with mock.patch.object(va, "head_of", return_value=head), mock.patch.object(
            va, "upsert_comment"
        ) as comment, mock.patch.object(va, "create_check") as check, mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ), mock.patch("sys.stdout", new_callable=io.StringIO):
            code = va.cmd_report(self.args(**over))
        return code, comment, check

    def test_the_head_run_paints_its_verdict(self):
        code, comment, check = self.report(head=OLD)
        self.assertEqual(code, 1)
        self.assertEqual(comment.call_count, 1)
        self.assertEqual(check.call_count, 1)

    def test_a_rerun_of_a_superseded_commit_paints_nothing(self):
        # The issue's repro: run one fails, a push fixes the prose, someone
        # re-runs run one. The exit code stays that run's own verdict, because
        # the commit it linted did have alerts, but the pull request's comment
        # and check run stay as the newer run left them.
        code, comment, check = self.report(head=NEW)
        self.assertEqual(code, 1)
        comment.assert_not_called()
        check.assert_not_called()

    def test_a_broken_stale_run_does_not_repaint_the_comment_red(self):
        write(
            self.stdout,
            json.dumps({"Code": "E201", "Text": "no styles", "Path": ".vale.ini"}),
        )
        code, comment, check = self.report(head=NEW, status=0)
        self.assertEqual(code, 2)
        comment.assert_not_called()
        check.assert_not_called()

    def test_a_broken_head_run_still_says_so(self):
        write(
            self.stdout,
            json.dumps({"Code": "E201", "Text": "no styles", "Path": ".vale.ini"}),
        )
        code, comment, check = self.report(head=OLD, status=0)
        self.assertEqual(code, 2)
        self.assertEqual(comment.call_count, 1)
        self.assertEqual(check.call_count, 1)


class Helpers(unittest.TestCase):
    def test_next_link(self):
        header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
        self.assertEqual(va.next_link(header), "https://api.github.com/x?page=2")

    def test_no_next_link_ends_the_walk(self):
        self.assertIsNone(va.next_link('<https://api.github.com/x?page=1>; rel="prev"'))
        self.assertIsNone(va.next_link(""))

    def test_boolean(self):
        for yes in ("true", "TRUE", " yes ", "1", "on"):
            self.assertTrue(va.boolean(yes), yes)
        for no in ("false", "no", "0", "", "maybe"):
            self.assertFalse(va.boolean(no), no)

    def test_plural(self):
        self.assertEqual(va.plural(1, "alert"), "1 alert")
        self.assertEqual(va.plural(0, "alert"), "0 alerts")
        self.assertEqual(va.plural(2, "file"), "2 files")

    def test_short(self):
        self.assertEqual(va.short(OLD), "aaaaaaa")
        self.assertEqual(va.short(""), "")


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
