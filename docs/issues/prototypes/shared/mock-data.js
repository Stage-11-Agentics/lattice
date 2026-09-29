/* Mock data for the issue-log prototypes (LAT-365).
   Shapes follow the LAT-361 plan: issues carry links and a closure; state is derived.
   The text is written the way agents and testers really file: uneven, long, sometimes vague. */
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
    { seq: 1, text: "Footer overlaps the Complete button in the detail panel at 400px wide", confidence: "definite", evidence: ["evidence/2026-09-24-qa/dp-footer-400.png"], source: "qa-sweep-2026-09-24", filed_by: "agent:qa-browser", filed_at: "2026-09-24T14:02:11Z", links: [L("LAT-370", "human:atin", "2026-09-24T18:40:00Z")] },
    { seq: 2, text: "Detail panel footer covers the last comment when the panel is shorter than about 520px. Same area as the overlap at 400px but this one is vertical. Reproduced on a 13 inch laptop with the dock showing.", confidence: "definite", evidence: ["evidence/2026-09-24-qa/dp-footer-short.png", "evidence/2026-09-24-qa/dp-footer-short-2.png"], source: "qa-sweep-2026-09-24", filed_by: "agent:qa-browser", filed_at: "2026-09-24T14:05:48Z", links: [L("LAT-370", "human:atin", "2026-09-24T18:40:00Z")] },
    { seq: 3, text: "Lane header count wraps to a second line when the column is at its minimum width and the lane name is In Validation", confidence: "definite", evidence: ["evidence/2026-09-24-qa/lane-wrap.png"], source: "qa-sweep-2026-09-24", filed_by: "agent:qa-browser", filed_at: "2026-09-24T14:09:30Z", links: [L("LAT-371", "human:atin", "2026-09-24T18:42:10Z")] },
    { seq: 4, text: "`lattice show LAT-100` prints the origin label on every event line. With 140 events the task header scrolls off the screen.", confidence: "definite", evidence: [], source: null, filed_by: "agent:claude-opus-impl", filed_at: "2026-09-24T16:20:05Z", links: [L("LAT-372", "agent:claude-fable-orchestrator", "2026-09-24T19:01:00Z")] },
    { seq: 5, text: "Pressing / while typing in the comment box jumps to search and the half-written comment is lost", confidence: "definite", evidence: ["evidence/2026-09-25-cp1/slash-focus.mov"], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-25T09:12:40Z", links: [L("LAT-373", "human:atin", "2026-09-25T09:30:00Z")] },
    { seq: 6, text: "Activity feed shows unarchive before archive for the same task when both happen within one second", confidence: "possible", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-25T10:44:19Z", links: [L("LAT-374", "agent:claude-fable-orchestrator", "2026-09-25T11:00:00Z")] },
    { seq: 7, text: "create --quiet output has an extra newline, breaks TASK=$(lattice create ... --quiet) when the variable is compared as a string", confidence: "possible", evidence: [], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-25T11:31:02Z", links: [L("LAT-375", "human:atin", "2026-09-25T17:15:00Z")] },
    { seq: 8, text: "Cube view is unreadable on the company board, nodes stacked on top of each other in the middle", confidence: "definite", evidence: ["evidence/2026-09-25-cp1/cube-252.png"], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-25T12:03:55Z", links: [L("LAT-376", "human:atin", "2026-09-25T12:10:00Z")] },
    { seq: 9, text: "review-status said claude running (41m) for a review whose process was gone. I closed the laptop lid during the run. ps shows no such pid.", confidence: "definite", evidence: [".lattice/.daemon/auto-code-review-task_01M2.log"], source: null, filed_by: "human:atin", filed_at: "2026-09-25T20:48:13Z", links: [L("LAT-377", "human:atin", "2026-09-25T20:55:00Z")] },
    { seq: 10, text: "review-status still reports running after the reviewer subprocess was killed with SIGKILL. No abandoned state is ever shown. Found while testing the timeout path; may be the same root cause as a machine sleep.", confidence: "definite", evidence: [], source: "LAT-357-impl", filed_by: "agent:claude-opus-impl", filed_at: "2026-09-26T08:17:44Z", links: [L("LAT-377", "agent:claude-fable-orchestrator", "2026-09-26T08:30:00Z")] },
    { seq: 11, text: "Lane colour picker closes when I click the swatch, have to open it twice", confidence: "definite", evidence: [], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-26T09:02:30Z", links: [L("LAT-378", "human:atin", "2026-09-26T09:10:00Z")] },
    { seq: 12, text: "Stats tab tag list is alphabetical. Expected the most used tags first, since the point of the list is to see where the work is going.", confidence: "possible", evidence: [], source: null, filed_by: "agent:research-sweep", filed_at: "2026-09-26T10:40:00Z", links: [L("LAT-379", "human:atin", "2026-09-26T13:00:00Z")] },
    { seq: 13, text: "plan write --stdin rejects a plan piped from a Windows-edited file: \"plan is still scaffold\". The file has CRLF endings. Converting with dos2unix first makes it work.", confidence: "definite", evidence: ["evidence/2026-09-26/plan-crlf.txt"], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-26T11:22:09Z", links: [L("LAT-380", "agent:claude-fable-orchestrator", "2026-09-26T11:40:00Z")] },
    { seq: 14, text: "weather says 252 active tasks, stats says 252 active and 108 archived, but weather's per-status lines add up to 360", confidence: "definite", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-26T13:05:51Z", links: [L("LAT-381", "human:atin", "2026-09-26T17:00:00Z")] },
    { seq: 15, text: "History tab in the detail panel lists linked tasks as task_01M0Q35EAC3HKB9QARGKYC4W9F instead of the short ID", confidence: "definite", evidence: ["evidence/2026-09-26/history-ulid.png"], source: "qa-sweep-2026-09-26", filed_by: "agent:qa-browser", filed_at: "2026-09-26T14:30:12Z", links: [L("LAT-382", "human:atin", "2026-09-26T17:02:00Z")] },
    { seq: 16, text: "Empty board says \"No tasks\" and nothing else. A first-time user has no idea what to type.", confidence: "possible", evidence: [], source: "first-run-study", filed_by: "agent:research-sweep", filed_at: "2026-09-26T15:10:00Z", links: [L("LAT-383", "human:atin", "2026-09-27T08:00:00Z")] },
    { seq: 17, text: "The Begin button on an empty dashboard does nothing when clicked", confidence: "definite", evidence: ["evidence/2026-09-26/begin-noop.mov"], source: "first-run-study", filed_by: "agent:qa-browser", filed_at: "2026-09-26T15:12:41Z", links: [L("LAT-383", "human:atin", "2026-09-27T08:00:00Z")] },
    { seq: 18, text: "After lattice init the hint says run lattice dashboard, but does not say which port or that the browser will not open by itself", confidence: "possible", evidence: [], source: "first-run-study", filed_by: "agent:research-sweep", filed_at: "2026-09-26T15:20:00Z", links: [L("LAT-383", "human:atin", "2026-09-27T08:00:00Z"), L("LAT-375", "human:atin", "2026-09-27T08:01:00Z")] },
    { seq: 19, text: "Footer overlaps Complete button on small screens", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-26", filed_by: "agent:qa-browser", filed_at: "2026-09-26T16:01:00Z", links: [], closure: { kind: "duplicate", duplicate_of: 1, by: "human:atin", at: "2026-09-26T17:05:00Z" } },
    { seq: 20, text: "Dashboard feels slow", confidence: "possible", evidence: [], source: null, filed_by: "agent:research-sweep", filed_at: "2026-09-26T16:30:00Z", links: [], closure: { kind: "dismissed", reason: "No page, no measurement, no repro. Refile with a timing if it comes back.", by: "human:atin", at: "2026-09-26T17:06:00Z" } },
    { seq: 21, text: "Card titles clamp at three lines, so two tickets that differ only at the end of a long title look identical on the board. Seen with the two \"Flaky: test_\" tickets.", confidence: "definite", evidence: ["evidence/2026-09-27/clamp-identical.png"], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T09:14:22Z", links: [] },
    { seq: 22, text: "lattice list --tag with an unknown tag prints nothing and exits 0. Hard to tell a typo from an empty result.", confidence: "possible", evidence: [], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-27T09:40:05Z", links: [] },
    { seq: 23, text: "Warning: unknown event type 'process_started' ignored during snapshot materialization. Printed 12 times on every lattice stats in the Lattice repo itself. It pushes the real output below the fold and every agent has learned to ignore warnings because of it.", confidence: "definite", evidence: [], source: null, filed_by: "agent:claude-fable-orchestrator", filed_at: "2026-09-27T10:02:30Z", links: [] },
    { seq: 24, text: "Dragging a card to Done on the board skips the completion policy message; the card snaps back with no explanation", confidence: "definite", evidence: ["evidence/2026-09-27/drag-done-snapback.mov"], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T10:31:47Z", links: [] },
    { seq: 25, text: "In the light theme the needs-human flag is orange text on a pale orange chip. Contrast looks under 3:1.", confidence: "possible", evidence: ["evidence/2026-09-27/flag-contrast-linear.png"], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T10:36:02Z", links: [] },
    { seq: 26, text: "Help drawer's keyboard shortcut table lists g then b for Board, but pressing it does nothing in Safari 19", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-27", filed_by: "agent:qa-browser", filed_at: "2026-09-27T10:41:19Z", links: [] },
    { seq: 27, text: "Cube view nodes pile up in the centre on any board over roughly 200 tasks", confidence: "definite", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-27T11:15:00Z", links: [], closure: { kind: "duplicate", duplicate_of: 8, by: "human:atin", at: "2026-09-27T12:00:00Z" } },
    { seq: 28, text: "lattice comment with a double-quoted argument containing backticks ran the text between them as a command. I lost a clause from the comment and did not notice until the reviewer asked what it meant. The skill now says to use --file, but the CLI itself gives no warning.", confidence: "definite", evidence: [], source: null, filed_by: "agent:codex-reviewer", filed_at: "2026-09-27T13:50:28Z", links: [] },
    { seq: 29, text: "The dashboard tab title is always \"Lattice Dashboard\". With four boards open I cannot tell the tabs apart.", confidence: "definite", evidence: [], source: "cp1-walkthrough", filed_by: "human:atin", filed_at: "2026-09-27T15:22:10Z", links: [] },
    { seq: 30, text: "New Task dialog: Tab order goes title, priority, then jumps to Cancel before description", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T08:05:33Z", links: [] },
    { seq: 31, text: "Activity tab: the filter chips move left by a few pixels when the first result loads", confidence: "definite", evidence: ["evidence/2026-09-28/activity-chip-shift.mov"], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T08:09:12Z", links: [] },
    { seq: 32, text: "lattice next --claim picked a task whose depends_on target was still in_progress", confidence: "possible", evidence: [], source: null, filed_by: "agent:claude-opus-impl", filed_at: "2026-09-28T09:47:50Z", links: [] },
    { seq: 33, text: "Typo in the user guide: \"recieve\" in the section on hooks", confidence: "definite", evidence: ["docs/user-guide.md"], source: null, filed_by: "agent:research-sweep", filed_at: "2026-09-28T10:12:00Z", links: [], closure: { kind: "dismissed", reason: "Fixed directly in a5c1e0f, no story needed.", by: "human:atin", at: "2026-09-28T10:30:00Z" } },
    { seq: 34, text: "Search finds nothing for a short ID typed in lower case (lat-341)", confidence: "definite", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T11:26:40Z", links: [] },
    { seq: 35, text: "On a 27 inch display the board leaves about a third of the screen empty on the right while lanes still scroll horizontally", confidence: "possible", evidence: ["evidence/2026-09-28/board-wide.png"], source: null, filed_by: "human:atin", filed_at: "2026-09-28T14:03:18Z", links: [] },
    { seq: 36, text: "Archived tasks cannot be reached from a link in a comment; clicking LAT-100 in a comment shows \"Task not found\"", confidence: "definite", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T16:44:09Z", links: [] },
    { seq: 37, text: "The plan-review log is overwritten on each spawn, so after a rework cycle there is no way to read why the first review failed", confidence: "possible", evidence: [], source: null, filed_by: "agent:codex-reviewer", filed_at: "2026-09-28T19:30:27Z", links: [] },
    { seq: 38, text: "Settings drawer swatch needs two clicks", confidence: "possible", evidence: [], source: "qa-sweep-2026-09-28", filed_by: "agent:qa-browser", filed_at: "2026-09-28T20:02:00Z", links: [] },
    { seq: 39, text: "Two agents filed plans for the same ticket within a minute of each other; the second plan write replaced the first with no notice to either", confidence: "possible", evidence: [], source: null, filed_by: "agent:validator", filed_at: "2026-09-29T07:12:45Z", links: [] },
    { seq: 40, text: "Reaction picker opens under the detail panel footer on the last comment", confidence: "definite", evidence: ["evidence/2026-09-29/reaction-under-footer.png"], source: "qa-sweep-2026-09-29", filed_by: "agent:qa-browser", filed_at: "2026-09-29T08:21:03Z", links: [] },
    { seq: 41, text: "stats counts a task twice in Assigned when it was reassigned during the same second", confidence: "possible", evidence: [], source: null, filed_by: "agent:qa-cli", filed_at: "2026-09-29T08:40:55Z", links: [] }
  ];

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
    i.id = "LAT-I" + i.seq;
    i.closure = i.closure || null;
    if (i.closure && i.closure.duplicate_of) { i.closure.duplicate_of = "LAT-I" + i.closure.duplicate_of; }
  });

  window.MOCK = { now: NOW, project_code: "LAT", me: "human:atin", next_task_seq: 384, tasks: tasks, issues: issues };
})();
