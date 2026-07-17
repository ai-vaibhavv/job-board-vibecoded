import { useState, type ReactNode } from "react";

/** Score badge. Colour comes from the API (`score_color`), which reuses the same
 * green/blue/orange thresholds as the Discord cards — one visual language. */
export function ScoreBadge({ score, color, size = "sm" }: { score: number; color: string; size?: "sm" | "lg" }) {
  const dims = size === "lg" ? "h-11 w-11 text-base" : "h-9 w-9 text-xs";
  return (
    <div
      className={`flex ${dims} shrink-0 flex-col items-center justify-center rounded-lg font-bold leading-none text-white`}
      style={{ backgroundColor: color }}
      title={`Relevance score ${score}/100`}
    >
      <span>{score}</span>
      {size === "lg" && <span className="mt-0.5 text-[9px] font-medium opacity-80">/100</span>}
    </div>
  );
}

export function Chip({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        muted
          ? "bg-surface-sunken text-ink-muted"
          : "bg-accent/10 text-accent"
      }`}
    >
      {children}
    </span>
  );
}

/** Org logo with a graceful letter fallback when the favicon 404s. */
export function Logo({ src, name, size = 40 }: { src: string | null; name: string | null; size?: number }) {
  const [broken, setBroken] = useState(false);
  const letter = (name ?? "?").trim().charAt(0).toUpperCase() || "?";
  if (!src || broken) {
    return (
      <div
        className="flex shrink-0 items-center justify-center rounded-lg bg-surface-sunken font-semibold text-ink-muted"
        style={{ width: size, height: size, fontSize: size * 0.4 }}
      >
        {letter}
      </div>
    );
  }
  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      onError={() => setBroken(true)}
      className="shrink-0 rounded-lg bg-white object-contain"
      style={{ width: size, height: size }}
    />
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
    </svg>
  );
}
