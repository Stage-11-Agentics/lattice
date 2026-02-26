import { loadConfig, type Config } from "./config";
import type { ChatAdapter } from "./adapter";
import type { ChatHistory } from "./types";
import { runWorkflow } from "./workflow/run.tsx";

// --- Load config ---
const configPath = process.argv[2] || "./config.yaml";
let config: Config;
try {
  config = loadConfig(configPath);
} catch (err) {
  console.error(`Failed to load config from ${configPath}:`, err);
  process.exit(1);
}

// --- Create adapters (dynamic import to avoid loading unused SDKs) ---
const adapters: ChatAdapter[] = [];
const botConfig = {
  trigger_prefixes: config.bot.trigger_prefixes,
  history_max_messages: config.bot.history_max_messages,
};

if (config.platforms.signal?.enabled !== false && config.platforms.signal) {
  const { SignalAdapter } = await import("./adapters/signal");
  adapters.push(new SignalAdapter(config.platforms.signal, botConfig));
}

if (config.platforms.discord?.enabled !== false && config.platforms.discord) {
  const { DiscordAdapter } = await import("./adapters/discord");
  adapters.push(new DiscordAdapter(config.platforms.discord, botConfig));
}

if (config.platforms.slack?.enabled !== false && config.platforms.slack) {
  const { SlackAdapter } = await import("./adapters/slack");
  adapters.push(new SlackAdapter(config.platforms.slack, botConfig));
}

if (config.platforms.telegram?.enabled !== false && config.platforms.telegram) {
  const { TelegramAdapter } = await import("./adapters/telegram");
  adapters.push(new TelegramAdapter(config.platforms.telegram, botConfig));
}

if (!adapters.length) {
  console.error("No platforms configured. Check config.yaml.");
  process.exit(1);
}

// --- Start each adapter with a shared message handler ---
const stopFunctions: (() => void)[] = [];

for (const adapter of adapters) {
  const handleMessage = async (history: ChatHistory): Promise<void> => {
    const ts = new Date().toISOString();
    const msg = history.triggered;
    console.log(
      `[${adapter.platform}] [${ts}] ${msg.sender}: "${msg.text}" (${history.recentMessages.length} msgs context)`,
    );

    try {
      const result = await runWorkflow(history, config);

      const imageBuffer = result.kanbanBase64
        ? Buffer.from(result.kanbanBase64, "base64")
        : null;

      await adapter.send(msg.conversation, {
        text: result.text,
        imageBuffer,
      });
      console.log(
        `[${adapter.platform}] [${ts}] Reply sent${imageBuffer ? " (with kanban)" : ""}`,
      );
    } catch (err) {
      console.error(`[${adapter.platform}] [${ts}] Workflow error:`, err);
      try {
        await adapter.send(msg.conversation, {
          text: "Sorry, something went wrong processing that command.",
          imageBuffer: null,
        });
      } catch (sendErr) {
        console.error(
          `[${adapter.platform}] [${ts}] Failed to send error:`,
          sendErr,
        );
      }
    }
  };

  const stop = await adapter.start(handleMessage);
  stopFunctions.push(stop);
  console.log(`[${adapter.platform}] Adapter started`);
}

// --- Graceful shutdown ---
const shutdown = () => {
  console.log("\nShutting down...");
  for (const stop of stopFunctions) stop();
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

console.log(
  `\nLattice Bot running on ${adapters.map((a) => a.platform).join(", ")}`,
);
console.log(`  Lattice root: ${config.lattice.project_root}`);
console.log(`  LLM model: ${config.llm.model}`);
console.log(
  `  Trigger prefixes: ${config.bot.trigger_prefixes.join(", ")}`,
);
