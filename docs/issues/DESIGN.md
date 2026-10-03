# Dashboard issue Inbox

The Inbox is an optional issue viewer with four writes: file an issue, post a top-level comment, close an issue with a reason, and reopen a closed one. Linking, promoting and marking duplicates stay with agents and the CLI.

## Approved design

Atin approved round 4, take A ("Inbox"), on 2026-10-02 at commit fcb9e46. It is the binding design: the build reproduces it one to one in layout, copy, interactions and keyboard behaviour. The prototype is `docs/issues/prototypes/take-a-inbox.html` with `shared/shell.js`, `shared/shell.css` and `shared/mock-data.js`; its filing panel is the one in `shell.js`. Where this document and the prototype differ in detail, the prototype wins. Side-by-side evidence is in `docs/issues/evidence/lat365/`.

## Availability

`issues.enabled` must be exactly `true` before the shared dashboard adds its Issues tab (with the count of issues that have no story), the `+ Issue` button, the stylesheet or the issue scripts. With the feature off, a direct `#/issues` URL returns to the board and the browser requests no issue endpoint or asset. Turning the feature off while the Inbox is open returns to the board on the next refresh.

A hosted dashboard, and any board whose issue reads answer `LOCAL_ONLY`, shows "Issues are not available on this board yet." in place of the Inbox, never a load error.

## Inbox behaviour

The left pane has four queues: "No story" (open), "Has story" (linked), "Resolved", and "Closed" (dismissed or duplicate). Rows have a fixed height and reserve a thumbnail slot. A row shows the issue ID, then a tag chosen by that issue's own state, whichever list it is in: the filer for an open issue, the first live story and its status for a linked or resolved one, "dismissed" or "duplicate of <ID>" for a closed one. A video is pictured by its first stored frame; a video without frames gets the dark slot with no picture.

The right pane shows the copyable issue ID, a fixed-width state chip, the filer as an actor chip with the machine it was filed from, the title, the description, media inline (a video uses its first frame as poster and lists the frames an agent sees), linked stories or the closure reason, top-level comments, and collapsed history. Origins read as the prototype's: just the machine for a human who is that machine's user, otherwise `user@machine`. History has one line per thing someone did: a filing with media is one line ("filed with 1 video", "filed with 1 photo and 1 video"), because the media events that share the filing's `op_id` fold into it; media added later keeps its own line ("added a photo").

`j`/`k` or the arrow keys move; `1`–`4` switch queues; `c` copies the ID; `f` opens or closes look closer; Space plays or pauses; Escape closes look closer or leaves a person view; `i` opens the filing panel from any dashboard view. The keys do nothing while a dashboard drawer or dialog (Settings, Filters, task detail, New Task) is open. Clicking an actor opens everything that person filed or commented on, newest activity first, with filing and comment counts and their machines, most used first. The right pane's header ends in one fixed-size control: Close for an open, linked or resolved issue, Reopen for a dismissed or duplicate one, and invisible (its space kept) with nothing selected. Close opens a one-line reason box under the header; the reason is required, so the confirm button and Enter do nothing while it is blank, and Escape or Cancel closes the box. Reopen posts at once. An operation refusal shows inline in the box (Close) or as a toast (Reopen). The control has no key of its own, and the queue keys stay inert while the reason box has focus.

## Filing

The panel is the prototype's: a required title (Enter moves to the description), an optional description, and a tray for photos and video with a running total against the per-issue limit (`issues.max_issue_media_mb`, per file `issues.max_media_mb`). Pasting or dropping a photo or video anywhere on the page opens the panel with it attached; while a file is dragged over the page, a border and "Drop to file a new issue with this attached" show where it will go. A file that is not a photo or video, or is over a limit, stays in the tray as a refusal and is not sent. ⌘ Enter (Ctrl Enter) files; Escape closes. "Record video" records from the camera into the tray.

A video goes up with its width, height, duration and up to eight JPEG frames sampled in the browser. A recording whose duration the browser cannot report (a WebM still reporting `Infinity`) is seeked to its end to learn it; if it stays unknown the upload says `duration_ms: null` and carries the first frame only.

## Refresh rule

The dashboard refreshes every few seconds. A refresh never interrupts the reader: a playing video keeps playing in the same element, the detail pane keeps its scroll, focus stays where it is, and half-typed comments and filings stay. An open reason box keeps its text and focus too; it closes only when the reader selects another issue or the issue stops being closable (someone else closed it). A refresh changes only what changed: queue rows and counts are patched in place, the selected issue is refetched only when its own row changed, and media are rebuilt only when the set of media or frames changes.

A refresh never moves the selection off the issue being read, whatever happens to it: linked, resolved, dismissed, out of the queue, its video playing, paused or never started. Only the reader moves it, with `j`/`k`, a click on a row, or a queue switch. While the selected issue is outside the queue's rows, the detail pane keeps showing it, updated in place, and no row is lit; `j` then goes to the row now where it would sit (the next one after it) and `k` to the row before, clamped to the first and last rows. Only an issue gone from the board lets the cursor land on the row at its old position, and the lit row follows. `issue-view-logic.js` decides each refresh (`planRefresh`); `tests/js/issue-view-dom.test.js` drives the real view through refreshes and fails if any of these is lost.

## Narrow widths

The nav is the dashboard's and is the same on every view at a given width; the Inbox never hides or restyles nav items. The Issues tab and `+ Issue` add about 200 px to it, so with issues on and the window below 1600 px the nav buys that back: Settings and Help show only their icons (their titles still name them), and the count badge, the tabs and the gaps are a little tighter. The nav is then one row wherever it was one row with issues off (default theme: from 1326 px, against 1327 px with issues off). At 1600 px and wider it is the prototype's. The right-hand group wraps rather than run off a narrow window. Inside the Inbox, the queue keeps the prototype's `clamp(25rem, 24vw, 34rem)` width above 760 px. At or below 760 px the queue stacks above the issue, with a rule between them. At or below 520 px the queue tabs and the detail header tighten. No width from 360 px to 1800 px scrolls the page sideways.

## API boundary and safety

`dashboard/api.py` adapts LAT-371's `issue_detail` and `issues_by` readers to the page's issue shape, removes local media paths and serves media and frames through the range-capable media route. Filing, commenting, closing and reopening are the registered `issue.file`, `issue.comment`, `issue.dismiss` and `issue.reopen` operations run by the board writer, as the board's configured human (`default_actor: human:...`) when there is one. Closing requires a non-blank reason, checked by the page and again by the route. The local issue-file JSON body gets its own computed allowance for encoded media and frames, with a hard 2 GiB ceiling; other local dashboard writes keep their 1 MiB cap.

Markup is built with escaped HTML and issue prose goes in through `textContent`; board text never becomes an inline handler. Pure rules live in `static/issue-view-logic.js` and the view in `static/issue-view.js`; both run under `node:test` (`tests/js/issue-view-logic.test.js`, `tests/js/issue-view-dom.test.js` with a small DOM in `tests/js/support/mini-dom.js`) through the pytest bridge in `tests/test_dashboard/test_js_issue_view_logic.py`.
