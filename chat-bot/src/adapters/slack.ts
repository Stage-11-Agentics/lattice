import { App } from "@slack/bolt";
import type { ChatAdapter } from "../adapter";
import type {
  ChatHistory,
  OutgoingMessage,
  ConversationId,
  IncomingMessage,
} from "../types";
import { MessageRouter } from "../router";

interface SlackConfig {
  bot_token: string;
  app_token: string;
  channels: string[];
}

interface BotConfig {
  trigger_prefixes: string[];
  history_max_messages: number;
}

export class SlackAdapter implements ChatAdapter {
  readonly platform = "slack";
  private app: App | null = null;

  constructor(
    private slackConfig: SlackConfig,
    private botConfig: BotConfig,
  ) {}

  async start(
    onMessage: (h: ChatHistory) => Promise<void>,
  ): Promise<() => void> {
    const allowedChannels = new Set(this.slackConfig.channels);
    const router = new MessageRouter(
      this.botConfig.trigger_prefixes,
      this.botConfig.history_max_messages,
      onMessage,
    );

    const app = new App({
      token: this.slackConfig.bot_token,
      appToken: this.slackConfig.app_token,
      socketMode: true,
    });
    this.app = app;

    app.message(async ({ message }) => {
      if (message.subtype) return;
      if (!("text" in message) || !message.text) return;
      if (!("channel" in message)) return;
      if (!allowedChannels.has(message.channel)) return;

      const msg: IncomingMessage = {
        text: message.text,
        sender: (message as any).user || "unknown",
        senderId: (message as any).user || "",
        conversation: {
          platform: "slack",
          channelId: message.channel,
        },
        timestamp: parseFloat((message as any).ts || "0") * 1000,
      };
      await router.handleMessage(msg);
    });

    await app.start();
    console.log("[slack] Bot connected via Socket Mode");

    return async () => {
      await app.stop();
      this.app = null;
    };
  }

  async send(
    conversation: ConversationId,
    message: OutgoingMessage,
  ): Promise<void> {
    if (!this.app) throw new Error("Slack app not started");
    const client = this.app.client;

    await client.chat.postMessage({
      channel: conversation.channelId,
      text: message.text,
    });

    if (message.imageBuffer) {
      await client.filesUploadV2({
        channel_id: conversation.channelId,
        file: message.imageBuffer,
        filename: "kanban.png",
        title: "Kanban Board",
      });
    }
  }
}
