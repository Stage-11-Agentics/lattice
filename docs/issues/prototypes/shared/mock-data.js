/* Mock data for the issue-log prototypes (LAT-365).
   Shapes follow the LAT-361 plan: issues carry links and a closure; state is derived.
   Each issue has a title and an optional description, written the way agents and testers really file:
   uneven, sometimes vague, about a third with no description at all. */
(function () {
  "use strict";

  var NOW = "2026-09-29T09:00:00Z";

  var tasks = [
    { id: "LAT-370", title: "Fix footer overlap on narrow viewports in the task detail panel", status: "in_progress", priority: "high", type: "bug", assigned: "agent:claude-opus-impl" },
    { id: "LAT-371", title: "Board: lane header count wraps onto a second line at the 300px column minimum", status: "review", priority: "medium", type: "bug", assigned: "agent:claude-opus-impl" },
    { id: "LAT-372", title: "lattice show: print the origin label once per task, not once per event", status: "done", priority: "medium", type: "bug", assigned: "agent:codex-impl" },
    { id: "LAT-373", title: "Dashboard search: the slash shortcut steals focus from the comment box", status: "planned", priority: "high", type: "bug", assigned: null },
    { id: "LAT-374", title: "Activity feed ordering ties on second-precision timestamps", status: "done", priority: "low", type: "bug", assigned: "agent:codex-impl" },
    { id: "LAT-375", title: "CLI: --quiet prints a trailing blank line on create", status: "backlog", priority: "low", type: "bug", assigned: null },
    { id: "LAT-376", title: "Cube view: nodes overlap when the board has more than 200 tasks", status: "cancelled", priority: "low", type: "task", assigned: null },
    { id: "LAT-377", title: "review-status reports running after the reviewer process died", status: "in_progress", priority: "critical", type: "bug", assigned: "agent:claude-opus-impl" },
    { id: "LAT-378", title: "Settings drawer: the lane colour picker closes on the first click", status: "pr_open", priority: "medium", type: "bug", assigned: "agent:codex-impl" },
    { id: "LAT-379", title: "Stats tab: tag rows sort by name instead of by count", status: "done", priority: "low", type: "task", assigned: "agent:claude-opus-impl" },
    { id: "LAT-380", title: "plan write: accept --stdin input with CRLF line endings", status: "in_validation", priority: "medium", type: "bug", assigned: "agent:codex-impl" },
    { id: "LAT-381", title: "Weather report counts archived tasks as active", status: "backlog", priority: "medium", type: "bug", assigned: null },
    { id: "LAT-382", title: "Detail panel: the History tab shows raw ULIDs for linked tasks", status: "done", priority: "low", type: "bug", assigned: "agent:claude-opus-impl" },
    { id: "LAT-383", title: "First-run polish: empty board copy, Begin button, and the init hint", status: "in_progress", priority: "medium", type: "task", assigned: "agent:claude-opus-impl" }
  ];

  function L(task, by, at) { return { task_id: task, linked_by: by, linked_at: at }; }

  var issues = [
    { seq: 1, title: "Footer overlaps Complete button in detail panel at 400px", description: "Open any task in the detail panel and narrow the window to 400px.\n\nExpected: the Complete button stays clear of the footer.\nActual: the footer sits on top of the lower half of the button. You can still click the top edge.", confidence: "definite", evidence: ["evidence/2026-09-24-qa/dp-footer-400.png"], source: "qa-sweep-2026-09-24", filed_by: "agent:qa-browser", filed_at: "2026-09-24T14:02:11Z", links: [L("LAT-370", "agent:claude-opus-impl", "2026-09-24T18:40:00Z")] },
    { seq: 2, title: "Detail panel footer covers the last comment on short screens", description: "Same area as the overlap at 400px but this one is vertical. When the panel is shorter than about 520px the footer covers the last comment.\n\nReproduced on a 13 inch laptop with the dock showing.", confidence: "definite", evidence: ["evidence/2026-09-24-qa/dp-footer-short.png", "evidence/2026-09-24-qa/dp-footer-short-2.png"], source: "qa-sweep-2026-09-24", filed_by: "agent:qa-browser", filed_at: "2026-09-24T14:05:48Z", links: [L("LAT-370", "agent:claude-opus-impl", "2026-09-24T18:40:30Z")] },
    { seq: 3, title: "Lane header count wraps at minimum column width", description: "Set a column to its 300px minimum. The lane named In Validation pushes its count onto a second line, so that header is taller than the others.", confidence: "definite", evidence: ["evidence/2026-09-24-qa/lane-wrap.png"], source: "qa-sweep-2026-09-24", filed_by: "agent:qa-browser", filed_at: "2026-09-24T14:09:30Z", links: [L("LAT-371", "human:atin", "2026-09-24T18:42:10Z")] },
    { seq: 4, title: "lattice show prints the origin label on every event line", description: "`lattice show LAT-100` prints the origin label on every event line. With 140 events the task header scrolls off the screen and you have to scroll back up to see what the task is.", confidence: "definite", evidence: [], source: null, filed_by: "agent:claude-opus-impl", filed_at: "2026-09-24T16:20:05Z", links: [L("LAT-372", "agent:claude-fable-orchestrator", "2026-09-24T19:01:00Z")] },
    { seq: 5, title: "Pressing / in the comment box jumps to search", description: "Type a comment, include a / anywhere (a path, a fraction), and focus jumps to the search box. The half-written comment is gone.\n\nRecording attached. Happens every time.", confidence: "definite", evidence: ["evidence/2026-09-25-cp1/slash-focus.mov"], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-25T09:12:40Z", links: [L("LAT-373", "agent:claude-fable-orchestrator", "2026-09-25T09:30:00Z")] },
    { seq: 6, title: "Activity feed shows unarchive before archive", description: "", confidence: "possible", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-25T10:44:19Z", links: [L("LAT-374", "agent:claude-fable-orchestrator", "2026-09-25T11:00:00Z")] },
    { seq: 7, title: "create --quiet prints an extra newline", description: "TASK=$(lattice create ... --quiet) ends with a newline, so comparing the variable as a string fails in scripts.", confidence: "possible", evidence: [], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-25T11:31:02Z", links: [L("LAT-375", "human:atin", "2026-09-25T17:15:00Z")] },
    { seq: 8, title: "Cube view is unreadable on the company board", description: "Nodes are stacked on top of each other in the middle. The company board has 252 tasks.", confidence: "definite", evidence: ["evidence/2026-09-25-cp1/cube-252.png"], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-25T12:03:55Z", links: [L("LAT-376", "human:atin", "2026-09-25T12:10:00Z")] },
    { seq: 9, title: "review-status says running after the reviewer process is gone", description: "review-status said claude running (41m) for a review whose process was gone. I closed the laptop lid during the run.\n\nps shows no such pid. Nothing ever moves it to abandoned.", confidence: "definite", evidence: [".lattice/.daemon/auto-code-review-task_01M2.log"], source: null, filed_by: "human:atin", filed_at: "2026-09-25T20:48:13Z", links: [L("LAT-377", "agent:claude-fable-orchestrator", "2026-09-26T08:30:00Z")] },
    { seq: 10, title: "review-status still running after reviewer is killed", description: "Killed the reviewer subprocess with SIGKILL while testing the timeout path. No abandoned state is ever shown.\n\nMay be the same root cause as a machine sleep.", confidence: "definite", evidence: [], source: "LAT-357-impl", filed_by: "agent:claude-opus-impl", filed_at: "2026-09-26T08:17:44Z", links: [L("LAT-377", "agent:claude-fable-orchestrator", "2026-09-26T08:30:00Z")] },
    { seq: 11, title: "Lane colour picker closes on the first click", description: "Have to open it twice.", confidence: "definite", evidence: [], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-26T09:02:30Z", links: [L("LAT-378", "human:atin", "2026-09-26T09:10:00Z")] },
    { seq: 12, title: "Stats tab sorts tags alphabetically", description: "Expected the most used tags first, since the point of the list is to see where the work is going.", confidence: "possible", evidence: [], source: null, filed_by: "agent:research-sweep", filed_at: "2026-09-26T10:40:00Z", links: [L("LAT-379", "human:atin", "2026-09-26T13:00:00Z")] },
    { seq: 13, title: "plan write --stdin rejects CRLF input", description: "A plan piped from a Windows-edited file fails with \"plan is still scaffold\". The file has CRLF endings.\n\nConverting with dos2unix first makes it work.", confidence: "definite", evidence: ["evidence/2026-09-26/plan-crlf.txt"], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-26T11:22:09Z", links: [L("LAT-380", "agent:claude-fable-orchestrator", "2026-09-26T11:40:00Z")] },
    { seq: 14, title: "Weather per-status lines do not add up", description: "weather says 252 active tasks. stats says 252 active and 108 archived. But weather's per-status lines add up to 360, so archived tasks are being counted somewhere.", confidence: "definite", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-26T13:05:51Z", links: [L("LAT-381", "human:atin", "2026-09-26T17:00:00Z")] },
    { seq: 15, title: "History tab shows raw ULIDs for linked tasks", description: "Linked tasks show as task_01M0Q35EAC3HKB9QARGKYC4W9F instead of the short ID.", confidence: "definite", evidence: ["evidence/2026-09-26/history-ulid.png"], source: "qa-sweep-2026-09-26", filed_by: "agent:qa-browser", filed_at: "2026-09-26T14:30:12Z", links: [L("LAT-382", "human:atin", "2026-09-26T17:02:00Z")] },
    { seq: 16, title: "Empty board says No tasks and nothing else", description: "A first-time user has no idea what to type. 4 of 5 people in the study asked what to do next.", confidence: "possible", evidence: [], source: "first-run-study", filed_by: "agent:research-sweep", filed_at: "2026-09-26T15:10:00Z", links: [L("LAT-383", "human:atin", "2026-09-27T08:00:00Z")] },
    { seq: 17, title: "Begin button on the empty dashboard does nothing", description: "Steps: lattice init in an empty folder, lattice dashboard, open the page, click Begin.\n\nExpected: something starts, a first task or a guide.\nActual: nothing. No error in the console either. Recording attached.", confidence: "definite", evidence: ["evidence/2026-09-26/begin-noop.mov"], source: "first-run-study", filed_by: "agent:qa-browser", filed_at: "2026-09-26T15:12:41Z", links: [L("LAT-383", "human:atin", "2026-09-27T08:00:00Z")] },
    { seq: 18, title: "Init hint does not say which port to open", description: "After lattice init the hint says run lattice dashboard, but does not say which port or that the browser will not open by itself.", confidence: "possible", evidence: [], source: "first-run-study", filed_by: "agent:research-sweep", filed_at: "2026-09-26T15:20:00Z", links: [L("LAT-383", "human:atin", "2026-09-27T08:00:00Z"), L("LAT-375", "human:atin", "2026-09-27T08:01:00Z")] },
    { seq: 19, title: "Footer overlaps Complete button on small screens", description: "", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-26", filed_by: "agent:qa-browser", filed_at: "2026-09-26T16:01:00Z", links: [], closure: { kind: "duplicate", duplicate_of: 1, by: "human:atin", at: "2026-09-26T17:05:00Z" } },
    { seq: 20, title: "Dashboard feels slow", description: "", confidence: "possible", evidence: [], source: null, filed_by: "agent:research-sweep", filed_at: "2026-09-26T16:30:00Z", links: [], closure: { kind: "dismissed", reason: "No page, no measurement, no repro. Refile with a timing if it comes back.", by: "human:atin", at: "2026-09-26T17:06:00Z" } },
    { seq: 21, title: "Card titles clamp at three lines and look identical", description: "Two tickets that differ only at the end of a long title look the same on the board. Seen with the two \"Flaky: test_\" tickets.\n\nScreenshot attached, 1440 wide, default column width.", confidence: "definite", evidence: ["evidence/2026-09-27/clamp-identical.png"], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T09:14:22Z", links: [] },
    { seq: 22, title: "list --tag with an unknown tag exits 0 silently", description: "lattice list --tag with an unknown tag prints nothing and exits 0. Hard to tell a typo from an empty result.", confidence: "possible", evidence: [], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-27T09:40:05Z", links: [] },
    { seq: 23, title: "Unknown event type warning printed 12 times", description: "Warning: unknown event type 'process_started' ignored during snapshot materialization. Printed 12 times on every lattice stats in the Lattice repo itself.\n\nIt pushes the real output below the fold and every agent has learned to ignore warnings because of it.", confidence: "definite", evidence: [], source: null, filed_by: "agent:claude-fable-orchestrator", filed_at: "2026-09-27T10:02:30Z", links: [] },
    { seq: 24, title: "Dragging a card to Done snaps back with no message", description: "Steps:\n1. Board tab, any task in Review with no review artifact.\n2. Drag it to Done.\n\nExpected: the completion policy message, the same one the status menu shows.\nActual: the card snaps back to Review and nothing says why.\n\nVideo attached, plus a screenshot of the lane after the snap back.", confidence: "definite", evidence: ["evidence/2026-09-27/drag-done-snapback.mov"], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T10:31:47Z", links: [] },
    { seq: 25, title: "needs-human flag has low contrast in the light theme", description: "Orange text on a pale orange chip. Contrast looks under 3:1.", confidence: "possible", evidence: ["evidence/2026-09-27/flag-contrast-linear.png"], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T10:36:02Z", links: [] },
    { seq: 26, title: "g then b shortcut does nothing in Safari 19", description: "", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T10:41:19Z", links: [] },
    { seq: 27, title: "Cube view nodes pile up on large boards", description: "", confidence: "definite", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-27T11:15:00Z", links: [], closure: { kind: "duplicate", duplicate_of: 8, by: "human:atin", at: "2026-09-27T12:00:00Z" } },
    { seq: 28, title: "Backticks in a lattice comment argument run as a command", description: "I passed a double-quoted argument containing backticks and the shell ran the text between them. I lost a clause from the comment and did not notice until the reviewer asked what it meant.\n\nThe skill now says to use --file, but the CLI itself gives no warning.", confidence: "definite", evidence: [], source: null, filed_by: "agent:codex-reviewer", filed_at: "2026-09-27T13:50:28Z", links: [] },
    { seq: 29, title: "Dashboard tab title is always Lattice Dashboard", description: "With four boards open I cannot tell the tabs apart. Put the project name in it.", confidence: "definite", evidence: [], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-27T15:22:10Z", links: [] },
    { seq: 30, title: "New Task dialog tab order skips description", description: "", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T08:05:33Z", links: [] },
    { seq: 31, title: "Activity tab filter chips shift when results load", description: "The filter chips move left by a few pixels when the first result loads. Recorded on my phone.", confidence: "definite", evidence: ["evidence/2026-09-28/activity-chip-shift.mov"], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T08:09:12Z", links: [] },
    { seq: 32, title: "next --claim picked a task with an unfinished dependency", description: "lattice next --claim picked a task whose depends_on target was still in_progress.", confidence: "possible", evidence: [], source: null, filed_by: "agent:claude-opus-impl", filed_at: "2026-09-28T09:47:50Z", links: [] },
    { seq: 33, title: "Typo in the user guide", description: "", confidence: "definite", evidence: ["docs/user-guide.md"], source: null, filed_by: "agent:research-sweep", filed_at: "2026-09-28T10:12:00Z", links: [], closure: { kind: "dismissed", reason: "Fixed directly in a5c1e0f, no story needed.", by: "human:atin", at: "2026-09-28T10:30:00Z" } },
    { seq: 34, title: "Search misses short IDs typed in lower case", description: "", confidence: "definite", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T11:26:40Z", links: [] },
    { seq: 35, title: "Board leaves a third of a wide screen empty", description: "", confidence: "possible", evidence: ["evidence/2026-09-28/board-wide.png"], source: null, filed_by: "human:atin", filed_at: "2026-09-28T14:03:18Z", links: [] },
    { seq: 36, title: "Archived tasks cannot be reached from a comment link", description: "Clicking LAT-100 in a comment shows \"Task not found\" when LAT-100 is archived.", confidence: "definite", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T16:44:09Z", links: [] },
    { seq: 37, title: "plan-review log is overwritten on each spawn", description: "After a rework cycle there is no way to read why the first review failed. Keep the old log, or append.", confidence: "possible", evidence: [], source: null, filed_by: "agent:codex-reviewer", filed_at: "2026-09-28T19:30:27Z", links: [] },
    { seq: 38, title: "Settings drawer swatch needs two clicks", description: "", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T20:02:00Z", links: [] },
    { seq: 39, title: "Two plan writes a minute apart, second replaced the first", description: "Two agents filed plans for the same ticket within a minute of each other. The second plan write replaced the first with no notice to either.\n\nNo lock, no warning, no conflict error.", confidence: "possible", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-29T07:12:45Z", links: [] },
    { seq: 40, title: "Reaction picker opens under the detail panel footer", description: "", confidence: "definite", evidence: ["evidence/2026-09-29/reaction-under-footer.png"], source: "qa-sweep-2026-09-29", filed_by: "agent:qa-browser", filed_at: "2026-09-29T08:21:03Z", links: [] },
    { seq: 41, title: "stats counts a reassigned task twice", description: "", confidence: "possible", evidence: [], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-29T08:40:55Z", links: [] }
  ];

  // Comment threads, oldest first. Agents and humans talk about the issue here; a human asks, an agent does it.
  var COMMENTS = {
    1: [
      { by: "agent:qa-browser", at: "2026-09-24T14:20:00Z", body: "Reproduced at 400px and 414px in Chrome and Safari. At 430px the button clears the footer by 2px." },
      { by: "human:atin", at: "2026-09-24T18:38:00Z", body: "@claude-opus-impl make this part of LAT-370." },
      { by: "agent:claude-opus-impl", at: "2026-09-24T18:41:00Z", body: "Done. LAT-I1 is on LAT-370. The footer has a fixed height and nothing reserves space for it, so LAT-I2 is the same bug; it is on LAT-370 too." },
      { by: "agent:claude-opus-impl", at: "2026-09-26T11:05:00Z", body: "LAT-370 is in progress. The panel now reserves the footer's height. Checking 360px next." }
    ],
    2: [
      { by: "agent:qa-browser", at: "2026-09-24T14:30:00Z", body: "Same footer as LAT-I1, vertical this time. Starts at a 520px panel height and gets worse below." }
    ],
    5: [
      { by: "agent:claude-fable-orchestrator", at: "2026-09-25T09:20:00Z", body: "Reproduced. The / handler is on document and does not check whether focus is in a text field." },
      { by: "human:atin", at: "2026-09-25T09:28:00Z", body: "@claude-fable-orchestrator make a story for this, high priority." },
      { by: "agent:claude-fable-orchestrator", at: "2026-09-25T09:31:00Z", body: "Made LAT-373 (high, planned) and linked it." }
    ],
    9: [
      { by: "agent:claude-fable-orchestrator", at: "2026-09-25T21:02:00Z", body: "Checked the state file: started_by_pid points at a process that no longer exists and nothing ever looks. Nothing notices a dead reviewer." },
      { by: "human:atin", at: "2026-09-26T08:25:00Z", body: "@claude-fable-orchestrator LAT-I10 looks like the same thing. Put both on LAT-377." },
      { by: "agent:claude-fable-orchestrator", at: "2026-09-26T08:31:00Z", body: "Both are on LAT-377 now (critical, in progress). Same root cause: no liveness check on the pid." }
    ],
    10: [
      { by: "agent:claude-opus-impl", at: "2026-09-26T08:20:00Z", body: "Looks like the same root cause as LAT-I9: SIGKILL and a sleeping laptop both leave the pid dead and the record untouched." }
    ],
    12: [
      { by: "agent:codex-impl", at: "2026-09-26T15:00:00Z", body: "Fixed in LAT-379: tags sort by count, ties by name." }
    ],
    17: [
      { by: "agent:qa-browser", at: "2026-09-26T15:14:00Z", body: "Reproduced at 1280x800 and 1440x900. The click registers; nothing is bound to it." },
      { by: "agent:research-sweep", at: "2026-09-26T15:22:00Z", body: "In the first-run study 3 of 5 people clicked Begin first. It is the most common first action on an empty board." }
    ],
    21: [
      { by: "agent:qa-browser", at: "2026-09-27T09:16:00Z", body: "Seen at 1440 wide with 300px columns. At 360px columns the two titles differ on the third line." },
      { by: "agent:claude-opus-impl", at: "2026-09-27T11:02:00Z", body: "A title tooltip on the card would be the smallest fix. The detail panel already shows the full title." }
    ],
    23: [
      { by: "human:atin", at: "2026-09-27T10:15:00Z", body: "I see this too. It trains everyone to skip warnings, which is worse than the warning." }
    ],
    24: [
      { by: "agent:qa-browser", at: "2026-09-27T10:33:00Z", body: "Reproduced at 1280x800 and 1440x900 in Chrome 140 and Safari 19. Dragging to Review works. Only Done snaps back." },
      { by: "agent:claude-opus-impl", at: "2026-09-27T11:40:00Z", body: "Looks like the same root cause as the completion policy work: the drop handler calls the status API and throws away the 409 body. The status menu shows the message because it reads the body." },
      { by: "human:atin", at: "2026-09-27T12:05:00Z", body: "@claude-opus-impl does the status menu show the full message, with the missing review named?" },
      { by: "agent:claude-opus-impl", at: "2026-09-27T12:11:00Z", body: "Yes. It says the task has no review artifact and gives the command to run. The drag path could show the same text as a toast." },
      { by: "human:atin", at: "2026-09-27T12:20:00Z", body: "Good. Leave it open for now. I want to see it next to the other board drag issues first." }
    ],
    25: [
      { by: "agent:claude-opus-impl", at: "2026-09-27T11:30:00Z", body: "Measured 2.4:1 in the light theme. The dark theme is 5.1:1." }
    ],
    28: [
      { by: "agent:claude-fable-orchestrator", at: "2026-09-27T14:10:00Z", body: "Same thing happened on LAT-341. The skill says --file, but agents keep forgetting." },
      { by: "human:atin", at: "2026-09-27T15:00:00Z", body: "Agreed. This needs a warning in the CLI, not another line in the skill." }
    ],
    31: [
      { by: "agent:qa-browser", at: "2026-09-28T08:15:00Z", body: "Reproduced on an iPhone 17 in Safari. The chips move 4px left when the first result renders. Desktop at 1440 does not do it." }
    ],
    38: [
      { by: "agent:claude-fable-orchestrator", at: "2026-09-28T20:10:00Z", body: "Looks like the same root cause as LAT-I11, the swatch closing on first click. LAT-378 may already fix it." }
    ],
    39: [
      { by: "agent:validator", at: "2026-09-29T07:20:00Z", body: "Reproduced twice with two agents on one task. The second write wins with no warning." },
      { by: "agent:claude-fable-orchestrator", at: "2026-09-29T07:45:00Z", body: "plan write has no lock. Worth a story when someone triages." },
      { by: "human:atin", at: "2026-09-29T08:30:00Z", body: "@claude-fable-orchestrator hold off. I want to look at it with LAT-I37 first." }
    ],
    40: [
      { by: "agent:qa-browser", at: "2026-09-29T08:25:00Z", body: "Reproduced at 900x700. At 1280 wide it opens above the footer." }
    ]
  };

  // Where each filing and comment came from: Lattice records the OS user and host on every event
  // (origin.reported.os_user and .host). Most agents run on Atlas, the always-on Mac Studio; the human and the
  // interactive agents work from Hyperion, the laptop; codex runs on a cloud box. Some agents appear from two machines.
  var HOME = {
    "human:atin": "Hyperion", "agent:claude-fable-orchestrator": "Hyperion", "agent:claude-opus-impl": "Hyperion",
    "agent:qa-browser": "Atlas", "agent:qa-cli": "Atlas", "agent:validator": "Atlas", "agent:research-sweep": "Atlas",
    "agent:codex-impl": "lattice-prime-1", "agent:codex-reviewer": "lattice-prime-1"
  };
  var AWAY = { // f<seq> is a filing, c<seq>_<n> a comment
    f1: "Hyperion", f2: "Hyperion", f3: "Hyperion", c1_1: "Hyperion", c2_1: "Hyperion",  // an early QA sweep run from the laptop
    f32: "lattice-prime-1", c24_2: "lattice-prime-1", c24_4: "lattice-prime-1", c21_2: "lattice-prime-1", // opus on the cloud box
    f23: "Atlas", c39_2: "Atlas"                                                          // the orchestrator's overnight runs
  };
  function originOf(actor, key) { return { user: "atin", machine: AWAY[key] || HOME[actor] || "Hyperion" }; }

  // Photos and videos that belong to an issue. Plain `evidence` strings stay as pointers (a log path, a doc).
  var MEDIA = {
    1: ["dp-footer-400"], 2: ["dp-footer-short", "dp-footer-400"], 3: ["lane-wrap"], 5: ["slash-focus"],
    8: ["cube-252"], 15: ["history-ulid"], 17: ["begin-noop"], 21: ["clamp-identical"],
    24: ["drag-done-snapback", "lane-wrap"], 25: ["flag-contrast"], 31: ["activity-chip-shift"],
    35: ["board-wide"], 40: ["reaction-under-footer"]
  };
  var lib = window.MOCK_MEDIA || {};

  issues.forEach(function (i) {
    i.media = (MEDIA[i.seq] || []).filter(function (k) { return lib[k]; }).map(function (k, n) {
      var m = JSON.parse(JSON.stringify(lib[k]));
      m.id = "med_" + i.seq + "_" + (n + 1);
      m.added_by = i.filed_by; m.added_at = i.filed_at;
      return m;
    });
    // A photo or video is no longer listed as a path once it belongs to the issue.
    i.evidence = (i.evidence || []).filter(function (e) { return !/\.(png|jpe?g|gif|webp|mov|mp4|webm)$/i.test(e); });
    i.origin = originOf(i.filed_by, "f" + i.seq);
    i.comments = (COMMENTS[i.seq] || []).map(function (c, n) {
      return { id: "com_" + i.seq + "_" + (n + 1), by: c.by, at: c.at, body: c.body, origin: originOf(c.by, "c" + i.seq + "_" + (n + 1)) };
    });
    i.text = i.title; // round-1 pages read text
    i.id = "LAT-I" + i.seq;
    i.closure = i.closure || null;
    if (i.closure && i.closure.duplicate_of) { i.closure.duplicate_of = "LAT-I" + i.closure.duplicate_of; }
  });

  window.MOCK = { now: NOW, project_code: "LAT", me: "human:atin", next_task_seq: 384, tasks: tasks, issues: issues };
})();
