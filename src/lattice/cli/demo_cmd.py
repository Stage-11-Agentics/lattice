"""Demo project seeder: lattice demo init."""

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
# Timeline: a weekend hackathon (Friday evening → Sunday night)
# ---------------------------------------------------------------------------


def _weekend_timeline() -> dict[str, str]:
    """Generate realistic timestamps across a weekend hackathon.

    Returns a dict of named moments → ISO timestamps.
    """
    # Start from "last Friday at 6pm UTC"
    now = datetime.now(timezone.utc)
    # Find last Friday
    days_since_friday = (now.weekday() - 4) % 7
    if days_since_friday == 0 and now.hour < 18:
        days_since_friday = 7
    friday = now - timedelta(days=days_since_friday)
    friday_6pm = friday.replace(hour=18, minute=0, second=0, microsecond=0)

    def ts(hours_offset: float, minutes: int = 0) -> str:
        t = friday_6pm + timedelta(hours=hours_offset, minutes=minutes)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        # Friday evening — setting up infrastructure
        "fri_6pm": ts(0),
        "fri_630pm": ts(0, 30),
        "fri_7pm": ts(1),
        "fri_730pm": ts(1, 30),
        "fri_8pm": ts(2),
        "fri_9pm": ts(3),
        "fri_10pm": ts(4),
        "fri_11pm": ts(5),
        # Saturday — auth + core product
        "sat_9am": ts(15),
        "sat_10am": ts(16),
        "sat_11am": ts(17),
        "sat_noon": ts(18),
        "sat_1pm": ts(19),
        "sat_2pm": ts(20),
        "sat_3pm": ts(21),
        "sat_4pm": ts(22),
        "sat_5pm": ts(23),
        "sat_6pm": ts(24),
        "sat_8pm": ts(26),
        "sat_10pm": ts(28),
        # Sunday — billing discussion, landing page, polish
        "sun_10am": ts(40),
        "sun_11am": ts(41),
        "sun_noon": ts(42),
        "sun_1pm": ts(43),
        "sun_2pm": ts(44),
        "sun_3pm": ts(45),
        "sun_4pm": ts(46),
        "sun_5pm": ts(47),
        "sun_6pm": ts(48),
    }


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def _task_definitions(ts: dict[str, str]) -> list[dict]:
    """Return the full set of demo tasks with metadata.

    Each dict contains: title, type, priority, status, assigned_to,
    description, tags, events (additional events beyond creation),
    branch, comments, plan_content.
    """
    tasks = [
        # -----------------------------------------------------------------
        # EPICS (1-6)
        # -----------------------------------------------------------------
        {
            "title": "Infrastructure & DevOps",
            "type": "epic",
            "priority": "high",
            "status": "backlog",
            "ts": ts["fri_6pm"],
            "description": "Set up the foundational infrastructure: repo, hosting, database, CI/CD.",
            "tags": ["infra"],
        },
        {
            "title": "Authentication & Users",
            "type": "epic",
            "priority": "high",
            "status": "backlog",
            "ts": ts["fri_6pm"],
            "description": "Complete auth system with email/password, OAuth, and user management.",
            "tags": ["auth"],
        },
        {
            "title": "Billing & Payments",
            "type": "epic",
            "priority": "high",
            "status": "backlog",
            "ts": ts["fri_6pm"],
            "description": "Stripe integration, pricing tiers, subscription management.",
            "tags": ["billing", "stripe"],
        },
        {
            "title": "Core Product: AI Writing Assistant",
            "type": "epic",
            "priority": "critical",
            "status": "backlog",
            "ts": ts["fri_6pm"],
            "description": "The heart of the product — rich text editor with AI generation capabilities.",
            "tags": ["core", "ai"],
        },
        {
            "title": "Landing Page & Marketing",
            "type": "epic",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["fri_6pm"],
            "description": "Public-facing landing page with pricing, features, and waitlist.",
            "tags": ["marketing", "landing"],
        },
        {
            "title": "Launch Prep",
            "type": "epic",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["fri_6pm"],
            "description": "Documentation, analytics, domain setup, and launch strategy.",
            "tags": ["launch"],
        },
        # -----------------------------------------------------------------
        # INFRASTRUCTURE tasks (7-10) — all done
        # -----------------------------------------------------------------
        {
            "title": "Set up Next.js + TypeScript monorepo",
            "type": "task",
            "priority": "critical",
            "status": "done",
            "assigned_to": "agent:claude",
            "ts": ts["fri_630pm"],
            "description": "Initialize Next.js 15 with TypeScript, ESLint, Tailwind CSS. Turborepo for monorepo structure with apps/web and packages/ui.",
            "tags": ["infra", "setup"],
            "status_history": [
                ("in_progress", ts["fri_630pm"], "agent:claude"),
                ("done", ts["fri_7pm"], "agent:claude"),
            ],
            "branch": "feat/DEMO-7-nextjs-setup",
            "parent_idx": 0,
        },
        {
            "title": "Configure Vercel deployment + preview branches",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "agent:claude",
            "ts": ts["fri_7pm"],
            "description": "Connect GitHub repo to Vercel. Configure preview deployments for PRs, production branch mapping, and environment variables.",
            "tags": ["infra", "deploy"],
            "status_history": [
                ("in_progress", ts["fri_7pm"], "agent:claude"),
                ("done", ts["fri_730pm"], "agent:claude"),
            ],
            "parent_idx": 0,
        },
        {
            "title": "Provision Postgres on Neon",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "agent:cursor",
            "ts": ts["fri_730pm"],
            "description": "Set up Neon serverless Postgres. Configure connection pooling, create dev and prod branches. Initialize Prisma with base schema.",
            "tags": ["infra", "database"],
            "status_history": [
                ("in_progress", ts["fri_730pm"], "agent:cursor"),
                ("done", ts["fri_8pm"], "agent:cursor"),
            ],
            "parent_idx": 0,
            "comments": [
                (
                    "Neon free tier gives us 0.5 GB storage and 1 compute endpoint — enough for MVP. "
                    "Branching is killer for preview deployments.",
                    ts["fri_8pm"],
                    "agent:cursor",
                ),
            ],
        },
        {
            "title": "Set up GitHub Actions CI",
            "type": "task",
            "priority": "medium",
            "status": "done",
            "assigned_to": "agent:claude",
            "ts": ts["fri_8pm"],
            "description": "CI pipeline: lint, type-check, test on every PR. Cache node_modules and Turborepo artifacts.",
            "tags": ["infra", "ci"],
            "status_history": [
                ("in_progress", ts["fri_8pm"], "agent:claude"),
                ("done", ts["fri_9pm"], "agent:claude"),
            ],
            "parent_idx": 0,
        },
        # -----------------------------------------------------------------
        # AUTH tasks (11-15) — mostly done
        # -----------------------------------------------------------------
        {
            "title": "Design user data model + Prisma schema",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "human:alex",
            "ts": ts["fri_9pm"],
            "description": "User, Account, Session, VerificationToken tables. Support both credentials and OAuth providers. Include profile fields: name, avatar, bio.",
            "tags": ["auth", "database"],
            "status_history": [
                ("in_progress", ts["fri_9pm"], "human:alex"),
                ("done", ts["fri_10pm"], "human:alex"),
            ],
            "parent_idx": 1,
        },
        {
            "title": "Implement NextAuth email/password",
            "type": "task",
            "priority": "high",
            "status": "done",
            "assigned_to": "agent:claude",
            "ts": ts["fri_10pm"],
            "description": "NextAuth.js CredentialsProvider with bcrypt password hashing. Email verification flow with magic links. Rate limiting on auth endpoints.",
            "tags": ["auth"],
            "status_history": [
                ("in_progress", ts["fri_10pm"], "agent:claude"),
                ("done", ts["fri_11pm"], "agent:claude"),
            ],
            "branch": "feat/DEMO-12-nextauth",
            "parent_idx": 1,
            "comments": [
                (
                    "Using NextAuth v5 (Auth.js). The new Prisma adapter handles session management cleanly. "
                    "Added rate limiting with upstash/ratelimit — 5 login attempts per minute per IP.",
                    ts["fri_11pm"],
                    "agent:claude",
                ),
            ],
        },
        {
            "title": "Add Google OAuth provider",
            "type": "task",
            "priority": "medium",
            "status": "done",
            "assigned_to": "agent:cursor",
            "ts": ts["sat_9am"],
            "description": "Add Google as an OAuth provider in NextAuth. Handle account linking for users who signed up with email first.",
            "tags": ["auth", "oauth"],
            "status_history": [
                ("in_progress", ts["sat_9am"], "agent:cursor"),
                ("done", ts["sat_10am"], "agent:cursor"),
            ],
            "parent_idx": 1,
        },
        {
            "title": "Build user settings page",
            "type": "task",
            "priority": "medium",
            "status": "in_progress",
            "assigned_to": "agent:cursor",
            "ts": ts["sat_10am"],
            "description": "Settings page with tabs: Profile, Notifications, Billing (placeholder), Security. Avatar upload to S3.",
            "tags": ["auth", "ui"],
            "status_history": [
                ("in_progress", ts["sat_10am"], "agent:cursor"),
            ],
            "branch": "feat/DEMO-14-settings",
            "parent_idx": 1,
            "comments": [
                (
                    "Profile and notifications tabs are done. Billing tab shows a placeholder card pointing to the pricing page. "
                    "Security tab needs 2FA setup — parking that for post-MVP.",
                    ts["sat_2pm"],
                    "agent:cursor",
                ),
            ],
        },
        {
            "title": "Implement password reset flow",
            "type": "task",
            "priority": "medium",
            "status": "in_progress",
            "assigned_to": "agent:claude",
            "ts": ts["sat_11am"],
            "description": "Forgot password page, email with reset link (1h expiry), new password form with strength meter.",
            "tags": ["auth"],
            "status_history": [
                ("in_progress", ts["sat_11am"], "agent:claude"),
            ],
            "branch": "feat/DEMO-15-password-reset",
            "parent_idx": 1,
        },
        # -----------------------------------------------------------------
        # BILLING tasks (16-19) — blocked chain
        # -----------------------------------------------------------------
        {
            "title": "Decide pricing tiers and plan structure",
            "type": "task",
            "priority": "critical",
            "status": "needs_human",
            "assigned_to": "human:alex",
            "ts": ts["sun_10am"],
            "description": "Need founder decision on pricing model. Options: (A) $9/$19/$49 three-tier, (B) $19/$49/$99 premium positioning, (C) freemium + $29/$99. Competitive analysis attached.",
            "tags": ["billing", "decision"],
            "status_history": [
                ("in_planning", ts["sun_10am"], "agent:claude"),
                ("needs_human", ts["sun_11am"], "agent:claude"),
            ],
            "parent_idx": 2,
            "comments": [
                (
                    "Completed competitive analysis. Jasper charges $49/mo, Copy.ai has freemium at $49/mo pro, "
                    "Writesonic does $19/mo. For an MVP, I'd recommend Option C (freemium + $29/$99) — "
                    "free tier drives adoption, paid tiers capture serious users.",
                    ts["sun_10am"],
                    "agent:claude",
                ),
                (
                    "Good analysis. Leaning toward Option C but with $19/$49 instead of $29/$99. "
                    "We want to undercut the incumbents on price while we build reputation. "
                    "Let me sleep on it and decide tomorrow morning.",
                    ts["sun_11am"],
                    "human:alex",
                ),
            ],
        },
        {
            "title": "Integrate Stripe Checkout",
            "type": "task",
            "priority": "high",
            "status": "planned",
            "assigned_to": "agent:claude",
            "ts": ts["sun_11am"],
            "description": "Stripe Checkout for subscription signup. Webhook handler for payment events. Customer portal for self-service management.",
            "tags": ["billing", "stripe"],
            "status_history": [
                ("in_planning", ts["sun_11am"], "agent:claude"),
                ("planned", ts["sun_noon"], "agent:claude"),
            ],
            "parent_idx": 2,
            "comments": [
                (
                    "Ready to implement as soon as pricing tiers are decided. "
                    "Stripe Checkout + Customer Portal is the fastest path. "
                    "Webhook handler will cover: checkout.session.completed, customer.subscription.updated/deleted.",
                    ts["sun_noon"],
                    "agent:claude",
                ),
            ],
        },
        {
            "title": "Build subscription management portal",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["sun_noon"],
            "description": "In-app subscription management: current plan, usage, upgrade/downgrade, cancel. Wraps Stripe Customer Portal.",
            "tags": ["billing", "ui"],
            "parent_idx": 2,
        },
        {
            "title": "Add usage metering + overage alerts",
            "type": "task",
            "priority": "low",
            "status": "backlog",
            "ts": ts["sun_noon"],
            "description": "Track AI generation usage per user. Send email alerts at 80% and 100% of plan limits. Soft block at limit with upgrade CTA.",
            "tags": ["billing", "metering"],
            "parent_idx": 2,
        },
        # -----------------------------------------------------------------
        # CORE PRODUCT tasks (20-25) — the meaty middle
        # -----------------------------------------------------------------
        {
            "title": "Design core editor UX wireframes",
            "type": "task",
            "priority": "critical",
            "status": "done",
            "assigned_to": "human:jordan",
            "ts": ts["fri_6pm"],
            "description": "Wireframes for the main editor view: document canvas, AI sidebar, toolbar, document list. Mobile-responsive layout.",
            "tags": ["core", "design"],
            "status_history": [
                ("in_progress", ts["fri_6pm"], "human:jordan"),
                ("review", ts["sat_9am"], "human:jordan"),
                ("done", ts["sat_10am"], "human:alex"),
            ],
            "parent_idx": 3,
        },
        {
            "title": "Implement rich text editor with Tiptap",
            "type": "task",
            "priority": "critical",
            "status": "done",
            "assigned_to": "agent:claude",
            "ts": ts["sat_10am"],
            "description": "Tiptap editor with: headings, bold/italic/underline, lists, code blocks, links, images, tables. Markdown import/export.",
            "tags": ["core", "editor"],
            "status_history": [
                ("in_progress", ts["sat_10am"], "agent:claude"),
                ("done", ts["sat_2pm"], "agent:claude"),
            ],
            "branch": "feat/DEMO-21-tiptap-editor",
            "parent_idx": 3,
            "comments": [
                (
                    "Using Tiptap v2 with StarterKit + Table + Image extensions. "
                    "Markdown roundtrip via tiptap-markdown. The editor state is stored as JSON for version history compatibility.",
                    ts["sat_2pm"],
                    "agent:claude",
                ),
            ],
        },
        {
            "title": "Build AI generation sidebar",
            "type": "task",
            "priority": "critical",
            "status": "in_progress",
            "assigned_to": "agent:claude",
            "ts": ts["sat_3pm"],
            "description": "Sidebar panel for AI writing assistance: generate, rewrite, expand, summarize, change tone. Streaming responses via SSE. Context-aware (uses document content as context).",
            "tags": ["core", "ai"],
            "status_history": [
                ("in_progress", ts["sat_3pm"], "agent:claude"),
            ],
            "branch": "feat/DEMO-22-ai-sidebar",
            "parent_idx": 3,
            "comments": [
                (
                    "Using OpenAI GPT-4o-mini for generation (best cost/quality ratio for this use case). "
                    "Streaming via Server-Sent Events works beautifully with Next.js Route Handlers. "
                    "Still need to add: rate limiting per user tier, prompt templates for each action type, "
                    "and error handling for API failures.",
                    ts["sat_8pm"],
                    "agent:claude",
                ),
                (
                    "Looking good! Can we add a 'custom prompt' option where users type their own instruction? "
                    "That's the power-user feature that'll differentiate us.",
                    ts["sat_10pm"],
                    "human:alex",
                ),
                (
                    "Great idea — adding it to the action list. Custom prompt with a text field that "
                    "remembers the last 5 prompts per user (stored in localStorage for now, DB later).",
                    ts["sun_10am"],
                    "agent:claude",
                ),
            ],
            "plan_content": (
                "# DEMO-22: Build AI generation sidebar\n\n"
                "## Summary\n\n"
                "Sidebar panel with AI writing actions: generate, rewrite, expand, summarize, change tone, custom prompt.\n\n"
                "## Approach\n\n"
                "1. **UI Component** — Collapsible sidebar with action buttons + output preview\n"
                "2. **API Route** — `POST /api/ai/generate` with streaming (SSE)\n"
                "3. **Prompt Engineering** — System prompts per action type, document context injection\n"
                "4. **Rate Limiting** — Per-user limits based on plan tier (free: 10/day, pro: 100/day, team: unlimited)\n"
                "5. **Error Handling** — Graceful fallbacks for API failures, timeout handling\n\n"
                "## Acceptance Criteria\n\n"
                "- [ ] All 6 action types working with streaming output\n"
                "- [x] Custom prompt option with history (localStorage)\n"
                "- [ ] Rate limiting enforced per tier\n"
                "- [x] Loading states and error messages\n"
                "- [ ] Mobile responsive\n"
            ),
        },
        {
            "title": "Add template library",
            "type": "task",
            "priority": "medium",
            "status": "in_progress",
            "assigned_to": "agent:cursor",
            "ts": ts["sat_5pm"],
            "description": "Pre-built document templates: blog post, newsletter, product description, social media thread, press release. Template browser with preview.",
            "tags": ["core", "templates"],
            "status_history": [
                ("in_progress", ts["sat_5pm"], "agent:cursor"),
            ],
            "branch": "feat/DEMO-23-templates",
            "parent_idx": 3,
            "comments": [
                (
                    "Built 8 templates so far. Using MDX for template definitions — each template has metadata "
                    "(name, category, description) and a Tiptap JSON body. Template browser uses a grid layout with "
                    "live previews on hover.",
                    ts["sun_2pm"],
                    "agent:cursor",
                ),
            ],
        },
        {
            "title": "Implement version history",
            "type": "task",
            "priority": "medium",
            "status": "review",
            "assigned_to": "agent:claude",
            "ts": ts["sat_6pm"],
            "description": "Auto-save with version snapshots. Diff view between versions. Restore to any previous version. Keep last 50 versions per document.",
            "tags": ["core", "versioning"],
            "status_history": [
                ("in_progress", ts["sat_6pm"], "agent:claude"),
                ("review", ts["sun_3pm"], "agent:claude"),
            ],
            "branch": "feat/DEMO-24-version-history",
            "parent_idx": 3,
            "comments": [
                (
                    "Implementation complete. Using a diff-based approach with json-diff for Tiptap JSON. "
                    "Each save creates a snapshot if content changed (debounced to 30s). "
                    "Diff view highlights additions in green, deletions in red. "
                    "Review needed for: conflict resolution when restoring old version while having unsaved changes.",
                    ts["sun_3pm"],
                    "agent:claude",
                ),
            ],
        },
        {
            "title": "Add real-time collaboration",
            "type": "task",
            "priority": "low",
            "status": "backlog",
            "ts": ts["sun_noon"],
            "description": "Real-time collaborative editing with presence (cursors, selections). WebSocket or Yjs CRDT approach. Post-MVP but designing for it now.",
            "tags": ["core", "collaboration"],
            "parent_idx": 3,
            "complexity": "high",
        },
        # -----------------------------------------------------------------
        # LANDING PAGE tasks (26-29) — early stage
        # -----------------------------------------------------------------
        {
            "title": "Write landing page copy",
            "type": "task",
            "priority": "high",
            "status": "needs_human",
            "assigned_to": "human:alex",
            "ts": ts["sun_1pm"],
            "description": "Hero headline, subheadline, feature descriptions, social proof section, CTA copy. Must communicate the AI-native writing experience.",
            "tags": ["marketing", "copy"],
            "status_history": [
                ("in_planning", ts["sun_1pm"], "human:alex"),
                ("needs_human", ts["sun_2pm"], "agent:claude"),
            ],
            "parent_idx": 4,
            "comments": [
                (
                    "I drafted three headline options:\n"
                    'A) "Write better, faster, with AI that understands your voice"\n'
                    'B) "Your AI writing partner — from first draft to final polish"\n'
                    'C) "Stop staring at blank pages. Start creating."\n'
                    "Need Alex to pick a direction before I can flesh out the full copy.",
                    ts["sun_2pm"],
                    "agent:claude",
                ),
            ],
        },
        {
            "title": "Design hero section + product screenshots",
            "type": "task",
            "priority": "medium",
            "status": "in_planning",
            "assigned_to": "human:jordan",
            "ts": ts["sun_2pm"],
            "description": "Hero visual treatment, product screenshots for feature sections, responsive image assets. Dark/light mode variants.",
            "tags": ["marketing", "design"],
            "status_history": [
                ("in_planning", ts["sun_2pm"], "human:jordan"),
            ],
            "parent_idx": 4,
        },
        {
            "title": "Build pricing comparison table",
            "type": "task",
            "priority": "medium",
            "status": "planned",
            "ts": ts["sun_2pm"],
            "description": "Interactive pricing table showing plan comparison. Feature matrix, toggle for monthly/annual billing, highlighted recommended plan.",
            "tags": ["marketing", "billing"],
            "status_history": [
                ("in_planning", ts["sun_2pm"], "agent:claude"),
                ("planned", ts["sun_3pm"], "agent:claude"),
            ],
            "parent_idx": 4,
        },
        {
            "title": "Implement waitlist signup form",
            "type": "task",
            "priority": "medium",
            "status": "planned",
            "ts": ts["sun_3pm"],
            "description": "Email capture form with animated success state. Store in Postgres, send welcome email via Resend. Referral tracking.",
            "tags": ["marketing"],
            "status_history": [
                ("in_planning", ts["sun_3pm"], "agent:claude"),
                ("planned", ts["sun_4pm"], "agent:claude"),
            ],
            "parent_idx": 4,
        },
        # -----------------------------------------------------------------
        # LAUNCH PREP tasks (30-33) — backlog
        # -----------------------------------------------------------------
        {
            "title": "Write API documentation",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "assigned_to": "agent:claude",
            "ts": ts["sun_5pm"],
            "description": "API reference docs with OpenAPI spec. Cover auth endpoints, document CRUD, AI generation API. Interactive playground.",
            "tags": ["launch", "docs"],
            "parent_idx": 5,
        },
        {
            "title": "Set up PostHog analytics",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["sun_5pm"],
            "description": "PostHog for product analytics: page views, feature usage, funnel analysis (signup → first document → AI generation → upgrade). Session replay for UX debugging.",
            "tags": ["launch", "analytics"],
            "parent_idx": 5,
        },
        {
            "title": "Configure custom domain + SSL",
            "type": "task",
            "priority": "medium",
            "status": "backlog",
            "ts": ts["sun_5pm"],
            "description": "Point custom domain to Vercel. Configure SSL certificate. Set up email sending domain (SPF, DKIM, DMARC for Resend).",
            "tags": ["launch", "infra"],
            "parent_idx": 5,
        },
        {
            "title": "Prepare Product Hunt launch",
            "type": "task",
            "priority": "high",
            "status": "backlog",
            "assigned_to": "human:alex",
            "ts": ts["sun_6pm"],
            "description": "Product Hunt listing: tagline, description, gallery images, maker comment, first-day strategy. Coordinate with early users for launch day upvotes.",
            "tags": ["launch", "marketing"],
            "parent_idx": 5,
            "comments": [
                (
                    "Not doing this until the product is polished. Target: 2 weeks after MVP launch. "
                    "Need at least 10 happy beta users first for social proof.",
                    ts["sun_6pm"],
                    "human:alex",
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
    # Pricing decision blocks Stripe integration
    (15, "blocks", 16),
    # Stripe integration blocks subscription management
    (16, "blocks", 17),
    # Pricing decision blocks pricing comparison table
    (15, "blocks", 27),
    # Editor wireframes must complete before editor implementation
    (19, "blocks", 20),
    # NextAuth must exist before OAuth
    (11, "blocks", 12),
]

# (source_idx, "depends_on", target_idx) — optional softer dependencies
_DEPENDENCY_RELS: list[tuple[int, str, int]] = [
    # AI sidebar depends on editor
    (21, "depends_on", 20),
    # Templates depend on editor
    (22, "depends_on", 20),
    # Version history depends on editor
    (23, "depends_on", 20),
    # Password reset depends on NextAuth
    (14, "depends_on", 11),
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
    config: dict = dict(default_config())
    config["instance_id"] = generate_instance_id()
    config["project_code"] = "DEMO"
    config["instance_name"] = "Ship a SaaS MVP in a Weekend"
    config["default_actor"] = "human:alex"
    atomic_write(lattice_dir / "config.json", serialize_config(config))

    # Initialize ids.json
    save_id_index(lattice_dir, _default_index())

    # Write context.md
    context_content = (
        "# Ship a SaaS MVP in a Weekend\n\n"
        "## Purpose\n\n"
        "This is a demo project showcasing how a small team (2 humans + 2 AI agents) "
        "coordinates a weekend hackathon to ship an AI writing assistant SaaS.\n\n"
        "## Team\n\n"
        "- **human:alex** — Founder. Makes product decisions, writes copy.\n"
        "- **human:jordan** — Designer. Wireframes, visual design, screenshots.\n"
        "- **agent:claude** — Primary dev agent. Architecture, backend, core features.\n"
        "- **agent:cursor** — Secondary dev agent. UI components, OAuth, templates.\n\n"
        "## Conventions\n\n"
        "- Weekend timeline: Friday 6pm → Sunday midnight\n"
        "- Epics group related tasks into workstreams\n"
        "- `needs_human` flags tasks waiting on founder decisions\n"
    )
    atomic_write(lattice_dir / "context.md", context_content)

    # 3. Generate timeline
    ts = _weekend_timeline()

    # 4. Create all tasks
    task_defs = _task_definitions(ts)
    task_ids: list[str] = []  # index-parallel with task_defs
    short_ids: list[str] = []

    for i, tdef in enumerate(task_defs):
        task_id = generate_task_id()
        task_ids.append(task_id)

        # Allocate short ID
        sid, _ = allocate_short_id(lattice_dir, "DEMO", task_ulid=task_id)
        short_ids.append(sid)

        # Build creation event with initial status = "backlog" (we'll transition later)
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
        create_actor = tdef.get("assigned_to", "human:alex")

        create_event_obj = create_event(
            type="task_created",
            task_id=task_id,
            actor=create_actor,
            data=event_data,
            ts=tdef.get("ts", ts["fri_6pm"]),
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
                ts=tdef.get("ts", ts["fri_6pm"]),
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
                ts=tdef.get("ts", ts["fri_6pm"]),
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
                ts=tdef.get("ts", ts["fri_6pm"]),
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
                lattice_dir, config, task_ids, i, "subtask_of", parent_idx, ts["fri_6pm"]
            )

    # blocking and dependency relationships
    for source_idx, rel_type, target_idx in _BLOCKING_RELS + _DEPENDENCY_RELS:
        _add_relationship(
            lattice_dir, config, task_ids, source_idx, rel_type, target_idx, ts["fri_6pm"]
        )

    if not quiet:
        click.echo(f"\nDemo project seeded: {len(task_defs)} tasks across 6 epics.")
        click.echo(f"Location: {lattice_dir}")
        click.echo("\nTo explore:")
        click.echo(f"  cd {target_dir}")
        click.echo("  lattice list")
        click.echo("  lattice dashboard")


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
        actor="agent:claude",
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
def demo_init(target_path: str | None, quiet: bool) -> None:
    """Seed a demo Lattice project: 'Ship a SaaS MVP in a Weekend'.

    Creates a fully populated Lattice instance with epics, tasks,
    comments, relationships, and branch links — perfect for
    showcasing the dashboard and cube.
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
        click.echo("Seeding demo project: Ship a SaaS MVP in a Weekend")
        click.echo(f"Target: {target_dir}\n")

    _seed_demo(target_dir, quiet=quiet)
