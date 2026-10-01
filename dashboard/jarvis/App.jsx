import React, { useEffect, useState } from "react";
import { Link, usePath } from "./router.jsx";
import Progress from "./pages/Progress.jsx";
import Profile from "./pages/Profile.jsx";
import Opportunities from "./pages/Opportunities.jsx";
import OpportunityDetail from "./pages/OpportunityDetail.jsx";
import { QueueItem, QueueList } from "./pages/Queue.jsx";
import { ApplicationDetail, ApplicationList, Tracking } from "./pages/Applications.jsx";
import Outreach from "./pages/Outreach.jsx";
import Settings from "./pages/Settings.jsx";
import Health from "./pages/Health.jsx";

const SCREENS = [
  ["/", "Progress"],
  ["/opportunities", "Opportunities"],
  ["/queue", "Queue"],
  ["/applications", "Applications"],
  ["/tracking", "Tracking"],
  ["/outreach", "Outreach"],
  ["/profile", "Profile"],
  ["/settings", "Settings"],
  ["/health", "Health"],
];

function route(path) {
  const [, section, id] = path.split("/");
  switch (`/${section}`) {
    case "/": return <Progress />;
    case "/opportunities": return id ? <OpportunityDetail id={id} /> : <Opportunities />;
    case "/queue": return id ? <QueueItem id={id} /> : <QueueList />;
    case "/applications": return id ? <ApplicationDetail id={id} /> : <ApplicationList />;
    case "/tracking": return <Tracking />;
    case "/outreach": return <Outreach />;
    case "/profile": return <Profile />;
    case "/settings": return <Settings />;
    case "/health": return <Health />;
    default: return <div className="empty"><p>No page at {path}.</p></div>;
  }
}

function useOfflineCopy() {
  const [cachedAt, setCachedAt] = useState(null);
  useEffect(() => {
    const on = (event) => setCachedAt(event.detail.cachedAt);
    window.addEventListener("jarvis:freshness", on);
    return () => window.removeEventListener("jarvis:freshness", on);
  }, []);
  return cachedAt;
}

export default function App() {
  const path = usePath();
  const cachedAt = useOfflineCopy();
  const section = "/" + (path.split("/")[1] || "");
  return (
    <div className="app">
      <header className="header">
        <h1>Jarvis</h1>
        <div className="sub">Local. Your data stays on this machine.</div>
      </header>
      {cachedAt && (
        <div className="notice">
          <strong>Offline — this is a saved copy</strong> from {cachedAt}. Nothing can be changed
          until Jarvis is reachable again.
        </div>
      )}
      <nav className="tabs">
        {SCREENS.map(([to, label]) => (
          <Link key={to} to={to} className={section === to ? "active" : ""}>{label}</Link>
        ))}
      </nav>
      {route(path)}
    </div>
  );
}
