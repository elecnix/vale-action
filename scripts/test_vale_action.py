#!/usr/bin/env python3
"""Unit tests for the verdict logic. Stdlib unittest, no install step.

The end-to-end proof lives in CI, which runs the composite action against the
three fixture repositories. These tests pin the decisions that are cheap to get
wrong and expensive to notice: what counts as a failed run, what `fail-on`
includes, and that a message cannot escape its table cell.
"""

import json
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
