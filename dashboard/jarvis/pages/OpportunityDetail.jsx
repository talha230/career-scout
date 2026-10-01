import React from "react";
import { api, safeHref } from "../api.js";
import { navigate } from "../router.jsx";
import { Action, Age, Screen, Section, useApi } from "../ui.jsx";
import { InterviewPractice } from "./Interview.jsx";

const num = (v, digits = 2) => (typeof v === "number" ? v.toFixed(digits) : "—");

function Breakdown({ assessment }) {
  const components = assessment.inputs.components || {};
  return (
    <>
      <table className="breakdown">
        <thead>
          <tr><th>Component</th><th>Score</th><th>Declared</th><th>Effective</th><th>Contribution</th></tr>
        </thead>
        <tbody>
          {Object.entries(components).map(([name, c]) => {
            const unscored = c.score === null || c.score === undefined;
            return (
              <tr key={name} className={unscored ? "unscored" : ""}>
                <td>
                  {name.replace(/_/g, " ")}
                  {unscored && <div style={{ fontSize: 11.5 }}>UNSCORED — {c.unscored_reason}</div>}
                </td>
                <td>{unscored ? "—" : num(c.score)}</td>
                <td>{(c.weight * 100).toFixed(0)}%</td>
                <td>{unscored ? "0 (redistributed)" : num(c.effective_weight, 4)}</td>
                <td>{unscored ? "—" : num(c.contribution, 3)}</td>
              </tr>
            );
          })}
          <tr>
            <td style={{ fontWeight: 700 }}>Total</td>
            <td style={{ fontWeight: 700 }}>
              {assessment.match_score === null ? "UNSCORED" : num(assessment.match_score)}
            </td>
            <td colSpan={3}>confidence: {assessment.confidence}</td>
          </tr>
        </tbody>
      </table>
      <div className="section">
        <h3>The arithmetic, in full</h3>
        <div className="arithmetic">{assessment.inputs.arithmetic}</div>
        <p className="age">{assessment.formula} · config {assessment.config_version}
          {" "}· fingerprint {assessment.weights.config_fingerprint}</p>
      </div>
    </>
  );
}

function Projection({ projection }) {
  if (!projection) return <p className="age">No projection was attempted for this posting.</p>;
  return (
    <>
      <div className="arithmetic">{projection.arithmetic}</div>
      {projection.lines?.length > 0 && (
        <table className="plain" style={{ marginTop: 8 }}>
          <thead><tr><th>Line</th><th>USD / month</th><th>Source</th></tr></thead>
          <tbody>
            {projection.lines.map((line, i) => (
              <tr key={i}>
                <td>{line.label}
                  <div className="age">{line.direction} · {line.arithmetic}</div>
                  {line.granularity === "country" &&
                    <div className="age">a national figure standing in for a city</div>}
                  {line.assumption && <div className="age">assumes: {line.assumption}</div>}</td>
                <td>{line.direction === "income" ? "" : "−"}{num(line.amount_usd_month)}</td>
                <td className="age">
                  {safeHref(line.source_url)
                    ? <a href={safeHref(line.source_url)} target="_blank" rel="noreferrer noopener">source</a>
                    : "—"}
                  {line.as_of && <> · as of {line.as_of}</>}
                  {line.fx_rate_id && <> · fx {line.fx_rate_id.slice(0, 8)}</>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {projection.unscored?.length > 0 && (
        <ul className="reasons bad" style={{ marginTop: 8 }}>
          {projection.unscored.map((u) => <li key={u.component}><strong>{u.component}</strong>: {u.reason}</li>)}
        </ul>
      )}
      <p className="age">Floor {num(projection.floor_usd_month, 0)} USD/month ({projection.floor_source})
        {" "}· passes: {projection.passes_floor === null ? "unknown" : String(projection.passes_floor)}</p>
    </>
  );
}

export default function OpportunityDetail({ id }) {
  const state = useApi(`/opportunities/${id}`);
  return (
    <Screen state={state}>
      {(o) => (
        <>
          <h2 style={{ margin: "0 0 4px" }}>{o.title}</h2>
          <div style={{ color: "var(--muted)", fontSize: 13.5, marginBottom: 10 }}>
            {o.employer} · {o.country || "country unresolved"} · {o.work_arrangement || "arrangement unstated"}
            {o.deadline && <> · deadline {o.deadline}</>} · fetched <Age at={o.fetched_at} />
            {safeHref(o.source_url) && <> · <a href={safeHref(o.source_url)} target="_blank"
                                              rel="noreferrer noopener">open posting ↗</a></>}
          </div>

          {o.rejections.length > 0 && (
            <Section title="Hard filters">
              {o.rejections.map((r, i) => (
                <div key={i} className="filtered-row">
                  <div className="rule">{r.active ? "REJECTED" : "LIFTED"} · {r.rule}</div>
                  <div>{r.reason}</div>
                  {r.evidence && <div className="evidence">{r.evidence}</div>}
                  {!r.active && <div className="age">lifted: {r.lifted_reason} <Age at={r.lifted_at} /></div>}
                </div>
              ))}
            </Section>
          )}

          {o.eligibility && (
            <Section title={`Eligibility — ${o.eligibility.verdict}`}>
              <p style={{ fontSize: 13, marginTop: 0 }}>{o.eligibility.detail}</p>
              <table className="plain">
                <tbody>
                  {o.eligibility.findings.map((f) => (
                    <tr key={f.dimension}>
                      <td>{f.dimension.replace(/_/g, " ")}</td>
                      <td><span className={`pill ${f.state === "blocked" ? "bad" : f.state === "ok" ? "good" : "warn"}`}>{f.state}</span></td>
                      <td>{f.reason}
                        {f.posting_evidence && <div className="evidence">posting: {f.posting_evidence}</div>}
                        {f.profile_evidence && <div className="age">you: {f.profile_evidence}</div>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Section>
          )}

          {o.assessment ? (
            <>
              <Section title="Score"><Breakdown assessment={o.assessment} /></Section>
              <Section title="Savings projection"><Projection projection={o.assessment.projection} /></Section>
              <div className="age">Scored <Age at={o.assessment.computed_at} /></div>
            </>
          ) : (
            <p className="age">Not scored yet. Scoring happens in the daily run.</p>
          )}

          <Section title={`Can your profile support an application? ${o.sufficiency?.verdict || "not checked"}`}>
            {o.sufficiency?.reason && <p style={{ fontSize: 13 }}>{o.sufficiency.reason}</p>}
            {o.sufficiency?.missing_field_paths?.length > 0 && (
              <p style={{ fontSize: 13 }}>Missing from your confirmed profile:{" "}
                <strong>{o.sufficiency.missing_field_paths.join(", ")}</strong></p>
            )}
            <Action label="Prepare the application" primary
                    disabled={o.sufficiency && o.sufficiency.verdict !== "sufficient"}
                    why="only a posting your confirmed profile supports gets an application"
                    run={() => api.post(`/opportunities/${o.id}/package`)}
                    onDone={(pkg) => navigate(`/queue/${pkg.id}`)} />
            <p className="age">Preparing writes the documents and runs QC. Nothing is sent —
              you review and approve it on the next screen.</p>
          </Section>

          <InterviewPractice opportunityId={o.id} />
        </>
      )}
    </Screen>
  );
}
