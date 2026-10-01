/*
 * The one way the UI talks to `career-scout serve`. Every write carries X-Career-Scout: 1 —
 * the server refuses writes without it (a cross-site form cannot set it).
 * The server's `detail` message is surfaced as the error text: it is written
 * for the user and says why something was refused.
 */
async function request(method, path, body) {
  const init = { method, headers: { Accept: "application/json" } };
  if (method !== "GET") {
    init.headers["X-Career-Scout"] = "1";
    init.headers["Content-Type"] = "application/json";
    if (body !== undefined) init.body = JSON.stringify(body);
  }
  const response = await fetch(`/api${path}`, init);
  // The service worker marks a saved copy served offline; the app shows a banner.
  const cachedAt = response.headers.get("X-Career-Scout-Cached-At");
  window.dispatchEvent(new CustomEvent("career-scout:freshness", { detail: { cachedAt } }));
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.detail || `HTTP ${response.status}`);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),
  put: (path, body) => request("PUT", path, body),
};

/** A posting's URL is third-party text; only http(s) may become a link. */
export const safeHref = (url) => (/^https?:\/\//i.test(url || "") ? url : null);
