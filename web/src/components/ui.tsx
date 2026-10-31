import { useCallback, useEffect, useState } from "react";
import { dueIn } from "../format";

export const STATIONS = ["Case", "Claim pack", "Query", "Payer", "Bank"];

/** The real logo artwork goes in web/public/logo.svg. Until it is there, plain text stands in;
 *  the wordmark is never re-typed in a font. */
export function Logo({ height = 18 }: { height?: number }) {
  const [missing, setMissing] = useState(false);
  if (missing) return <span className="brand-text">Claims to cash</span>;
  return <img src="/logo.svg" alt="Aeternix" height={height} onError={() => setMissing(true)} style={{ display: "block" }} />;
}

/** The arc: case → claim pack → query → payer → bank. */
export function Arc({ station, state, closed, compact, subs }: {
  station: number; state?: string; closed?: boolean; compact?: boolean; subs?: (string | null)[];
}) {
  return (
    <div className={`arc${compact ? " compact" : ""}`} role="img"
         aria-label={`Stage ${station + 1} of 5: ${STATIONS[station]}${state ? `, ${state}` : ""}`}>
      {STATIONS.map((s, i) => {
        const cls = closed || i < station ? "done" : i === station ? "now" : "";
        return (
          <div key={s} className={`station ${cls}`}>
            <span className="dot" />
            {!compact && <>{s}{subs?.[i] && <span className="sub">{subs[i]}</span>}</>}
          </div>
        );
      })}
    </div>
  );
}

export function Due({ at, prefix }: { at: string | null | undefined; prefix?: string }) {
  if (!at) return null;
  const d = dueIn(at);
  return <span className={`clock ${d.tone}`}>{prefix ? `${prefix} ` : ""}{d.text}</span>;
}

export function CheckPill({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="pill">Not checked</span>;
  const label = { red: "Red flags", amber: "Waiting for review", green: "Check passed" }[status] || status;
  return <span className={`pill ${status}`}>{label}</span>;
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error" role="alert">{error}</div> : null;
}

/** Load data, expose reload, keep the last error for display. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    return fn().then((d) => { setData(d); setError(null); })
      .catch((e: Error) => setError(e.message)).finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { load(); }, [load]);
  return { data, setData, error, setError, loading, reload: load };
}

/** Run an action, show its error, and report busy state. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true); setError(null);
    try { return await fn(); } catch (e) { setError((e as Error).message); return undefined; } finally { setBusy(false); }
  }, []);
  return { busy, error, setError, run };
}
