import React, { useEffect, useState } from "react";

// Enough routing for a handful of screens: the History API plus one event.
// The server answers every non-/api path with index.html, so deep links work.

export function navigate(to) {
  window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function usePath() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

export function Link({ to, children, ...rest }) {
  function onClick(event) {
    // Let the browser handle new-tab / new-window clicks.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(to);
  }
  return <a href={to} onClick={onClick} {...rest}>{children}</a>;
}
