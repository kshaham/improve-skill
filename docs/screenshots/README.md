# Board screenshots

[← Back to the project README](../../README.md#local-board)

These are browser captures of this repository's actual board at
`http://127.0.0.1:8765`, taken on 2026-10-05 UTC while updating the documentation.
The UI is from commit `6485e6c`; task state is local, untracked project history.
The images contain recorded work, not demo fixtures or generated mockups.

| Image                        | View                                            |
| ---------------------------- | ----------------------------------------------- |
| [board.png](board.png)       | Current run, working columns, and run controls  |
| [history.png](history.png)   | Current run's completed work in compact rows    |
| [overview.png](overview.png) | Current run's area totals and recorded activity |

The board was serving during capture; no background improvement daemon was running.
Unavailable run controls reflect that state. Screenshots are a snapshot, so their counts
and task statuses will differ from the live board as work continues.

To refresh them:

1. Start the board for this checkout with `scripts/improve-board.sh --repo "$PWD" --start`.
2. Open the returned URL in a clean browser session with a 1440 × 1120 viewport and
   device scale factor 1. These captures used Chrome through Playwright, with reduced motion.
3. Capture the initial Board view with `#run=current`.
4. Open `#view=history&run=current&outcome=done`, then `#view=overview&run=current`.
   For these views, scroll the tab bar to 24 pixels below the top of the viewport.
5. Save each viewport as the matching PNG above. Inspect the images and update this
   capture date/revision when replacing them. Do not fabricate task data for the gallery.

The captures do not submit requests or change the run. Test-generated screenshots are
separate artifacts described in the [development guide](../development.md).
