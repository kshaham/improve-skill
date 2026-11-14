# Board screenshots

[← Back to the project README](../../README.md#local-board)

These are browser captures of this repository's own task ledger, taken on 2026-11-14 UTC
during a timed improvement run of the board UI. The UI is from commit `62bbe79` on
`improve/2026-11-14`, served by `scripts/improve_board.py` from that checkout against a copy
of the run's `.improve` ledger (its tasks, discovery notes, and run state). The live board was
serving the installed copy of the skill at the time, so the copy let the captures show the
updated UI without disturbing it. The images contain recorded work, not demo fixtures or
generated mockups.

| Image                        | View                                               |
| ---------------------------- | -------------------------------------------------- |
| [board.png](board.png)       | Run controls and the current run's working columns |
| [history.png](history.png)   | Current run's completed work in compact rows       |
| [overview.png](overview.png) | Current run's area totals and recorded activity    |
| [wiki.png](wiki.png)         | Knowledge > Wiki overview built at session end      |

No background improvement daemon was running, so the run controls are unavailable. The
Knowledge tab shows 0 because the copy has no knowledge graph. Screenshots are a snapshot:
their counts and task statuses will differ from the live board as work continues.

wiki.png was captured the same way from `improve/2026-11-14-2` at commit `bd6f2eb`. The
copy also held this repository's knowledge graph and its run was marked finished, then the
documented `improve_graph.py sync --dream` built the wiki from it. The notes, areas and
session shown are this repository's own.

To refresh them:

1. Start the board for this checkout with `scripts/improve-board.sh --repo "$PWD" --start`.
2. Open the returned URL in a clean browser session with a 1440 × 1120 viewport and
   device scale factor 1. These captures used Chrome through Playwright.
3. For the Board view, open `#run=current`, choose **Hide** on Focus areas (a per-browser
   preference that does not change the run), and scroll Run controls to 20 pixels below the
   top of the viewport so the working columns are in view.
4. Open `#view=history&run=current&outcome=done`, then `#view=overview&run=current`.
   For these views, scroll the tab bar to 24 pixels below the top of the viewport, or as far
   as the page allows.
5. For the wiki, open `#view=graph&run=current`, choose **Wiki**, and scroll the tab bar to
   24 pixels below the top. The wiki exists only after a session has finished.
6. Save each viewport as the matching PNG above. Inspect the images and update this
   capture date/revision when replacing them. Do not fabricate task data for the gallery.

The captures do not submit requests or change the run. Test-generated screenshots are
separate artifacts described in the [development guide](../development.md).
