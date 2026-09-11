# vale-action

**A prose linter for pull requests that ships no rules of its own.**

[Vale](https://vale.sh) checks writing the way a linter checks code. This action
runs it on the markdown a pull request changed, posts one comment it keeps
updating, and sets a check.

The rules come from your repository. Not one rule ships in here.

## Why no rules

A rule set inside the action is a second copy of your style guide, and the two
copies drift. The first time either side changes, a contributor whose editor
says the file is clean gets a red check on a pull request they cannot reproduce
locally. Nobody trusts the gate after that.

So the action reads your `.vale.ini`, your `StylesPath`, and your `Packages =`
pins. Whatever `vale` prints on a contributor's laptop is what it prints here.

## Quick start

```yaml
name: prose
on: pull_request

permissions:
  contents: read
  pull-requests: write
  checks: write

concurrency:
  group: prose-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  vale:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: elecnix/vale-action@v1
```

That lints every markdown file the pull request touched, against the
`.vale.ini` in your repository.

A repository that commits its styles instead of pinning them turns the fetch
off:

```yaml
      - uses: elecnix/vale-action@v1
        with:
          sync: false
```

## Inputs

| Input | Default | What it does |
| --- | --- | --- |
| `config` | *(empty)* | Path to your `.vale.ini`. Empty lets Vale find it by walking up from the linted files, which is the normal case. |
| `sync` | `true` | Run `vale sync` first, to fetch what a `Packages =` line pins. Set `false` when the repository commits its styles. |
| `styles` | *(empty)* | An extra styles directory to add on top of the resolved `StylesPath`, for rules that live outside the pinned pack. |
| `files` | *(empty)* | What to lint, space or newline separated. Empty means the markdown this pull request changed. |
| `fail-on` | `error` | The alert level that fails the check: `suggestion`, `warning`, or `error`. |
| `comment` | `true` | Post the pinned pull-request comment. The check is set either way. |
| `check` | `true` | Create the `vale` check run. |
| `vale-version` | `3.20.0` | The Vale release to install. |
| `token` | `github.token` | Used to read the changed files and decorate the pull request. |

## Outputs

| Output | What it holds |
| --- | --- |
| `vale-version` | The Vale version that produced the verdict, so a reader can explain a verdict that changed. |
| `outcome` | `success`, `failure`, or `error` — the last meaning the linter could not run. |
| `alerts` | Total alerts Vale reported, at every level. |

## What it does on a pull request

**One comment, edited in place.** Every run finds the comment it left last time
and rewrites it. A push never adds a second one.

**A check named `vale`.** Green when nothing reached `fail-on`, red when
something did, and red with the words *could not run* when Vale itself failed.

**`--no-global`, always.** Without it a personal `~/.vale.ini` on the machine
changes the verdict, and a gate that reads a different rule set per machine is
not a gate.

**A pinned Vale version, verified.** The download is checked against the
release's published SHA-256 before it is unpacked, and the version lands in the
job log and in an output.

## A linter that could not run has not passed

This is the failure mode the action is built around.

`vale` exits 1 when it finds alerts, which is an ordinary verdict. It exits 2
when it failed. It also fails while exiting 0, by printing a JSON *error
object* in place of the results — a `StylesPath` that does not exist comes back
that way, and the object parses cleanly and contains no alerts.

Read that as a result and you get a green check on prose nothing ever read. The
action treats every one of those shapes as a failed run: exit code 2, a red
check, and a comment that says so.

## Pinning

Pin a tag, not a branch:

```yaml
- uses: elecnix/vale-action@v1        # moving major
- uses: elecnix/vale-action@v1.0.0    # exact
```

## Gotchas worth knowing

**`MinAlertLevel` in your `.vale.ini` outranks `fail-on`.** Vale never reports
what that setting hides, so `fail-on: suggestion` finds nothing under
`MinAlertLevel = error`. Lower the ini setting first.

**`BasedOnStyles` has to name every style directory.** A rule in a directory
missing from that list loads and then does nothing, with no warning.

**One rule per `.yml` file, and the filename becomes the rule name.** Two rules
in one file fail to load.

**Deleted files are dropped.** Vale reports a missing path as a failed run, so a
pull request that only removes prose stays green.

## Running it locally

The point of the no-rules design is that this reproduces the check exactly:

```sh
brew install vale          # or see https://vale.sh/docs/install
vale sync                  # only when your .vale.ini pins packages
vale --no-global $(git diff --name-only origin/main... -- '*.md')
```

`--no-global` matters as much on a laptop as on a runner. Drop it and your own
`~/.vale.ini` joins the rule set, and the answer stops matching CI.

## Fixtures

`fixtures/` holds three small repositories that CI runs the action against on
every change: one whose prose is clean, one whose prose is not, and one whose
configuration is broken. They pin the three verdicts — green, red, and *could
not run*.

## License

MIT. See [LICENSE](LICENSE).
