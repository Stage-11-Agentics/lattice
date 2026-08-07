import type { ChatHistory, OutgoingMessage, ConversationId } from "./types";

export interface ChatAdapter {
  readonly platform: string;

  /**
   * Start listening for messages.
   * Calls `onMessage` when a triggered message arrives with full chat history.
   * Returns a stop function.
   */
  start(
    onMessage: (history: ChatHistory) => Promise<void>,
  ): Promise<() => void>;

  /** Send a reply to a conversation. */
  send(
    conversation: ConversationId,
    message: OutgoingMessage,
  ): Promise<void>;
}
