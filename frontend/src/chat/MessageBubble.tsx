import type { ChatMessage } from "../types";

function dollars(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

type Props = {
  message: ChatMessage;
  showDetails?: boolean;
  onRate?: (value: 1 | -1) => void;
};

export function MessageBubble({ message, onRate, showDetails }: Props) {
  const meta = message.meta;
  return (
    <div className={`bubble ${message.role}`}>
      <p>{message.content}</p>

      {message.order && (
        <div className="attachment order">
          <strong>Order {message.order.order_number}</strong>
          <span className={`badge ${message.order.status}`}>{message.order.status}</span>
          <span>{dollars(message.order.total_cents)}</span>
        </div>
      )}

      {message.ticket && (
        <div className="attachment ticket">
          <strong>Ticket created</strong>
          <span>#{message.ticket.id.slice(0, 8)}</span>
          <span className="badge open">{message.ticket.status}</span>
          <div>{message.ticket.subject}</div>
        </div>
      )}

      {message.citations && message.citations.length > 0 && (
        <div className="attachment citations">
          <strong>Sources</strong>
          <ul>
            {message.citations.map((c, i) => (
              <li key={`${c.document_id}-${i}`}>
                <em>{c.title}</em>: {c.snippet}
              </li>
            ))}
          </ul>
        </div>
      )}

      {showDetails && meta && meta.outcome && (
        <details className="why">
          <summary>Why this answer?</summary>
          <dl className="meta">
            <dt>Understood as</dt>
            <dd>
              {meta.intent}
              {typeof meta.confidence === "number" ? ` (${Math.round(meta.confidence * 100)}% sure)` : ""}
            </dd>
            <dt>Outcome</dt>
            <dd>{meta.outcome}</dd>
            <dt>Speed</dt>
            <dd>
              {meta.latency_ms} ms · {meta.llm_calls} model calls · {meta.total_tokens} tokens
              {meta.cache_hit ? " · retrieval cached" : ""}
            </dd>
            <dt>Passages considered</dt>
            <dd>
              {meta.retrieved && meta.retrieved.length > 0
                ? meta.retrieved.map((r) => `${r.title} (${r.score.toFixed(2)})`).join(", ")
                : "none"}
            </dd>
          </dl>
        </details>
      )}

      {onRate && (
        <div className="rating" aria-label="Rate this answer">
          <button
            className={message.feedback === 1 ? "on" : ""}
            onClick={() => onRate(1)}
            aria-pressed={message.feedback === 1}
            title="Helpful"
          >
            👍
          </button>
          <button
            className={message.feedback === -1 ? "on" : ""}
            onClick={() => onRate(-1)}
            aria-pressed={message.feedback === -1}
            title="Not helpful"
          >
            👎
          </button>
        </div>
      )}
    </div>
  );
}
