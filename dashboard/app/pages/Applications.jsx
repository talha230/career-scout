import React, { useState } from "react";
import { api, safeHref } from "../api.js";
import { Link, navigate } from "../router.jsx";
import { Action, Age, Screen, Section, useApi } from "../ui.jsx";

const STATUSES = ["submitted", "acknowledged", "info_requested", "interview", "offer",
                  "rejected", "withdrawn"];
export const CLASSES = ["acknowledgement", "rejection", "interview_invite", "offer",
                        "information_request", "unclassified"];

export function ApplicationList() {
  const state = useApi("/applications");
  return (
    <Screen state={state}>
      {(rows) => rows.length === 0 ? (
        <div className="empty"><p>No applications yet.</p></div>
      ) : (
        <table className="plain">
          <thead><tr><th>Role</th><th>Status</th><th>Sent</th><th>Replies</th><th>Record</th></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id}>
                <td><Link to={`/applications/${a.id}`}>{a.title}</Link>
                  <div className="age">{a.employer} · {a.channel}</div></td>
                <td><span className="pill">{a.status.replace(/_/g, " ")}</span>
                  {a.followup_errors.length > 0 && <div className="age" style={{ color: "var(--warn)" }}>
                    follow-up: {a.followup_errors.join("; ")}</div>}</td>
                <td><Age at={a.sent_at} /></td>
                <td>{a.replies}</td>
                <td className="age">
                  {safeHref(a.gmail_link) && <a href={a.gmail_link} target="_blank" rel="noreferrer noopener">Gmail</a>}
                  {a.drive_record_url && <> · <a href={safeHref(a.drive_record_url)} target="_blank"
                                                   rel="noreferrer noopener">Drive record</a></>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Screen>
  );
}

function ReplyRow({ reply, onChange, applications }) {
  const [choice, setChoice] = useState(reply.effective_classification);
  const [linkTo, setLinkTo] = useState("");
  return (
    <div className="card">
      <h4>{reply.subject || "(no subject)"}</h4>
      <div className="age">from {reply.from_address} · <Age at={reply.received_at} />
        {reply.link_basis && <> · matched by {reply.link_basis.replace(/_/g, " ")}</>}</div>
      <div style={{ margin: "8px 0", fontSize: 13 }}>
        Classified <strong>{reply.classification}</strong>
        {reply.classification_corrected_to && <> → you corrected it to{" "}
          <strong>{reply.classification_corrected_to}</strong></>}
        {reply.classification_detail?.matched?.length > 0 && (
          <div className="age">because: {reply.classification_detail.matched.join("; ")}</div>)}
      </div>
      <div className="action">
        <select className="inline" value={choice} onChange={(e) => setChoice(e.target.value)}>
          {CLASSES.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
        </select>
        <Action label="Correct" disabled={choice === reply.effective_classification}
                run={() => api.post(`/replies/${reply.id}/correct`, { classification: choice })}
                onDone={onChange} />
        {!reply.application_id && applications && (
          <>
            <select className="inline" value={linkTo} onChange={(e) => setLinkTo(e.target.value)}>
              <option value="">link to application…</option>
              {applications.map((a) => <option key={a.id} value={a.id}>{a.title} — {a.employer}</option>)}
            </select>
            <Action label="Link" disabled={!linkTo}
                    run={() => api.post(`/replies/${reply.id}/link`, { application_id: linkTo })}
                    onDone={onChange} />
          </>
        )}
        {reply.application_id && reply.effective_classification !== "offer" && (
          <Action label="Draft a response"
                  run={() => api.post(`/replies/${reply.id}/draft`)}
                  onDone={(pkg) => navigate(`/queue/${pkg.id}`)} />
        )}
      </div>
    </div>
  );
}

export function ApplicationDetail({ id }) {
  const state = useApi(`/applications/${id}`);
  const [status, setStatus] = useState("");
  return (
    <Screen state={state}>
      {(a) => (
        <>
          <p><Link to="/applications">← applications</Link></p>
          <h2 style={{ margin: "0 0 4px" }}>{a.title}</h2>
          <div className="kv">
            <span>Employer</span><span>{a.employer}</span>
            <span>Status</span><span>{a.status}</span>
            <span>Sent to</span><span className="destination">{a.destination}</span>
            <span>Sent</span><span><Age at={a.sent_at} /></span>
            <span>Gmail thread</span><span>{safeHref(a.gmail_link)
              ? <a href={a.gmail_link} target="_blank" rel="noreferrer noopener">{a.gmail_label}</a> : "—"}</span>
            <span>Drive record</span><span>{safeHref(a.drive_record_url)
              ? <a href={a.drive_record_url} target="_blank" rel="noreferrer noopener">open</a> : "—"}</span>
          </div>

          <Section title="History">
            <table className="plain">
              <thead><tr><th>When</th><th>From</th><th>To</th><th>By</th><th>Note</th></tr></thead>
              <tbody>{a.history.map((h, i) => (
                <tr key={i}><td><Age at={h.changed_at} /></td><td>{h.from_status || "—"}</td>
                  <td>{h.to_status}</td><td>{h.actor}</td><td className="age">{h.note}</td></tr>))}
              </tbody>
            </table>
            <div className="action" style={{ marginTop: 8 }}>
              <select className="inline" value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">set status…</option>
                {STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
              </select>
              <Action label="Save" disabled={!status}
                      run={() => api.post(`/applications/${id}/status`, { status })}
                      onDone={state.reload} />
            </div>
          </Section>

          <Section title={`Replies (${a.replies.length})`}>
            {a.replies.length === 0 ? <p className="age">None yet.</p>
              : a.replies.map((r) => <ReplyRow key={r.id} reply={r} onChange={state.reload} />)}
          </Section>
        </>
      )}
    </Screen>
  );
}

export function Tracking() {
  const [unlinkedOnly, setUnlinkedOnly] = useState(false);
  const replies = useApi(`/replies?unlinked_only=${unlinkedOnly}`);
  const applications = useApi("/applications");
  const mailbox = useApi("/mailbox");
  return (
    <>
      {mailbox.data && !mailbox.data.connected && (
        <div className="notice"><strong>Replies are not being read.</strong> {mailbox.data.message}</div>
      )}
      {mailbox.data?.connected && (
        <div className="age" style={{ marginBottom: 10 }}>
          {mailbox.data.account} · last checked <Age at={mailbox.data.last_polled_at} />{" "}
          <Action label="Check now" run={() => api.post("/mailbox/poll")} onDone={replies.reload} />
        </div>
      )}
      <div className="filters">
        <label><input type="checkbox" checked={unlinkedOnly}
                      onChange={(e) => setUnlinkedOnly(e.target.checked)} />
          Only replies that could not be matched to one application</label>
      </div>
      <Screen state={replies}>
        {(rows) => rows.length === 0 ? <div className="empty"><p>No replies.</p></div>
          : rows.map((r) => <ReplyRow key={r.id} reply={r} onChange={replies.reload}
                                      applications={applications.data || []} />)}
      </Screen>
    </>
  );
}
