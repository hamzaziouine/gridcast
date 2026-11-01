// GridCast public API (Worker host, BUILD_PLAN §3.0 item 8).
// Serves the JSON snapshots the hourly/daily jobs publish to the R2 bucket gridcast-public
// (binding PUBLIC). Its only write is ops/dispatcher.json, the outcome of the last cron dispatch.
// It never holds a database credential.
//
//   GET /                    service index
//   GET /health              liveness + staleness of the newest forecast + last cron dispatch
//                            (always 200)
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
const DISPATCH_KEY = "ops/dispatcher.json";
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

// The last cron outcome without its error text: status, workflow, cron, at (or null).
async function dispatcherStatus(env) {
  const r = await readObject(env, DISPATCH_KEY);
  if (!r.text) return null;
  try {
    const d = JSON.parse(r.text);
    return {
      status: d.status ?? null,
      workflow: d.workflow ?? null,
      cron: d.cron ?? null,
      at: d.at ?? null,
    };
  } catch {
    return null;
  }
}

async function health(env, now) {
  const r = await readObject(env, META_KEY);
  const body = { service: "gridcast", status: "ok", dispatcher: await dispatcherStatus(env) };
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
// Triggers also dispatch the jobs. Duplicates are harmless (per-hour success, concurrency groups).
// No-op until the Worker secret GH_DISPATCH_TOKEN (fine-grained PAT, Actions: write on this repo)
// is set.
const REPO = "hamzaziouine/gridcast";
export const DISPATCH = { "2,22 * * * *": "hourly.yml", "20 13,18 * * *": "daily.yml" };

export async function dispatch(cron, env, fetchImpl = fetch) {
  const workflow = DISPATCH[cron];
  if (!workflow) return { cron, status: "unknown-cron" };
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
      // scheduler=cloudflare makes the run record run_kind 'scheduled' instead of 'manual'.
      body: JSON.stringify({ ref: "main", inputs: { scheduler: "cloudflare" } }),
    },
  );
  return { cron, workflow, status: res.status === 204 ? "dispatched" : `http-${res.status}` };
}

// Every cron outcome is logged and recorded in R2 so a dead dispatcher shows up in GET /health
// (status no-token / http-401 / error, or an old `at`) instead of only in Workers Logs: on
// 2026-10-25 it went silent for a week behind GitHub's sparse fallback runs (DATA_NOTES 20.5).
export async function runScheduled(event, env, fetchImpl = fetch) {
  let r;
  try {
    r = await dispatch(event.cron, env, fetchImpl);
  } catch (err) {
    r = { cron: event.cron, status: "error", error: String(err && err.message ? err.message : err) };
  }
  r.at = new Date(event.scheduledTime ?? Date.now()).toISOString();
  console.log(JSON.stringify(r));
  if (env.PUBLIC) {
    try {
      await env.PUBLIC.put(DISPATCH_KEY, JSON.stringify(r), {
        httpMetadata: { contentType: "application/json" },
      });
    } catch (err) {
      const error = String(err && err.message ? err.message : err);
      console.log(JSON.stringify({ status: "record-failed", error }));
    }
  }
  return r;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runScheduled(event, env));
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
