import React from "react";
import { Link } from "../router.jsx";
import { Age, Screen, Section, pct, useApi } from "../ui.jsx";

function Onboarding({ steps }) {
  const outstanding = steps.filter((s) => !s.done);
  if (!outstanding.length) return null;
  return (
    <Section title="Finish setting up">
      <ul className="checklist">
        {steps.map((step) => (
          <li key={step.key}>
            <span className={`tick ${step.done ? "yes" : "no"}`}>{step.done ? "✓" : "•"}</span>
            <strong>{step.label}</strong>
            {step.detail && <span style={{ color: "var(--muted)" }}> — {step.detail}</span>}
            {!step.done && (
              <div style={{ fontSize: 12.5, color: "var(--muted)", marginLeft: 18 }}>
                If you skip this: {step.if_skipped}
              </div>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}

export default function Progress() {
  const progress = useApi("/progress");
  const onboarding = useApi("/onboarding");

  return (
    <Screen state={progress}>
      {(data) => {
        const profile = data.profile;
        const mvp = profile.minimum_viable_profile;
        return (
          <>
            {data.mailbox.state === "reconnect" && (
              <div className="notice"><strong>Mailbox disconnected by Google.</strong>{" "}
                {data.mailbox.message}</div>
            )}

            <div className="stats">
              {[
                ["opportunities", "Opportunities found", "/opportunities"],
                ["scored", "Scored"],
                ["sufficient", "Your profile supports"],
                ["awaiting_approval", "Waiting for your approval", "/queue"],
                ["applications", "Applications sent", "/applications"],
                ["unlinked_replies", "Replies to sort out", "/tracking"],
              ].map(([key, label, to]) => (
                <div className="stat" key={key}>
                  <div className="value">
                    {to ? <Link to={to} style={{ color: "inherit", textDecoration: "none" }}>
                      {data.stages[key]}</Link> : data.stages[key]}
                  </div>
                  <div className="label">{label}</div>
                </div>
              ))}
            </div>

            <div className="age" style={{ marginBottom: 12 }}>
              Last run: {data.last_run
                ? <>{data.last_run.kind} · {data.last_run.status} · <Age at={data.last_run.started_at} /></>
                : "never — run `jarvis run`, or leave `jarvis serve` running for the daily pass"}
            </div>

            {onboarding.data && <Onboarding steps={onboarding.data.steps} />}

            <Section title="Your profile" right={<Link to="/profile">Review records</Link>}>
              <div className="bar" aria-label="profile completeness">
                <div style={{ width: pct(profile.completeness.percentage) }} />
              </div>
              <div className="age" style={{ marginTop: 4 }}>
                {pct(profile.completeness.percentage)} — {profile.completeness.note}
              </div>
            </Section>

            <Section title={`Minimum viable profile — ${mvp.satisfied ? "complete" : "incomplete"}`}>
              <p style={{ fontSize: 13, color: "var(--muted)", marginTop: 0 }}>
                Unlocks {mvp.unlocks}.
              </p>
              <ul className="checklist">
                {mvp.items.map((item) => (
                  <li key={item.key}>
                    <span className={`tick ${item.satisfied ? "yes" : "no"}`}>
                      {item.satisfied ? "✓" : "•"}</span>
                    <strong>{item.label}</strong>
                    <span style={{ color: "var(--muted)" }}> — {item.why}</span>
                    {!item.satisfied && item.still_needed.length > 0 && (
                      <div className="age" style={{ marginLeft: 18 }}>
                        Still needed: {item.still_needed.join(", ")}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </Section>

            {profile.next_best_actions.length > 0 && (
              <Section title="What to fill in next">
                <ul className="checklist">
                  {profile.next_best_actions.map((action) => (
                    <li key={action.field_path}>
                      <strong>{action.label}</strong> — unlocks {action.unlocks_opportunities}{" "}
                      shortlisted opportunit{action.unlocks_opportunities === 1 ? "y" : "ies"}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            <div className="age">Data as of <Age at={data.now} /></div>
          </>
        );
      }}
    </Screen>
  );
}
