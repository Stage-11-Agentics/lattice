import { execSync } from "node:child_process";

/** Status columns in display order */
const STATUS_ORDER = [
  "backlog",
  "in_planning",
  "planned",
  "in_progress",
  "review",
  "blocked",
  "needs_human",
  "done",
];

const PRIORITY_MARKERS: Record<string, string> = {
  critical: "!!",
  high: "!",
  medium: "",
  low: "~",
};

interface Task {
  short_id?: string;
  id: string;
  title: string;
  status: string;
  priority?: string;
  assigned_to?: string;
}

/** Run `lattice list --json` and return task array */
function fetchTasks(projectRoot: string): Task[] {
  try {
    const raw = execSync("lattice list --json", {
      cwd: projectRoot,
      encoding: "utf-8",
      timeout: 15_000,
    });
    const parsed = JSON.parse(raw);
    const tasks = parsed.ok ? parsed.data : parsed;
    return Array.isArray(tasks) ? tasks : tasks?.tasks ?? [];
  } catch {
    return [];
  }
}

/** Truncate a title to fit in a kanban card */
function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max - 3) + "...";
}

/** Escape special Mermaid characters in text */
function escape(s: string): string {
  return s.replace(/"/g, "'").replace(/[[\](){}]/g, "");
}

/**
 * Generate a Mermaid kanban diagram string from the current Lattice board.
 * Uses the Mermaid kanban diagram type.
 */
export function generateKanbanMermaid(projectRoot: string): string | null {
  const tasks = fetchTasks(projectRoot);
  if (!tasks.length) return null;

  // Group tasks by status
  const byStatus = new Map<string, Task[]>();
  for (const t of tasks) {
    const status = t.status || "backlog";
    if (!byStatus.has(status)) byStatus.set(status, []);
    byStatus.get(status)!.push(t);
  }

  // Build Mermaid kanban
  const lines: string[] = ["---", "config:", "  kanban:", "    ticketBaseUrl: ''", "---", "kanban"];

  for (const status of STATUS_ORDER) {
    const group = byStatus.get(status);
    if (!group?.length) continue;

    const label = status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    lines.push(`  ${label}`);

    for (const t of group) {
      const id = t.short_id || t.id.slice(0, 8);
      const pri = PRIORITY_MARKERS[t.priority || "medium"] || "";
      const title = escape(truncate(t.title, 40));
      const assignee = t.assigned_to ? ` @${t.assigned_to.replace(/^(human|agent):/, "")}` : "";
      lines.push(`    ${id}${pri ? " " + pri : ""}[${title}${assignee}]`);
    }
  }

  // Include any statuses not in STATUS_ORDER
  for (const [status, group] of byStatus) {
    if (STATUS_ORDER.includes(status)) continue;
    const label = status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    lines.push(`  ${label}`);
    for (const t of group) {
      const id = t.short_id || t.id.slice(0, 8);
      const title = escape(truncate(t.title, 40));
      lines.push(`    ${id}[${title}]`);
    }
  }

  return lines.join("\n") + "\n";
}
