import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "../src/styles.css";
import "./jarvis.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// Production builds only: in dev the worker's caching served a stale shell over
// Vite's fresh modules (the legacy dashboard learned this the hard way).
// Browsers register a service worker only on https or localhost; a phone on
// plain-http LAN gets a home-screen shortcut, not an offline app. `jarvis serve
// --certfile/--keyfile` serves https for a full install.
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* Offline support is a nicety; the app works without it. */
    });
  });
}
