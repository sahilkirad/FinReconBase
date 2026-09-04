import { StatusPill } from "@/components/ui/status-pill";

export function MilestonePlaceholder({
  title,
  milestone,
  summary,
  bullets,
}: {
  title: string;
  milestone: string;
  summary: string;
  bullets: string[];
}) {
  return (
    <section className="mx-auto max-w-3xl py-10">
      <StatusPill tone="pending">{milestone}</StatusPill>
      <h1 className="mt-3 text-2xl font-semibold text-navy">{title}</h1>
      <p className="mt-2 text-sm leading-relaxed text-slate-500">{summary}</p>
      <div className="mt-6 space-y-2">
        {bullets.map((b) => (
          <div
            key={b}
            className="flex items-start gap-2 rounded-lg border border-line bg-white p-3 text-sm text-slate-600"
          >
            <span className="mt-0.5 text-primary">◆</span>
            <span>{b}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
