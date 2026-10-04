# Performance journeys and measurement

Read this only when performance is an active lane. Preserve the intake’s selected journeys
and success criteria; do not substitute an easier benchmark.

**The performance lane is top-down, and it starts by building its own instruments.**
Performance here means what the user feels - how long the app takes to be usable, how long
a screen takes to show its data, whether the main list scrolls without dropping frames, how
long the server takes to answer at the 95th percentile - not how fast a function is. A
function can get ten times faster without anyone noticing; a journey cannot. So the lane
works from the journey down, never from the code up:

1. **Journeys first.** At preflight, name the journeys selected by the intake; use the
   docs, main screens, and busiest handlers to resolve unspecified details: *cold launch to first
   interactive frame*, *open the main screen with a warm cache*, *scroll the main list for
   five seconds*, *the top three endpoints under a fixed request mix*. If no harness can
   measure them, the lane's first items are to build one, additive and in the toolchain's
   native form - `XCTApplicationLaunchMetric`, `XCTOSSignpostMetric`, `XCTClockMetric` and a
   scrolling UI test on iOS; `testing.B` over `httptest` handlers plus a query counter per
   request on Go; a load script only if the repo already ships one. Land the harness as its
   own commit, run it for baseline numbers, write them under `journeys` in `run.json`.
2. **Profile, do not guess.** Each refill, the performance finder runs the slowest journey
   under the platform's profiler - `xctrace` Time Profiler and Hangs on iOS, `pprof` CPU and
   allocation profiles on Go, the browser performance trace on web - and reports the
   **largest contributors by share of that journey's time**, with the frame. That is its
   evidence. A finding without a profile share is a smell, not a finding, and goes to
   `quality` if it is anything.
3. **Fix the biggest share, re-measure the journey.** The item's measurement is the
   journey the profile came from, not a micro-benchmark of the function that changed.
   Interleaved, no overlap, five percent floor, as the parent SKILL.md verification step says. A change that made the
   function faster and the journey no faster is `rejected` with both numbers.
4. **Report the journeys every hour.** `launch 1.84s -> 1.12s (-39%)`, one line per journey,
   baseline to now, in every report. Over a multi-day run this line is the run's result.
   Everything else in the lane is in service of moving it.

What moves a journey is rarely exotic: work on the main thread that belongs off it, a screen
that waits on three requests it could make in parallel or one it could cache, images decoded
at full size for a thumbnail, a view whose body recomputes on every keystroke, a handler that
runs one query per row, a response ten times the size the screen needs. The loop is allowed
all of these. What it is not allowed to do - add an index, change a schema, bump a
dependency, alter the wire format - it proposes with the journey numbers that justify it,
and those proposals are usually the largest remaining wins, so they go at the top of the
report, not the bottom.

Say plainly, in the first report, how far this lane can take the repo. An app that does the
things above will move a lot; an app that already does none of them will not, and the lane
will run dry quickly and say so. Numbers measured on a simulator or a development machine
are relative, not absolute, and the report says which.
