import type { ChatResponse } from "../types";
import { ApiError, getToken, notifyUnauthorized } from "./client";

export type StageEvent = { node: string; label: string };

type ServerEvent = { name: string; data: unknown };

/** Split an SSE byte stream into events. Events are separated by a blank line. */
function parseBlock(block: string): ServerEvent | null {
  let name = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (data.length === 0) return null;
  try {
    return { name, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

/**
 * Send a chat message and report each pipeline step as it finishes.
 * Resolves with the final reply; rejects with ApiError for HTTP or in-stream errors.
 */
export async function streamMessage(
  content: string,
  conversationId: string | null,
  onStage: (stage: StageEvent) => void,
): Promise<ChatResponse> {
  const token = getToken();
  const response = await fetch("/chat/messages/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ content, conversation_id: conversationId }),
  });

  if (!response.ok || !response.body) {
    if (response.status === 401 && token) notifyUnauthorized();
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: ChatResponse | null = null;

  const handle = (block: string) => {
    const event = parseBlock(block);
    if (!event) return;
    if (event.name === "stage") {
      onStage(event.data as StageEvent);
    } else if (event.name === "done") {
      result = event.data as ChatResponse;
    } else if (event.name === "error") {
      const err = event.data as { status?: number; detail?: string };
      throw new ApiError(err.status ?? 500, err.detail ?? "Something went wrong");
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handle(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) handle(buffer);

  if (!result) throw new ApiError(502, "The reply was interrupted. Please try again.");
  return result;
}
