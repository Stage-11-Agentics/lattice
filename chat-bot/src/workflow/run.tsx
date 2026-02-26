import {
  createSmithers,
  runWorkflow as smithersRun,
} from "smithers-orchestrator";
import type { Config } from "../config";
import type { ChatHistory, ChatMessage } from "../types";
import {
  InterpretationSchema,
  WRITE_COMMANDS,
  type SingleCommand,
} from "./schemas";
import { LatticeInterpreterAgent } from "./agent";
import { buildCommandString, executeLatticeCommand } from "./execute";
import { formatLatticeResult } from "../formatter";
import { generateKanbanMermaid } from "../kanban/generate";
import { renderMermaidToBase64 } from "../kanban/render";

export interface WorkflowResult {
  /** Text message to send back */
  text: string;
  /** Base64-encoded PNG of kanban board (only for write commands) */
  kanbanBase64: string | null;
}

// Create Smithers instance with our schema registry.
// This sets up SQLite persistence for durable workflow state.
const { Workflow, Task, smithers, outputs } = createSmithers(
  {
    interpret: InterpretationSchema,
  },
  { dbPath: "./smithers.db" },
);

/**
 * Format chat history into a readable conversation transcript for the LLM.
 */
function formatChatContext(messages: ChatMessage[]): string {
  if (messages.length === 0) return "(no prior messages)";

  return messages
    .map((m) => {
      const time = new Date(m.timestamp).toLocaleTimeString();
      const prefix = m.isTriggered ? "[TRIGGERED] " : "";
      return `[${time}] ${prefix}${m.sender}: ${m.text}`;
    })
    .join("\n");
}

/**
 * Build a Smithers workflow definition for message interpretation.
 * The workflow has one agent task: Claude interprets the NL message
 * (with full chat history context) into a list of structured LatticeCommands.
 */
function buildInterpretWorkflow(config: Config) {
  const agent = new LatticeInterpreterAgent(config.llm.model);

  return smithers((ctx) => {
    const input = ctx.input as any;
    const prompt = [
      `## Recent Chat History`,
      ``,
      input.chatContext,
      ``,
      `## Triggered Message`,
      `From: ${input.sender}`,
      `Message: "${input.text}"`,
      ``,
      `Interpret this message (considering the chat history for context) as one or more Lattice commands.`,
    ].join("\n");

    return (
      <Workflow name="lattice-chat-bot">
        <Task id="interpret" output={outputs.interpret} agent={agent}>
          {prompt}
        </Task>
      </Workflow>
    );
  });
}

/**
 * Substitute $PREV_ID placeholders in a command's positional args.
 */
function substituteId(cmd: SingleCommand, prevId: string | null): SingleCommand {
  if (!prevId) return cmd;
  return {
    ...cmd,
    positional: cmd.positional.map((arg) =>
      arg === "$PREV_ID" ? prevId : arg,
    ),
  };
}

/**
 * Extract a task ID from a Lattice CLI JSON response.
 * Looks for short_id first, then falls back to full id.
 */
function extractTaskId(parsed: any): string | null {
  if (!parsed?.ok) return null;
  const data = parsed.data;
  return data?.short_id || data?.id || null;
}

/**
 * Run the full workflow for a chat message with chat history context:
 * 1. Smithers workflow: Claude interprets NL + context → list of LatticeCommands
 * 2. Execute each command sequentially, chaining $PREV_ID
 * 3. If any write command, generate kanban board PNG
 * 4. Return aggregated text + optional image
 */
export async function runWorkflow(
  history: ChatHistory,
  config: Config,
): Promise<WorkflowResult> {
  const msg = history.triggered;
  const chatContext = formatChatContext(history.recentMessages);
  console.log(
    `[workflow] Processing: "${msg.text}" from ${msg.sender} (${history.recentMessages.length} msgs context)`,
  );

  // Step 1: Smithers-orchestrated interpretation with chat history
  const workflow = buildInterpretWorkflow(config);
  const result = await smithersRun(workflow, {
    input: {
      text: msg.text,
      sender: msg.sender,
      senderId: msg.senderId,
      platform: msg.conversation.platform,
      channelId: msg.conversation.channelId,
      timestamp: String(msg.timestamp),
      chatContext,
    },
  });

  if (result.status !== "finished") {
    console.error(
      `[workflow] Smithers run ${result.status}:`,
      (result as any).error,
    );
    return {
      text: "Sorry, I couldn't process that command.",
      kanbanBase64: null,
    };
  }

  // Extract the interpretation output row
  const rows = (result.output as any[]) ?? [];
  const interpretation = rows[0];

  if (!interpretation) {
    return {
      text: "Sorry, I couldn't interpret that message.",
      kanbanBase64: null,
    };
  }

  const { understood, commands, explanation } = interpretation;
  console.log(
    `[workflow] Interpreted: ${commands?.length ?? 0} command(s), understood=${understood}`,
  );

  // Not understood
  if (!understood || !commands?.length || commands[0].command === "none") {
    return {
      text:
        explanation ||
        config.bot.help_text ||
        "I didn't understand that. Try @lattice help",
      kanbanBase64: null,
    };
  }

  // Help
  if (commands.length === 1 && commands[0].command === "help") {
    return {
      text:
        config.bot.help_text ||
        [
          "Available commands:",
          "  create - Create a new task",
          "  list - List tasks (with filters)",
          "  show <id> - Show task details",
          "  status <id> <status> - Change status",
          "  assign <id> <actor> - Assign task",
          "  complete <id> - Mark as done",
          "  comment <id> - Add a comment",
          "  next - Pick next task",
          "  weather - Project health",
          "  stats - Project statistics",
        ].join("\n"),
      kanbanBase64: null,
    };
  }

  // Step 2: Execute commands sequentially with $PREV_ID chaining
  const latticeConfig = {
    project_root: config.lattice.project_root,
    actor: config.lattice.actor,
  };

  const resultTexts: string[] = [];
  let prevId: string | null = null;
  let anyWrite = false;

  for (let i = 0; i < commands.length; i++) {
    const raw = commands[i];
    const cmd = substituteId(raw, prevId);

    const cmdStr = buildCommandString(cmd, latticeConfig);
    console.log(`[workflow] Executing [${i + 1}/${commands.length}]: ${cmdStr}`);

    const execResult = executeLatticeCommand(cmdStr, latticeConfig);
    const formatted = formatLatticeResult(cmd.command, execResult.parsed);
    resultTexts.push(formatted);

    // Track the produced task ID for chaining
    const taskId = extractTaskId(execResult.parsed);
    if (taskId) prevId = taskId;

    if (WRITE_COMMANDS.has(cmd.command)) anyWrite = true;

    // Stop executing further commands if one fails
    if (!execResult.ok) {
      console.warn(`[workflow] Command ${i + 1} failed, stopping chain`);
      break;
    }
  }

  const text = resultTexts.join("\n\n");

  // Step 3: Generate kanban image if any command was a write
  let kanbanBase64: string | null = null;
  if (anyWrite) {
    console.log("[workflow] Generating kanban board image...");
    const mermaid = generateKanbanMermaid(config.lattice.project_root);
    if (mermaid) {
      kanbanBase64 = await renderMermaidToBase64(mermaid);
      if (kanbanBase64) {
        console.log("[workflow] Kanban image generated");
      } else {
        console.warn("[workflow] Failed to render kanban image");
      }
    }
  }

  return { text, kanbanBase64 };
}
