import { z } from "zod";

/** A single Lattice CLI command to execute */
export const SingleCommandSchema = z.object({
  command: z
    .enum([
      "create",
      "list",
      "show",
      "status",
      "update",
      "comment",
      "assign",
      "complete",
      "next",
      "weather",
      "stats",
      "help",
      "none",
    ])
    .describe("The Lattice CLI command to execute"),
  positional: z
    .array(z.string())
    .describe(
      'Positional arguments in order. Use "$PREV_ID" as a placeholder for the task ID returned by the previous command (e.g., after a create)',
    ),
  args: z
    .record(z.string())
    .describe("Named arguments as --flag value pairs"),
  flags: z
    .array(z.string())
    .describe("Boolean flags to include"),
});

export type SingleCommand = z.infer<typeof SingleCommandSchema>;

/**
 * LLM interpretation: a message may map to one or more sequential Lattice commands.
 * E.g. "create a bug and assign it to alice" → [create, assign].
 */
export const InterpretationSchema = z.object({
  understood: z
    .boolean()
    .describe("Whether the message maps to one or more Lattice commands"),
  commands: z
    .array(SingleCommandSchema)
    .describe(
      "Ordered list of Lattice commands to execute. Use $PREV_ID in positional args to reference the task ID produced by the previous command.",
    ),
  explanation: z
    .string()
    .describe("Brief explanation of what was understood"),
});

export type Interpretation = z.infer<typeof InterpretationSchema>;

/** Whether a command mutates state (triggers kanban image) */
export const WRITE_COMMANDS = new Set([
  "create",
  "status",
  "update",
  "assign",
  "complete",
  "comment",
  "next",
]);
