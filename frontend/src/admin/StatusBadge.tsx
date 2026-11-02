import type { DocumentStatus } from "../types";

const LABELS: Record<DocumentStatus, string> = {
  pending: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

export function StatusBadge({ status }: { status: DocumentStatus }) {
  return <span className={`badge doc-${status}`}>{LABELS[status]}</span>;
}
