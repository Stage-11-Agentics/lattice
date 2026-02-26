import { readFileSync } from "node:fs";
import { z } from "zod";
import { parse as parseYaml } from "yaml";

const SignalConfigSchema = z.object({
  enabled: z.boolean().default(true),
  api_url: z.string().url(),
  phone_number: z.string().startsWith("+"),
  poll_interval_ms: z.number().default(1000),
  groups: z.array(z.string()).min(1),
});

const DiscordConfigSchema = z.object({
  enabled: z.boolean().default(true),
  bot_token: z.string(),
  channels: z.array(z.string()).min(1),
});

const SlackConfigSchema = z.object({
  enabled: z.boolean().default(true),
  bot_token: z.string(),
  app_token: z.string(),
  channels: z.array(z.string()).min(1),
});

const TelegramConfigSchema = z.object({
  enabled: z.boolean().default(true),
  bot_token: z.string(),
  chats: z.array(z.union([z.string(), z.number()])).min(1),
});

const ConfigSchema = z.object({
  platforms: z
    .object({
      signal: SignalConfigSchema.optional(),
      discord: DiscordConfigSchema.optional(),
      slack: SlackConfigSchema.optional(),
      telegram: TelegramConfigSchema.optional(),
    })
    .refine(
      (p) =>
        Object.values(p).some((v) => v !== undefined && v.enabled !== false),
      "At least one platform must be configured and enabled",
    ),
  lattice: z.object({
    project_root: z.string(),
    actor: z
      .string()
      .regex(/^[a-z]+:.+$/, "Actor must be prefix:identifier"),
  }),
  llm: z.object({
    model: z.string().default("claude-sonnet-4-5-20250929"),
  }),
  bot: z.object({
    name: z.string().default("LatticeBot"),
    help_text: z.string().optional(),
    trigger_prefixes: z
      .array(z.string())
      .default(["@lattice", "/lat"]),
    history_max_messages: z.number().default(50),
  }),
});

export type Config = z.infer<typeof ConfigSchema>;

export function loadConfig(path: string): Config {
  const raw = readFileSync(path, "utf-8");
  const parsed = parseYaml(raw);
  return ConfigSchema.parse(parsed);
}
