# Characterization

A rewrite is only "the same" if "the same" is a test. The characterization corpus is that
test: recorded inputs and the outputs the current implementation produces for them, replayed
against whatever implementation is in place. It is built before a subsystem is touched,
landed as its own commit, and proven by mutation like any other gate.

## Where it lives

    .improve/corpus/<subsystem>/
      README.md          what was recorded, from where, how to re-record
      record.*           the script that produced the corpus
      cases/             one file per case: input, expected output, and the case's origin
      replay_test.*      the test that feeds every case through the implementation

The replay test is written in the repo's test idiom and placed where the repo's tests live,
so the ordinary gate runs it. The corpus files themselves stay under `.improve/corpus/`,
which is gitignored: they are the loop's working evidence, and they can be large. The
`README.md` says how to regenerate them.

## What to record, per kind of subsystem

**Handlers and services.** Request and response pairs, including headers that matter,
status codes, and error bodies. Sources, in order of preference: the repo's own integration
tests and fixtures; a request log or HAR from development; synthesized requests that walk
every branch the handler's tests name. Include the error cases - the malformed input, the
missing record, the unauthorized caller. A corpus of happy paths pins nothing worth pinning.

**Data layers.** Queries and their result sets over the development-data snapshot in
`.improve/corpus/data/`. Order-insensitive comparison unless the query orders. For writes,
the state of the affected tables before and after.

**View models and screens.** The view model's inputs (state, events) and outputs (rendered
state, emitted actions) as pairs; for the screen itself, snapshot tests at the sizes and
appearances the repo already tests. A rewrite of the UI layer passes when the snapshots
match pixel-for-pixel or within the repo's existing tolerance - not a new, looser one.

**Pure computation.** Inputs and outputs, with the edge cases the tests and comments name,
plus property-style generated inputs where the domain allows (dates around DST and month
ends, empty and single-element collections, maximum sizes, unicode).

**Anything money-adjacent or once-only.** All of the above, plus every edge the code's own
comments and the git history of the file mention. Read `git log -p` on the file; every bug
fixed there is a case. This is the corpus that must be complete before the invariant is
moved, and it is the reason those invariants may be moved at all.

## Proving the corpus

Before it is trusted, the replay is run against the *current* implementation under the
parent's mutation check: break the implementation in a way its own tests would catch and
confirm the replay goes red on the relevant case. If the corpus stays green, it is not
observing the behaviour that matters, and more cases are needed before any bet is placed.

Record the mutation used and the case that caught it in the corpus `README.md`.

## Shadow comparison

While a piece can run both ways - the old implementation still present behind the seam -
run both on the full corpus and on the journey harness's traffic, and diff every output.

- Zero differences: the piece may land.
- A difference that is an intended change (a bug the rewrite fixes, a field the new stack
  serialises in a canonical order): pin it. Update the case's expected output, record the
  reason in the case file, and note the change in the piece's commit message. The corpus
  now describes the new behaviour and the human can read why.
- A difference the loop cannot explain: investigate once. A second unexplained difference
  kills the bet. "Probably fine" is not an explanation.

Shadow results go on the piece in `bets.jsonl`: `"shadow":"0 diffs / 1,240 cases"` or
`"shadow":"2 pinned / 1,240 cases: see cases/0412, cases/0980"`.

## Retiring tests by successor

When the old implementation is removed, its tests go with it - but only by this rule. For
each original test being removed:

1. Name the corpus case, or the new test, that asserts the same thing.
2. Prove it: apply the mutation that makes the original test go red to the *new*
   implementation and confirm the successor goes red too.
3. Record the pair in `.improve/corpus/<subsystem>/retired.md`: original test, successor,
   mutation used.

A test with no provable successor is not removed. It is ported to the new implementation,
or the piece that would remove it does not land. The removal commit's message lists the
retired tests and points at `retired.md`. This is the rule a rewrite is most tempted to
bend, and it is the one that makes a rewritten system trustworthy to the human who did not
watch it happen.
