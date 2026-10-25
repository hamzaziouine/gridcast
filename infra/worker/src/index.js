// GridCast public API (Worker host, BUILD_PLAN §3.0 item 8).
// Serves the JSON snapshots the hourly/daily jobs publish to the R2 bucket gridcast-public
// (binding PUBLIC, read-only use). It never holds a database credential.
//
//   GET /                    service index
//   GET /health              liveness + staleness of the newest forecast (always 200)
//   GET /v1/forecast/latest  latest/forecast.json
//   GET /v1/scoreboard       latest/scoreboard.json
//
// A snapshot that is not published yet (or an R2 error) is a 503 JSON body, an unknown path
// a 404 JSON body. Every response: CORS *, JSON, short public cache.

const KEYS = {
  "/v1/forecast/latest": "latest/forecast.json",
  "/v1/scoreboard": "latest/scoreboard.json",
};
const META_KEY = "latest/meta.json";
const STALE_AFTER_MIN = 90;

const BASE_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, OPTIONS",
  "access-control-max-age": "86400",
};

function json(body, status = 200, cache = "public, max-age=60") {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...BASE_HEADERS, "cache-control": cache },
  });
}

async function readObject(env, key) {
  if (!env.PUBLIC) return { error: "storage binding missing" };
  try {
    const obj = await env.PUBLIC.get(key);
    if (obj === null) return { missing: true };
    return { obj, text: await obj.text() };
  } catch (err) {
    return { error: String(err && err.message ? err.message : err) };
  }
}

async function snapshot(env, key) {
  const r = await readObject(env, key);
  if (r.missing) return json({ error: "not published yet", key }, 503, "no-store");
  if (r.error) return json({ error: "storage unavailable" }, 503, "no-store");
  const headers = { ...BASE_HEADERS, "cache-control": "public, max-age=60" };
  if (r.obj.httpEtag) headers.etag = r.obj.httpEtag;
  if (r.obj.uploaded) headers["last-modified"] = new Date(r.obj.uploaded).toUTCString();
  try {
    const doc = JSON.parse(r.text);
    if (doc.model_version) headers["x-model-version"] = doc.model_version;
    if (doc.issue_time) headers["x-issue-time"] = doc.issue_time;
  } catch {
    return json({ error: "snapshot unreadable" }, 503, "no-store");
  }
  return new Response(r.text, { status: 200, headers });
}

async function health(env, now) {
  const r = await readObject(env, META_KEY);
  const body = { service: "gridcast", status: "ok" };
  if (r.text) {
    try {
      const meta = JSON.parse(r.text);
      const last = meta.last_issue_time ? Date.parse(meta.last_issue_time) : NaN;
      const age = Number.isNaN(last) ? null : Math.round((now - last) / 60000);
      Object.assign(body, {
        last_issue_time: meta.last_issue_time ?? null,
        last_issue_age_minutes: age,
        stale: age === null || age > (meta.stale_after_minutes ?? STALE_AFTER_MIN),
        champion_version: meta.champion?.model_version ?? null,
        champion_status: meta.champion?.champion_status ?? null,
        last_run_status: meta.last_run?.status ?? null,
        attribution: meta.attribution ?? [],
      });
      return json(body, 200, "public, max-age=30");
    } catch {
      // unreadable meta counts as stale (below)
    }
  }
  return json({ ...body, last_issue_time: null, stale: true }, 200, "no-store");
}

// External scheduler (ADR-0014): GitHub's own cron drops and delays runs, so Cloudflare Cron
// Triggers also dispatch the jobs. Duplicates are harmless: hourly/daily judge success per
// logical time, retrain dedupes per weekly slot, keepalive is idempotent, and every workflow has
// a concurrency group. Every dispatch sends `scheduler: cloudflare`, which the workflows map to
// run_kind 'scheduled' (it is the scheduler, not a person). No-op until the Worker secret
// GH_DISPATCH_TOKEN (fine-grained PAT, Actions: write on this repo) is set.
// Keys must match wrangler.jsonc `triggers.crons` character for character. Cloudflare weekdays:
// 1 = Sunday, so names are used.
const REPO = "hamzaziouine/gridcast";
const CF = { scheduler: "cloudflare" };
export const DISPATCH = {
  "2,22 * * * *": { workflow: "hourly.yml", inputs: CF },
  "20 13,18 * * *": { workflow: "daily.yml", inputs: CF },
  "0 3 * * SUN": { workflow: "retrain.yml", inputs: { ...CF, reason: "weekly", profile: "fast" } },
  "0 6 * * MON": { workflow: "keepalive.yml", inputs: CF },
};

export async function dispatch(cron, env, fetchImpl = fetch) {
  const job = DISPATCH[cron];
  if (!job) return { cron, status: "unknown-cron" };
  const { workflow, inputs } = job;
  if (!env.GH_DISPATCH_TOKEN) return { cron, workflow, status: "no-token" };
  const res = await fetchImpl(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "gridcast-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  return { cron, workflow, status: res.status === 204 ? "dispatched" : `http-${res.status}` };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(event.cron, env).then((r) => console.log(JSON.stringify(r))));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: BASE_HEADERS });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method not allowed" }, 405, "no-store");
    }
    const path = url.pathname.replace(/\/+$/, "") || "/";
    if (path === "/health") return health(env, Date.now());
    if (KEYS[path]) return snapshot(env, KEYS[path]);
    if (path === "/") {
      return json({
        service: "gridcast",
        description: "Live 24h-ahead Ontario electricity demand forecasts, scored against IESO.",
        endpoints: ["/health", "/v1/forecast/latest", "/v1/scoreboard"],
        source: "https://github.com/hamzaziouine/gridcast",
      });
    }
    return json({ error: "not found" }, 404, "no-store");
  },
};
