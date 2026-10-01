import React, { useState } from "react";
import { api } from "../api.js";
import { Action, Screen, Section, useApi } from "../ui.jsx";

/* T090. Questions from this posting's gap; only the user's typed answer is scored. */
function Question({ opportunityId, question }) {
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState(null);
  return (
    <div className="card">
      <h4>{question.text}</h4>
      <div className="age">{question.competency} — {question.why}</div>
      <textarea className="inline" rows={4} style={{ width: "100%", marginTop: 8 }}
                value={answer} onChange={(e) => setAnswer(e.target.value)}
                placeholder="Your answer, in your own words" />
      <Action label="Score my answer" disabled={!answer.trim()}
              run={() => api.post(`/opportunities/${opportunityId}/interview/score`, { answer })}
              onDone={setResult} />
      {result && (
        <div style={{ marginTop: 8, fontSize: 13 }}>
          <strong>{result.total}/100</strong> ({result.word_count} words)
          <div className="arithmetic" style={{ margin: "6px 0" }}>{result.arithmetic}</div>
          <ul className="reasons">{result.feedback.map((f, i) => <li key={i}>{f}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

export function InterviewPractice({ opportunityId }) {
  const state = useApi(`/opportunities/${opportunityId}/interview`);
  return (
    <Section title="Interview practice">
      <p className="age" style={{ marginTop: 0 }}>Scored against a published rubric from what you
        type. No model answer is written for you.</p>
      <Screen state={state}>
        {(qs) => qs.map((q) => <Question key={q.id} opportunityId={opportunityId} question={q} />)}
      </Screen>
    </Section>
  );
}

function Story({ story, onSaved }) {
  const [form, setForm] = useState({
    situation: story.situation.status === "sourced" ? story.situation.text : "",
    task: story.task.status === "sourced" ? story.task.text : "",
    result: story.result.source === "your answer" ? story.result.text : "",
  });
  const field = (name) => {
    const element = story[name];
    if (element.status === "sourced" && element.source !== "your answer") {
      return <div><strong>{name}:</strong> {element.text} <span className="age">({element.source})</span></div>;
    }
    return (
      <div><strong>{name}:</strong>
        {element.prompt && <div className="age">{element.prompt}</div>}
        <textarea className="inline" rows={2} style={{ width: "100%" }} value={form[name]}
                  onChange={(e) => setForm({ ...form, [name]: e.target.value })} />
      </div>
    );
  };
  return (
    <div className="card">
      <h4>{story.title} {story.ready && <span className="pill good">ready</span>}</h4>
      {field("situation")}{field("task")}
      <div><strong>action:</strong> {story.action.text} <span className="age">({story.action.source})</span></div>
      {field("result")}
      <Action label="Save my answers" run={() => api.post(`/star/${story.record_id}`, form)}
              onDone={onSaved} />
    </div>
  );
}

export function StarBank() {
  const state = useApi("/star");
  return (
    <Section title="STAR stories">
      <p className="age" style={{ marginTop: 0 }}>Built from your confirmed achievements. Anything
        the record does not say is left for you to answer — nothing is invented.</p>
      <Screen state={state}>
        {(stories) => stories.length === 0
          ? <p className="age">No confirmed achievements yet.</p>
          : stories.map((s) => <Story key={s.record_id} story={s} onSaved={state.reload} />)}
      </Screen>
    </Section>
  );
}
