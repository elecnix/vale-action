#!/usr/bin/env python3
"""Unit tests for the verdict logic. Stdlib unittest, no install step.

The end-to-end proof lives in CI, which runs the composite action against the
three fixture repositories. These tests pin the decisions that are cheap to get
wrong and expensive to notice: what counts as a failed run, what `fail-on`
includes, which lines of a diff a change is answerable for, and that a message
cannot escape its table cell.
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


# A real `git diff --no-index` output, with the empty context line spelled as the
# single space a diff spells it with.
PATCH = (
    "diff --git a/docs/guide.md b/docs/guide.md\n"
    "index 1111111..2222222 100644\n"
    "--- a/docs/guide.md\n"
    "+++ b/docs/guide.md\n"
    "@@ -1,4 +1,5 @@\n"
    " # Guide\n"
    " \n"
    "-The old line.\n"
    "+The new line, which is very long.\n"
    "+And a second new line.\n"
    " Context stays.\n"
)


class PatchParsing(unittest.TestCase):
    """Which lines of a diff a change is answerable for."""

    def test_the_added_lines_are_the_ones_counted(self):
        self.assertEqual(va.parse_patch(PATCH), {3, 4})

    def test_the_file_header_is_not_added_text(self):
        self.assertEqual(va.parse_patch("+++ b/docs/guide.md\n--- a/docs/guide.md\n"), set())

    def test_a_patch_without_hunks_adds_nothing(self):
        # A rename or a mode change. The pull request did not write prose.
        self.assertEqual(va.parse_patch("diff --git a/x.md b/y.md\nsimilarity index 100%\n"), set())

    def test_a_new_file_is_added_in_full(self):
        patch = "@@ -0,0 +1,3 @@\n+One.\n+Two.\n+Three.\n"
        self.assertEqual(va.parse_patch(patch), {1, 2, 3})

    def test_added_text_that_looks_like_a_file_header_is_added_text(self):
        patch = "@@ -1,1 +1,2 @@\n # Guide\n+++ b/not-a-header.md\n"
        self.assertEqual(va.parse_patch(patch), {2})

    def test_a_removed_line_does_not_move_the_new_side(self):
        patch = "@@ -1,3 +1,2 @@\n keep\n-gone\n+added\n keep\n"
        self.assertEqual(va.parse_patch(patch), {2})

    def test_a_no_newline_note_is_not_a_line(self):
        patch = "@@ -1,1 +1,2 @@\n keep\n+added\n\\ No newline at end of file\n"
        self.assertEqual(va.parse_patch(patch), {2})

    def test_several_hunks_each_count_their_own_lines(self):
        patch = "@@ -1,1 +1,1 @@\n-one\n+one\n@@ -40,1 +40,2 @@\n forty\n+forty-one\n"
        self.assertEqual(va.parse_patch(patch), {1, 41})


class ChangedLines(unittest.TestCase):
    def test_only_paths_with_a_patch_are_mapped(self):
        files = [
            {"filename": "a.md", "status": "modified", "patch": "@@ -1,1 +1,2 @@\n a\n+b\n"},
            {"filename": "b.md", "status": "modified"},
        ]
        self.assertEqual(va.changed_lines(files), {"a.md": {2}})

    def test_a_path_without_a_patch_is_absent_rather_than_empty(self):
        # GitHub stops sending the patch once a diff passes its size limit.
        # "No diff information" must not read as "this change wrote nothing",
        # or every alert in a large file turns green.
        self.assertNotIn("b.md", va.changed_lines([{"filename": "b.md", "status": "modified"}]))


class Narrowing(unittest.TestCase):
    """Keeping only the alerts the change is answerable for."""

    def keep(self, alerts, changed):
        """The lines that survive, and the count left out."""
        kept, ignored = va.alerts_on_changed_lines(alerts, changed)
        return [row[1] for row in va.rows(kept)], ignored

    def test_an_alert_on_a_written_line_is_kept(self):
        self.assertEqual(self.keep({"a.md": [alert(line=7)]}, {"a.md": {7}}), ([7], 0))

    def test_an_inherited_alert_is_counted_but_not_kept(self):
        alerts = {"a.md": [alert(line=3), alert(line=7), alert(line=9)]}
        self.assertEqual(self.keep(alerts, {"a.md": {7}}), ([7], 2))

    def test_a_path_with_no_diff_information_keeps_every_alert(self):
        alerts = {"big.md": [alert(line=1), alert(line=900)]}
        self.assertEqual(self.keep(alerts, {}), ([1, 900], 0))

    def test_a_path_whose_patch_adds_nothing_keeps_nothing(self):
        alerts = {"renamed.md": [alert(line=2)]}
        self.assertEqual(self.keep(alerts, {"renamed.md": set()}), ([], 1))

    def test_a_line_nobody_can_read_is_counted_rather_than_hidden(self):
        for line in (0, None, "7", True):
            entry = alert(line=line)
            self.assertTrue(va.answerable(entry, set()), line)

    def test_a_narrowed_file_that_still_has_alerts_keeps_its_rows(self):
        kept, _ = va.alerts_on_changed_lines(
            {"a.md": [alert(line=3)], "b.md": [alert(line=4), alert(line=5)]},
            {"a.md": {3}, "b.md": {4}},
        )
        self.assertEqual(sorted(kept), ["a.md", "b.md"])


class ResolveFiles(unittest.TestCase):
    """Working out what to lint, and which of its lines are new."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.guide = os.path.join(self.root, "docs", "guide.md")
        os.makedirs(os.path.dirname(self.guide))
        write(self.guide, "# Guide\n\nIt is very fine.\n")
        self.files = os.path.join(self.root, "files.json")
        write(
            self.files,
            json.dumps(
                [
                    {
                        "filename": self.guide,
                        "status": "modified",
                        "patch": "@@ -1,2 +1,3 @@\n # Guide\n \n+It is very fine.\n",
                    }
                ]
            ),
        )
        self.lines = os.path.join(self.root, "lines.json")
        self.diff = os.path.join(self.root, "change.diff")
        write(
            self.diff,
            "diff --git a/%s b/%s\nindex 1111111..2222222 100644\n--- a/%s\n+++ b/%s\n"
            "@@ -1,2 +1,3 @@\n # Guide\n \n+It is very fine.\n"
            % ((self.guide, self.guide) * 2),
        )

    def args(self, **over):
        base = dict(
            repo=None, pr=None, token=None, files_json=self.files, lines_out=None, diff=None
        )
        base.update(over)
        return argparse.Namespace(**base)

    def resolve(self, **over):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = va.cmd_resolve_files(self.args(**over))
        return code, out.getvalue()

    def test_a_saved_file_list_is_read_instead_of_the_api(self):
        with mock.patch.object(va, "paginate") as api:
            code, listed = self.resolve()
        api.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(listed.strip(), self.guide)

    def test_the_diff_is_the_source_of_the_added_lines(self):
        # The files API left this patch out; the pull request's own diff still
        # names the lines, which is the whole point of preferring it.
        write(self.files, json.dumps([{"filename": self.guide, "status": "modified"}]))
        self.resolve(lines_out=self.lines, diff=self.diff)
        self.assertEqual(
            json.loads(va.read(self.lines)), {"lines": {self.guide: [3]}, "whole": []}
        )

    def test_a_file_the_diff_does_not_carry_is_named_whole(self):
        write(self.diff, "")
        write(self.files, json.dumps([{"filename": self.guide, "status": "modified"}]))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.resolve(lines_out=self.lines, diff=self.diff)
        self.assertEqual(
            json.loads(va.read(self.lines)), {"lines": {}, "whole": [self.guide]}
        )
        self.assertIn("No diff for", err.getvalue())

    def test_the_files_api_patch_is_the_fallback_the_diff_missed(self):
        write(self.diff, "diff --git a/other.md b/other.md\n@@ -1,1 +1,2 @@\n a\n+b\n")
        self.resolve(lines_out=self.lines, diff=self.diff)
        self.assertEqual(
            json.loads(va.read(self.lines)), {"lines": {self.guide: [3]}, "whole": []}
        )

    def test_only_the_paths_that_get_linted_are_written_out(self):
        # A path Vale is not run on has no alerts to narrow, and a path missing
        # from disk is dropped from the list; neither belongs in the JSON.
        write(self.diff, "")
        write(
            self.files,
            json.dumps(
                [
                    {"filename": self.guide, "status": "modified", "patch": "@@ -1,1 +1,2 @@\n a\n+b\n"},
                    {"filename": os.path.join(self.root, "gone.md"), "status": "modified", "patch": "@@ -1,1 +1,2 @@\n a\n+b\n"},
                    {"filename": os.path.join(self.root, "main.go"), "status": "modified", "patch": "@@ -1,1 +1,2 @@\n a\n+b\n"},
                ]
            ),
        )
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.resolve(lines_out=self.lines, diff=self.diff)
        self.assertEqual(
            json.loads(va.read(self.lines)), {"lines": {self.guide: [2]}, "whole": []}
        )

    def test_a_file_list_that_is_not_a_list_is_a_mis_invocation(self):
        write(self.files, json.dumps({"files": []}))
        with self.assertRaises(ValueError):
            va.load_files_json(self.files)

    def test_no_source_at_all_is_a_mis_invocation(self):
        args = self.args(files_json=None)
        with self.assertRaises(ValueError) as caught:
            va.cmd_resolve_files(args)
        self.assertIn("--files-json", str(caught.exception))


class Narrowed(unittest.TestCase):
    """Reading the lines file, and refusing to narrow on a guess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "lines.json")

    def narrowed(self, path=None):
        args = argparse.Namespace(changed_lines=self.path if path is None else path)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            changed, whole = va.narrowed(args)
        return (changed, whole), err.getvalue()

    def refused(self, payload):
        """A lines file the run must not trust. Returns what it warned about."""
        write(self.path, payload)
        (changed, _), err = self.narrowed()
        self.assertIsNone(changed)
        return err

    def test_no_flag_means_every_line_counts(self):
        (changed, whole), _ = self.narrowed(path=None)
        self.assertIsNone(changed)
        self.assertEqual(whole, [])

    def test_the_lines_out_are_read_as_sets(self):
        write(self.path, json.dumps({"lines": {"a.md": [3, 9]}, "whole": []}))
        (changed, whole), _ = self.narrowed()
        self.assertEqual(changed, {"a.md": {3, 9}})
        self.assertEqual(whole, [])

    def test_the_older_flat_shape_is_still_read(self):
        # A lines file written by the previous version has no whole-file names
        # in it, which is not an error.
        write(self.path, json.dumps({"a.md": [3, 9]}))
        (changed, whole), _ = self.narrowed()
        self.assertEqual(changed, {"a.md": {3, 9}})
        self.assertEqual(whole, [])

    def test_files_counted_whole_are_read_and_named(self):
        write(self.path, json.dumps({"lines": {}, "whole": ["plan.md"]}))
        (changed, whole), _ = self.narrowed()
        self.assertEqual(changed, {})
        self.assertEqual(whole, ["plan.md"])

    def test_a_missing_lines_file_counts_everything_and_says_so(self):
        (changed, _), err = self.narrowed(os.path.join(self.tmp.name, "absent.json"))
        self.assertIsNone(changed)
        self.assertIn("every alert counts", err)

    def test_an_empty_file_is_not_narrowing(self):
        # Legitimate: no changed path had a diff GitHub sent. Claiming to have
        # narrowed while counting everything would misreport the verdict.
        self.assertNotIn("::warning::", self.refused("{}\n"))

    def test_line_numbers_that_are_not_numbers_are_refused(self):
        self.assertIn("wrong line numbers", self.refused(json.dumps({"a.md": ["3"]})))

    def test_file_names_that_are_not_names_are_refused(self):
        payload = json.dumps({"lines": {}, "whole": [{"path": "a.md"}]})
        self.assertIn("wrong file names", self.refused(payload))

    def test_an_object_that_is_not_a_map_is_refused(self):
        self.assertIn("expected an object", self.refused(json.dumps([3, 9])))


class AddedLines(unittest.TestCase):
    """Reading one unified diff for the whole pull request."""

    def test_each_file_keeps_its_own_lines(self):
        diff = (
            "diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n@@ -1,1 +1,2 @@\n x\n+y\n"
            "diff --git a/b.md b/b.md\n--- a/b.md\n+++ b/b.md\n@@ -8,1 +8,2 @@\n x\n+y\n"
        )
        self.assertEqual(va.added_lines_by_path(diff), {"a.md": {2}, "b.md": {9}})

    def test_a_deleted_file_has_no_new_side(self):
        diff = (
            "diff --git a/gone.md b/gone.md\ndeleted file mode 100644\n--- a/gone.md\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-x\n-y\n"
        )
        self.assertEqual(va.added_lines_by_path(diff), {})

    def test_a_rename_is_keyed_on_the_new_path(self):
        diff = (
            "diff --git a/old.md b/new.md\nsimilarity index 90%\nrename from old.md\n"
            "rename to new.md\n--- a/old.md\n+++ b/new.md\n@@ -1,1 +1,2 @@\n x\n+y\n"
        )
        self.assertEqual(va.added_lines_by_path(diff), {"new.md": {2}})

    def test_a_file_with_no_hunks_adds_nothing(self):
        # A mode-only change carries no `+++` line, so the header is the only
        # place its path appears.
        diff = "diff --git a/a.md b/a.md\nold mode 100644\nnew mode 100755\n"
        self.assertEqual(va.added_lines_by_path(diff), {"a.md": set()})

    def test_a_quoted_path_with_a_space_is_read_whole(self):
        diff = (
            'diff --git "a/docs/my file.md" "b/docs/my file.md"\n'
            '--- "a/docs/my file.md"\n+++ "b/docs/my file.md"\n@@ -1,1 +1,2 @@\n x\n+y\n'
        )
        self.assertEqual(va.added_lines_by_path(diff), {"docs/my file.md": {2}})

    def test_an_empty_diff_names_nothing(self):
        self.assertEqual(va.added_lines_by_path(""), {})

    def test_the_file_header_is_not_added_text(self):
        diff = "diff --git a/a.md b/a.md\n--- a/a.md\n+++ b/a.md\n"
        self.assertEqual(va.added_lines_by_path(diff), {"a.md": set()})


class NarrowedBody(unittest.TestCase):
    """A narrowed verdict has to say that it is one."""

    def body(self, alerts, failed, ignored, narrow=True):
        return va.comment_body(alerts, 1, "error", "3.20.0", failed, ignored, narrow)

    def test_the_scope_is_stated(self):
        body = self.body({"a.md": [alert(line=7)]}, 1, 2)
        self.assertIn("the lines this change writes", body)
        self.assertIn("2 further alerts on untouched lines are left out", body)

    def test_a_single_inherited_alert_reads_as_one(self):
        body = self.body({}, 0, 1)
        self.assertIn("1 further alert on untouched lines is left out", body)
        self.assertIn("Nothing on the lines this change writes", body)

    def test_a_whole_file_run_claims_no_narrowing(self):
        body = self.body({"a.md": [alert()]}, 1, 0, narrow=False)
        self.assertNotIn("Counted here", body)
        self.assertIn("every line of every file linted", body)

    def test_the_check_title_says_what_was_counted(self):
        self.assertEqual(
            va.verdict_title(0, "error", 2, True), "Clean — nothing on the changed lines"
        )
        self.assertIn("on the changed lines", va.verdict_title(3, "error", 2, True))
        self.assertEqual(va.verdict_title(0, "error", 2, False), "Clean — 2 files linted")


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
        self.outputs_path = os.path.join(self.tmp.name, "outputs")
        os.environ["GITHUB_OUTPUT"] = self.outputs_path
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT", None)

    def summary_text(self):
        return va.read(self.summary)

    def outputs(self):
        return dict(
            line.split("=", 1)
            for line in va.read(self.outputs_path).splitlines()
            if "=" in line
        )


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
            changed_lines=None,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def report(self, head, **over):
        with mock.patch.object(va, "head_of", return_value=head), mock.patch.object(
            va, "upsert_comment"
        ) as comment, mock.patch.object(va, "create_check") as check, mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as err, mock.patch("sys.stdout", new_callable=io.StringIO):
            code = va.cmd_report(self.args(**over))
        self.err_text = err.getvalue()
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

    def lines(self, mapping):
        path = os.path.join(self.tmp.name, "lines.json")
        write(path, json.dumps(mapping))
        return path

    def test_a_file_counted_whole_is_named_and_changes_the_title(self):
        # The case that started this: the files API left PLAN.md's patch out, so
        # its four inherited alerts came back as "on the changed lines". The
        # fallback still counts the whole file, and now it says so.
        write(self.stdout, json.dumps({"plan.md": [alert(line=50)]}))
        lines = self.lines({"lines": {"a.md": [7]}, "whole": ["plan.md"]})
        code, comment, check = self.report(head=OLD, changed_lines=lines)
        self.assertEqual(code, 1)
        self.assertEqual(check.call_args[0][3], "failure")
        self.assertIn("counted whole", check.call_args[0][4])
        self.assertEqual(self.outputs()["alerts"], "1")
        self.assertIn("GitHub sent no diff", comment.call_args[0][3])
        self.assertIn("`plan.md`", comment.call_args[0][3])

    def test_an_inherited_alert_is_reported_but_does_not_fail_the_check(self):
        # The issue's repro: one line of a file that is already red, and the
        # line this change wrote carries nothing. The check goes green, and the
        # inherited alerts are counted and named rather than silently dropped.
        write(self.stdout, json.dumps({"a.md": [alert(line=9), alert(line=40)]}))
        code, comment, check = self.report(head=OLD, changed_lines=self.lines({"a.md": [12]}))
        self.assertEqual(code, 0)
        self.assertEqual(check.call_args[0][3], "success")
        self.assertEqual(self.outputs()["alerts"], "0")
        self.assertEqual(self.outputs()["ignored"], "2")
        self.assertIn("left out of the verdict", comment.call_args[0][3])

    def test_an_alert_the_change_wrote_still_fails_the_check(self):
        write(self.stdout, json.dumps({"a.md": [alert(line=9), alert(line=40)]}))
        code, comment, check = self.report(head=OLD, changed_lines=self.lines({"a.md": [40]}))
        self.assertEqual(code, 1)
        self.assertEqual(check.call_args[0][3], "failure")
        self.assertEqual(self.outputs()["alerts"], "1")

    def test_a_run_that_did_not_narrow_says_zero_ignored(self):
        self.report(head=OLD)
        self.assertEqual(self.outputs()["ignored"], "0")

    def test_an_unreadable_lines_file_counts_everything_and_says_so(self):
        code, _, _ = self.report(
            head=OLD, changed_lines=os.path.join(self.tmp.name, "absent.json")
        )
        self.assertEqual(code, 1)
        self.assertIn("every alert counts", self.err_text)
        self.assertEqual(self.outputs()["alerts"], "1")


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
