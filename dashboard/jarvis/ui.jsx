import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

/** Load one API path; `reload` re-fetches after a write. */
export function useApi(path) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const load = useCallback(() => {
    if (!path) return;
    setState((s) => ({ ...s, loading: true }));
    api.get(path)
      .then((data) => setState({ data, error: null, loading: false }))
      .catch((error) => setState({ data: null, error: error.message, loading: false }));
  }, [path]);
  useEffect(load, [load]);
  return { ...state, reload: load };
}

/** "3 h ago", with the exact time on hover. Every screen shows how old its data is. */
export function Age({ at, prefix = "" }) {
  if (!at) return <span className="age">{prefix}never</span>;
  const then = new Date(at);
  const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
  const text = seconds < 90 ? "just now"
    : seconds < 5400 ? `${Math.round(seconds / 60)} min ago`
    : seconds < 172800 ? `${Math.round(seconds / 3600)} h ago`
    : `${Math.round(seconds / 86400)} days ago`;
  return <span className="age" title={then.toLocaleString()}>{prefix}{text}</span>;
}

export function Loading() {
  return <p style={{ color: "var(--muted)" }}>Loading…</p>;
}

export function Failure({ error }) {
  return <div className="notice"><strong>Could not load this:</strong> {error}</div>;
}

/** Wraps a screen's data: loading, error, or the children with the data. */
export function Screen({ state, children }) {
  if (state.error) return <Failure error={state.error} />;
  if (!state.data) return <Loading />;
  return children(state.data);
}

export function Section({ title, children, right }) {
  return (
    <div className="section">
      <h3 style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <span>{title}</span>{right}
      </h3>
      {children}
    </div>
  );
}

/** A button that runs an async action and shows its outcome or refusal. */
export function Action({ label, run, disabled, why, primary, onDone }) {
  const [state, setState] = useState({ busy: false, message: null, ok: null });
  async function click() {
    setState({ busy: true, message: null, ok: null });
    try {
      const result = await run();
      setState({ busy: false, message: null, ok: true });
      onDone?.(result);
    } catch (error) {
      setState({ busy: false, message: error.message, ok: false });
    }
  }
  return (
    <span className="action">
      <button className={primary ? "primary" : ""} onClick={click}
              disabled={disabled || state.busy} title={disabled ? why : undefined}>
        {state.busy ? "Working…" : label}
      </button>
      {disabled && why && <span className="why">{why}</span>}
      {state.ok === false && <span className="refused">{state.message}</span>}
    </span>
  );
}

export const pct = (n) => `${Math.round(n)}%`;
export const bytes = (n) => n > 1e9 ? `${(n / 1e9).toFixed(1)} GB`
  : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`;
