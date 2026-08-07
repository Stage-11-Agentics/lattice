import { execSync } from "node:child_process";
import type { SingleCommand } from "./schemas";

interface LatticeConfig {
  project_root: string;
  actor: string;
}

/** Shell-escape a string argument */
function shellEscape(s: string): string {
  // Wrap in single quotes, escaping any existing single quotes
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

/** Build a CLI command string from a parsed SingleCommand */
export function buildCommandString(
  cmd: SingleCommand,
  config: LatticeConfig,
): string {
  const parts: string[] = ["lattice", cmd.command];

  // Positional args
  for (const arg of cmd.positional) {
    parts.push(shellEscape(arg));
  }

  // Named args
  for (const [key, value] of Object.entries(cmd.args)) {
    const flag = key.startsWith("--") ? key : `--${key}`;
    parts.push(flag, shellEscape(value));
  }

  // Boolean flags
  for (const flag of cmd.flags) {
    const f = flag.startsWith("--") ? flag : `--${flag}`;
    if (!parts.includes(f)) {
      parts.push(f);
    }
  }

  // Ensure --json is always present
  if (!parts.includes("--json")) {
    parts.push("--json");
  }

  // Add actor for write commands
  const writeCommands = new Set([
    "create", "status", "update", "assign", "complete", "comment", "next",
  ]);
  if (writeCommands.has(cmd.command) && !parts.includes("--actor")) {
    // Actor is passed via LATTICE_ACTOR env var instead for cleanliness
  }

  return parts.join(" ");
}

/** Execute a Lattice CLI command and return structured result */
export function executeLatticeCommand(
  cmdStr: string,
  config: LatticeConfig,
): { ok: boolean; raw: string; parsed: any } {
  try {
    const raw = execSync(cmdStr, {
      cwd: config.project_root,
      env: {
        ...process.env,
        LATTICE_ACTOR: config.actor,
      },
      encoding: "utf-8",
      timeout: 30_000,
    });

    try {
      const parsed = JSON.parse(raw);
      return { ok: parsed.ok ?? true, raw, parsed };
    } catch {
      return { ok: true, raw, parsed: { ok: true, data: raw.trim() } };
    }
  } catch (err: any) {
    const stderr = err.stderr || "";
    const stdout = err.stdout || "";
    try {
      const parsed = JSON.parse(stdout);
      return { ok: false, raw: stdout, parsed };
    } catch {
      return {
        ok: false,
        raw: stderr || stdout,
        parsed: {
          ok: false,
          error: { code: "EXEC_ERROR", message: stderr || err.message },
        },
      };
    }
  }
}

