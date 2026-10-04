# Local Kanban board

The board is a read-only view of the target repository's `.improve/` ledger. It needs
Python 3.9+ and a browser, with no npm install, hosted account, or external assets.
It does not run agents, advance the clock, approve proposals, or change task status.

## Launch and share the URL

Use the script belonging to the installed skill; do not assume it exists in the target
application repository. For example, with the checkout under `~/Code/improve-skill`:

```sh
improve_board="$HOME/Code/improve-skill/scripts/improve-board.sh"
improve_repo="$HOME/Code/my-app"
"$improve_board" --repo "$improve_repo" --start
"$improve_board" --repo "$improve_repo" --status --json
"$improve_board" --repo "$improve_repo" --stop
```

`--start` launches a detached service and returns its actual local URL. Repeated starts
reuse the same healthy service for that repository. The preferred port is 8765; an
occupied preferred port falls back to an available one. Use `--port 0` to request a free
port or `--port 9000` for a specific port. An occupied explicit port is an error. To change
an existing service's port, stop it first. Omitting `--start` runs the server in the
foreground until Ctrl-C. All modes require the Git repository root.

For foreground/scheduler runs, start it after intake and repo selection. Daemon runs
auto-start it after preparing valid run state, using either Codex or Claude. Daemon
`--board-port` controls the port; `--no-board` skips automatic startup without stopping an
existing board. Child cycles never manage the service. Starting or stopping the board
does not restart, extend, finalize, or stop the improvement run. A startup error should
be reported while improvement work continues.

Share the URL only after start/status confirms it is serving. Include it in opening,
hourly, and final reports. The service survives terminal closure and run completion;
it ends when explicitly stopped, killed, or the machine restarts. After a restart,
`--start` restores the view from disk. A remote machine's loopback URL is local to that
machine; access it through an SSH local port forward when needed.

## Keep the board faithful to the work

| Column | Ledger status | Meaning |
|---|---|---|
| Queued | `ready` | Accepted work waiting to begin |
| In progress | `in_progress` | The current task is being investigated or edited |
| Done | `done` | Verification or investigation finished; evidence recorded |
| Blocked | `blocked`, legacy `benched` | Work cannot proceed; note explains recovery |
| Proposed | `proposed` | A concrete proposal awaiting a user decision |
| Rejected | `rejected` | An idea failed verification or was ruled out; reason retained |

Create the row before work begins, using a stable ID, useful title/claim, area, acceptance
checks, and status. On each transition update its UTC `updated_at`, evidence/verification,
note, and actual commit when one exists. Keep one canonical row per ID, atomically rewrite
the ledger, and retain every finished/rejected item. Clear `run.json.active_item` only
after the task is settled. Legacy active items appear in In progress even when an older
backlog still says ready; an unresolved active item in a terminal run appears Blocked.
Explicitly `in_progress` tasks also appear Blocked once the run ends, with a note that
they were not settled. Unknown statuses appear Blocked with a warning rather than disappearing.

For `improve-max`, the board also reads `bets.jsonl`: `spiking`/`placed` map to In progress,
`landed` to Done, `killed` to Rejected, and `proposed` to Proposed. Each bet's details show
the original phase, journey, spike/landed measurements, implementation pieces with their
statuses/commits, and kill criteria. Maintain the original bet ledger; do not duplicate
bets in the ordinary backlog. Bet IDs and ordinary task IDs remain separate in the view.

Track meaningful investigations in `discovery.jsonl`, including empty and failed scans;
the board shows recent passes. A substantial standalone investigation can also have a
task card with `kind: "investigation"`, clearly identified and backed by findings. Do not
turn every tool call into a card, invent retrospective outcomes, or create busywork.
This board can show only recorded work; it does not reconstruct missing tasks from chat.

The browser defaults to **All runs**. Search covers task text, files, evidence, and commits;
filters narrow by run and area. Open a card for evidence, acceptance checks, verification,
files, and commit details. Export downloads the currently filtered tasks as JSON. The page
refreshes every three seconds while visible and every ten seconds while hidden. Connection
failures retain the last view and show a warning. Partial/malformed ledger rows generate
warnings while valid rows remain visible.
Non-finite measurements (`NaN`, infinities, or overflowing numbers) are invalid records;
they produce warnings rather than breaking the browser's entire task response.

## State and boundaries

The **Current run** health panel shows the saved engine/model, cycle/phase, work and
supervisor checkpoint times, account-limit retry, consecutive failures, and pending final
report. Checkpoint timestamps are saved observations, not proof of continuous activity.
**Board connected** means the viewer is responding; daemon liveness comes from its OS lock.
An active supervisor's null outcome takes precedence over an unconfirmed worker completion.
Foreground completion recorded before its deadline is flagged without modifying the ledger.
The panel remains about the current run when filtering archived task history.

An ended run with **Final report pending** still needs its report-only retry. During a
live report retry the board says **Writing final report** while retaining the original
outcome in stored history. Viewer warnings and pending reports never approve unverified work.

`.improve/board.json` contains the URL, process identity, and private stop token;
`board.lock` enforces one server per repo and `board.log` records startup errors. Keep
`.improve/` out of version control. Daemon `--new-run` archives the ledger under `history/`
but preserves these service files and the existing URL. Daemon `--status --json` includes
board liveness and its URL without exposing the token.

The server binds only `127.0.0.1`, validates local Host headers, and serves allowlisted
board assets and ledger fields. It does not serve arbitrary repository files, intake
answers, raw logs, or authorization state. Task text renders as text, never HTML. The
browser has no write/stop controls; the CLI stop action uses a private token. Treat task
content as locally visible and keep secrets out of task evidence. No remote fonts,
analytics, CDNs, or model calls are used.
