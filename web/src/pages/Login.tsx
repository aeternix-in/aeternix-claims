import { FormEvent, useState } from "react";
import { api, setToken } from "../api";
import type { Me } from "../App";
import { ErrorNote, Logo } from "../components/ui";

export default function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await api.post<{ token: string }>("/api/auth/login", { email, password });
      setToken(r.token);
      onSignedIn(await api.get<Me>("/api/auth/me"));
    } catch (err) {
      setError((err as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <div className="login">
      <form onSubmit={submit}>
        <div className="brand"><Logo height={24} /></div>
        <h1>Claims to cash</h1>
        <p className="muted">Every claim, from the counter to the bank.</p>
        <label>Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <ErrorNote error={error} />
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
