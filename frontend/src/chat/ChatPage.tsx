import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { getConversationMessages, listConversations, setFeedback } from "../api/client";
import { streamMessage } from "../api/stream";
import type { ChatMessage, ConversationSummary } from "../types";
import { MessageBubble } from "./MessageBubble";

export function ChatPage({ isAdmin }: { isAdmin: boolean }) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [stages, setStages] = useState<string[]>([]);
  const [loadingThread, setLoadingThread] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await listConversations());
    } catch {
      /* the sidebar is a convenience; chat still works without it */
    }
  }, []);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function openConversation(id: string) {
    if (id === conversationId || busy) return;
    setLoadingThread(true);
    setError(null);
    try {
      const stored = await getConversationMessages(id);
      setMessages(
        stored
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({
            id: m.id,
            role: m.role as "user" | "assistant",
            content: m.content,
            citations: m.citations ?? undefined,
            ticket: m.ticket,
            order: m.order,
            feedback: m.feedback,
            meta: m.meta,
          })),
      );
      setConversationId(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open that conversation");
    } finally {
      setLoadingThread(false);
    }
  }

  function newChat() {
    if (busy) return;
    setConversationId(null);
    setMessages([]);
    setError(null);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || busy) return;
    setDraft("");
    setError(null);
    setBusy(true);
    setStages([]);
    setMessages((prev) => [...prev, { id: `local-${crypto.randomUUID()}`, role: "user", content }]);
    try {
      const reply = await streamMessage(content, conversationId, (stage) =>
        setStages((prev) => [...prev, stage.label]),
      );
      setConversationId(reply.conversation_id);
      setMessages((prev) => [
        ...prev,
        {
          id: reply.message_id ?? `local-${crypto.randomUUID()}`,
          role: "assistant",
          content: reply.content,
          citations: reply.citations,
          ticket: reply.ticket,
          order: reply.order,
          feedback: null,
          meta: reply.meta,
        },
      ]);
      void refreshConversations();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Message failed");
      setDraft(content); // let the customer retry without retyping
    } finally {
      setBusy(false);
      setStages([]);
    }
  }

  async function rate(messageId: string, current: number | null | undefined, value: 1 | -1) {
    const next = current === value ? null : value; // clicking the active rating clears it
    setMessages((prev) => prev.map((m) => (m.id === messageId ? { ...m, feedback: next } : m)));
    try {
      await setFeedback(messageId, next);
    } catch {
      setMessages((prev) => prev.map((m) => (m.id === messageId ? { ...m, feedback: current ?? null } : m)));
    }
  }

  return (
    <div className="chat-layout">
      <aside className="history">
        <button className="primary" onClick={newChat} disabled={busy}>
          + New chat
        </button>
        <ul>
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                className={c.id === conversationId ? "active" : ""}
                onClick={() => void openConversation(c.id)}
                title={c.title}
              >
                {c.title}
              </button>
            </li>
          ))}
          {conversations.length === 0 && <li className="hint">No conversations yet</li>}
        </ul>
      </aside>

      <div className="chat">
        <div className="messages">
          {loadingThread && <p className="hint">Loading…</p>}
          {!loadingThread && messages.length === 0 && (
            <p className="hint">
              Try: “What is your return policy?”, “Where is order ACM-1001?”, or “I need a refund
              exception”.
            </p>
          )}
          {messages.map((m) => (
            <MessageBubble
              key={m.id}
              message={m}
              showDetails={isAdmin}
              onRate={
                m.role === "assistant" && !m.id.startsWith("local-")
                  ? (value) => void rate(m.id, m.feedback, value)
                  : undefined
              }
            />
          ))}
          {busy && (
            <div className="progress" aria-live="polite">
              {stages.map((label, i) => (
                <div key={i} className="done-step">
                  ✓ {label}
                </div>
              ))}
              <div className="hint">Working on it…</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        {error && <p className="error">{error}</p>}
        <form className="composer" onSubmit={onSubmit}>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Ask a question…"
            disabled={busy}
          />
          <button type="submit" disabled={busy || !draft.trim()}>
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
