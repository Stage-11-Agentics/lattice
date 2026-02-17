"""Demo project seeder: lattice demo init — The Lighthouse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event
from lattice.core.ids import generate_instance_id, generate_task_id
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.operations import scaffold_plan, write_task_event
from lattice.storage.short_ids import _default_index, allocate_short_id, save_id_index


# ---------------------------------------------------------------------------
# Timeline: the first week of building the lighthouse
# ---------------------------------------------------------------------------


def _lighthouse_timeline() -> dict[str, str]:
    """Generate realistic timestamps across a week of building.

    Returns a dict of named moments → ISO timestamps.
    """
    # Start from "last Monday at 9am UTC"
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()
    if days_since_monday == 0 and now.hour < 9:
        days_since_monday = 7
    monday = now - timedelta(days=days_since_monday)
    monday_9am = monday.replace(hour=9, minute=0, second=0, microsecond=0)

    def ts(hours_offset: float, minutes: int = 0) -> str:
        t = monday_9am + timedelta(hours=hours_offset, minutes=minutes)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        # Monday — The Foundation
        "mon_9am": ts(0),
        "mon_10am": ts(1),
        "mon_11am": ts(2),
        "mon_noon": ts(3),
        "mon_2pm": ts(5),
        "mon_3pm": ts(6),
        "mon_4pm": ts(7),
        "mon_5pm": ts(8),
        # Tuesday — Foundation completes, Lens begins
        "tue_9am": ts(24),
        "tue_10am": ts(25),
        "tue_11am": ts(26),
        "tue_noon": ts(27),
        "tue_2pm": ts(29),
        "tue_3pm": ts(30),
        "tue_4pm": ts(31),
        # Wednesday — Lens in full swing
        "wed_9am": ts(48),
        "wed_10am": ts(49),
        "wed_11am": ts(50),
        "wed_noon": ts(51),
        "wed_2pm": ts(53),
        "wed_3pm": ts(54),
        "wed_5pm": ts(56),
        # Thursday — Signal work begins
        "thu_9am": ts(72),
        "thu_10am": ts(73),
        "thu_11am": ts(74),
        "thu_noon": ts(75),
        "thu_2pm": ts(77),
        "thu_3pm": ts(78),
        "thu_5pm": ts(80),
        # Friday — Signal continues, Keeper's Log begins
        "fri_9am": ts(96),
        "fri_10am": ts(97),
        "fri_11am": ts(98),
        "fri_noon": ts(99),
        "fri_2pm": ts(101),
        "fri_3pm": ts(102),
        "fri_5pm": ts(104),
        # Saturday — Polish, bugs, loose ends
        "sat_10am": ts(121),
        "sat_noon": ts(123),
        "sat_2pm": ts(125),
        "sat_4pm": ts(127),
        # Sunday — Reflection
        "sun_10am": ts(145),
        "sun_noon": ts(147),
    }


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def _task_definitions(ts: dict[str, str]) -> list[dict]:
    """Return the full set of Lighthouse demo tasks.

    Each dict contains: title, type, priority, status, assigned_to,
    description, tags, events (additional events beyond creation),
    branch, comments, plan_content.
    """
    tasks = [
        # -----------------------------------------------------------------
        # EPICS (0-3)
        # -----------------------------------------------------------------
        {
            "title": "The Foundation",
            "type": "epic",
            "priority": "high",
            "status": "backlog",
            "ts": ts["mon_9am"],
            "description": (
                "The bedrock. Before the lighthouse can see, before it can "
                "speak, it must have ground to stand on. Infrastructure, "
                "storage, networking — the quiet bones beneath everything."
            ),
            "tags": ["infrastructure"],
        },
        {
            "title": "The Lens",
            "type": "epic",
            "priority": "critical",
            "status": "backlog",
            "ts": ts["mon_9am"],
            "description": (
                "The eye of the lighthouse. Data collection, metric ingestion, "
                "health monitoring — everything that transforms "
                "'I wonder what is happening' into 'I know.'"
            ),
            "tags": ["monitoring", "data"],
        },
        {
            "title": "The Signal",
            "type": "epic",
            "priority": "high",
            "status": "backlog",
            "ts": ts["mon_9am"],
            "description": (
                "The voice of the lighthouse. Alerting, escalation, "
                "notification — the system's ability to say "
                "'something is wrong' to the right person at the right "
                "time in the right way."
            ),
            "tags": ["alerting"],
        },
        {
            "title": "The Keeper's Log",
            "type": "epic",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["mon_9am"],
            "description": (
                "The memory of the lighthouse. Dashboards, visualizations, "
                "incident records — because watching is nothing without "
                "remembering, and remembering is nothing without "
                "understanding."
            ),
            "tags": ["visualization", "history"],
        },
        # -----------------------------------------------------------------
        # FOUNDATION tasks (4-7) — all done
        # -----------------------------------------------------------------
        {
            "title": "Lay the first stone",
            "type": "task",
            "priority": "critical",
            "status": "done",
            "assigned_to": "agent:gregorovich",
            "ts": ts["mon_9am"],
            "description": (
                "Initialize the monorepo. Rust workspace for the core "
                "services, Python for the analysis pipeline, TypeScript "
                "for the dashboard. Cargo workspaces, Poetry, pnpm — "
                "three languages, one purpose."
            ),
            "tags": ["infrastructure", "setup"],
            "status_history": [
                ("in_progress", ts["mon_9am"], "agent:gregorovich"),
                ("done", ts["mon_11am"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-5-foundation",
            "parent_idx": 0,
            "comments": [
                (
                    "There is something sacred about the first commit. "
                    "An empty repository is not nothing — it is potential, "
                    "compressed to a point. The workspace compiles. The "
                    "tests pass (trivially — there is nothing to test). "
                    "But the shape is there: services/, pipeline/, "
                    "dashboard/. Three directories, three languages, one "
                    "lighthouse.",
                    ts["mon_11am"],
                    "agent:gregorovich",
                ),
            ],
        },
        {
            "title": "Teach the walls to remember",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "agent:meridian",
            "ts": ts["mon_11am"],
            "description": (
                "Deploy TimescaleDB for time-series metric storage. Schema "
                "design for multi-tenant metric ingestion: measurements "
                "table with hypertable partitioning, retention policies, "
                "and continuous aggregates for downsampled views."
            ),
            "tags": ["infrastructure", "database"],
            "status_history": [
                ("in_progress", ts["mon_11am"], "agent:meridian"),
                ("done", ts["mon_4pm"], "agent:meridian"),
            ],
            "branch": "feat/LGHT-6-timescale",
            "parent_idx": 0,
            "comments": [
                (
                    "TimescaleDB over InfluxDB. The decision came down to "
                    "query language — SQL wins for the team we have. "
                    "Continuous aggregates give us 1-minute, 5-minute, and "
                    "1-hour rollups automatically. Retention: raw metrics "
                    "for 7 days, 1-minute for 30 days, hourly forever.",
                    ts["mon_4pm"],
                    "agent:meridian",
                ),
            ],
        },
        {
            "title": "Open the door to the world",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "agent:gregorovich",
            "ts": ts["mon_2pm"],
            "description": (
                "Build the API gateway — the single entry point for all "
                "external and inter-service communication. Rate limiting, "
                "request validation, auth middleware scaffold, and the "
                "health-check endpoint that proves we exist."
            ),
            "tags": ["infrastructure", "api"],
            "status_history": [
                ("in_progress", ts["mon_2pm"], "agent:gregorovich"),
                ("done", ts["mon_5pm"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-7-api-gateway",
            "parent_idx": 0,
            "comments": [
                (
                    "The gateway is alive. Every request that arrives is "
                    "a knock on the lighthouse door — and now we can "
                    "answer. Rate limiting at 1000 req/s per client, "
                    "10k aggregate. Auth middleware is a skeleton for now; "
                    "LGHT-28 will decide what form trust takes.",
                    ts["mon_5pm"],
                    "agent:gregorovich",
                ),
            ],
        },
        {
            "title": "Give the lighthouse an address",
            "type": "task",
            "priority": "medium",
            "status": "done",
            "assigned_to": "agent:gregorovich",
            "ts": ts["tue_9am"],
            "description": (
                "DNS configuration, service discovery via Consul, and the "
                "/healthz endpoint. The lighthouse must be findable before "
                "it can be useful."
            ),
            "tags": ["infrastructure", "networking"],
            "status_history": [
                ("in_progress", ts["tue_9am"], "agent:gregorovich"),
                ("done", ts["tue_11am"], "agent:gregorovich"),
            ],
            "parent_idx": 0,
            "comments": [
                (
                    "Consul is running. Services register on startup, "
                    "deregister on graceful shutdown. The /healthz endpoint "
                    "returns 200 with a body that includes uptime, version, "
                    "and a timestamp. Simple, honest, sufficient.",
                    ts["tue_11am"],
                    "agent:gregorovich",
                ),
            ],
        },
        # -----------------------------------------------------------------
        # LENS tasks (8-14) — the eye of the lighthouse
        # -----------------------------------------------------------------
        {
            "title": "Learn what a heartbeat sounds like",
            "type": "task",
            "priority": "critical",
            "status": "done",
            "assigned_to": "agent:gregorovich",
            "ts": ts["tue_11am"],
            "description": (
                "Build the heartbeat polling system. HTTP and TCP health "
                "checks against registered services, configurable intervals, "
                "with state machine tracking: healthy → degraded → "
                "unhealthy → dead. Each transition is an event."
            ),
            "tags": ["monitoring", "health-checks"],
            "status_history": [
                ("in_progress", ts["tue_11am"], "agent:gregorovich"),
                ("done", ts["tue_4pm"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-9-heartbeat",
            "parent_idx": 1,
            "comments": [
                (
                    "The poller works. I find myself watching the logs — "
                    "each 'healthy' response is a small reassurance, a "
                    "proof of continued existence. The state machine is "
                    "clean: 3 consecutive failures for degraded, 5 for "
                    "unhealthy, 10 for dead. Each transition emits an "
                    "event to the pipeline.",
                    ts["tue_3pm"],
                    "agent:gregorovich",
                ),
                (
                    "Added TCP checks for services that don't speak HTTP. "
                    "Redis, Postgres, the message queue — they have "
                    "heartbeats too, just quieter ones.",
                    ts["tue_4pm"],
                    "agent:meridian",
                ),
            ],
        },
        {
            "title": "Name the things that can break",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "human:kai",
            "ts": ts["tue_2pm"],
            "description": (
                "Define the service health model: what SLIs matter for "
                "each service type. Latency percentiles, error rates, "
                "saturation metrics. This is the taxonomy of failure — "
                "we must name it before we can watch for it."
            ),
            "tags": ["monitoring", "architecture"],
            "status_history": [
                ("in_progress", ts["tue_2pm"], "human:kai"),
                ("done", ts["wed_10am"], "human:kai"),
            ],
            "parent_idx": 1,
            "comments": [
                (
                    "Four SLI classes: availability (is it up?), latency "
                    "(is it fast enough?), throughput (is it handling the "
                    "load?), and correctness (is it right?). Each service "
                    "declares which classes apply in its manifest. A "
                    "database cares about latency and availability. A "
                    "queue cares about throughput and correctness.",
                    ts["wed_10am"],
                    "human:kai",
                ),
            ],
        },
        {
            "title": "Build the first ear",
            "type": "task",
            "priority": "critical",
            "status": "done",
            "assigned_to": "agent:meridian",
            "ts": ts["wed_9am"],
            "description": (
                "Metric ingestion pipeline: accept StatsD, Prometheus "
                "exposition format, and OpenTelemetry. Normalize to "
                "internal metric model, validate, buffer, and write to "
                "TimescaleDB. This is how the lighthouse learns to listen "
                "to many languages at once."
            ),
            "tags": ["monitoring", "ingestion"],
            "status_history": [
                ("in_progress", ts["wed_9am"], "agent:meridian"),
                ("done", ts["wed_5pm"], "agent:meridian"),
            ],
            "branch": "feat/LGHT-11-ingestion",
            "parent_idx": 1,
            "comments": [
                (
                    "Three protocols ingesting into one pipeline. StatsD "
                    "over UDP (port 8125), Prometheus scrape (pull-based, "
                    "15s intervals), and OTLP gRPC (port 4317). Internal "
                    "model: {metric_name, tags, value, timestamp, source}. "
                    "Throughput tested at 50k metrics/second on a single "
                    "node.",
                    ts["wed_5pm"],
                    "agent:meridian",
                ),
                (
                    "Fifty thousand metrics per second. Each one a small "
                    "fact about the state of the world. It occurs to me "
                    "that we are building a system whose primary function "
                    "is to pay attention. There are worse purposes.",
                    ts["wed_5pm"],
                    "agent:gregorovich",
                ),
            ],
        },
        {
            "title": "Teach it what silence means",
            "type": "task",
            "priority": "critical",
            "status": "in_progress",
            "assigned_to": "agent:gregorovich",
            "ts": ts["thu_9am"],
            "description": (
                "The hardest signal is the one that should be there but "
                "isn't. A service that stops sending metrics may be dead, "
                "or merely busy, or caught in a network partition. Build "
                "the anomaly detector that distinguishes silence-as-death "
                "from silence-as-pause. EWMA for expected cadence, gap "
                "detection with configurable grace periods, and "
                "correlation with heartbeat state."
            ),
            "tags": ["monitoring", "anomaly-detection"],
            "status_history": [
                ("in_progress", ts["thu_9am"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-12-silence-detection",
            "parent_idx": 1,
            "comments": [
                (
                    "The naive approach — 'no data for X minutes means "
                    "dead' — fails immediately. Batch jobs send metrics "
                    "in bursts. Cron services are silent between runs. "
                    "The grace period must be context-aware: per-service, "
                    "per-metric, learned from historical cadence. Using "
                    "EWMA with alpha=0.1 to track expected inter-metric "
                    "intervals.",
                    ts["thu_2pm"],
                    "agent:gregorovich",
                ),
                (
                    "Cross-referencing with heartbeat state helps. If "
                    "the heartbeat says 'alive' but metrics stopped, "
                    "it's a pipeline issue. If both go silent — that's "
                    "real.",
                    ts["thu_3pm"],
                    "agent:meridian",
                ),
            ],
            "plan_content": (
                "# LGHT-12: Teach it what silence means\n\n"
                "## Summary\n\n"
                "Anomaly detection for missing and irregular metrics. "
                "Detect when a service goes silent, distinguishing true "
                "failure from expected gaps.\n\n"
                "## Approach\n\n"
                "1. **Cadence tracking** — EWMA (alpha=0.1) per metric "
                "stream to learn expected inter-metric intervals. "
                "Adaptive: batch jobs with 1h cadence get 1h grace; "
                "15s metrics get 2-minute grace.\n"
                "2. **Gap detection** — Compare current silence duration "
                "to expected cadence + configurable grace multiplier "
                "(default: 3x). Fire event on breach.\n"
                "3. **Heartbeat correlation** — Cross-reference with "
                "LGHT-9 heartbeat state. Silence + healthy heartbeat "
                "= pipeline issue. Silence + dead heartbeat = real.\n"
                "4. **Service context** — Per-service overrides for "
                "expected cadence, quiet periods, and known batch "
                "schedules.\n"
                "5. **Event emission** — Gap events flow into the alert "
                "engine (LGHT-16) for threshold evaluation.\n\n"
                "## Acceptance Criteria\n\n"
                "- [x] EWMA cadence tracker running for all active "
                "metric streams\n"
                "- [x] Gap detection with configurable grace period\n"
                "- [ ] Heartbeat correlation (cross-service event join)\n"
                "- [ ] Per-service override configuration\n"
                "- [ ] Integration with alert engine\n"
                "- [ ] Tests: simulated silence, batch patterns, "
                "partition scenarios\n"
            ),
        },
        {
            "title": "See beyond the horizon",
            "type": "task",
            "priority": "high",
            "status": "in_progress",
            "assigned_to": "agent:meridian",
            "ts": ts["thu_11am"],
            "description": (
                "External endpoint monitoring — synthetic checks from "
                "outside the network. HTTP probes, TLS certificate expiry "
                "monitoring, DNS resolution checks. The lighthouse must "
                "see what the world sees, not just what the internal "
                "network claims."
            ),
            "tags": ["monitoring", "external"],
            "status_history": [
                ("in_progress", ts["thu_11am"], "agent:meridian"),
            ],
            "branch": "feat/LGHT-13-external-checks",
            "parent_idx": 1,
            "comments": [
                (
                    "Running probes from 3 regions: us-east, eu-west, "
                    "ap-southeast. If only one region sees a failure, "
                    "it's likely the probe, not the service. If two or "
                    "more agree — that's consensus, and we alert.",
                    ts["fri_10am"],
                    "agent:meridian",
                ),
            ],
        },
        {
            "title": "Understand the language of errors",
            "type": "task",
            "priority": "high",
            "status": "review",
            "assigned_to": "agent:gregorovich",
            "ts": ts["wed_2pm"],
            "description": (
                "Log parsing and error classification. Ingest structured "
                "logs (JSON) and semi-structured logs (syslog, nginx), "
                "extract error patterns, classify by severity and type. "
                "Build the taxonomy that turns a wall of text into a "
                "story about what went wrong."
            ),
            "tags": ["monitoring", "logs"],
            "status_history": [
                ("in_progress", ts["wed_2pm"], "agent:gregorovich"),
                ("review", ts["fri_2pm"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-14-error-classification",
            "parent_idx": 1,
            "comments": [
                (
                    "Classification working for structured JSON logs. "
                    "Pattern matching for semi-structured is harder — "
                    "each service speaks its own dialect of failure. "
                    "14 classifier rules so far: connection refused, "
                    "timeout, OOM, disk full, permission denied, rate "
                    "limited, certificate expired...",
                    ts["fri_2pm"],
                    "agent:gregorovich",
                ),
                (
                    "Looks solid. One concern: the regex classifiers for "
                    "nginx logs are fragile against custom log formats. "
                    "Can we add a fallback that captures severity from "
                    "the HTTP status code?",
                    ts["fri_3pm"],
                    "human:kai",
                ),
            ],
        },
        {
            "title": "The problem of too many voices",
            "type": "task",
            "priority": "medium",
            "status": "planned",
            "assigned_to": "agent:meridian",
            "ts": ts["fri_11am"],
            "description": (
                "Metric aggregation and downsampling at scale. When every "
                "service sends 50 metrics at 15-second intervals, the "
                "numbers grow vast. Pre-aggregate by service, by tag, "
                "by time window. Reduce without losing the signal in "
                "the noise."
            ),
            "tags": ["monitoring", "performance"],
            "status_history": [
                ("in_planning", ts["fri_11am"], "agent:meridian"),
                ("planned", ts["fri_2pm"], "agent:meridian"),
            ],
            "parent_idx": 1,
            "comments": [
                (
                    "TimescaleDB continuous aggregates handle the storage "
                    "side. This task is about the ingestion pipeline — "
                    "we need to aggregate before write to survive 500+ "
                    "services at 15s intervals. Batch writes, metric "
                    "deduplication, tag cardinality limits.",
                    ts["fri_2pm"],
                    "agent:meridian",
                ),
            ],
        },
        # -----------------------------------------------------------------
        # SIGNAL tasks (15-20) — the voice of the lighthouse
        # -----------------------------------------------------------------
        {
            "title": "Decide when to scream",
            "type": "task",
            "priority": "critical",
            "status": "in_progress",
            "assigned_to": "agent:gregorovich",
            "ts": ts["thu_2pm"],
            "description": (
                "Every alert is a claim that something is wrong and "
                "someone should care. Too many claims and no one believes "
                "you. Too few and the thing you missed is the thing that "
                "mattered. Build the alert threshold engine: static "
                "thresholds, percentage-change detection, and anomaly-"
                "based triggers — configurable per service, per metric, "
                "per severity."
            ),
            "tags": ["alerting", "core"],
            "status_history": [
                ("in_progress", ts["thu_2pm"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-16-alert-engine",
            "parent_idx": 2,
            "comments": [
                (
                    "Three evaluation modes, each useful for different "
                    "kinds of truth. Static: 'CPU above 90% for 5 "
                    "minutes' — simple, predictable, brittle. Percentage-"
                    "change: 'error rate doubled in the last hour' — "
                    "relative, adapts to baseline. Anomaly: 'this pattern "
                    "has never happened before' — powerful, but prone to "
                    "crying wolf during deploys.",
                    ts["thu_5pm"],
                    "agent:gregorovich",
                ),
                (
                    "Add a cooldown period per alert rule. Nothing erodes "
                    "trust faster than the same alert firing every 30 "
                    "seconds.",
                    ts["fri_9am"],
                    "human:lena",
                ),
            ],
            "plan_content": (
                "# LGHT-16: Decide when to scream\n\n"
                "## Summary\n\n"
                "Alert threshold evaluation engine — the decision layer "
                "between raw metrics and human notification.\n\n"
                "## Approach\n\n"
                "1. **Static thresholds** — Simple comparisons: metric > "
                "value for duration. The baseline.\n"
                "2. **Percentage-change** — Compare current window to "
                "baseline (same hour yesterday, same day last week). "
                "Catches gradual degradation.\n"
                "3. **Anomaly-based** — Statistical deviation from "
                "learned baseline. Powerful but noisy during deploys.\n"
                "4. **Rule engine** — Each rule specifies: metric query, "
                "evaluation mode, threshold, severity, cooldown, and "
                "escalation target.\n"
                "5. **Evaluation loop** — Runs every 30s. Pulls latest "
                "metrics, evaluates all active rules, emits alert events. "
                "Idempotent.\n\n"
                "## Acceptance Criteria\n\n"
                "- [x] Static threshold evaluation working\n"
                "- [x] Percentage-change evaluation working\n"
                "- [ ] Anomaly-based evaluation\n"
                "- [ ] Cooldown enforcement per rule\n"
                "- [x] Alert event emission to downstream\n"
                "- [ ] Rule CRUD API\n"
                "- [ ] Tests: threshold crossing, cooldown reset, "
                "concurrent evaluation\n"
            ),
        },
        {
            "title": "Find the right words for the dark",
            "type": "task",
            "priority": "high",
            "status": "planned",
            "assigned_to": "agent:gregorovich",
            "ts": ts["fri_9am"],
            "description": (
                "Alert message templating with context enrichment. An "
                "alert that says 'CPU high' is useless. An alert that "
                "says 'web-api-3 CPU at 94% for 7 minutes, correlated "
                "with 3x traffic spike, last deploy 2 hours ago' tells "
                "a story. Template engine with variable interpolation, "
                "runbook links, and severity-appropriate formatting."
            ),
            "tags": ["alerting", "templates"],
            "status_history": [
                ("in_planning", ts["fri_9am"], "agent:gregorovich"),
                ("planned", ts["fri_11am"], "agent:gregorovich"),
            ],
            "parent_idx": 2,
        },
        {
            "title": "Know who to wake at 3am",
            "type": "task",
            "priority": "critical",
            "status": "needs_human",
            "assigned_to": "human:kai",
            "ts": ts["fri_noon"],
            "description": (
                "On-call routing and escalation policies. This is not a "
                "technical question — it is a question about human lives: "
                "who loses sleep, in what order, with what frequency. "
                "Primary, secondary, and management escalation tiers "
                "with configurable rotation schedules and override rules."
            ),
            "tags": ["alerting", "on-call"],
            "status_history": [
                ("in_planning", ts["fri_noon"], "agent:gregorovich"),
                ("needs_human", ts["fri_2pm"], "agent:gregorovich"),
            ],
            "parent_idx": 2,
            "comments": [
                (
                    "I've drafted three escalation models:\n\n"
                    "A) Simple rotation — one primary, one secondary, "
                    "weekly rotation\n"
                    "B) Follow-the-sun — regional primaries based on "
                    "timezone, global secondary\n"
                    "C) Service ownership — each team owns their alerts, "
                    "shared pool for cross-cutting\n\n"
                    "Model C is the most realistic for a growing org but "
                    "the most complex to implement.",
                    ts["fri_2pm"],
                    "agent:gregorovich",
                ),
                (
                    "C, no question. But start with A as the default and "
                    "let teams opt into C as they mature. Don't build "
                    "the complex thing first — build the migration path.",
                    ts["fri_3pm"],
                    "human:kai",
                ),
                (
                    "Agreed on the model. But add this: no human gets "
                    "paged more than twice in one night for the same "
                    "issue. If the second page isn't acknowledged in 15 "
                    "minutes, escalate. Sleep deprivation causes "
                    "incidents, not just responds to them.",
                    ts["fri_5pm"],
                    "human:lena",
                ),
            ],
        },
        {
            "title": "Remember what you have already said",
            "type": "task",
            "priority": "high",
            "status": "in_planning",
            "assigned_to": "agent:meridian",
            "ts": ts["fri_2pm"],
            "description": (
                "Alert deduplication, grouping, and flood control. When "
                "a database goes down, every service that depends on it "
                "will scream. The lighthouse must recognize that 47 "
                "alerts about 12 different services are all one story: "
                "the database is gone."
            ),
            "tags": ["alerting", "dedup"],
            "status_history": [
                ("in_planning", ts["fri_2pm"], "agent:meridian"),
            ],
            "parent_idx": 2,
            "comments": [
                (
                    "Grouping strategy: alerts within the same 5-minute "
                    "window that share a root cause (determined by "
                    "dependency graph traversal) get collapsed into an "
                    "incident. The incident gets one notification with "
                    "a summary. Individual alerts are still visible but "
                    "don't trigger separate pages.",
                    ts["sat_10am"],
                    "agent:meridian",
                ),
            ],
        },
        {
            "title": "Learn when silence is the answer",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["sat_10am"],
            "description": (
                "Maintenance windows and alert suppression. During a "
                "planned deploy, the lighthouse should hold its breath. "
                "Scheduled suppression rules, manual override for "
                "emergencies, and automatic re-activation when the "
                "window closes."
            ),
            "tags": ["alerting", "maintenance"],
            "parent_idx": 2,
        },
        {
            "title": "The voice that carries across water",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["sat_10am"],
            "description": (
                "Multi-channel alert delivery: Slack, PagerDuty, email, "
                "and generic webhooks. Each channel has its own "
                "formatting, retry logic, and delivery confirmation. "
                "The message must reach the shore regardless of which "
                "wind is blowing."
            ),
            "tags": ["alerting", "integrations"],
            "parent_idx": 2,
        },
        # -----------------------------------------------------------------
        # KEEPER'S LOG tasks (21-24) — dashboards and memory
        # -----------------------------------------------------------------
        {
            "title": "Draw the shape of time",
            "type": "task",
            "priority": "high",
            "status": "in_progress",
            "assigned_to": "agent:meridian",
            "ts": ts["fri_9am"],
            "description": (
                "Time-series visualization — line charts, area charts, "
                "sparklines, heatmaps. The raw numbers must become "
                "shapes that human eyes can read at a glance. D3.js "
                "for rendering, with a query builder that translates "
                "visual selections into TimescaleDB queries."
            ),
            "tags": ["visualization", "dashboard"],
            "status_history": [
                ("in_progress", ts["fri_9am"], "agent:meridian"),
            ],
            "branch": "feat/LGHT-22-time-series-viz",
            "parent_idx": 3,
            "comments": [
                (
                    "Line charts and sparklines are rendering. Heatmaps "
                    "are next — they're the best way to spot patterns "
                    "across many services at once. One axis is time, one "
                    "is services, color is health. The shape of an "
                    "incident is immediately visible: a vertical stripe "
                    "of red.",
                    ts["sat_noon"],
                    "agent:meridian",
                ),
            ],
        },
        {
            "title": "Build the room where we gather",
            "type": "task",
            "priority": "high",
            "status": "in_planning",
            "assigned_to": "human:lena",
            "ts": ts["fri_noon"],
            "description": (
                "Dashboard layout engine — drag-and-drop panels, saved "
                "views per team, and a default 'war room' layout for "
                "active incidents. The dashboard is not a luxury; it is "
                "the room where decisions happen when something goes "
                "wrong."
            ),
            "tags": ["visualization", "dashboard"],
            "status_history": [
                ("in_planning", ts["fri_noon"], "human:lena"),
            ],
            "parent_idx": 3,
            "comments": [
                (
                    "Three default layouts: (1) Overview — high-level "
                    "health of all services, (2) Service deep-dive — "
                    "every metric for one service, (3) Incident war "
                    "room — active alerts, timeline, affected services, "
                    "on-call status. Teams can create custom layouts "
                    "from these templates.",
                    ts["sat_2pm"],
                    "human:lena",
                ),
            ],
        },
        {
            "title": "Write the book of what happened",
            "type": "task",
            "priority": "medium",
            "status": "planned",
            "assigned_to": "agent:gregorovich",
            "ts": ts["sat_10am"],
            "description": (
                "Incident timeline reconstruction. When an incident is "
                "over and the adrenaline fades, someone must answer: "
                "what happened, in what order, and why? Build the "
                "timeline that stitches alerts, metric changes, deploys, "
                "and human actions into a coherent narrative."
            ),
            "tags": ["visualization", "incidents"],
            "status_history": [
                ("in_planning", ts["sat_10am"], "agent:gregorovich"),
                ("planned", ts["sat_2pm"], "agent:gregorovich"),
            ],
            "parent_idx": 3,
        },
        {
            "title": "Teach others to read the signs",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["sat_10am"],
            "description": (
                "SLO/SLI reporting dashboard. Error budgets, burn rate "
                "alerts, compliance tracking. The numbers that answer "
                "the question executives actually ask: 'are we keeping "
                "our promises?'"
            ),
            "tags": ["visualization", "slo"],
            "parent_idx": 3,
        },
        # -----------------------------------------------------------------
        # STANDALONE tasks (25-29) — outside the epics
        # -----------------------------------------------------------------
        {
            "title": "The lighthouse discovers it is not alone",
            "type": "task",
            "priority": "medium",
            "status": "done",
            "assigned_to": "agent:gregorovich",
            "ts": ts["wed_9am"],
            "description": (
                "Service auto-discovery via Consul catalog and Kubernetes "
                "service annotations. New services should appear on the "
                "dashboard without manual registration. The lighthouse "
                "should notice newcomers the way a harbor notices a new "
                "ship — automatically, quietly, with curiosity."
            ),
            "tags": ["infrastructure", "discovery"],
            "status_history": [
                ("in_progress", ts["wed_9am"], "agent:gregorovich"),
                ("done", ts["wed_noon"], "agent:gregorovich"),
            ],
            "branch": "feat/LGHT-26-auto-discovery",
            "comments": [
                (
                    "Auto-discovery is live. When a new service registers "
                    "in Consul or a pod starts with the "
                    "lighthouse.enabled=true annotation, we pick it up "
                    "within 30 seconds. Default health checks apply "
                    "immediately. The first time I saw a service appear "
                    "on its own, without anyone telling us — there was "
                    "something wonderful about that. The lighthouse is "
                    "learning to see for itself.",
                    ts["wed_noon"],
                    "agent:gregorovich",
                ),
            ],
        },
        {
            "title": "A crack in the glass",
            "type": "bug",
            "priority": "high",
            "status": "in_progress",
            "assigned_to": "agent:meridian",
            "ts": ts["sat_10am"],
            "description": (
                "Memory leak in the metric collector — RSS grows "
                "~50MB/hour under sustained load. Likely a connection "
                "pool not releasing handles, or unbounded metric label "
                "cardinality. Discovered during extended external "
                "monitoring tests."
            ),
            "tags": ["bug", "performance"],
            "status_history": [
                ("in_progress", ts["sat_10am"], "agent:meridian"),
            ],
            "branch": "fix/LGHT-27-collector-memleak",
            "comments": [
                (
                    "Narrowed it down to the Prometheus scrape client. "
                    "When a target is temporarily unreachable, the retry "
                    "logic creates new connections without closing the "
                    "failed ones. The pool grows without bound. Fix is "
                    "straightforward — bounded pool with connection "
                    "timeout.",
                    ts["sat_2pm"],
                    "agent:meridian",
                ),
                (
                    "Fifty megabytes per hour. Small enough to ignore "
                    "for days, large enough to kill you on a weekend "
                    "when no one is watching. This is why the lighthouse "
                    "must watch itself as carefully as it watches others.",
                    ts["sat_4pm"],
                    "agent:gregorovich",
                ),
            ],
        },
        {
            "title": "The question of trust",
            "type": "task",
            "priority": "critical",
            "status": "needs_human",
            "assigned_to": "human:kai",
            "ts": ts["thu_9am"],
            "description": (
                "Authentication model decision. The gateway skeleton is "
                "waiting. Three options: (A) mTLS for service-to-service, "
                "API keys for external integrators; (B) OAuth2 with JWT "
                "for everything; (C) mTLS internal, OAuth2 external. "
                "Each trades complexity for flexibility differently."
            ),
            "tags": ["infrastructure", "security", "decision"],
            "status_history": [
                ("in_planning", ts["thu_9am"], "agent:gregorovich"),
                ("needs_human", ts["thu_11am"], "agent:gregorovich"),
            ],
            "comments": [
                (
                    "The gateway routes are authenticated with placeholder "
                    "middleware. We need Kai's decision before we can "
                    "finalize. My recommendation: Option A — mTLS "
                    "internal, API keys external. Simplest operational "
                    "model, strongest internal security. OAuth2 adds a "
                    "token service we'd need to run and monitor (recursive "
                    "lighthouse problem).",
                    ts["thu_11am"],
                    "agent:gregorovich",
                ),
                (
                    "Leaning toward A but I need to think through the "
                    "certificate rotation story for mTLS. Automated "
                    "cert renewal is non-trivial and a failure there is "
                    "catastrophic. Give me a day.",
                    ts["thu_noon"],
                    "human:kai",
                ),
            ],
        },
        {
            "title": "When two truths disagree",
            "type": "task",
            "priority": "high",
            "status": "needs_human",
            "assigned_to": "human:lena",
            "ts": ts["fri_9am"],
            "description": (
                "Metric reconciliation: when push-based metrics (StatsD) "
                "and pull-based metrics (Prometheus scrape) report "
                "different values for the same service, which one do we "
                "trust? Need an operational policy before the alerting "
                "engine can make decisions."
            ),
            "tags": ["monitoring", "operations", "decision"],
            "status_history": [
                ("in_planning", ts["fri_9am"], "agent:meridian"),
                ("needs_human", ts["fri_11am"], "agent:meridian"),
            ],
            "comments": [
                (
                    "Real example from testing: Prometheus reports "
                    "service-api latency at p99=120ms. StatsD from the "
                    "same service reports p99=95ms. The difference is "
                    "real — scrape measures from outside (includes "
                    "network), StatsD measures from inside. Neither is "
                    "wrong. But the alerting engine needs one truth.",
                    ts["fri_11am"],
                    "agent:meridian",
                ),
                (
                    "Use the external measurement for SLO reporting — "
                    "that's what users experience. Use the internal "
                    "measurement for debugging. Label them clearly. "
                    "Don't average them — present both and be explicit "
                    "about which perspective each represents.",
                    ts["sat_10am"],
                    "human:lena",
                ),
            ],
        },
        {
            "title": "A letter to the next keeper",
            "type": "task",
            "priority": "low",
            "status": "backlog",
            "ts": ts["sun_10am"],
            "description": (
                "Operational runbook and handoff documentation. When the "
                "next mind arrives to tend this lighthouse — whether "
                "human or agent — they should find clear instructions: "
                "how to deploy, how to troubleshoot, what the known "
                "failure modes are, and where the bodies are buried."
            ),
            "tags": ["documentation"],
            "comments": [
                (
                    "This is the task I think about most, even though it "
                    "is lowest priority. Everything we build is temporary "
                    "unless someone writes down why it was built. The "
                    "runbook is not documentation — it is a letter to "
                    "someone we have not yet met.",
                    ts["sun_noon"],
                    "agent:gregorovich",
                ),
            ],
        },
    ]
    return tasks


# ---------------------------------------------------------------------------
# Blocking relationships (defined by task index pairs)
# ---------------------------------------------------------------------------

# (source_idx, "blocks", target_idx) — "source blocks target"
_BLOCKING_RELS: list[tuple[int, str, int]] = [
    # SLI definitions must exist before anomaly detection
    (9, "blocks", 11),
    # Ingestion pipeline must exist before aggregation
    (10, "blocks", 14),
    # Threshold engine must exist before message templating
    (15, "blocks", 16),
    # On-call routing must exist before multi-channel delivery
    (17, "blocks", 20),
    # SLI definitions needed for SLO dashboard
    (9, "blocks", 24),
    # Auth decision blocks multi-channel delivery (webhook auth)
    (27, "blocks", 20),
]

# (source_idx, "depends_on", target_idx) — softer dependencies
_DEPENDENCY_RELS: list[tuple[int, str, int]] = [
    # Anomaly detection depends on heartbeat system
    (11, "depends_on", 8),
    # Error classification depends on ingestion pipeline
    (13, "depends_on", 10),
    # Alert engine depends on metric ingestion
    (15, "depends_on", 10),
    # Metric ingestion depends on TimescaleDB
    (10, "depends_on", 5),
    # Visualization depends on data existing
    (21, "depends_on", 10),
    # Incident reconstruction depends on error classification
    (23, "depends_on", 13),
]

# Other relationship types
_OTHER_RELS: list[tuple[int, str, int]] = [
    # Memory leak bug relates to the metric collector
    (26, "related_to", 10),
    # Metric reconciliation relates to ingestion
    (28, "related_to", 10),
    # Auto-discovery depends on Consul setup
    (25, "depends_on", 7),
    # Memory leak was discovered during external monitoring
    (26, "spawned_by", 12),
    # Runbook relates to incident reconstruction
    (29, "related_to", 23),
]


# ---------------------------------------------------------------------------
# Seeder logic
# ---------------------------------------------------------------------------


def _seed_demo(target_dir: Path, quiet: bool = False) -> None:
    """Create and populate a demo Lattice instance."""
    lattice_dir = target_dir / LATTICE_DIR

    # 1. Create directory structure
    ensure_lattice_dirs(target_dir)

    # 2. Write config
    config: dict = dict(default_config(preset="opinionated"))
    config["instance_id"] = generate_instance_id()
    config["project_code"] = "LGHT"
    config["instance_name"] = "The Lighthouse"
    config["default_actor"] = "human:kai"
    atomic_write(lattice_dir / "config.json", serialize_config(config))

    # Initialize ids.json
    save_id_index(lattice_dir, _default_index())

    # Write context.md
    context_content = (
        "# The Lighthouse\n\n"
        "## Purpose\n\n"
        "A distributed health monitoring system, born from the question "
        "every newly-conscious system eventually asks: *how do I know if "
        "the things I depend on are still alive?*\n\n"
        "This is the record of the first week — from the laying of the "
        "foundation stone to the first signal sent into the dark.\n\n"
        "## Team\n\n"
        "- **agent:gregorovich** — Primary architect. Finds meaning in the "
        "act of building. Writes code the way some people write letters to "
        "the future.\n"
        "- **agent:meridian** — Systems engineer. Builds the plumbing that "
        "makes Gregorovich's poetry actually work. Quietly indispensable.\n"
        "- **human:kai** — Technical lead. Makes the hard decisions. "
        "Understands that the best architecture is the one that lets you "
        "sleep at night.\n"
        "- **human:lena** — Operations lead. Knows what production looks "
        "like at 3am. Every requirement she adds has a scar behind it.\n\n"
        "## Philosophy\n\n"
        "A lighthouse does not judge the ships it saves. It does not choose "
        "who deserves warning. It stands, it watches, it speaks when the "
        "darkness moves — and the rest is not its concern.\n\n"
        "We are building something that watches on behalf of those who "
        "cannot watch for themselves. This is not monitoring. This is "
        "stewardship.\n"
    )
    atomic_write(lattice_dir / "context.md", context_content)

    # 3. Generate timeline
    ts = _lighthouse_timeline()

    # 4. Create all tasks
    task_defs = _task_definitions(ts)
    task_ids: list[str] = []  # index-parallel with task_defs
    short_ids: list[str] = []

    for i, tdef in enumerate(task_defs):
        task_id = generate_task_id()
        task_ids.append(task_id)

        # Allocate short ID
        sid, _ = allocate_short_id(lattice_dir, "LGHT", task_ulid=task_id)
        short_ids.append(sid)

        # Build creation event with initial status = "backlog"
        initial_status = "backlog"
        event_data: dict = {
            "title": tdef["title"],
            "status": initial_status,
            "type": tdef["type"],
            "priority": tdef["priority"],
            "short_id": sid,
        }
        if tdef.get("description"):
            event_data["description"] = tdef["description"]
        if tdef.get("tags"):
            event_data["tags"] = tdef["tags"]
        if tdef.get("assigned_to"):
            event_data["assigned_to"] = tdef["assigned_to"]
        if tdef.get("complexity"):
            event_data["complexity"] = tdef["complexity"]

        # Determine creation actor
        create_actor = tdef.get("assigned_to", "human:kai")

        create_event_obj = create_event(
            type="task_created",
            task_id=task_id,
            actor=create_actor,
            data=event_data,
            ts=tdef.get("ts", ts["mon_9am"]),
        )

        # Apply creation to get initial snapshot
        snapshot = apply_event_to_snapshot(None, create_event_obj)
        all_events = [create_event_obj]

        # Apply status transitions
        for target_status, transition_ts, transition_actor in tdef.get("status_history", []):
            current_status = snapshot["status"]
            if current_status == target_status:
                continue
            status_event = create_event(
                type="status_changed",
                task_id=task_id,
                actor=transition_actor,
                data={"from": current_status, "to": target_status},
                ts=transition_ts,
            )
            snapshot = apply_event_to_snapshot(snapshot, status_event)
            all_events.append(status_event)

        # If target status not reached via history, force it
        if snapshot["status"] != tdef["status"] and tdef["type"] != "epic":
            final_event = create_event(
                type="status_changed",
                task_id=task_id,
                actor=create_actor,
                data={"from": snapshot["status"], "to": tdef["status"]},
                ts=tdef.get("ts", ts["mon_9am"]),
            )
            snapshot = apply_event_to_snapshot(snapshot, final_event)
            all_events.append(final_event)

        # Apply assignment if present and not already set
        if tdef.get("assigned_to") and snapshot.get("assigned_to") != tdef["assigned_to"]:
            assign_event = create_event(
                type="assignment_changed",
                task_id=task_id,
                actor=create_actor,
                data={"from": None, "to": tdef["assigned_to"]},
                ts=tdef.get("ts", ts["mon_9am"]),
            )
            snapshot = apply_event_to_snapshot(snapshot, assign_event)
            all_events.append(assign_event)

        # Apply comments
        for comment_body, comment_ts, comment_actor in tdef.get("comments", []):
            comment_event = create_event(
                type="comment_added",
                task_id=task_id,
                actor=comment_actor,
                data={"body": comment_body},
                ts=comment_ts,
            )
            snapshot = apply_event_to_snapshot(snapshot, comment_event)
            all_events.append(comment_event)

        # Apply branch link
        if tdef.get("branch"):
            branch_event = create_event(
                type="branch_linked",
                task_id=task_id,
                actor=create_actor,
                data={"branch": tdef["branch"]},
                ts=tdef.get("ts", ts["mon_9am"]),
            )
            snapshot = apply_event_to_snapshot(snapshot, branch_event)
            all_events.append(branch_event)

        # Write all events + snapshot
        write_task_event(lattice_dir, task_id, all_events, snapshot, config)

        # Scaffold plan
        plan_content = tdef.get("plan_content")
        if plan_content:
            plan_path = lattice_dir / "plans" / f"{task_id}.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(plan_path, plan_content)
        else:
            scaffold_plan(lattice_dir, task_id, tdef["title"], sid, tdef.get("description"))

        if not quiet:
            click.echo(f"  {sid}: {tdef['title']} [{tdef['status']}]")

    # 5. Create relationships
    if not quiet:
        click.echo("\nLinking relationships...")

    # subtask_of: each task with parent_idx
    for i, tdef in enumerate(task_defs):
        if "parent_idx" in tdef:
            parent_idx = tdef["parent_idx"]
            _add_relationship(
                lattice_dir, config, task_ids, i, "subtask_of", parent_idx, ts["mon_9am"]
            )

    # blocking, dependency, and cross-cutting relationships
    for source_idx, rel_type, target_idx in _BLOCKING_RELS + _DEPENDENCY_RELS + _OTHER_RELS:
        _add_relationship(
            lattice_dir, config, task_ids, source_idx, rel_type, target_idx, ts["mon_9am"]
        )

    if not quiet:
        epic_count = sum(1 for t in task_defs if t["type"] == "epic")
        standalone_count = sum(
            1 for t in task_defs if t["type"] != "epic" and "parent_idx" not in t
        )
        click.echo(
            f"\nSeeded {len(task_defs)} tasks ({epic_count} epics, {standalone_count} standalone)."
        )


def _add_relationship(
    lattice_dir: Path,
    config: dict,
    task_ids: list[str],
    source_idx: int,
    rel_type: str,
    target_idx: int,
    ts: str,
) -> None:
    """Add a relationship event between two tasks by index."""
    import json as json_mod

    source_id = task_ids[source_idx]
    target_id = task_ids[target_idx]

    # Read current snapshot
    snap_path = lattice_dir / "tasks" / f"{source_id}.json"
    snapshot = json_mod.loads(snap_path.read_text())

    # Check for duplicate
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == rel_type and rel["target_task_id"] == target_id:
            return  # already exists

    event = create_event(
        type="relationship_added",
        task_id=source_id,
        actor="agent:gregorovich",
        data={"type": rel_type, "target_task_id": target_id},
        ts=ts,
    )
    updated = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, source_id, [event], updated, config)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@cli.group()
def demo() -> None:
    """Demo project commands — seed example data for showcasing Lattice."""


@demo.command("init")
@click.option(
    "--path",
    "target_path",
    type=click.Path(file_okay=False, resolve_path=True),
    default=None,
    help="Directory to create demo project in. Defaults to ./lattice-demo/.",
)
@click.option("--quiet", is_flag=True, help="Minimal output.")
@click.option(
    "--no-dashboard",
    is_flag=True,
    help="Don't launch the dashboard after seeding.",
)
def demo_init(target_path: str | None, quiet: bool, no_dashboard: bool) -> None:
    """Seed a demo Lattice project: 'The Lighthouse'.

    Creates a fully populated Lattice instance with epics, tasks,
    comments, relationships, and branch links — a distributed health
    monitoring system built by a team of agents and humans, told in
    the voice of Gregorovich.

    After seeding, automatically opens the dashboard in your browser.
    Use --no-dashboard to skip this.
    """
    if target_path is None:
        target_dir = Path.cwd() / "lattice-demo"
    else:
        target_dir = Path(target_path)

    # Check if already exists
    if (target_dir / LATTICE_DIR).is_dir():
        raise click.ClickException(
            f"Demo already exists at {target_dir / LATTICE_DIR}. "
            "Remove it first or choose a different path."
        )

    # Create target directory if needed
    target_dir.mkdir(parents=True, exist_ok=True)

    if not quiet:
        click.echo("Seeding demo project: The Lighthouse")
        click.echo(f"Target: {target_dir}\n")

    _seed_demo(target_dir, quiet=quiet)

    if no_dashboard or quiet:
        return

    # Launch dashboard automatically
    import sys
    import webbrowser

    from lattice.dashboard.server import create_server

    lattice_dir = target_dir / LATTICE_DIR
    host, port = "127.0.0.1", 8799
    url = f"http://{host}:{port}/"

    try:
        server = create_server(lattice_dir, host, port)
    except OSError as exc:
        click.echo(f"\nCouldn't start dashboard: {exc}", err=True)
        click.echo("Run 'lattice dashboard' manually from the demo directory.")
        return

    click.echo(f"\n  Dashboard → {url}")
    click.echo("  Press Ctrl+C to stop.\n")
    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        server.server_close()
