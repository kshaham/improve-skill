# Discovering and recording this repo's gates

A gate is the command that proves a change is good. Discover them once at preflight, record
them in `.improve/run.json`, and never re-derive them mid-run - a gate that changes shape
between cycles makes every earlier "green" uninterpretable.

## Discovery order

Look for what the repo already uses, in this order. Stop at the first that fits.

1. **The repo's own runner scripts.** `scripts/*test*`, `scripts/check*`, `bin/test`. These
   usually encode warm-cache tricks and correctness traps that a raw toolchain invocation
   loses. Read the script's comments before using it - they are the cheapest source of the
   repo's real constraints.
2. **A Makefile / Justfile target.** `make check`, `make test`, `just test`.
3. **The CI workflow.** `.github/workflows/*.yml` is the authoritative statement of what
   "good" means for this project. Note that CI is often deliberately cold and slow; take the
   *commands*, not the cold-start choices.
4. **The toolchain default.** `go test ./...`, `cargo test`, `npm test`, `pytest`,
   `xcodebuild test`.

## Record both a fast gate and a batch gate

Most repos have a cheap check suitable for every change and an expensive one worth running
hourly. Record both:

    "gates": {
      "go":    { "fast": "cd backend && go test ./internal/<pkg>/... && go vet ./...",
                 "batch": "cd backend && make check" },
      "swift": { "fast": "./scripts/ios-test.sh --no-build",
                 "batch": "./scripts/ios-test.sh" }
    }

Run the fast gate on every change and the batch gate once per hour before the report. A red
batch gate after green fast gates means the fast gate has a hole - report that as a finding
in its own right, because it is more valuable than whatever item exposed it.

## Measure the gate before trusting your instinct about it

Time each gate once at preflight and record the seconds in `run.json`. Assumptions about what
is slow are wrong often enough to be worth ten seconds of measurement:

- A test *suite* is rarely the cost. Compilation usually is. If a scoped run and a full run
  cost the same, scoping is not an optimisation and you should always run everything.
- Where a toolchain separates compiling from running (`xcodebuild build-for-testing` then
  `test-without-building`; `go test -c`; `cargo build --tests`), use the split. Build once
  per cycle, then run the gate as often as you like for nearly free.

## Language-specific traps worth encoding

**Go.** `go vet ./...` must be **module-wide**, every time, not scoped to the package you
touched. `go build ./...` does not type-check `_test.go` files, and a package-scoped
`go test ./internal/x/...` never compiles a distant package that depends on `x` by struct
conversion. Module-wide vet is the only thing that catches it.

**Swift / Xcode.** A new file that does not trigger project regeneration is never compiled,
so its tests do not fail - they silently do not exist. If the project is generated from
filesystem globs (Tuist, XcodeGen), adding a file must regenerate. Batch all file *additions*
in a cycle to one point so the regeneration cost is paid once.

**Anything with a coverage report.** Record how to produce one; T2 refill depends on it.

## Take a scanner baseline, and make it a gate

Whatever security scanners are already on this machine are used twice: once at preflight to
seed the backlog, and once an hour to check the loop's own commits. Probe `PATH` for the
usual ones - `gitleaks`, `semgrep`, `trivy`, `govulncheck`, `gosec`, `npm audit`,
`pip-audit`, `bandit`, `cargo audit`, `osv-scanner` - and record the ones present with the
exact JSON-emitting invocation under `scanners` in `run.json`. **Never install one** during a
run: new tooling on a machine unattended is outside what the loop may do, and a scanner
that appears mid-run makes earlier cycles' "nothing found" uninterpretable. If none are
present, say so in the first report and name which one would have paid for itself.

**Preflight - the baseline.** Run each scanner once on the baseline commit and store its
finding count in `run.json` next to its invocation. Seed the security backlog from the
output as `finder-briefs.md` describes. This is the only time scanner output becomes
backlog; refills do not re-run scanners, because a scanner's answer does not change until
the code does.

**Hourly - the gate on yourself.** Re-run each scanner before the hourly report and compare
the count against the baseline. A count that went *up* means a commit this loop made
introduced something a scanner can see - a logged token, an injectable string, a new
dependency path - and that is a red batch gate: find the commit by bisecting the hour's
SHAs, revert it, mark its item `rejected` with the scanner's line, and say so in the report
above everything else. A count that went down is expected and is not reported as an
achievement unless the reduction is one of the hour's landed items.

Time each scanner once at preflight alongside the gates and record the seconds.

## Record the detectors and the journey harnesses

Some lanes have a gate of their own. Discover and record each at preflight next to the
gates, or record that it is absent:

- **concurrency** - the race detector: `go test -race`, Swift strict-concurrency
  diagnostics, ThreadSanitizer. Under `detectors.race`.
- **accessibility** - the platform audit in the UI test target (`performAccessibilityAudit`,
  `axe`), with its baseline violation count. Under `detectors.a11y`. Only when the lane is on.
- **contracts** - the round-trip or schema-validation test, if one exists. Under
  `detectors.contracts`. Only when the lane is on.
- **performance** - one harness per journey under `journeys`, each with its unit and
  baseline runs. If none exists, the lane builds them as its first items; until then the
  field is empty and every performance finding is `proposed`.
- **gate-speed** - no separate gate; its measurement is `gate_seconds`, re-timed
  interleaved.

Run each detector once at preflight for its baseline, exactly as the gates. A detector that
is red on the baseline is reported and its lane is benched for the run - the loop cannot
attribute a race or an audit violation to its own change if the baseline already had it.

## Record each gate's signal

Alongside the command, record what the gate reports when it is green on the baseline - the
number of tests it ran. That is the gate's `signal`, and step 4 of the cycle compares every
later run against it: fewer tests than the baseline is red regardless of exit status,
because in this loop the count can only go up.

    "gates": {
      "go":    { "fast": "...", "batch": "...", "signal": { "fast": 212, "batch": 640 } }
    }

How to read the count is per toolchain - `go test -v` lines beginning `--- PASS`/`--- FAIL`,
`pytest`'s summary line, `xcodebuild`'s "Executed N tests", `cargo test`'s "N passed". Record
the extraction alongside the command so it is not re-derived mid-run. If a gate offers no
count, record `null` and treat empty output as red for that gate.

## Establish the baseline by running them

Run every recorded gate once at preflight. Green is the baseline. Red means stop and report:
a loop that starts on red cannot attribute failures to its own changes, and will thrash
reverting work that was never the problem.
