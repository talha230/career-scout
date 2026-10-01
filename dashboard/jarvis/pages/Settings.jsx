import React, { useState } from "react";
import { api } from "../api.js";
import { Action, Age, Screen, Section, useApi } from "../ui.jsx";

// Grouped for reading. Every key the server knows is shown somewhere — a setting
// that exists but has no place on this screen would be a hidden control.
const GROUPS = [
  ["Thresholds", ["savings_floor_usd_month", "scholarship_funding_floor_usd_month",
                  "fallback_savings_floor_usd_month", "countries_requiring_own_floor",
                  "fx_rate_max_age_days", "requirements_confidence_floor",
                  "staleness_window_days", "generate_top_n"]],
  ["Sending limits (protect your own mailbox)", ["cap_per_destination_per_day",
                  "cap_total_per_day", "min_send_interval_seconds"]],
  ["Channels — every one off until you switch it on", ["channel_email_autosend",
                  "channel_published_api", "channel_outreach"]],
  ["Auto-approval — your own rules, off until you write them", ["auto_approve_enabled",
                  "auto_approve_min_score", "auto_approve_countries", "auto_approve_max_per_day"]],
  ["Schedule", ["discovery_schedule_hour", "gmail_poll_minutes", "run_staleness_alert_hours",
                "effort_split_jobs", "effort_split_scholarships"]],
  ["Backups", ["backup_enabled", "backup_target", "backup_keep_daily", "backup_keep_weekly",
               "backup_stale_after_hours"]],
  ["AI", ["ai_provider", "ai_api_key"]],
  ["This computer", ["web_port", "web_bind"]],
];
const HELP = {
  channel_email_autosend: "Lets an application you approved leave from your Gmail. Approval is still required for each one.",
  savings_floor_usd_month: "Blank = use the country's sourced default, then the global fallback.",
  countries_requiring_own_floor: "Countries where the global fallback is never applied silently.",
  ai_api_key: "Optional. Without it, unattended runs use templates only; nothing breaks.",
  auto_approve_enabled: "Approves an application only if it meets every rule below, has no warnings and is eligible and sufficient. Never replies or outreach. Only changeable here, on this computer.",
  auto_approve_min_score: "Match score 0–100. Blank = nothing is auto-approved.",
  auto_approve_countries: "Allowed countries, e.g. [\"US\", \"DE\"]. Empty = nothing is auto-approved.",
  backup_target: "A folder on an external drive is best. Blank = inside the data directory.",
  web_bind: "0.0.0.0 lets your phone on the same Wi-Fi open the app. Approval still works only here.",
};
const HIDDEN = new Set(["cv_style_variant", "install_id", "completeness_definition_version",
                        "sufficiency_rule_version"]);

function parse(text) {
  if (text.trim() === "") return null;
  try { return JSON.parse(text); } catch { return text; }
}

function SettingRow({ name, value, onSaved }) {
  const secret = name === "ai_api_key";
  // A secret is never shown back: the API returns "***set***", never the value.
  const [text, setText] = useState(secret ? "" : JSON.stringify(value) ?? "");
  const changed = secret ? text !== "" : text !== (JSON.stringify(value) ?? "");
  return (
    <tr>
      <td><code>{name}</code>{HELP[name] && <div className="age">{HELP[name]}</div>}</td>
      <td>
        {typeof value === "boolean" && !secret ? (
          <Action label={value ? "On — switch off" : "Off — switch on"} primary={!value}
                  run={() => api.put(`/settings/${name}`, { value: !value })} onDone={onSaved} />
        ) : (
          <span className="action">
            <input className="inline" value={text} type={secret ? "password" : "text"}
                   placeholder={secret ? (value ? "set — type to replace" : "not set") : "blank = default"}
                   onChange={(e) => setText(e.target.value)} />
            <Action label="Save" disabled={!changed}
                    run={() => api.put(`/settings/${name}`, { value: secret ? text : parse(text) })}
                    onDone={onSaved} />
          </span>
        )}
      </td>
    </tr>
  );
}

function Countries() {
  const state = useApi("/countries");
  return (
    <Screen state={state}>
      {(rows) => (
        <table className="plain">
          <thead><tr><th>Country</th><th>Searched</th><th>Savings floor</th></tr></thead>
          <tbody>{rows.map((c) => (
            <tr key={c.iso2}>
              <td>{c.name} <span className="age">{c.iso2}</span></td>
              <td><Action label={c.enabled ? "On — switch off" : "Off — switch on"}
                          run={() => api.put(`/countries/${c.iso2}`, { enabled: !c.enabled })}
                          onDone={state.reload} /></td>
              <td className="age">{c.floor.value === null
                ? `UNSCORED — ${c.floor.reason}` : `${c.floor.value} USD/month (${c.floor.source})`}</td>
            </tr>))}
          </tbody>
        </table>
      )}
    </Screen>
  );
}

export default function Settings() {
  const settings = useApi("/settings");
  const health = useApi("/system/health");
  return (
    <>
      {health.data && (
        <Section title="Google">
          {health.data.mailbox.connected
            ? <p style={{ fontSize: 13 }}>Connected as <strong>{health.data.mailbox.account}</strong>,
                last checked <Age at={health.data.mailbox.last_polled_at} />.</p>
            : <p style={{ fontSize: 13 }}>{health.data.mailbox.message}</p>}
          <p className="age">Connecting, reconnecting or switching account happens in the terminal:{" "}
            <code>jarvis setup google</code>. Your Google project is your own.</p>
        </Section>
      )}
      <Section title="Countries"><Countries /></Section>
      <Screen state={settings}>
        {(values) => {
          const shown = new Set(GROUPS.flatMap(([, keys]) => keys));
          const rest = Object.keys(values).filter((k) => !shown.has(k) && !HIDDEN.has(k));
          return [...GROUPS, ...(rest.length ? [["Other", rest]] : [])].map(([title, keys]) => (
            <Section key={title} title={title}>
              <table className="plain"><tbody>
                {keys.filter((k) => k in values || k === "ai_api_key").map((k) => (
                  <SettingRow key={`${k}:${JSON.stringify(values[k])}`} name={k}
                              value={values[k]}
                              onSaved={settings.reload} />))}
              </tbody></table>
            </Section>
          ));
        }}
      </Screen>
      {health.data && (
        <Section title="Where your data lives">
          <div className="destination">{health.data.disk.data_directory}</div>
          <p className="age">Set JARVIS_HOME before starting Jarvis to use another folder.
            Backup key: <code>{health.data.backups.key_file}</code> — copy it off this computer once.</p>
        </Section>
      )}
    </>
  );
}
