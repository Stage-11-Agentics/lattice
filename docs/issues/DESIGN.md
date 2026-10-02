# Dashboard issue Inbox

The Inbox is an optional, read-only issue viewer with two writes: file an issue and post a top-level comment. Issue lifecycle decisions stay with agents and the CLI.

## Availability

`issues.enabled` must be exactly `true` before the shared dashboard adds its Issues tab, `+ Issue` button, stylesheet, or issue scripts. With the feature off, a direct `#/issues` URL returns to the board and the browser does not request an issue endpoint or issue asset. The shared hosted dashboard loads the view but renders “Issues are not available on this board yet” without requesting issue data or sending a write.

## Inbox behavior

The left pane has four queues: open issues (“No story”), linked issues (“Has story”), resolved issues, and closed issues (dismissed or duplicate). Queue rows retain a fixed height and reserve a media thumbnail slot. The right pane shows a copyable issue ID, state, filing actor and origin, title, optional description, attached media, linked task facts or closure reason, top-level comments, and collapsed history. Linked task facts are read-only.

`j`/`k` or arrow keys move through the queue; `1`–`4` switch queues; `c` copies the displayed ID; `f` opens or closes the media viewer; `i` opens quick-file; Escape closes the media viewer or returns from a person view. Clicking an actor opens that actor’s filed and commented issues, ordered by their latest activity, with filing/comment counts and machine origins. Video frames seek the recording to the time represented by each frame.

Quick-file accepts a title, optional description, and pasted, dropped, chosen, or recorded media. Video uploads include browser-derived dimensions, duration, and up to eight JPEG frames. The comment composer sends only a body; it has no reply control.

## API and dependency boundary

`dashboard/api.py` owns the adapter between the dashboard’s stable issue shape and the LAT-371 issue detail/comment readers. LAT-371’s `issue_detail` reader is used when present; on the LAT-366 base the adapter composes the existing issue view, event log, comment materializer, and media reader without writing state. It removes local media paths and exposes media through the existing range-capable media route.

All POSTs remain named operations executed by the existing board writer. The adapter inspects the registered `issue.file` parameter shape so the LAT-366 `text` signature can receive the title and description while the LAT-371 signature receives its separate fields. Comment submission targets the registered `issue.comment` operation. When that operation is absent, the board’s normal unknown-operation response is preserved; the dashboard does not create a comment event itself.

Issue API and media requests accept loopback Host names or the configured dashboard host. Media IDs are validated before constructing media URLs, and media ETags are emitted only for lowercase 64-character SHA-256 digests.

The local issue-file JSON body gets a separate computed allowance for encoded media and video frames, with a hard 2 GiB ceiling. Other local dashboard writes retain their 1 MiB cap; hosted requests keep the hosted server's existing body policy.

## Safety and tests

The component uses escaped HTML for markup and `textContent` for issue prose. Actions are delegated through data attributes; board-provided text never becomes an inline handler. Pure queue and person-view rules live in `static/issue-view-logic.js` and run under `node:test` through the pytest bridge. API and body-limit behavior are covered by focused dashboard tests. The LAT-371 operation contract and hosted runtime path still require post-rebase validation.
