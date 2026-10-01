import React, { useState } from "react";
import { api } from "../api.js";
import { Action, Screen, useApi } from "../ui.jsx";
import { StarBank } from "./Interview.jsx";

function Record({ record, onChange }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(record.value || "");
  const restricted = record.value === "[restricted]";
  return (
    <tr>
      <td><code>{record.field_path}</code></td>
      <td>
        {editing ? (
          <input className="inline" value={value} onChange={(e) => setValue(e.target.value)}
                 style={{ width: "100%" }} />
        ) : record.value}
        <div className="age">
          {record.source === "document" ? "read from a document" : "typed by you"}
          {record.locator?.snippet && <> · “{record.locator.snippet.slice(0, 80)}”</>}
        </div>
      </td>
      <td>
        {record.confirmed
          ? <span className="pill good">confirmed</span>
          : <span className="pill warn">not confirmed</span>}
      </td>
      <td style={{ whiteSpace: "nowrap" }}>
        {!restricted && !record.confirmed && !editing && (
          <Action label="Confirm" primary run={() =>
            api.post(`/profile/records/${record.id}/confirm`)} onDone={onChange} />
        )}{" "}
        {!restricted && !editing && (
          <button className="inline" onClick={() => setEditing(true)}>Correct</button>
        )}
        {editing && (
          <Action label="Save" primary run={() =>
            api.post(`/profile/records/${record.id}/correct`, { value })}
                  onDone={() => { setEditing(false); onChange(); }} />
        )}
      </td>
    </tr>
  );
}

export default function Profile() {
  const [pendingOnly, setPendingOnly] = useState(true);
  const state = useApi(`/profile/records?unconfirmed_only=${pendingOnly}`);
  return (
    <>
      <div className="notice info">
        Only <strong>confirmed</strong> records can appear in an application. Anything read from
        a document arrives unconfirmed — check it here. A correction keeps the original.
      </div>
      <div className="filters">
        <label>
          <input type="checkbox" checked={pendingOnly}
                 onChange={(e) => setPendingOnly(e.target.checked)} />
          Only what still needs checking
        </label>
      </div>
      <Screen state={state}>
        {(rows) => rows.length === 0 ? (
          <div className="empty"><p>{pendingOnly ? "Nothing waiting for your check." :
            "No profile records yet. Import a CV with `career-scout setup profile`."}</p></div>
        ) : (
          <table className="plain">
            <thead><tr><th>Field</th><th>Value</th><th>State</th><th /></tr></thead>
            <tbody>
              {rows.map((r) => <Record key={r.id} record={r} onChange={state.reload} />)}
            </tbody>
          </table>
        )}
      </Screen>
      <StarBank />
    </>
  );
}
