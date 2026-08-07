import { Bot, InputFile } from "grammy";
import type { ChatAdapter } from "../adapter";
import type {
  ChatHistory,
  OutgoingMessage,
  ConversationId,
  IncomingMessage,
} from "../types";
import { MessageRouter } from "../router";

interface TelegramConfig {
  bot_token: string;
  chats: (string | number)[];
}

interface BotConfig {
  trigger_prefixes: string[];
  history_max_messages: number;
}

export class TelegramAdapter implements ChatAdapter {
  readonly platform = "telegram";
  private bot: Bot | null = null;

  constructor(
    private telegramConfig: TelegramConfig,
    private botConfig: BotConfig,
  ) {}

  async start(
    onMessage: (h: ChatHistory) => Promise<void>,
  ): Promise<() => void> {
    const allowedChats = new Set(
      this.telegramConfig.chats.map(String),
    );
    const router = new MessageRouter(
      this.botConfig.trigger_prefixes,
      this.botConfig.history_max_messages,
      onMessage,
    );

    const bot = new Bot(this.telegramConfig.bot_token);
    this.bot = bot;

    bot.on("message:text", async (ctx) => {
      const chatId = String(ctx.chat.id);
      if (!allowedChats.has(chatId)) return;

      const msg: IncomingMessage = {
        text: ctx.message.text,
        sender:
          ctx.from.first_name +
          (ctx.from.last_name ? ` ${ctx.from.last_name}` : ""),
        senderId: String(ctx.from.id),
        conversation: { platform: "telegram", channelId: chatId },
        timestamp: ctx.message.date * 1000,
      };
      await router.handleMessage(msg);
    });

    bot.start();
    console.log("[telegram] Bot started (long-polling)");

    return () => {
      bot.stop();
      this.bot = null;
    };
  }

  async send(
    conversation: ConversationId,
    message: OutgoingMessage,
  ): Promise<void> {
    if (!this.bot) throw new Error("Telegram bot not started");
    const chatId = conversation.channelId;

    await this.bot.api.sendMessage(chatId, message.text);

    if (message.imageBuffer) {
      await this.bot.api.sendPhoto(
        chatId,
        new InputFile(message.imageBuffer, "kanban.png"),
      );
    }
  }
}
