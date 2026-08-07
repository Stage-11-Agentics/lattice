import { Client, GatewayIntentBits, AttachmentBuilder } from "discord.js";
import type { ChatAdapter } from "../adapter";
import type {
  ChatHistory,
  OutgoingMessage,
  ConversationId,
  IncomingMessage,
} from "../types";
import { MessageRouter } from "../router";

interface DiscordConfig {
  bot_token: string;
  channels: string[];
}

interface BotConfig {
  trigger_prefixes: string[];
  history_max_messages: number;
}

export class DiscordAdapter implements ChatAdapter {
  readonly platform = "discord";
  private client: Client | null = null;

  constructor(
    private discordConfig: DiscordConfig,
    private botConfig: BotConfig,
  ) {}

  async start(
    onMessage: (h: ChatHistory) => Promise<void>,
  ): Promise<() => void> {
    const allowedChannels = new Set(this.discordConfig.channels);
    const router = new MessageRouter(
      this.botConfig.trigger_prefixes,
      this.botConfig.history_max_messages,
      onMessage,
    );

    const client = new Client({
      intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
      ],
    });
    this.client = client;

    client.on("messageCreate", async (discordMsg) => {
      if (discordMsg.author.bot) return;
      if (!allowedChannels.has(discordMsg.channelId)) return;

      const msg: IncomingMessage = {
        text: discordMsg.content,
        sender:
          discordMsg.member?.displayName ||
          discordMsg.author.displayName ||
          discordMsg.author.username,
        senderId: discordMsg.author.id,
        conversation: {
          platform: "discord",
          channelId: discordMsg.channelId,
        },
        timestamp: discordMsg.createdTimestamp,
      };
      await router.handleMessage(msg);
    });

    await client.login(this.discordConfig.bot_token);
    console.log(`[discord] Bot logged in as ${client.user?.tag}`);

    return () => {
      client.destroy();
      this.client = null;
    };
  }

  async send(
    conversation: ConversationId,
    message: OutgoingMessage,
  ): Promise<void> {
    if (!this.client) throw new Error("Discord client not started");
    const channel = await this.client.channels.fetch(conversation.channelId);
    if (!channel?.isTextBased() || !("send" in channel)) {
      throw new Error(`Cannot send to channel ${conversation.channelId}`);
    }

    const opts: Record<string, unknown> = { content: message.text };
    if (message.imageBuffer) {
      opts.files = [
        new AttachmentBuilder(message.imageBuffer, { name: "kanban.png" }),
      ];
    }
    await (channel as any).send(opts);
  }
}
