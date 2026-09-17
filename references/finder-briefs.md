# Finder briefs

Dispatch these as **read-only** subagents, four per refill by the rotation in SKILL.md (`Explore`,
or a reviewer agent with no write tools). They search and report. They never edit - editing
happens serially in the main session, so that one gate run maps to exactly one change.

## Rules that apply to every brief

Append this to each:

> Return findings as a JSON array matching `.improve/backlog.jsonl`'s schema. Every finding
> MUST carry: the file path, the line, a one-sentence claim, the evidence you actually read,
> and a concrete failure scenario - specific inputs or state leading to a specific wrong
> outcome. A finding that cannot name what breaks and when is a preference, not a finding;
> drop it rather than padding the list.
>
> Do not report style nits the repo's own linter would already catch, and do not report
> anything that contradicts a documented decision in CLAUDE.md, AGENTS.md, or a comment that
> explains why the code is the way it is. This codebase records rejected alternatives in
> comments; re-proposing one is a false positive.
>
> Here are the findings already in the ledger, including dismissed ones. Do not repeat any of
> them: <paste the deduped claim list>
>
> Return at most 8 findings. Fewer real ones beats more padded ones. Returning an empty array
> is a valid and useful answer.

## security - always dispatched, always preempts

**The scanner baseline is already in the ledger; do not redo it.** At preflight the loop ran
whatever scanners this machine has (see `verification.md`) and seeded the security backlog
from their output: secrets in source are `ready` at severity 5 with the scanner's line as
evidence; SAST hits are `ready` only once you, the finder, have traced them to an untrusted
input, and `rejected` with the reason if you cannot; vulnerable-dependency advisories are
`proposed`, always, because fixing one is a dependency bump. Your brief is the space the
scanners cannot reach: authorization decided in the wrong place, a business rule that can be
walked around, a check made on one copy of the data and a decision made on another, trust
extended to a caller because it is "internal". Time spent re-deriving what a secret scanner
printed is time this lane does not have.

- T1: injection reachable from untrusted input (SQL built by concatenation, command
  construction, path traversal in file handling); authz checks missing on a protected
  endpoint; secrets or tokens in source, logs, or error messages; unsafe deserialization;
  weak password hashing; missing rate limiting on auth endpoints.
- T2: input validated at the wrong boundary; error responses leaking internal detail;
  cookies missing `httpOnly`/`Secure`; TLS or certificate handling that accepts too much.
- T3: dependency surface reachable from user input; privilege boundaries that are conventions
  rather than enforced.
- T4: systemic patterns - a whole layer that trusts its caller.

Report a finding only if you can trace the path from untrusted input to the vulnerable sink.
"This function does not validate its argument" is not a vulnerability unless something
reaches it with something bad.

## quality

- T1: swallowed errors and empty catch blocks; a result assigned and never checked; nil or
  optional dereferenced without the guard the surrounding code uses elsewhere; off-by-one and
  boundary handling; a race between a read and a dependent write; resource leaks.
- T2: error messages with no context; inconsistent handling of the same failure in two places.
- T3: duplicated logic with drift between copies; dead code with no caller; a file that has
  outgrown one responsibility; a comment that contradicts its code.
- T4: a boundary that leaks internals and could be narrowed.

## coverage

- T1: an uncovered branch on a path that can actually fail in production.
- T2: error paths and boundary cases with no test; public surface with no test at all. Use
  the repo's coverage report to target this rather than guessing.

**Run the profile with every twin the repo can actually reach.** A suite that skips a
repository twin when an environment variable is unset reports the skipped twin's code as
uncovered, and a finder reading that profile files findings against code that IS tested - or
worse, reads a genuine gap as a skip and drops it. Check the harness for a skip switch before
trusting a percentage, and say in the report which twins ran.

**Regenerate the coverage profile with the cache off.** In Go that is `-count=1`. Without it the
toolchain serves profiles built at an earlier revision, and the merged file contains overlapping
cover blocks at two different line numberings - so a block-level claim can name a percentage for
code that has since been covered, or point at the wrong line entirely. A finder once reported a
function at 39.5% that the previous commit had already taken to full coverage. Any block-level
conclusion drawn from a cached profile is unreliable, and this is cheap to get right.
- T3: tests asserting implementation detail instead of behaviour; a test whose name promises
  more than it checks.
- T4: whole subsystems tested only through their happy path.

New tests must match the repo's existing test idiom exactly - its framework, its naming, its
file placement, and any filename conventions that carry meaning. Read a neighbouring test
file before writing one. A test that does not run because it was placed or named wrong is
worse than no test, because it reports success.

## performance - what the user feels, from the journey down

You are given the journeys in `run.json` with their current numbers, and the profiler
invocation recorded at preflight. Your brief is not to read the code for slow-looking
things. It is to run the slowest journey under the profiler and report what its time is
made of.

- T1: the largest single contributor to the slowest journey, by share of its time, with the
  frame: main-thread work that belongs off it; a screen waiting on requests it could make
  in parallel or serve from a cache it already has; images decoded at full size for a
  thumbnail; a view whose body recomputes on every state change; a handler running one
  query per row; a response many times larger than the screen consumes; synchronous disk
  or network on the launch path.
- T2: the next contributors down to five percent of the journey; a journey that is fast
  warm and slow cold, with what the cold path does that the warm one does not; a hang the
  profiler's hang detector reports.
- T3: work done on every journey that only some journeys need; a cache that exists and is
  not hit.
- T4: a data flow that forces the journey to wait on something the user never sees
  (proposal when the fix is a schema, an index, a wire-format change or a dependency).

Every finding carries the journey, the profile share, and the frame or query. "This could
be faster" is not a finding. "Cold launch spends 41% of 1.84s in `ImageLoader.decode` on
the main thread, called from `FeedView.body`, for 12 thumbnails at 4000x3000" is. The gate
is the journey's own harness, interleaved, as SKILL.md step 4 says - a change that made the
function faster and the journey no faster is rejected.

If `run.json` has no journeys yet, your only findings are the journeys themselves: which
three to five matter, from the repo's docs, main screens and busiest handlers, and how each
would be measured in this toolchain. Those become the lane's first items.

## gate-speed - the loop's own lever

Make this repo's gates faster without making them prove less. Every second saved here is
paid back on every cycle of the rest of the run, which is why this lane is always on.

- T1: a cache that is configured but missing (`DerivedData`, `GOCACHE`, `node_modules`,
  `~/.cargo`) on the path the gate takes; a test suite that runs its slowest package first
  or serially where the toolchain parallelises by default; a `sleep` or a real-time wait in
  a test that could be a fake clock; a build-for-testing / test-without-building split the
  gate does not use.
- T2: a test fixture rebuilt per test that could be built per suite; a database or simulator
  booted per test file; compilation of targets the gate does not exercise.
- T3: tests that are slow because they are integration tests in disguise and could be
  unit tests over the same behaviour (report only - the original stays; a faster twin is
  additive).
- T4: a CI workflow that is cold by design where warm would be safe (proposal only - CI
  files are the human's).

Every finding here carries a **before time** from `gate_seconds` in `run.json` and an
estimated after. The gate for a gate-speed change is the gate itself, timed three times
interleaved exactly as a performance item, plus its signal count unchanged: a faster gate
that ran fewer tests is a broken gate, not a faster one.

## concurrency

- T1: shared mutable state reached from two goroutines / tasks / threads without a lock or
  an actor; a check-then-act on shared state; a callback that mutates UI state off the main
  actor; a channel or continuation that can be resumed twice or never; a `Task` that
  captures `self` past its lifetime.
- T2: a lock held across an await or a blocking call; a context or cancellation token that
  is accepted and never checked; a goroutine started with no way to stop it.
- T3: an actor or lock protecting too much, so the hot path serialises on it.
- T4: a design that shares by communicating in one half and by memory in the other.

**The gate is the race detector, not a reading.** `go test -race` over the package, Swift 6
strict-concurrency diagnostics (`-strict-concurrency=complete`), ThreadSanitizer for
anything native, `--detectOpenHandles` and fake timers where the runtime has no detector.
Record the detector invocation in `run.json` at preflight. A finding the detector cannot
reproduce is reported at confidence 0.5 or below and lands only with a test that makes the
interleaving deterministic. Never "fix" a race by adding a sleep.

## resilience

- T1: a network or subprocess call with no timeout; a retry with no backoff or no cap; a
  write that is retried and is not idempotent; an error logged without the identifier
  needed to find the request it belonged to; a failure that leaves a resource half-updated.
- T2: a cancellation that is ignored so the work completes for nobody; an error type
  collapsed to a string so callers cannot branch on it; a fallback that silently serves
  stale or empty data with no signal.
- T3: the same failure handled three ways in three places; a dependency called with no
  circuit-breaking where one outage takes the whole surface down.
- T4: a boundary with no degraded mode at all.

Each finding names the **fault** (the dependency goes away; the response arrives after N
seconds; the process is killed between these two writes) and what a caller observes. The
gate is a fault-injection test - a failing fake, a short deadline, a cancelled context -
that goes red before the fix and green after, in the repo's own test idiom.

## docs

Runs at every third rotation turn by default. Its findings are cheap and safe; they are
rarely urgent.

- T1: a command in `README`, `CONTRIBUTING` or `docs/` that no longer runs, or a setup step
  that no longer matches the tree (a renamed script, a moved config, a flag that was
  removed); a comment that states the opposite of the code beneath it.
- T2: a documented environment variable or config key the code never reads, or one the
  code reads that no document names; an example that cannot compile against the current
  API.
- T3: architecture notes describing a layer that no longer exists.
- T4: a doc set organised around a design the code has left behind (proposal only).

The gate is running the documented command or compiling the documented example. A fix
here never invents new documentation - it corrects what is wrong and removes what is
dead. Fixing the code to match the doc is a `quality` item, not a docs one, and needs its
own evidence.

## accessibility - only when preflight found a UI target

- T1: an interactive control with no accessible label; an image that conveys meaning with
  no description; a custom control that reports no trait or role, so assistive tech cannot
  operate it; text that does not scale with the platform's type setting where the
  surrounding screen does.
- T2: focus order that does not follow reading order; a state change (loading, error,
  success) announced visually only; contrast below the platform's guideline on a text the
  user must read; a touch target below the platform minimum.
- T3: a whole screen with no accessibility identifiers, so it cannot be UI-tested either.
- T4: a component library that would need a shared accessible base (proposal only).

Each finding names the screen, the element, and what a screen-reader or switch user
cannot do. The gate is the platform's audit run in the UI test - `performAccessibilityAudit`
in XCUITest, `axe` in a browser test, the accessibility inspector's script on RN - recorded
in `run.json` at preflight with its baseline count. A fix lands with the audit count down
and the signal count up, never by suppressing an audit rule.

## contracts - only when preflight found a server and a client of it

- T1: a field the server emits under one name and the client reads under another; a field
  the client treats as required that the server can omit; an enum value the server can
  return that the client does not handle; an error status the server sends that the client
  maps to "unknown error".
- T2: a date, number or identifier format that differs between the two sides; pagination or
  cursor semantics the client assumes and the server does not guarantee.
- T3: a type duplicated by hand on both sides where the tree has a generator or a shared
  package it is not using.
- T4: an API surface with no schema at all (proposal: what to generate it from).

Each finding names both sides with file and line and the payload that breaks. The gate is a
round-trip test: the server's actual response shape decoded by the client's actual decoder,
or the schema file validated against both. Never change the wire format to fix a client -
that is a migration by another name and it is the human's; fix the side that is wrong, and
if both could be, the item is `proposed`.
