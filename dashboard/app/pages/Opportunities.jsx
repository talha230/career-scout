import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { Link } from "../router.jsx";

const fmtConfidence = (c) => (typeof c === "number" ? c.toFixed(2) : c ?? "—");

function Card({ row }) {
  const unscored = row.match_score === null || row.match_score === undefined;
  const set_aside = row.rejected || row.hidden;
  return (
    <Link to={`/opportunities/${row.id}`} className={`job${set_aside ? " dimmed" : ""}`}
          style={{ borderLeftColor: set_aside ? "var(--border)" : "var(--accent)" }}>
      <div className="score">
        {unscored ? "—" : row.match_score.toFixed(0)}
        <small>{unscored ? "unscored" : "score"}</small>
      </div>
      <div>
        <div className="title">{row.title}</div>
        <div className="meta">
          <span>{row.employer}</span><span>·</span>
          <span>{row.country || "country unresolved"}</span><span>·</span>
          <span>{row.role_family_label}</span>
          {row.kind === "scholarship" && <><span>·</span><span>scholarship</span></>}
        </div>
        {row.hidden && row.eligibility_detail && (
          <div className="meta" style={{ marginTop: 5, color: "var(--warn)" }}>{row.eligibility_detail}</div>
        )}
      </div>
      <div className="right">
        {row.rejected && <span className="pill bad">rejected</span>}
        {row.hidden && <span className="pill warn">ineligible</span>}
        {row.passes_floor === true && <span className="pill good">passes floor</span>}
        {row.passes_floor === false && <span className="pill warn">below floor</span>}
        <span className="pill">{row.sufficiency.replace(/_/g, " ")}</span>
        {row.pay_basis && <span className="pill">pay: {row.pay_basis}</span>}
        {!unscored && <span className="pill">conf {fmtConfidence(row.confidence)}</span>}
      </div>
    </Link>
  );
}

export default function Opportunities() {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [kind, setKind] = useState("");
  const [floorOnly, setFloorOnly] = useState(false);
  const [showSetAside, setShowSetAside] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams({ limit: "200" });
    if (kind) params.set("kind", kind);
    if (floorOnly) params.set("passes_floor_only", "true");
    setRows(null);
    setError(null);
    api.get(`/opportunities?${params}`).then(setRows).catch((e) => setError(e.message));
  }, [kind, floorOnly]);

  // Rejected and ineligible rows are collapsed, not dropped: the count stays
  // visible and one click shows them with their reasons.
  const setAside = useMemo(() => (rows || []).filter((r) => r.rejected || r.hidden), [rows]);
  const visible = useMemo(
    () => (rows || []).filter((r) => showSetAside || !(r.rejected || r.hidden)),
    [rows, showSetAside]
  );

  if (error) return <div className="notice"><strong>Could not load opportunities:</strong> {error}</div>;
  if (!rows) return <p style={{ color: "var(--muted)" }}>Loading…</p>;

  return (
    <>
      <div className="filters">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">Jobs and scholarships</option>
          <option value="job">Jobs</option>
          <option value="scholarship">Scholarships</option>
        </select>
        <label>
          <input type="checkbox" checked={floorOnly} onChange={(e) => setFloorOnly(e.target.checked)} />
          Passes my savings floor
        </label>
        <label>
          <input type="checkbox" checked={showSetAside} onChange={(e) => setShowSetAside(e.target.checked)} />
          Show {setAside.length} rejected or ineligible
        </label>
      </div>

      {visible.length === 0 ? (
        <div className="empty">
          <p>No opportunities to show.</p>
          <p style={{ fontSize: 13 }}>Run <code>career-scout discover</code> to fetch postings.</p>
        </div>
      ) : (
        <div className="job-list">{visible.map((row) => <Card key={row.id} row={row} />)}</div>
      )}
    </>
  );
}
