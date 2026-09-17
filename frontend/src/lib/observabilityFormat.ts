/**
 * Display-only formatting for observability data.
 *
 * The API keeps ISO-8601 timestamps and millisecond durations as the stable
 * machine contract. This module converts them at the UI boundary so tables
 * remain readable without changing JSONL or trace payloads.
 */

function numeric(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(parsed) ? parsed : undefined;
}

/** Format an ISO timestamp in the viewer's local timezone. */
export function formatObservabilityTime(value: unknown, fallback = "-"): string {
  if (value == null || value === "") return fallback;
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return fallback;
  const parts = [date.getFullYear(), date.getMonth() + 1, date.getDate()].map((part, index) =>
    index === 0 ? String(part) : String(part).padStart(2, "0")
  );
  const clock = [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return `${parts.join("-")} ${clock}`;
}

/** Convert an API millisecond duration to seconds for display. */
export function formatDurationSeconds(value: unknown, fallback = "-"): string {
  const milliseconds = numeric(value);
  if (milliseconds === undefined) return fallback;
  const seconds = Math.max(0, milliseconds) / 1000;
  const rendered = seconds.toFixed(3).replace(/\.?(0+)$/, "");
  return `${rendered} s`;
}

