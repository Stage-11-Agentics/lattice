import type { ChatMessage, ChatHistory, IncomingMessage } from "./types";

/**
 * Shared message routing logic: prefix detection, stripping, and
 * per-conversation ring buffer. Every adapter embeds one of these.
 */
export class MessageRouter {
  private history = new Map<string, ChatMessage[]>();

  constructor(
    private triggerPrefixes: string[],
    private maxMessages: number,
    private onTriggered: (history: ChatHistory) => Promise<void>,
  ) {}

  /** Feed a raw message. Handles prefix detection, history, and dispatch. */
  async handleMessage(msg: IncomingMessage): Promise<void> {
    const stripped = this.stripPrefix(msg.text);
    const isTriggered = stripped !== null;

    const key = `${msg.conversation.platform}:${msg.conversation.channelId}`;
    this.pushHistory(key, {
      sender: msg.sender,
      text: msg.text,
      timestamp: msg.timestamp,
      isTriggered,
    });

    if (!isTriggered) return;

    const triggered: IncomingMessage = { ...msg, text: stripped!.trim() };
    if (!triggered.text) return;

    await this.onTriggered({
      triggered,
      recentMessages: this.getHistory(key),
    });
  }

  private stripPrefix(text: string): string | null {
    const lower = text.toLowerCase();
    for (const prefix of this.triggerPrefixes) {
      if (lower.startsWith(prefix.toLowerCase())) {
        return text.slice(prefix.length);
      }
    }
    return null;
  }

  private pushHistory(key: string, msg: ChatMessage): void {
    let buf = this.history.get(key);
    if (!buf) {
      buf = [];
      this.history.set(key, buf);
    }
    buf.push(msg);
    while (buf.length > this.maxMessages) {
      buf.shift();
    }
  }

  private getHistory(key: string): ChatMessage[] {
    return [...(this.history.get(key) ?? [])];
  }
}
