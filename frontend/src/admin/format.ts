export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const ACCEPTED_EXTENSIONS = [".md", ".markdown", ".txt", ".pdf", ".docx"];
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
