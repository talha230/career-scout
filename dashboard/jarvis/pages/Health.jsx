import React from "react";
import { Age, Screen, Section, bytes, useApi } from "../ui.jsx";

// T073. Everything that can stop working quietly, and how long ago it last worked.
export default function Health() {
  const state = useApi("/system/health");
  return (
    <Screen state={state}>
      {(h) => (
        <>
          {h.backups.alert && <div className="notice"><strong>Backup:</strong> {h.backups.alert}</div>}
          {h.stale_kinds.length > 0 && (
            <div className="notice"><strong>Stale:</strong> no successful {h.stale_kinds.join(", ")}{" "}
              run in {h.alert_after_hours} hours.</div>
          )}
          {!h.mailbox.connected && <div className="notice"><strong>Mailbox:</strong> {h.mailbox.message}</div>}
          {h.unreconciled_sends.length > 0 && (
            <div className="notice"><strong>{h.unreconciled_sends.length} send(s) need reconciling.</strong>{" "}
              They reached the send step but no result was recorded. Check your Gmail Sent folder —
              Jarvis will never send them again on its own.</div>
          )}

          <Section title="Runs">
            <table className="plain">
              <thead><tr><th>Kind</th><th>Last started</th><th>Status</th><th>Error</th></tr></thead>
              <tbody>{h.latest_runs.map((r) => (
                <tr key={r.kind}><td>{r.kind}</td><td><Age at={r.started_at} /></td>
                  <td><span className={`pill ${r.status === "ok" ? "good" : r.status === "failed" ? "bad" : "warn"}`}>
                    {r.status}</span></td><td className="age">{r.error}</td></tr>))}
                {h.latest_runs.length === 0 && <tr><td colSpan={4} className="age">Nothing has run yet.</td></tr>}
              </tbody>
            </table>
          </Section>

          <Section title="Backups">
            <div className="kv">
              <span>Newest</span><span>{h.backups.newest
                ? <><Age at={h.backups.newest.taken_at} /> · {h.backups.newest.status} · {bytes(h.backups.newest.size_bytes)}</>
                : "none"}</span>
              <span>Last restore drill</span><span>{h.backups.last_drill
                ? <><Age at={h.backups.last_drill.restored_at} /> · {h.backups.last_drill.restore_result}</>
                : "never — a backup never restored is not yet a backup"}</span>
              <span>Key file</span><span><code>{h.backups.key_file}</code></span>
            </div>
          </Section>

          <Section title="Disk">
            <div className="kv">
              <span>Database</span><span>{bytes(h.disk.database_bytes)}</span>
              <span>Documents</span><span>{bytes(h.disk.documents_bytes)}</span>
              <span>Snapshots</span><span>{bytes(h.disk.snapshots_bytes)}</span>
              <span>Backups</span><span>{bytes(h.disk.backups_bytes)}</span>
              <span>Location</span><span><code>{h.disk.data_directory}</code></span>
            </div>
          </Section>

          <Section title="Setup still outstanding">
            {h.onboarding.outstanding.length === 0 ? <p className="age">Nothing.</p> : (
              <ul className="checklist">{h.onboarding.steps.filter((s) => !s.done).map((s) => (
                <li key={s.key}><strong>{s.label}</strong><div className="age">If skipped: {s.if_skipped}</div></li>))}
              </ul>
            )}
          </Section>

          <div className="age">Version {h.version} · config {h.config_version} ({h.config_fingerprint}) ·
            as of <Age at={h.now} /></div>
        </>
      )}
    </Screen>
  );
}
