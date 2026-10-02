# Dashboard issue Inbox

The Inbox is an optional, read-only issue viewer with two writes: file an issue and post a top-level comment. Issue lifecycle decisions stay with agents and the CLI.

## Approved design

Atin approved round 4, take A ("Inbox"), on 2026-10-02 at commit fcb9e46. It is the binding design: the build reproduces it one to one in layout, copy, interactions and keyboard behaviour. The prototype is `docs/issues/prototypes/take-a-inbox.html` with `shared/shell.js`, `shared/shell.css` and `shared/mock-data.js`; its filing panel is the one in `shell.js`. Where this document and the prototype differ in detail, the prototype wins. Side-by-side evidence is in `docs/issues/evidence/lat365/`.

## Availability

`issues.enabled` must be exactly `true` before the shared dashboard adds its Issues tab (with the count of issues that have no story), the `+ Issue` button, the stylesheet or the issue scripts. With the feature off, a direct `#/issues` URL returns to the board and the browser requests no issue endpoint or asset. Turning the feature off while the Inbox is open returns to the board on the next refresh.

A hosted dashboard, and any board whose issue reads answer `LOCAL_ONLY`, shows "Issues are not available on this board yet." in place of the Inbox, never a load error.

## Inbox behaviour

The left pane has four queues: "No story" (open), "Has story" (linked), "Resolved", and "Closed" (dismissed or duplicate). Rows have a fixed height and reserve a thumbnail slot. A row shows the issue ID, then a tag chosen by that issue's own state, whichever list it is in: the filer for an open issue, the first live story and its status for a linked or resolved one, "dismissed" or "duplicate of <ID>" for a closed one. A video is pictured by its first stored frame; a video without frames gets the dark slot with no picture.

The right pane shows the copyable issue ID, a fixed-width state chip, the filer as an actor chip with the machine it was filed from, the title, the description, media inline (a video uses its first frame as poster and lists the frames an agent sees), linked stories or the closure reason, top-level comments, and collapsed history. Origins read as the prototype's: just the machine for a human who is that machine's user, otherwise `user@machine`.

`j`/`k` or the arrow keys move; `1`–`4` switch queues; `c` copies the ID; `f` opens or closes look closer; Space plays or pauses; Escape closes look closer or leaves a person view; `i` opens the filing panel from any dashboard view. The keys do nothing while a dashboard drawer or dialog (Settings, Filters, task detail, New Task) is open. Clicking an actor opens everything that person filed or commented on, newest activity first, with filing and comment counts and their machines, most used first.

## Filing

The panel is the prototype's: a required title (Enter moves to the description), an optional description, and a tray for photos and video with a running total against the per-issue limit (`issues.max_issue_media_mb`, per file `issues.max_media_mb`). Pasting or dropping a photo or video anywhere on the page opens the panel with it attached; while a file is dragged over the page, a border and "Drop to file a new issue with this attached" show where it will go. A file that is not a photo or video, or is over a limit, stays in the tray as a refusal and is not sent. ⌘ Enter (Ctrl Enter) files; Escape closes. "Record video" records from the camera into the tray.

A video goes up with its width, height, duration and up to eight JPEG frames sampled in the browser. A recording whose duration the browser cannot report (a WebM still reporting `Infinity`) is seeked to its end to learn it; if it stays unknown the upload says `duration_ms: null` and carries the first frame only.

## Refresh rule

The dashboard refreshes every few seconds. A refresh never interrupts the reader: a playing video keeps playing in the same element, the detail pane keeps its scroll, focus stays where it is, and half-typed comments and filings stay. A refresh changes only what changed: queue rows and counts are patched in place, the selected issue is refetched only when its own row changed, and media are rebuilt only when the set of media or frames changes. While the reader is playing the selected issue's video or has a comment draft or focus there, the selection stays on that issue even if it leaves the queue. `issue-view-logic.js` decides each refresh (`planRefresh`); `tests/js/issue-view-dom.test.js` drives the real view through refreshes and fails if any of these is lost.

## Narrow widths

The nav is the dashboard's and is the same on every view at a given width; the Inbox never hides or restyles nav items. The `+ Issue` button lets the nav's right-hand group wrap so nothing runs off a narrow window. Inside the Inbox, the queue keeps the prototype's `clamp(25rem, 24vw, 34rem)` width down to 760 px. Below 760 px the queue stacks above the issue, with a rule between them. Below 520 px the queue tabs and the detail header tighten. No width from 360 px to 1800 px scrolls the page sideways.

## API boundary and safety

`dashboard/api.py` adapts LAT-371's `issue_detail` and `issues_by` readers to the page's issue shape, removes local media paths and serves media and frames through the range-capable media route. Filing and commenting are the registered `issue.file` and `issue.comment` operations run by the board writer. The local issue-file JSON body gets its own computed allowance for encoded media and frames, with a hard 2 GiB ceiling; other local dashboard writes keep their 1 MiB cap.

Markup is built with escaped HTML and issue prose goes in through `textContent`; board text never becomes an inline handler. Pure rules live in `static/issue-view-logic.js` and the view in `static/issue-view.js`; both run under `node:test` (`tests/js/issue-view-logic.test.js`, `tests/js/issue-view-dom.test.js` with a small DOM in `tests/js/support/mini-dom.js`) through the pytest bridge in `tests/test_dashboard/test_js_issue_view_logic.py`.
