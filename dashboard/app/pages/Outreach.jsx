import React, { useState } from "react";
import { api, safeHref } from "../api.js";
import { navigate } from "../router.jsx";
import { Action, Age, Screen, Section, useApi } from "../ui.jsx";

/*
 * T083–T086. A contact is only ever someone an employer published, with the page
 * it came from shown beside them. Suppression and erasure are one click and
 * permanent; the address is kept only as a hash afterwards.
 */
function AddContact({ onAdded }) {
  const [form, setForm] = useState({ email: "", name: "", published_source_url: "" });
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });
  return (
    <Section title="Add a published contact">
      <p className="age" style={{ marginTop: 0 }}>Career Scout fetches the page and stores the contact only
        if the address (and the name, if you give one) appears on it exactly as written.</p>
      <div className="action">
        <input className="inline" placeholder="email as published" value={form.email} onChange={set("email")} />
        <input className="inline" placeholder="name as published (optional)" value={form.name} onChange={set("name")} />
        <input className="inline" placeholder="https://page-it-is-published-on" value={form.published_source_url}
               onChange={set("published_source_url")} style={{ minWidth: 260 }} />
        <Action label="Check and add" primary disabled={!form.email || !form.published_source_url}
                run={() => api.post("/contacts", form)}
                onDone={() => { setForm({ email: "", name: "", published_source_url: "" }); onAdded(); }} />
      </div>
    </Section>
  );
}

function Draft({ contact }) {
  const [opportunity, setOpportunity] = useState("");
  return (
    <span className="action">
      <input className="inline" placeholder="opportunity id" value={opportunity}
             onChange={(e) => setOpportunity(e.target.value)} />
      <Action label="Draft a message" disabled={!opportunity}
              run={() => api.post(`/contacts/${contact.id}/draft`, { opportunity_id: opportunity })}
              onDone={(pkg) => navigate(`/queue/${pkg.id}`)} />
    </span>
  );
}

export default function Outreach() {
  const state = useApi("/contacts");
  return (
    <>
      <div className="notice info">Outreach is off until you switch <code>channel_outreach</code> on in
        Settings, and every message is approved by you, one at a time. Each carries a notice saying
        where the address came from and that replying “remove” ends it — which Career Scout honours.</div>
      <AddContact onAdded={state.reload} />
      <Screen state={state}>
        {(rows) => rows.length === 0 ? <div className="empty"><p>No contacts.</p></div> : (
          <table className="plain">
            <thead><tr><th>Contact</th><th>Published on</th><th>State</th><th /></tr></thead>
            <tbody>{rows.map((c) => (
              <tr key={c.id}>
                <td>{c.erased_at ? <em>erased</em> : <>{c.name || <em>name not published</em>}
                  <div className="age">{c.email}{c.role && ` · ${c.role}`}</div></>}</td>
                <td className="age">{safeHref(c.published_source_url)
                  ? <a href={c.published_source_url} target="_blank" rel="noreferrer noopener">page</a> : "—"}
                  {" "}· read <Age at={c.published_read_at} /></td>
                <td>{c.erased_at ? <span className="pill bad">erased</span>
                  : c.do_not_contact ? <span className="pill bad">do not contact</span>
                  : <span className="pill">active</span>}</td>
                <td>{!c.erased_at && !c.do_not_contact && (
                  <>
                    <Draft contact={c} />{" "}
                    <Action label="Do not contact" run={() => api.post(`/contacts/${c.id}/suppress`)}
                            onDone={state.reload} />
                  </>
                )}{!c.erased_at && (
                  <Action label="Erase" run={() => api.post(`/contacts/${c.id}/erase`)} onDone={state.reload} />
                )}</td>
              </tr>))}
            </tbody>
          </table>
        )}
      </Screen>
    </>
  );
}
