/** Platform-agnostic identifier for a conversation (group, channel, chat) */
export interface ConversationId {
  platform: string;
  channelId: string;
}

/** A parsed inbound message, platform-neutral */
export interface IncomingMessage {
  text: string;
  sender: string;
  senderId: string;
  conversation: ConversationId;
  timestamp: number;
}

/** A single chat message stored in the history ring buffer */
export interface ChatMessage {
  sender: string;
  text: string;
  timestamp: number;
  isTriggered: boolean;
}

/** Chat history for a conversation, passed to the workflow */
export interface ChatHistory {
  triggered: IncomingMessage;
  recentMessages: ChatMessage[];
}

/** What the workflow produces — each adapter knows how to send this */
export interface OutgoingMessage {
  text: string;
  imageBuffer: Buffer | null;
}
