// GridCast public API, P0.4 hello-world.
// Serves published snapshots only; it never holds a database credential.

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "access-control-allow-origin": "*",
  "cache-control": "no-store",
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method not allowed" }, 405);
    }
    if (url.pathname === "/health") {
      return json({ service: "gridcast", status: "ok", phase: "hello-world" });
    }
    if (url.pathname === "/") {
      return json({
        service: "gridcast",
        description: "Live 24h-ahead Ontario electricity demand forecasts (coming soon).",
        endpoints: ["/health"],
        source: "https://github.com/hamzaziouine/gridcast",
      });
    }
    return json({ error: "not found" }, 404);
  },
};
