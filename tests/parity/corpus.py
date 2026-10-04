"""The golden parity corpus: named scenarios over today's board-writing commands.

Each scenario runs on a fresh ``lattice init --project-code PAR`` board (auto-review
disabled in config), once plain and once with ``--json`` on every command that
has it (``record.py``). Together the scenarios exercise every board-writing
command of ``docs/hosted/SPEC.md`` §3.3 except the review runs of ``code-review``
/ ``plan-review`` (their rejections are covered, including a failed agent run
through ``tests/fixtures/fake_agent.py``), every rejection code of SPEC §3.1 the
CLI emits today (``REQUIRED_CODES``),
``status --force --reason``, ``--name`` session actors, the dashboard settings
POST, and board hooks (the sentinel scenario).

Step arguments may carry placeholders, resolved from the board just before the
step runs so the same step works in both modes:

- ``<<root>>``: the board's project root (a temp dir);
- ``<<task:PAR-1>>``: the task's ULID;
- ``<<event:PAR-1:comment_added:0>>``: the ID of the task's first event of that type;
- ``<<artifact:PAR-1:0>>``: the ID of the task's first attached artifact;
- ``<<python>>``, ``<<repo>>``, ``<<path>>``: the running interpreter, the
  repository root, and the caller's ``PATH`` (for agent shims).

Every scenario runs under a frozen clock (``record.FROZEN_NOW``), so output that
renders durations against the clock is deterministic without normalizing it.

Scenario names are the golden file stems (``golden/<name>.<plain|json>.json``).
Adding a step changes the golden; re-record with ``python -m tests.parity.record``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Cli:
    """One in-process CLI invocation. ``--json`` is appended in the JSON run."""

    args: tuple[str, ...]
    plain_only: bool = False  # the command has no --json; skipped in the JSON run
    env: dict[str, str] = field(default_factory=dict)
    stdin: str | None = None


@dataclass(frozen=True)
class WriteFile:
    """Write a file relative to the board root (fixtures, plans, hook scripts)."""

    path: str
    text: str
    executable: bool = False


@dataclass(frozen=True)
class Git:
    """Run ``git <args>`` in the board root, with fixed identity and dates (stable SHAs)."""

    args: tuple[str, ...]


@dataclass(frozen=True)
class DeleteFile:
    path: str


@dataclass(frozen=True)
class DashboardPost:
    """A POST through the in-process dashboard server, recorded as request/response."""

    path: str
    body: Any


Step = Cli | WriteFile | DeleteFile | Git | DashboardPost


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    steps: tuple[Step, ...]
    config: dict[str, Any] = field(default_factory=dict)


def c(*args: str, plain_only: bool = False, env: dict[str, str] | None = None) -> Cli:
    return Cli(tuple(args), plain_only=plain_only, env=env or {})


H = ("--actor", "human:parity")
A = ("--actor", "agent:worker")
B = ("--actor", "agent:other")

PLAN = "# Plan\n\n## Approach\n\n- Do the work in one careful pass.\n"


def plan(short: str) -> WriteFile:
    return WriteFile(f".lattice/plans/<<task:{short}>>.md", PLAN)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

LIFECYCLE = Scenario(
    name="lifecycle",
    description="create/update/edit-description/assign/status/archive and their rejections",
    steps=(
        c("create", "Alpha task", *H),
        c(
            "create",
            "Beta task",
            "--type",
            "bug",
            "--priority",
            "high",
            "--urgency",
            "immediate",
            "--complexity",
            "low",
            "--description",
            "Beta description",
            "--tags",
            "a,b",
            "--tag",
            "c",
            "--assigned-to",
            "agent:worker",
            "--reason",
            "provenance reason",
            "--on-behalf-of",
            "human:boss",
            "--triggered-by",
            "<<event:PAR-1:task_created:0>>",
            *H,
        ),
        c(
            "create",
            "Gamma task",
            "--status",
            "in_planning",
            "--model",
            "m-1",
            "--session",
            "s-1",
            *A,
        ),
        c("create", "Delta task", "--id", "task_01JAAAAAAAAAAAAAAAAAAAAAAA", *H),
        c("create", "Delta again", "--id", "task_01JAAAAAAAAAAAAAAAAAAAAAAA", *H),
        c("create", "Bad id", "--id", "bogus", *H),
        c("create", "Bad assignee", "--assigned-to", "nocolon", *H),
        c("create", "Bad type", "--type", "epic", *H),
        c("create", "Bad priority", "--priority", "urgent", *H),
        c("create", "Bad status", "--status", "nowhere", *H),
        c("create", "No actor"),
        c("create", "Bad actor", "--actor", "nocolon"),
        c("create", "Bad on-behalf", "--on-behalf-of", "nocolon", *H),
        c("create", "", *H),
        c(
            "update",
            "PAR-1",
            "title=Alpha renamed",
            "priority=low",
            "urgency=high",
            "type=chore",
            "complexity=high",
            "tags=x,y",
            *H,
        ),
        c("update", "PAR-1", "priority=low", *H),
        c("update", "PAR-1", "status=done", *H),
        c("update", "PAR-1", "nonsense=1", *H),
        c("update", "PAR-1", "noequals", *H),
        c("update", "PAR-1", "priority=urgent", *H),
        c("update", "PAR-1", *H),
        c("edit-description", "PAR-1", "A new description", *H),
        c("edit-description", "PAR-1", "A new description", *H),
        c("assign", "PAR-1", "agent:worker", *H),
        c("assign", "PAR-1", "agent:worker", *H),
        c("assign", "PAR-1", "nocolon", *H),
        c("assign", "NOPE-99", "agent:worker", *H),
        c("status", "PAR-1", "in_planning", *H),
        c("status", "PAR-1", "in_planning", *H),
        c("status", "PAR-1", "planned", "--no-auto-review", *H),
        c("status", "PAR-1", "in_progress", *H),
        plan("PAR-1"),
        c("status", "PAR-1", "in_progress", *A),
        c("status", "PAR-1", "done", *H),
        c("status", "PAR-1", "nowhere", *H),
        c("status", "PAR-1", "review", "--force", *H),
        c("status", "PAR-2", "in_progress", "--force", "--reason", "skip planning", *H),
        c("status", "PAR-2", "review", "--no-auto-review", *H),
        c("status", "PAR-2", "done", *H),
        c("status", "PAR-2", "done", "--force", "--reason", "ship without review", *H),
        c("status", "<<task:PAR-3>>", "planned", *A),
        c("status", "par-3", "cancelled", *A),
        c("status", "NOPE-99", "planned", *H),
        c("status", "not-an-id!", "planned", *H),
        c("status", "PAR-4", "blocked", *H),
        c("archive", "PAR-2", *H),
        c("archive", "PAR-2", *H),
        c("status", "PAR-2", "in_progress", *H),
        c("comment", "PAR-2", "on an archived task", *H),
        c("unarchive", "PAR-2", *H),
        c("unarchive", "PAR-2", *H),
        c("archive", "PAR-3", "PAR-4", *H),
        c("archive", "--stale", *H),
        c("archive", *H),
        c("unarchive", "PAR-3", "PAR-4", *H),
    ),
)

REVIEW_CYCLES = Scenario(
    name="review_cycles",
    description="the review-cycle safety valve and completion policy gates on status",
    config={"workflow": {"review_cycle_limit": 1}},
    steps=(
        c("create", "Cycling task", *H),
        c("status", "PAR-1", "in_progress", "--force", "--reason", "straight to work", *H),
        c("status", "PAR-1", "review", "--no-auto-review", *H),
        c("status", "PAR-1", "in_progress", *H),
        plan("PAR-1"),
        c("status", "PAR-1", "review", "--no-auto-review", *H),
        c("status", "PAR-1", "done", *H),
        c("status", "PAR-1", "in_progress", *H),
        c("status", "PAR-1", "review", "--no-auto-review", *H),
        c("status", "PAR-1", "in_progress", *H),
        c("status", "PAR-1", "in_planning", *H),
        c("status", "PAR-1", "in_progress", "--force", "--reason", "exceptional", *H),
        c("status", "PAR-1", "review", "--no-auto-review", *H),
        c("comment", "PAR-1", "Reviewed: fine.", "--role", "review", *H),
        c("status", "PAR-1", "in_validation", *H),
        c("status", "PAR-1", "pr_open", *H),
        c("comment", "PAR-1", "Saw it work.", "--role", "validation", *H),
        c("status", "PAR-1", "pr_open", *H),
        c("status", "PAR-1", "done", *H),
    ),
)

PLAN_INTEGRITY = Scenario(
    name="plan_integrity",
    description="plan-gate edge cases: missing plan, diverging active/archived plans",
    steps=(
        c("create", "No plan task", *H),
        c("status", "PAR-1", "in_planning", *H),
        c("status", "PAR-1", "planned", "--no-auto-review", *H),
        DeleteFile(".lattice/plans/<<task:PAR-1>>.md"),
        c("status", "PAR-1", "in_progress", *H),
        c("plan-review", "PAR-1", *H),
        plan("PAR-1"),
        WriteFile(".lattice/archive/plans/<<task:PAR-1>>.md", "# A different plan\n\nOther.\n"),
        c("status", "PAR-1", "in_progress", *H),
        DeleteFile(".lattice/archive/plans/<<task:PAR-1>>.md"),
        c("status", "PAR-1", "in_progress", *H),
    ),
)

PLAN_READ = Scenario(
    name="plan_read",
    description="the legacy plan read (lattice plan <task>), recorded before plan became a group",
    steps=(
        c("create", "Planned task", "--description", "Why it matters.", *H),
        c("plan", "PAR-1"),
        plan("PAR-1"),
        c("plan", "PAR-1"),
        c("plan", "<<task:PAR-1>>"),
        c("plan", "par-1"),
        c("plan", "--json", "PAR-1", plain_only=True),
        c("plan", "NOPE-1"),
        c("plan", "not-an-id!"),
        c("create", "Planless task", *H),
        DeleteFile(".lattice/plans/<<task:PAR-2>>.md"),
        c("plan", "PAR-2"),
        c("archive", "PAR-1", *H),
        c("plan", "PAR-1"),
        c("plan"),
    ),
)

PROSE_WRITES = Scenario(
    name="prose_writes",
    description="v2-only: plan write, notes write, context write, board write, and rejections",
    steps=(
        c("create", "Planned task", *H),
        WriteFile("plan-src.md", PLAN),
        c("plan", "write", "PAR-1", "--file", "<<root>>/plan-src.md", *A),
        c("plan", "write", "PAR-1", "--file", "<<root>>/plan-src.md", *A),
        c("plan", "PAR-1"),
        Cli(("plan", "write", "PAR-1", "--stdin", *A), stdin="# Plan v2\n\nFrom stdin.\n"),
        c(
            "plan",
            "write",
            "PAR-1",
            "--file",
            "<<root>>/plan-src.md",
            "--expect-sha256",
            "0" * 64,
            *A,
        ),
        c("plan", "write", "PAR-1", "--file", "<<root>>", *A),
        c("plan", "write", "PAR-1", "--file", "<<root>>/plan-src.md", "--stdin", *A),
        c("plan", "write", "PAR-1", *A),
        c("plan", "write", "NOPE-1", "--file", "<<root>>/plan-src.md", *A),
        c("plan", "write", "PAR-1", "--file", "<<root>>/plan-src.md"),
        Cli(("notes", "write", "PAR-1", "--stdin", *A), stdin="Scratch.\n"),
        c("archive", "PAR-1", *H),
        Cli(("notes", "write", "PAR-1", "--stdin", *A), stdin="After archive.\n"),
        Cli(("context", "write", "--stdin"), stdin="# Context\n\nWhy.\n"),
        c("context", "write", "--file", "<<root>>"),
        c("board", "write", "orchestration/run-state.md", "--file", "<<root>>/plan-src.md"),
        Cli(("board", "write", "plans/review-pack.md", "--stdin"), stdin="Pack.\n"),
        c("board", "write", "plans/<<task:PAR-1>>.md", "--file", "<<root>>/plan-src.md"),
        c("board", "write", "locks/x.lock", "--file", "<<root>>/plan-src.md"),
        c("board", "write", "../escape.md", "--file", "<<root>>/plan-src.md"),
    ),
)

COMMENTS = Scenario(
    name="comments",
    description="comment, threads, roles, comment-edit/delete, react/unreact",
    steps=(
        c("create", "Discussed task", *H),
        c("comment", "PAR-1", "First comment", *H),
        WriteFile("comment.md", "A comment from a file.\n\nWith `backticks` and $(dollars).\n"),
        c("comment", "PAR-1", "--file", "<<root>>/comment.md", *A),
        c(
            "comment",
            "PAR-1",
            "A reply",
            "--reply-to",
            "<<event:PAR-1:comment_added:0>>",
            *B,
        ),
        c("comment", "PAR-1", "Review findings", "--role", "review", *H),
        c("comment", "PAR-1", "Bad role", "--role", "bogus", *H),
        c("comment", "PAR-1", *H),
        c("comment", "PAR-1", "Both", "--file", "<<root>>/comment.md", *H),
        c("comment", "PAR-1", "--file", "<<root>>/missing.md", *H),
        c("comment", "PAR-1", "Orphan reply", "--reply-to", "ev_01JCCCCCCCCCCCCCCCCCCCCCCC", *H),
        c("comment", "NOPE-1", "Nowhere", *H),
        c(
            "comment",
            "PAR-1",
            "Provenance",
            "--reason",
            "why",
            "--on-behalf-of",
            "human:boss",
            *A,
        ),
        c("comment-edit", "PAR-1", "<<event:PAR-1:comment_added:0>>", "First, edited", *H),
        c("comment-edit", "PAR-1", "<<event:PAR-1:comment_added:0>>", "First, edited", *H),
        c(
            "comment-edit",
            "PAR-1",
            "<<event:PAR-1:comment_added:1>>",
            "--file",
            "<<root>>/comment.md",
            *H,
        ),
        c("comment-edit", "PAR-1", "<<event:PAR-1:comment_added:3>>", "--clear-role", *H),
        c(
            "comment-edit",
            "PAR-1",
            "<<event:PAR-1:comment_added:3>>",
            "Findings",
            "--clear-role",
            *H,
        ),
        c(
            "comment-edit",
            "PAR-1",
            "<<event:PAR-1:comment_added:0>>",
            "Seen",
            "--role",
            "validation",
            *H,
        ),
        c(
            "comment-edit",
            "PAR-1",
            "<<event:PAR-1:comment_added:0>>",
            "x",
            "--role",
            "v",
            "--clear-role",
            *H,
        ),
        c(
            "comment-edit",
            "PAR-1",
            "<<event:PAR-1:comment_added:0>>",
            "Seen",
            "--role",
            "bogus",
            *H,
        ),
        c("comment-edit", "PAR-1", "ev_01JCCCCCCCCCCCCCCCCCCCCCCC", "Missing", *H),
        c("comment-edit", "PAR-1", "<<event:PAR-1:comment_added:0>>", *H),
        c("react", "PAR-1", "<<event:PAR-1:comment_added:0>>", "thumbsup", *A),
        c("react", "PAR-1", "<<event:PAR-1:comment_added:0>>", "thumbsup", *A),
        c("react", "PAR-1", "<<event:PAR-1:comment_added:0>>", "rocket", *B),
        c("react", "PAR-1", "ev_01JCCCCCCCCCCCCCCCCCCCCCCC", "rocket", *B),
        c("unreact", "PAR-1", "<<event:PAR-1:comment_added:0>>", "thumbsup", *A),
        c("unreact", "PAR-1", "<<event:PAR-1:comment_added:0>>", "thumbsup", *A),
        c("comment-delete", "PAR-1", "<<event:PAR-1:comment_added:2>>", *B),
        c("comment-delete", "PAR-1", "<<event:PAR-1:comment_added:2>>", *B),
        c("comment-edit", "PAR-1", "<<event:PAR-1:comment_added:2>>", "Revive", *B),
        c("react", "PAR-1", "<<event:PAR-1:comment_added:2>>", "rocket", *B),
        c("comment-delete", "PAR-1", "ev_01JCCCCCCCCCCCCCCCCCCCCCCC", *B),
    ),
)

FLAGS = Scenario(
    name="flags",
    description="needs-human set/clear, --file reasons, and the flag conflicts",
    steps=(
        c("create", "Flagged task", *H),
        c("needs-human", "PAR-1", "Need a call on the schema", *A),
        c("needs-human", "PAR-1", "Asking again", *A),
        c("needs-human", "PAR-1", "--clear", "--note", "Decided: dual-write", *H),
        c("needs-human", "PAR-1", "--clear", *H),
        WriteFile("reason.md", "A long reason\n\nwith `backticks`.\n"),
        c("needs-human", "PAR-1", "--file", "<<root>>/reason.md", *A),
        c("needs-human", "PAR-1", "--clear", *H),
        c("needs-human", "PAR-1", *A),
        c("needs-human", "PAR-1", "Both", "--file", "<<root>>/reason.md", *A),
        c("needs-human", "PAR-1", "Clear with reason", "--clear", *A),
        c("needs-human", "NOPE-3", "Nowhere", *A),
    ),
)

LINKS = Scenario(
    name="links",
    description="link/unlink, branch-link/unlink, file-link/unlink",
    steps=(
        c("create", "Source", *H),
        c("create", "Target", *H),
        c("create", "Parent", *H),
        c("link", "PAR-1", "blocks", "PAR-2", "--note", "because", *H),
        c("link", "PAR-1", "blocks", "PAR-2", *H),
        c("link", "PAR-1", "subtask_of", "PAR-3", *H),
        c("link", "PAR-2", "depends_on", "<<task:PAR-3>>", *H),
        c("link", "PAR-1", "related_to", "PAR-1", *H),
        c("link", "PAR-1", "bogus", "PAR-2", *H),
        c("link", "PAR-1", "blocks", "NOPE-9", *H),
        c("unlink", "PAR-1", "blocks", "PAR-2", *H),
        c("unlink", "PAR-1", "blocks", "PAR-2", *H),
        c("branch-link", "PAR-1", "feat/PAR-1-thing", "--repo", "origin", *H),
        c("branch-link", "PAR-1", "feat/PAR-1-thing", "--repo", "origin", *H),
        c("branch-link", "PAR-1", "feat/other", *H),
        c("branch-unlink", "PAR-1", "feat/PAR-1-thing", "--repo", "origin", *H),
        c("branch-unlink", "PAR-1", "feat/PAR-1-thing", "--repo", "origin", *H),
        c("file-link", "PAR-1", "src/a.py", "src/b.py", "--reason", "decision", *H),
        c("file-link", "PAR-1", "src/a.py", *H),
        c("file-unlink", "PAR-1", "src/a.py", *H),
        c("file-unlink", "PAR-1", "src/a.py", *H),
        c("file-link", "PAR-1", "../outside.py", *H),
    ),
)

CRITERIA = Scenario(
    name="criteria",
    description="criterion add/edit/retire and criterion-linked evidence",
    steps=(
        c("create", "Criteria task", *H),
        c("criterion", "add", "PAR-1", "Outcome one holds", "--id", "c-one", *H),
        WriteFile("criterion.md", "Outcome two, from a file.\n"),
        c("criterion", "add", "PAR-1", "--file", "<<root>>/criterion.md", *H),
        c("criterion", "add", "PAR-1", "Duplicate id", "--id", "c-one", *H),
        c("criterion", "add", "PAR-1", "Bad id", "--id", "Bad Id!", *H),
        c("criterion", "add", "PAR-1", *H),
        c("criterion", "edit", "PAR-1", "c-one", "Outcome one holds, edited", *H),
        c("criterion", "edit", "PAR-1", "c-one", "Outcome one holds, edited", *H),
        c("criterion", "edit", "PAR-1", "c-missing", "Nope", *H),
        c("comment", "PAR-1", "Observed outcome one.", "--criterion", "c-one", *H),
        c("comment", "PAR-1", "Unknown criterion.", "--criterion", "c-missing", *H),
        c("criterion", "retire", "PAR-1", "c-one", "--reason", "superseded", *H),
        c("criterion", "retire", "PAR-1", "c-one", *H),
        c("criterion", "edit", "PAR-1", "c-one", "Edit a retired one", *H),
        c("comment", "PAR-1", "Evidence for retired.", "--criterion", "c-one", *H),
    ),
)

ARTIFACTS = Scenario(
    name="artifacts",
    description="attach (file, inline, url), complete, custom events",
    steps=(
        c("create", "Artifact task", *H),
        WriteFile("report.md", "# Report\n\nAll good.\n"),
        c(
            "attach",
            "PAR-1",
            "<<root>>/report.md",
            "--title",
            "Report",
            "--summary",
            "A summary",
            "--type",
            "file",
            "--role",
            "review",
            *H,
        ),
        c("attach", "PAR-1", "--inline", "Inline evidence text", "--title", "Inline", *A),
        c(
            "attach",
            "PAR-1",
            "https://example.com/build/1",
            "--title",
            "Build",
            "--role",
            "validation",
            "--sensitive",
            *A,
        ),
        c(
            "attach",
            "PAR-1",
            "--inline",
            "Caller id",
            "--id",
            "art_01JBBBBBBBBBBBBBBBBBBBBBBB",
            *A,
        ),
        c(
            "attach",
            "PAR-1",
            "--inline",
            "Caller id again",
            "--id",
            "art_01JBBBBBBBBBBBBBBBBBBBBBBB",
            *A,
        ),
        # A JSONL payload whose lines carry their own origin: user data, kept whole.
        WriteFile(
            "trace.jsonl",
            '{"origin": "keep", "ts": "note", "type": "user"}\n'
            '{"origin": {"host": "user-host"}, "ts": "2026-01-01T00:00:00Z", "type": "x"}\n',
        ),
        c("attach", "PAR-1", "<<root>>/trace.jsonl", "--title", "Trace", *A),
        c("attach", "PAR-1", "--inline", "x", "--id", "nonsense", *A),
        c("attach", "PAR-1", "--inline", "x", "--role", "bogus", *A),
        c("attach", "PAR-1", "<<root>>/missing.md", *A),
        c("attach", "PAR-1", *A),
        c("criterion", "add", "PAR-1", "Report exists", "--id", "report", *H),
        c("attach", "PAR-1", "--inline", "evidence", "--criterion", "report", *A),
        c("event", "PAR-1", "x_deploy", "--data", '{"env": "staging", "n": 1}', *A),
        c(
            "event",
            "PAR-1",
            "x_payload",
            "--data",
            '{"type": "user", "ts": "2026-01-01T00:00:00Z", "origin": "user-data"}',
            *A,
        ),
        c("event", "PAR-1", "x_deploy", "--id", "ev_01JCCCCCCCCCCCCCCCCCCCCCCC", *A),
        c("event", "PAR-1", "x_deploy", "--id", "ev_01JCCCCCCCCCCCCCCCCCCCCCCC", *A),
        c("event", "PAR-1", "status_changed", *A),
        c("event", "PAR-1", "x_bad", "--data", "not json", *A),
        c("event", "PAR-1", "x_bad", "--data", "[1, 2]", *A),
        c("create", "Complete me", *H),
        c("status", "PAR-2", "in_progress", "--force", "--reason", "straight to work", *H),
        c("complete", "PAR-2", "--review", "Reviewed: all good.", *A),
        c("complete", "PAR-2", "--review", "Again.", *A),
        c("create", "Complete from file", *H),
        c("status", "PAR-3", "in_progress", "--force", "--reason", "straight to work", *H),
        WriteFile("review.md", "Multi-paragraph review.\n\nSecond paragraph with `code`.\n"),
        c("complete", "PAR-3", "--review-file", "<<root>>/review.md", *A),
        c("create", "Complete without review", *H),
        c("complete", "PAR-4", *A),
        c("complete", "PAR-4", "--review", "x", "--review-file", "<<root>>/review.md", *A),
        c("complete", "PAR-4", "--review", "From backlog.", *A),
        c("complete", "NOPE-4", "--review", "Nowhere.", *A),
    ),
)

COMPLETION_GIT_POLICY = Scenario(
    name="completion_git_policy",
    description="require_reachable_review_commit refuses completion outside a git worktree",
    config={
        "workflow": {
            "completion_policies": {
                "done": {"require_roles": ["review"], "require_reachable_review_commit": True}
            }
        }
    },
    steps=(
        c("create", "Git gated", *H),
        c("status", "PAR-1", "in_progress", "--force", "--reason", "straight to work", *H),
        c("complete", "PAR-1", "--review", "Reviewed.", *A),
        c("status", "PAR-1", "review", "--no-auto-review", *H),
        c("comment", "PAR-1", "Reviewed.", "--role", "review", *H),
        c("status", "PAR-1", "done", *H),
    ),
)

CLAIMS = Scenario(
    name="claims",
    description="claim/unclaim (c11 surface binding, c11 absent) and next --claim",
    steps=(
        c("create", "High one", "--priority", "high", *H),
        c("create", "Medium one", *H),
        c("create", "Planned one", "--status", "planned", *H),
        c("claim", "PAR-1", "--surface", "surface:7", *A),
        c("claim", "PAR-1", "--surface", "surface:8", *A),
        c("claim", "PAR-2", *A),
        c("unclaim", "PAR-1", *A),
        c("unclaim", "PAR-1", *A),
        c("next", *A),
        c("next", "--claim"),
        c("next", "--claim", *A),
        plan("PAR-1"),
        c("next", "--claim", *A),
        c("next", "--claim", *A),
        c("next", "--status", "in_progress", *B),
        plan("PAR-2"),
        c("next", "--claim", *B),
        c("next", "--status", "backlog", *B),
        c("create", "Assigned elsewhere", "--assigned-to", "agent:third", *H),
        plan("PAR-4"),
        c("next", "--status", "backlog", "--claim", "--actor", "agent:third"),
        c("create", "Orphan in progress", *H),
        c("status", "PAR-5", "in_progress", "--force", "--reason", "nobody owns it", *H),
        plan("PAR-5"),
        # An unassigned in-progress task still refuses a claim.
        c("assign", "PAR-5", "none", *H),
        c("next", "--status", "in_progress", "--claim", "--actor", "agent:fourth"),
        c("assign", "PAR-5", "agent:fourth", *H),
        c("next", "--status", "in_progress", "--claim", "--actor", "agent:fourth"),
        c("next", "--status", "nowhere", *B),
        c("next", "--claim", "--name", "Ghost"),
        # A repeat in-progress claim stays an idempotent resume; the final
        # planned/scaffold claim records the explicit PLAN_REQUIRED refusal.
        c("next", "--status", "in_progress", "--claim", "--actor", "agent:fourth"),
        c("next", "--status", "planned", "--claim", "--actor", "agent:fifth"),
    ),
)

RESOURCES = Scenario(
    name="resources",
    description="resource create/acquire/heartbeat/release, holders, eviction, expiry",
    config={"resources": {"stale-lock": {"ttl_seconds": -3600, "description": "always stale"}}},
    steps=(
        c("create", "Resource user", *H),
        c(
            "resource",
            "create",
            "lock-a",
            "--description",
            "Shared lock",
            "--max-holders",
            "2",
            "--ttl",
            "600",
            *H,
        ),
        c("resource", "create", "lock-a", *H),
        c("resource", "create", "lock-z", "--ttl", "0", *H),
        c("resource", "create", "lock-y", "--max-holders", "0", *H),
        c("resource", "create", "lock-x", "--id", "res_01JDDDDDDDDDDDDDDDDDDDDDDD", *H),
        c("resource", "create", "lock-w", "--id", "bogus", *H),
        c("resource", "acquire", "lock-a", "--task", "PAR-1", "--reason", "deploying", *A),
        c("resource", "acquire", "lock-a", *B),
        c("resource", "acquire", "lock-a", "--actor", "agent:third"),
        c("resource", "acquire", "lock-a", *A),
        c("resource", "heartbeat", "lock-a", *A),
        c("resource", "heartbeat", "lock-a", "--actor", "agent:third"),
        c("resource", "release", "lock-a", *B),
        c("resource", "release", "lock-a", *B),
        c("resource", "acquire", "lock-a", "--force", "--actor", "agent:third"),
        c("resource", "acquire", "auto-made", *A),
        c("resource", "acquire", "auto-made", "--wait", "--timeout", "1", *A),
        c("resource", "create", "lock-b", *H),
        c("resource", "acquire", "lock-b", "--wait", "--timeout", "1", *A),
        c("resource", "acquire", "lock-b", "--wait", "--timeout", "0", *B),
        c("resource", "acquire", "stale-lock", *A),
        c("resource", "heartbeat", "stale-lock", *A),
        c("resource", "acquire", "stale-lock", *B),
        c("resource", "heartbeat", "nowhere", *A),
        c("resource", "release", "nowhere", *A),
        c("resource", "acquire", "lock-a", "--task", "NOPE-1", *B),
    ),
)

SESSIONS = Scenario(
    name="sessions",
    description="session start/end and --name session actors on writing commands",
    steps=(
        c(
            "session",
            "start",
            "--name",
            "Argus",
            "--model",
            "claude-opus-5-5",
            "--framework",
            "claude-code",
            "--agent-type",
            "advance",
            "--prompt",
            "lattice",
            "--parent",
            "human:atin",
        ),
        c(
            "session",
            "start",
            "--name",
            "Argus",
            "--model",
            "claude-opus-5-5",
            "--framework",
            "codex-cli",
        ),
        c("session", "start", "--name", "Human", "--model", "human"),
        c("session", "start", "--name", "NoFramework", "--model", "claude-opus-5-5"),
        c("session", "start", "--name", "Bad/Name", "--model", "human"),
        c("create", "Session task", "--name", "Argus-1"),
        c("comment", "PAR-1", "From a session", "--name", "Argus-1"),
        c(
            "status",
            "PAR-1",
            "in_progress",
            "--force",
            "--reason",
            "session work",
            "--name",
            "Argus-1",
        ),
        c("needs-human", "PAR-1", "Session asks", "--name", "Argus-1"),
        c("assign", "PAR-1", "human:atin", "--name", "Human-1"),
        c("complete", "PAR-1", "--review", "Session review.", "--name", "Argus-1"),
        c("create", "Ghost task", "--name", "Ghost"),
        c("create", "Both", "--name", "Argus-1", "--actor", "human:parity"),
        c("resource", "create", "lock-s", "--name", "Argus-1"),
        c("resource", "acquire", "lock-s", "--name", "Argus-1"),
        c("resource", "release", "lock-s", "--name", "Argus-1"),
        c("session", "end", "Argus-1", "--reason", "done for the day"),
        c("session", "end", "Argus-1"),
        c("create", "Base name only", "--name", "Argus"),
        c("create", "Second session", "--name", "Argus-2"),
        c("create", "After end", "--name", "Argus-1"),
        c("session", "end", "Ghost"),
    ),
)

PROJECT_CODES = Scenario(
    name="project_codes",
    description="set-project-code and set-subproject-code (no --json: plain run only)",
    steps=(
        c("create", "Before rename", *H),
        c("set-project-code", "NEW", plain_only=True),
        c("set-project-code", "NEW", "--force", plain_only=True),
        c("set-project-code", "bad code!", "--force", plain_only=True),
        c("create", "After rename", *H),
        c("set-subproject-code", "SUB", plain_only=True),
        c("set-subproject-code", "SUB", plain_only=True),
        c("set-subproject-code", "SUB2", "--force", plain_only=True),
        c("create", "After subproject", *H),
        c("status", "PAR-1", "in_planning", *H),
    ),
)

REJECTIONS = Scenario(
    name="rejections",
    description="rejection codes not reached elsewhere: uninitialized root, wait, surfaces",
    steps=(
        WriteFile("empty/.keep", ""),
        c("create", "Nowhere", *H, env={"LATTICE_ROOT": "<<root>>/empty"}),
        c("status", "PAR-1", "planned", *H, env={"LATTICE_ROOT": "<<root>>/empty"}),
        c("create", "Waited on", *H),
        c("wait", ",", "--status", "done"),
        c("wait", "NOPE-1", "--status", "done"),
        c("claim", "PAR-1", *H),
        c("status", "PAR-1", "in_planning", "--actor", "agent:"),
        c("status", "PAR-1", "in_planning", "--on-behalf-of", "bad", *H),
        c("status", "PAR-1", "in_planning", "--triggered-by", "not-an-event", *H),
        c("code-review", "PAR-1", *H),
        c("code-review", "PAR-1", "--worktree", "<<root>>/empty", *H),
    ),
)

HOOK_SCRIPT = """#!/bin/sh
# Parity sentinel: record argv, a fixed set of env vars, then the event on stdin.
printf 'HOOK %s | type=%s task=%s event=%s actor=%s root=%s dir=%s from=%s to=%s res=%s/%s\\n' \\
  "$*" "$LATTICE_EVENT_TYPE" "${LATTICE_TASK_ID:-}" "$LATTICE_EVENT_ID" "$LATTICE_ACTOR" \\
  "$LATTICE_ROOT" "$LATTICE_DIR" "${LATTICE_FROM_STATUS:-}" "${LATTICE_TO_STATUS:-}" \\
  "${LATTICE_RESOURCE_ID:-}" "${LATTICE_RESOURCE_NAME:-}" >> "$LATTICE_ROOT/hook-sentinel.log"
printf 'STDIN %s\\n' "$(cat)" >> "$LATTICE_ROOT/hook-sentinel.log"
"""


def _hook(*argv: str) -> str:
    return "sh <<root>>/hook.sh " + " ".join(argv)


HOOKS = Scenario(
    name="hooks_sentinel",
    description="post_event, on.<type>, and transitions hooks fire in a fixed set and order",
    config={
        "hooks": {
            "post_event": _hook("post_event"),
            "on": {
                "comment_added": _hook("on", "comment_added"),
                "status_changed": _hook("on", "status_changed"),
                "resource_acquired": _hook("on", "resource_acquired"),
            },
            "transitions": {
                "backlog -> in_planning": _hook("transition", "exact"),
                "* -> planned": _hook("transition", "any-to-planned"),
                "planned -> *": _hook("transition", "planned-to-any"),
                "* -> *": _hook("transition", "any"),
            },
        }
    },
    steps=(
        WriteFile("hook.sh", HOOK_SCRIPT),
        c("create", "Hooked task", *H),
        c("status", "PAR-1", "in_planning", *H),
        c("status", "PAR-1", "planned", "--no-auto-review", *A),
        c("comment", "PAR-1", "Hooked comment", *A),
        c("needs-human", "PAR-1", "Hooked flag", *A),
        c("status", "PAR-1", "in_progress", "--force", "--reason", "hooked force", *H),
        c("complete", "PAR-1", "--review", "Hooked review.", *A),
        c("resource", "create", "hooked-lock", *H),
        c("resource", "acquire", "hooked-lock", *A),
        c("resource", "release", "hooked-lock", *A),
        c("status", "PAR-1", "blocked", *H),
    ),
)

REVIEW_STATE_HELD = (
    '{"agents": [], "auto_fired": false, "mode": "single", "review_type": "plan-review", '
    '"started_at": "2026-06-01T11:59:00Z", "started_by_pid": 1, "task_id": "<<task:PAR-1>>"}\n'
)

# A stand-in `claude` first on PATH: the existing fake agent, told to fail.
FAILING_AGENT = """#!/bin/sh
LATTICE_FAKE_BEHAVIOR=fail LATTICE_AGENT_OUTPUT=/dev/null exec "<<python>>" \\
  "<<repo>>/tests/fixtures/fake_agent.py"
"""

REVIEWS = Scenario(
    name="reviews",
    description="code-review / plan-review rejections: in-flight, failed agent, empty diff",
    steps=(
        c("create", "Reviewed task", *H),
        plan("PAR-1"),
        # pid 1 is always alive, so the recorded holder is a live other process.
        WriteFile(".lattice/review_state/<<task:PAR-1>>.json", REVIEW_STATE_HELD),
        c("plan-review", "PAR-1", "--mode", "single", *A),
        c("plan-review", "PAR-1", "--mode", "inline", *A),
        c("code-review", "PAR-1", "--mode", "inline", *A),
        DeleteFile(".lattice/review_state/<<task:PAR-1>>.json"),
        WriteFile("bin/claude", FAILING_AGENT, executable=True),
        c(
            "plan-review",
            "PAR-1",
            "--mode",
            "single",
            *A,
            env={"PATH": "<<root>>/bin:<<path>>"},
        ),
        Git(("init", "-q", "-b", "main")),
        WriteFile("src.txt", "base\n"),
        Git(("add", "src.txt")),
        Git(("commit", "-q", "-m", "base")),
        c("code-review", "PAR-1", "--base", "main", "--head", "main", *A),
        c("code-review", "PAR-1", "--head", "no-such-branch", *A),
        c("code-review", "PAR-1", "--base", "no-such-base", "--head", "main", *A),
    ),
)

DASHBOARD = Scenario(
    name="dashboard_settings",
    description="the dashboard settings POST through the in-process dashboard server",
    steps=(
        DashboardPost(
            "/api/config/dashboard",
            {
                "theme": "dark",
                "font_size": 14,
                "lane_colors": {"backlog": "#123456"},
                "lane_sort": {"backlog": "priority"},
                "done_display": "recent",
                "voice": "calm",
                "column_width": 320,
                "day_start_hour": 6,
                "heat_map_enabled": True,
                "max_items_per_column": 25,
            },
        ),
        DashboardPost("/api/config/dashboard", {"voice": True}),
        DashboardPost("/api/config/dashboard", {"theme": "light", "background_image": None}),
        DashboardPost("/api/config/dashboard", {"unknown_key": 1}),
        DashboardPost("/api/config/dashboard", {"lane_colors": ["not", "an", "object"]}),
        DashboardPost("/api/config/dashboard", {"lane_sort": {"backlog": 3}}),
        DashboardPost("/api/config/dashboard", {"theme": 7}),
        DashboardPost("/api/config/dashboard", {"background_image": 7}),
        DashboardPost("/api/config/dashboard", "not json"),
    ),
)

MAINTENANCE = Scenario(
    name="maintenance",
    description="local-only maintenance commands: rebuild, doctor, backfill-ids, migrate",
    steps=(
        c("create", "Maintained one", *H),
        c("create", "Maintained two", *H),
        c("comment", "PAR-1", "Some history", *H),
        c("rebuild", "PAR-1"),
        c("rebuild", "<<task:PAR-1>>"),
        c("rebuild", "--all"),
        c("rebuild"),
        c("doctor"),
        c("doctor", "--fix"),
        c("backfill-ids"),
        c("migrate", "needs-human", "--dry-run"),
        c("migrate", "needs-human", *H),
        WriteFile(".lattice/events/<<task:PAR-2>>.jsonl", "{not json\n"),
        c("rebuild", "<<task:PAR-2>>"),
        c("rebuild", "--all"),
    ),
)

# v2-only (LAT-303): recorded by the ticket that added erase and unerase.
TOMBSTONES = Scenario(
    name="tombstones",
    description="erase and unerase: hidden views, show, TASK_ERASED, restore, doctor",
    steps=(
        c("create", "Erase me", *H),
        c("create", "Keep me", *H),
        c("status", "PAR-1", "in_planning", *H),
        c("erase", "PAR-1", *H),
        c("erase", "PAR-1", "--reason", "Filed twice", *H),
        c("list"),
        c("list", "--include-tombstoned"),
        c("next"),
        # Compact only: the full view's event lines carry this machine's origin.
        c("show", "PAR-1", "--compact"),
        c("comment", "PAR-1", "Still here?", *A),
        c("status", "PAR-1", "planned", *H),
        c("assign", "PAR-1", "agent:worker", *H),
        c("erase", "PAR-1", "--reason", "Again", *H),
        c("doctor"),
        c("unerase", "PAR-2", "--reason", "Not erased", *H),
        c("unerase", "PAR-1", "--reason", "Not a duplicate after all", *H),
        c("list"),
        c("show", "PAR-1", "--compact"),
        c("comment", "PAR-1", "Back again", *A),
        DeleteFile(".lattice/events/<<task:PAR-2>>.jsonl"),
        c("doctor"),
    ),
)

ISSUES = Scenario(
    name="issues",
    description="hosted and local issue filing, editing, closing, linking and fallback help",
    config={"issues": {"enabled": True}},
    steps=(
        c("create", "Issue-linked task", *H),
        c("issue", "file", "Observed a race", *H),
        c("issue", "link", "PAR-I1", "PAR-1", *H),
        c("issue", "show", "PAR-I1"),
        c("issue", "unlink", "PAR-I1", "PAR-1", *H),
        c("issue", "file", "A second observation", "--description", "More detail.", *H),
        c("issue", "edit", "PAR-I2", "--title", "A corrected observation", *H),
        c("issue", "comment", "PAR-I2", "Seen again on the nightly run", *H),
        c("issue", "dismiss", "PAR-I2", "--reason", "noise", *H),
        c("issue", "reopen", "PAR-I2", *H),
        c("issue", "file", "A repeat of the first", *H),
        c("issue", "duplicate", "PAR-I3", "--of", "PAR-I1", *H),
        c("issue", "promote", "PAR-I2", "--title", "Fix the observation", *H),
        c("issue", "show", "PAR-I2"),
        c("issue", "list", "--all"),
        c("issue", "file", "--help", plain_only=True),
        c("issue", "attach", "--help", plain_only=True),
        c("server", "project", "import", "--help", plain_only=True),
    ),
)


SCENARIOS: tuple[Scenario, ...] = (
    LIFECYCLE,
    REVIEW_CYCLES,
    PLAN_INTEGRITY,
    PLAN_READ,
    PROSE_WRITES,
    COMMENTS,
    FLAGS,
    LINKS,
    CRITERIA,
    ARTIFACTS,
    COMPLETION_GIT_POLICY,
    CLAIMS,
    RESOURCES,
    SESSIONS,
    PROJECT_CODES,
    REJECTIONS,
    HOOKS,
    REVIEWS,
    DASHBOARD,
    MAINTENANCE,
    TOMBSTONES,
    ISSUES,
)

# Every rejection code of SPEC §3.1 that the CLI emits today. test_local_parity
# asserts each appears in the goldens. One is left out because no CLI input can
# reach it deterministically:
# - HEAD_SHA_UNKNOWN: the head ref is verified with the same rev-parse before its
#   SHA is read, so only a ref moving mid-command reaches it. Covered by
#   TestFailedReviewIsVisible::test_unknown_head_sha_fails_before_writing_an_artifact
#   in tests/test_cli/test_review_cmds.py (patched resolve_diff).
REQUIRED_CODES = frozenset(
    {
        "TIMEOUT",
        "REBUILD_ERROR",
        "REVIEW_IN_FLIGHT",
        "REVIEW_FAILED",
        "DIFF_RESOLUTION_FAILED",
        "EMPTY_DIFF",
        "VALIDATION_ERROR",
        "MISSING_ARGS",
        "INVALID_ID",
        "INVALID_ROLE",
        "INVALID_ACTOR",
        "MISSING_ACTOR",
        "NOT_FOUND",
        "NOT_INITIALIZED",
        "PLAN_NOT_FOUND",
        "SESSION_NOT_FOUND",
        "CONFLICT",
        "ALREADY_CLAIMED",
        "RESOURCE_HELD",
        "NOT_HELD",
        "EXPIRED",
        "FLAG_ALREADY_SET",
        "FLAG_NOT_SET",
        "INVALID_TRANSITION",
        "PLAN_REQUIRED",
        "COMPLETION_BLOCKED",
        "REVIEW_CYCLE_LIMIT",
        "INTEGRITY_ERROR",
        "MISSING_SURFACE",
        "TASK_ERASED",
    }
)

# Board-writing commands of SPEC §3.3 (command paths), each of which must appear.
REQUIRED_COMMANDS = frozenset(
    {
        "create",
        "update",
        "edit-description",
        "status",
        "assign",
        "needs-human",
        "comment",
        "comment-edit",
        "comment-delete",
        "react",
        "unreact",
        "complete",
        "link",
        "unlink",
        "branch-link",
        "branch-unlink",
        "file-link",
        "file-unlink",
        "criterion add",
        "criterion edit",
        "criterion retire",
        "archive",
        "unarchive",
        "claim",
        "unclaim",
        "next",
        "attach",
        "event",
        "set-project-code",
        "set-subproject-code",
        "resource create",
        "resource acquire",
        "resource release",
        "resource heartbeat",
        "session start",
        "session end",
        "erase",
        "unerase",
        "plan write",
        "notes write",
        "context write",
        "board write",
        "issue file",
        "issue link",
        "issue unlink",
        "issue edit",
        "issue comment",
        "issue dismiss",
        "issue reopen",
        "issue duplicate",
        "issue promote",
    }
)
