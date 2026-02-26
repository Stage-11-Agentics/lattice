/** Format Lattice --json output into readable chat messages */
export function formatLatticeResult(
  command: string,
  result: { ok: boolean; data?: any; error?: { code?: string; message?: string } },
): string {
  if (!result.ok) {
    return `Error: ${result.error?.message || "Unknown error"}`;
  }

  const data = result.data;

  switch (command) {
    case "create": {
      const id = data?.short_id || data?.id || "?";
      return [
        `Created ${id}: "${data?.title}"`,
        `Status: ${data?.status || "backlog"} | Priority: ${data?.priority || "medium"}`,
      ].join("\n");
    }

    case "list": {
      const tasks = Array.isArray(data) ? data : data?.tasks;
      if (!tasks?.length) return "No tasks found.";
      const lines = tasks.map((t: any) => {
        const id = t.short_id || t.id?.slice(0, 8);
        const assigned = t.assigned_to ? ` [${t.assigned_to}]` : "";
        return `  ${id}  ${t.status}  ${t.priority || "-"}  "${t.title}"${assigned}`;
      });
      return `Tasks (${tasks.length}):\n${lines.join("\n")}`;
    }

    case "show": {
      const id = data?.short_id || data?.id;
      return [
        `${id}: "${data?.title}"`,
        `Status: ${data?.status} | Priority: ${data?.priority} | Type: ${data?.type || "task"}`,
        `Assigned: ${data?.assigned_to || "unassigned"}`,
        data?.description ? `\n${data.description}` : "",
      ]
        .filter(Boolean)
        .join("\n");
    }

    case "status":
      return `Updated ${data?.short_id || data?.id}: status -> ${data?.status}`;

    case "assign":
      return `Assigned ${data?.short_id || data?.id} to ${data?.assigned_to || "nobody"}`;

    case "complete":
      return `Completed ${data?.short_id || data?.id}`;

    case "comment":
      return `Comment added to ${data?.task_id || data?.short_id || "task"}`;

    case "weather": {
      const h = data?.headline;
      const v = data?.vital_signs;
      if (!h || !v) return JSON.stringify(data, null, 2).slice(0, 1500);
      return [
        `${h.project || "Project"} Weather: ${h.weather}`,
        `Active: ${v.active_tasks} | In Progress: ${v.in_progress} | Done recently: ${v.done_recently}`,
        data.attention?.length ? `Attention needed: ${data.attention.length} items` : "",
      ]
        .filter(Boolean)
        .join("\n");
    }

    case "stats": {
      const s = data?.summary || data;
      if (!s) return "No stats available.";
      return [
        `Tasks: ${s.active_tasks ?? "?"} active, ${s.archived_tasks ?? "?"} archived (${s.total_tasks ?? "?"} total)`,
        s.total_events ? `Events: ${s.total_events}` : "",
      ]
        .filter(Boolean)
        .join("\n");
    }

    case "next": {
      if (!data) return "No tasks ready to claim.";
      const id = data.short_id || data.id;
      return `Next up: ${id} "${data.title}" (${data.priority || "medium"})`;
    }

    default:
      return typeof data === "string"
        ? data
        : JSON.stringify(data, null, 2).slice(0, 1500);
  }
}
