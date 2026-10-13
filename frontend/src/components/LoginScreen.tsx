/**
 * Sign-in.
 *
 * Deliberately austere. An EOC console has exactly one job here: get the right
 * person in, tell them plainly what they are allowed to do, and get out of the
 * way. The role selector is present because CUSTOS maps roles to *capabilities*
 * (a viewer may read the twin but may not simulate), and the operator should
 * choose their real role rather than discover the boundary mid-incident.
 */

"use client";

import { useEffect, useState } from "react";

import { auth, session } from "@/lib/api";
import { errorText, useConsole } from "@/lib/store";

import { Logo } from "./Logo";

const ROLES = [
  { id: "viewer", label: "Viewer — liaison / observer (read-only)" },
  { id: "analyst", label: "Analyst — runs models, proposes" },
  { id: "commander", label: "Commander — commits actions to the field" },
  { id: "admin", label: "Administrator — configuration and drills" },
];

export function LoginScreen({ onSignedIn }: { onSignedIn: () => void }) {
  const toast = useConsole((state) => state.toast);
  const [subject, setSubject] = useState("commander.rao");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("commander");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const reason = session.signedOutReason;
    if (reason) setError(reason);
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const tokens = await auth.login(subject.trim(), password, role);
      toast("success", `Signed in as ${tokens.subject}`, `${tokens.role}, ${tokens.permissions.length} permissions granted`);
      onSignedIn();
    } catch (failure) {
      // Never say "user not found": that is an enumeration oracle. The API already
      // answers uniformly, and the console must not be more helpful than the API.
      setError(errorText(failure));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <Logo size={56} />
          <h1>CUSTOS</h1>
          <p>CRISIS DIGITAL TWIN · URBAN FLOOD COMMAND</p>
        </div>

        {error ? <div className="login-error">{error}</div> : null}

        <div className="field">
          <label htmlFor="subject">Operator</label>
          <input
            id="subject"
            type="text"
            value={subject}
            autoComplete="username"
            onChange={(event) => setSubject(event.target.value)}
            placeholder="name.surname"
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="role">Role for this session</label>
          <select id="role" value={role} onChange={(event) => setRole(event.target.value)}>
            {ROLES.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label}
              </option>
            ))}
          </select>
        </div>

        <button className="btn primary" type="submit" disabled={busy || !subject || !password}>
          {busy ? <span className="spin" /> : null}
          {busy ? "Authenticating…" : "Enter the command console"}
        </button>

        <div className="login-hint">
          Every sign-in, refusal and committed action is written to a hash-chained audit trail on this appliance. The console never
          stores your password, and your session is discarded when the tab closes.
        </div>
      </form>
    </div>
  );
}
