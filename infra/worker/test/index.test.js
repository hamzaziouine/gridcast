// node --test: the Worker's routes against a fake R2 binding (no network, no wrangler).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import worker, { DISPATCH, dispatch } from "../src/index.js";

function bucket(objects) {
  return {
    async get(key) {
      if (!(key in objects)) return null;
      const body = objects[key];
      if (body instanceof Error) throw body;
      return {
        text: async () => body,
        httpEtag: '"e1"',
        uploaded: new Date("2026-10-24T17:03:00Z"),
      };
    },
  };
}

const call = (path, env, method = "GET") =>
  worker.fetch(new Request(`https://gridcast.example${path}`, { method }), env);

const forecast = JSON.stringify({
  issue_time: "2026-10-24T17:00:00Z",
  model_version: "lgbm-x",
  rows: [],
});

test("forecast is served from R2 with CORS, cache and model headers", async () => {
  const res = await call("/v1/forecast/latest", {
    PUBLIC: bucket({ "latest/forecast.json": forecast }),
  });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("access-control-allow-origin"), "*");
  assert.equal(res.headers.get("cache-control"), "public, max-age=60");
  assert.equal(res.headers.get("x-model-version"), "lgbm-x");
  assert.equal(res.headers.get("x-issue-time"), "2026-10-24T17:00:00Z");
  assert.equal(res.headers.get("etag"), '"e1"');
  assert.deepEqual(await res.json(), JSON.parse(forecast));
});

test("missing snapshot -> 503 JSON; R2 error or no binding -> 503; unknown path -> 404", async () => {
  let res = await call("/v1/scoreboard", { PUBLIC: bucket({}) });
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error, "not published yet");
  res = await call("/v1/scoreboard", {
    PUBLIC: bucket({ "latest/scoreboard.json": new Error("boom") }),
  });
  assert.equal(res.status, 503);
  res = await call("/v1/forecast/latest", {});
  assert.equal(res.status, 503);
  res = await call("/nope", { PUBLIC: bucket({}) });
  assert.equal(res.status, 404);
  assert.equal(res.headers.get("access-control-allow-origin"), "*");
  assert.equal((await res.json()).error, "not found");
});

test("health reports staleness from meta.json and is always 200", async () => {
  const fresh = new Date(Date.now() - 20 * 60000).toISOString().replace(/\.\d+Z$/, "Z");
  const meta = JSON.stringify({
    last_issue_time: fresh,
    stale_after_minutes: 90,
    champion: { model_version: "lgbm-x", champion_status: "ok" },
    last_run: { status: "ok" },
    attribution: ["IESO"],
  });
  let res = await call("/health", { PUBLIC: bucket({ "latest/meta.json": meta }) });
  assert.equal(res.status, 200);
  let body = await res.json();
  assert.equal(body.stale, false);
  assert.equal(body.last_issue_age_minutes, 20);
  assert.equal(body.champion_version, "lgbm-x");
  res = await call("/health", { PUBLIC: bucket({}) });
  body = await res.json();
  assert.equal(res.status, 200);
  assert.equal(body.stale, true);
});

test("index, trailing slash, preflight and method checks", async () => {
  assert.equal((await call("/", {})).status, 200);
  const sb = { PUBLIC: bucket({ "latest/scoreboard.json": "{}" }) };
  assert.equal((await call("/v1/scoreboard/", sb)).status, 200);
  assert.equal((await call("/v1/scoreboard", {}, "OPTIONS")).status, 204);
  assert.equal((await call("/v1/scoreboard", {}, "POST")).status, 405);
});

test("cron dispatch: no-op without token, POSTs workflow_dispatch on main with it", async () => {
  assert.equal((await dispatch("2,22 * * * *", {})).status, "no-token");
  assert.equal((await dispatch("* * * * *", { GH_DISPATCH_TOKEN: "t" })).status, "unknown-cron");
  const calls = [];
  const fake = async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status: 204 });
  };
  const r = await dispatch("20 13,18 * * *", { GH_DISPATCH_TOKEN: "t" }, fake);
  assert.equal(r.status, "dispatched");
  assert.equal(
    calls[0].url,
    "https://api.github.com/repos/hamzaziouine/gridcast/actions/workflows/daily.yml/dispatches",
  );
  assert.equal(calls[0].init.headers.authorization, "Bearer t");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    ref: "main",
    inputs: { scheduler: "cloudflare" },
  });
  const bad = await dispatch("2,22 * * * *", { GH_DISPATCH_TOKEN: "t" }, async () =>
    new Response(null, { status: 403 }));
  assert.equal(bad.status, "http-403");
});

test("weekly crons dispatch retrain.yml (weekly, fast) and keepalive.yml", async () => {
  const calls = [];
  const fake = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response(null, { status: 204 });
  };
  const env = { GH_DISPATCH_TOKEN: "t" };
  assert.equal((await dispatch("0 3 * * SUN", env, fake)).workflow, "retrain.yml");
  assert.equal((await dispatch("0 6 * * MON", env, fake)).workflow, "keepalive.yml");
  assert.match(calls[0].url, /\/workflows\/retrain\.yml\/dispatches$/);
  assert.deepEqual(calls[0].body, {
    ref: "main",
    inputs: { scheduler: "cloudflare", reason: "weekly", profile: "fast" },
  });
  assert.deepEqual(calls[1].body, { ref: "main", inputs: { scheduler: "cloudflare" } });
});

test("wrangler.jsonc crons equal the DISPATCH keys (<= 5 on the free plan)", () => {
  const raw = readFileSync(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  const crons = JSON.parse(raw.replace(/^\s*\/\/.*$/gm, "")).triggers.crons;
  assert.deepEqual([...crons].sort(), Object.keys(DISPATCH).sort());
  assert.ok(crons.length <= 5);
});

test("every dispatched workflow declares the inputs the Worker sends", () => {
  for (const { workflow, inputs } of Object.values(DISPATCH)) {
    const yml = readFileSync(new URL(`../../../.github/workflows/${workflow}`, import.meta.url), "utf8");
    for (const name of Object.keys(inputs)) {
      assert.match(yml, new RegExp(`^      ${name}:`, "m"), `${workflow} lacks input ${name}`);
    }
  }
});
