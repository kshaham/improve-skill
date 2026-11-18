# Board screenshots

[← Back to the project README](../../README.md#the-board)

These are browser captures of this repository's own task ledger, retaken on 2026-11-14 for the
1.19.0 board, served by `scripts/improve_board.py` against a copy of this repository's
`.improve` state, so the live board was not disturbed. The tasks, knowledge, wiki, and history shown are this repository's own records.

Two things in the copy were staged for the captures, and are not how the real run stood:

- Seven finished tasks were set back to queued, in progress, and blocked, so the working
  columns show cards. In the real ledger they are done.
- The run's deadline was moved forward and its outcome cleared, so the board shows a live run.

| Image                        | View                                                              |
| ---------------------------- | ----------------------------------------------------------------- |
| [focus.png](focus.png)       | Focus areas: the Work in parallel slider and improvement categories (an unsaved choice) |
| [board.png](board.png)       | The working columns with their status glyphs                      |
| [history.png](history.png)   | Completed and rejected work in compact rows                       |
| [overview.png](overview.png) | Area totals and recorded activity                                 |
| [wiki.png](wiki.png)         | Knowledge > Wiki overview built at session end                    |

No background improvement daemon was running, so the run controls are unavailable.
Screenshots are a snapshot: their counts and task statuses will differ from the live board as
work continues.

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
