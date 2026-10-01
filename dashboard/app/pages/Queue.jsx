import React from "react";
import { api } from "../api.js";
import { Link } from "../router.jsx";
import { Action, Age, Screen, Section, useApi } from "../ui.jsx";

/*
 * T070. The queue is a list of links and nothing else: there is no checkbox,
 * no "select all" and no approve button on this page. Each package is opened,
 * read and approved on its own screen (I-07).
 */
export function QueueList() {
  const state = useApi("/queue");
  return (
    <Screen state={state}>
      {(rows) => rows.length === 0 ? (
        <div className="empty"><p>Nothing is waiting for you.</p>
          <p style={{ fontSize: 13 }}>Packages appear here after a run, or when you prepare one
            from an opportunity.</p></div>
      ) : (
        <div className="job-list">
          {rows.map((row) => (
            <Link key={row.package_id} to={`/queue/${row.package_id}`} className="job"
                  style={{ borderLeftColor: row.state === "approved" ? "var(--good)" : "var(--accent)",
                           gridTemplateColumns: "1fr auto" }}>
              <div>
                <div className="title">{row.title}</div>
                <div className="meta">
                  <span>{row.employer}</span><span>·</span><span>{row.kind}</span>
                  <span>·</span><span>{row.route}</span>
                  {row.deadline && <><span>·</span><span>deadline {row.deadline}</span></>}
                </div>
                {row.warnings.map((w, i) => (
                  <div key={i} className="meta" style={{ color: "var(--warn)" }}>{w}</div>
                ))}
              </div>
              <div className="right">
                <span className={`pill ${row.state === "approved" ? "good" : ""}`}>
                  {row.state !== "approved" ? "needs your approval"
                    : row.approved_by === "rule" ? "approved by your rules, not sent"
                    : "approved, not sent"}</span>
                <Age at={row.approved_at || row.generated_at} />
              </div>
            </Link>
          ))}
        </div>
      )}
    </Screen>
  );
}

function TraceView({ pkg }) {
  return pkg.drafts.map((draft) => (
    <Section key={draft.doc_type} title={draft.doc_type.replace(/_/g, " ")}
             right={<a href={`/api/packages/${pkg.id}/documents/${draft.doc_type}`}>
               {draft.doc_type === "reply" ? "text" : "download .docx"}</a>}>
      <div className="trace">
        {draft.lines.map((line, i) => (
          <React.Fragment key={i}>
            <div className={`line ${line.kind}`}>{line.text}</div>
            <div className={`src ${line.origin}`}>
              {line.origin === "profile" && line.sources.map((id) => {
                const record = pkg.records[id];
                return (
                  <div key={id}>
                    <code>{record?.field_path || id.slice(0, 8)}</code>
                    {record && <> — {record.source}{!record.confirmed && " · NOT CONFIRMED"}
                      {!record.current && " · SINCE CORRECTED"}</>}
                  </div>
                );
              })}
              {line.origin === "posting" && "quoted from the posting"}
              {line.origin === "template" && "fixed wording, no claim"}
            </div>
          </React.Fragment>
        ))}
      </div>
    </Section>
  ));
}

export function QueueItem({ id }) {
  const state = useApi(`/packages/${id}`);
  const who = useApi("/whoami");
  const local = who.data?.loopback;

  return (
    <Screen state={state}>
      {(pkg) => (
        <>
          <p><Link to="/queue">← queue</Link></p>
          <h2 style={{ margin: "0 0 4px" }}>{pkg.title}</h2>
          <div style={{ color: "var(--muted)", fontSize: 13.5 }}>
            {pkg.employer} · {pkg.kind} · generated <Age at={pkg.generated_at} />
            {pkg.deadline && <> · deadline {pkg.deadline}</>}
          </div>

          <Section title="Where it goes">
            {pkg.route === "email" ? (
              <>
                <div className="destination">{pkg.destination || "no address"}</div>
                <p className="age">Sent from your own Gmail. This is the complete address — check it.</p>
              </>
            ) : (
              <p style={{ fontSize: 13.5 }}>Nothing is sent by Career Scout. Submit it yourself on the employer's
                site using the worksheet below, then record that you did.</p>
            )}
          </Section>

          {pkg.superseded_by && (
            <div className="notice">A newer version of this package exists.{" "}
              <Link to={`/queue/${pkg.superseded_by}`}>Open it</Link>.</div>
          )}
          {pkg.warnings.map((w, i) => <div key={i} className="notice"><strong>Warning:</strong> {w}</div>)}
          {pkg.qc_verdict !== "pass" && (
            <div className="notice"><strong>Blocked by QC:</strong> {pkg.blocked_reason}</div>
          )}

          <TraceView pkg={pkg} />

          {pkg.qc_findings.length > 0 && (
            <Section title="QC findings">
              <table className="plain">
                <tbody>{pkg.qc_findings.map((f, i) => (
                  <tr key={i}><td><span className={`pill ${f.severity === "FAIL" ? "bad" : "warn"}`}>
                    {f.severity}</span></td><td><code>{f.check}</code></td>
                    <td>{f.reason}{f.line && <div className="age">“{f.line}”</div>}</td></tr>
                ))}</tbody>
              </table>
            </Section>
          )}

          {pkg.omissions.length > 0 && (
            <Section title="Left out because it is not confirmed">
              <ul className="reasons">{pkg.omissions.map((o) => (
                <li key={o.field_path}><code>{o.field_path}</code>: {o.value}</li>))}</ul>
              <p className="age">Confirm these on the Profile screen and prepare the package again
                to include them.</p>
            </Section>
          )}

          <Section title="Your decision">
            <p className="age">Content hash <code>{pkg.content_hash?.slice(0, 16)}…</code> — approving
              approves exactly this.</p>
            {pkg.route === "email" && pkg.state === "awaiting_approval" && (
              <Action label="Approve this application" primary
                      disabled={!local || pkg.qc_verdict !== "pass" || !!pkg.superseded_by}
                      why={!local ? "approval works only on the computer running Career Scout, not over the network"
                        : "this package cannot be approved"}
                      run={() => api.post(`/packages/${pkg.id}/approve`, { content_hash: pkg.content_hash })}
                      onDone={state.reload} />
            )}
            {pkg.route === "email" && pkg.state === "approved" && (
              <Action label="Send now" primary
                      run={() => api.post(`/packages/${pkg.id}/send`, { content_hash: pkg.content_hash })}
                      onDone={state.reload} />
            )}
            {pkg.route === "portal" && pkg.state === "awaiting_approval" && (
              <Action label="I submitted this myself" primary
                      run={() => api.post(`/packages/${pkg.id}/submitted`, {})}
                      onDone={state.reload} />
            )}
            {pkg.state === "submitted" && <span className="pill good">submitted</span>}
          </Section>
        </>
      )}
    </Screen>
  );
}
