import type { ChatAdapter } from "../adapter";
import type {
  ChatHistory,
  OutgoingMessage,
  ConversationId,
  IncomingMessage,
} from "../types";
import { MessageRouter } from "../router";

interface SignalConfig {
  api_url: string;
  phone_number: string;
  poll_interval_ms: number;
  groups: string[];
}

interface BotConfig {
  trigger_prefixes: string[];
  history_max_messages: number;
}

interface SignalEnvelope {
  account: string;
  envelope: {
    source: string;
    sourceUuid: string;
    sourceName?: string;
    timestamp: number;
    dataMessage?: {
      message: string;
      groupInfo?: { groupId: string; type: string };
      timestamp: number;
    };
    syncMessage?: {
      sentMessage?: {
        message: string;
        groupInfo?: { groupId: string };
        timestamp: number;
      };
    };
  };
}

export class SignalAdapter implements ChatAdapter {
  readonly platform = "signal";

  constructor(
    private signalConfig: SignalConfig,
    private botConfig: BotConfig,
  ) {}

  async start(
    onMessage: (h: ChatHistory) => Promise<void>,
  ): Promise<() => void> {
    const allowedGroups = new Set(this.signalConfig.groups);
    const router = new MessageRouter(
      this.botConfig.trigger_prefixes,
      this.botConfig.history_max_messages,
      onMessage,
    );

    let running = true;

    const loop = async () => {
      while (running) {
        try {
          const envelopes = await this.receive();
          for (const env of envelopes) {
            const msg = this.parseEnvelope(env);
            if (!msg) continue;
            if (!allowedGroups.has(msg.conversation.channelId)) continue;
            await router.handleMessage(msg);
          }
        } catch (err) {
          console.error("[signal] Poll error:", err);
        }
        await Bun.sleep(this.signalConfig.poll_interval_ms);
      }
    };

    loop();
    return () => {
      running = false;
    };
  }

  async send(
    conversation: ConversationId,
    message: OutgoingMessage,
  ): Promise<void> {
    const body: Record<string, unknown> = {
      message: message.text,
      number: this.signalConfig.phone_number,
      recipients: [conversation.channelId],
    };
    if (message.imageBuffer) {
      body.base64_attachments = [message.imageBuffer.toString("base64")];
    }
    const resp = await fetch(`${this.signalConfig.api_url}/v2/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Signal send failed: ${resp.status} ${text}`);
    }
  }

  private async receive(): Promise<SignalEnvelope[]> {
    const url = `${this.signalConfig.api_url}/v1/receive/${encodeURIComponent(this.signalConfig.phone_number)}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Signal receive failed: ${resp.status}`);
    return resp.json();
  }

  private parseEnvelope(env: SignalEnvelope): IncomingMessage | null {
    const data = env.envelope.dataMessage;
    if (!data?.message || !data.groupInfo?.groupId) return null;
    return {
      text: data.message,
      sender: env.envelope.sourceName || env.envelope.source,
      senderId: env.envelope.sourceUuid,
      conversation: {
        platform: "signal",
        channelId: data.groupInfo.groupId,
      },
      timestamp: data.timestamp,
    };
  }
}
