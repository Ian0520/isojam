import assert from "node:assert/strict";
import test from "node:test";

import { YouTubeImports, importFailure } from "../app/static/imports.js";

const id = "11111111-1111-4111-8111-111111111111";
const uploadId = "22222222-2222-4222-8222-222222222222";
const jobId = "33333333-3333-4333-8333-333333333333";
const url = "https://youtu.be/BaW_jenozKc";
const item = (status = "pending", extra = {}) => ({
  id, video_id: "BaW_jenozKc", status,
  upload_id: status === "ready" ? uploadId : null,
  filename: status === "ready" ? "Recording.wav" : null,
  error_code: status === "failed" ? "interrupted" : null,
  created_at: "2026-10-20T12:00:00Z", ...extra,
});
const job = { id: jobId, status: "pending", upload_id: uploadId, outputs: {} };

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function setup(request, values = new Map(), owner = "first@example.com") {
  const timers = new Set(), jobs = [], calls = [];
  let keys = 0;
  const controller = new YouTubeImports(async (path, options = {}) => {
    calls.push({ path, ...options });
    return request(path, options);
  }, () => {}, (...args) => jobs.push(args), {
    get: (key) => values.get(key) || null,
    set: (key, value) => values.set(key, value),
    remove: (key) => values.delete(key),
  }, {
    newKey: () => `request-${++keys}`,
    setTimer: (callback) => { timers.add(callback); return callback; },
    clearTimer: (timer) => timers.delete(timer),
  });
  controller.connect(owner);
  return { controller, timers, jobs, values, calls, async poll() {
    const callback = timers.values().next().value;
    assert.ok(callback, "Expected a scheduled import poll");
    timers.delete(callback);
    await callback();
  } };
}

test("preparation polls before submitting separation with a stable import-derived key", async () => {
  const responses = [item(), item("processing"), item("ready"), job];
  const env = setup(async () => responses.shift());
  await env.controller.start(url);
  assert.equal(env.controller.phase, "pending");
  assert.equal(env.jobs.length, 0);
  await env.poll();
  assert.equal(env.controller.phase, "processing");
  assert.equal(env.calls.some((call) => call.path === "/jobs"), false);
  await env.poll();
  assert.equal(env.controller.phase, "submitted");
  assert.equal(env.controller.active, false);
  assert.deepEqual(env.calls.map((call) => call.path), ["/imports", `/imports/${id}`, `/imports/${id}`, "/jobs"]);
  assert.equal(env.calls[3].headers["Idempotency-Key"], `youtube-import:${id}`);
  assert.deepEqual(env.calls[3].body, { upload_id: uploadId });
  assert.deepEqual(env.jobs, [[job, item("ready")]]);
  assert.equal(env.values.size, 0);
  assert.equal(env.timers.size, 0);
});

test("a lost import response keeps the key before POST and replays it after reload", async () => {
  const values = new Map();
  const first = setup(async () => {
    assert.equal(JSON.parse(values.get("youtube.first@example.com")).key, "request-1");
    throw new Error("Connection lost");
  }, values);
  await first.controller.start(url);
  assert.equal(first.controller.active, true);
  assert.match(first.controller.error, /Connection lost/);
  const next = setup(async (path) => path.startsWith("/imports?") ? [] : path === "/jobs" ? job : item("ready"), values);
  await next.controller.restore();
  const replay = next.calls.find((call) => call.path === "/imports");
  assert.equal(replay.headers["Idempotency-Key"], first.calls[0].headers["Idempotency-Key"]);
  assert.equal(next.jobs.length, 1);
});

test("a lost separation response retries the same job without downloading again", async () => {
  let attempts = 0;
  const env = setup(async (path) => {
    if (path === "/jobs" && attempts++ === 0) throw new Error("Response lost");
    return path === "/jobs" ? job : item("ready");
  });
  await env.controller.start(url);
  assert.equal(env.controller.phase, "ready");
  assert.match(env.controller.error, /Response lost/);
  await env.controller.retry();
  assert.equal(env.calls.filter((call) => call.path === "/imports").length, 1);
  const submissions = env.calls.filter((call) => call.path === "/jobs");
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0].headers["Idempotency-Key"], submissions[1].headers["Idempotency-Key"]);
  assert.equal(env.jobs.length, 1);
});

test("opening a ready server-side import works without a browser bookmark", async () => {
  const env = setup(async (path) => path === "/jobs" ? job : item("ready"));
  await env.controller.open(item("ready"));
  assert.deepEqual(env.calls.map((call) => call.path), [`/imports/${id}`, "/jobs"]);
  assert.equal(env.calls[1].headers["Idempotency-Key"], `youtube-import:${id}`);
});

test("failed imports need a deliberate new key while a rejected submission can retain its key", async () => {
  const env = setup(async () => item("failed"));
  await env.controller.start(url);
  assert.equal(env.controller.phase, "failed");
  assert.equal(env.controller.active, false);
  await env.controller.start(url);
  assert.notEqual(env.calls[0].headers["Idempotency-Key"], env.calls[1].headers["Idempotency-Key"]);
  assert.equal(env.jobs.length, 0);
  assert.match(importFailure("interrupted"), /interrupted/);

  const rejected = setup(async () => { throw Object.assign(new Error("Queue full"), { status: 429 }); });
  await rejected.controller.start(url);
  assert.equal(rejected.controller.phase, "rejected");
  assert.equal(rejected.controller.active, false);
  await rejected.controller.start(url);
  assert.equal(rejected.calls[0].headers["Idempotency-Key"], rejected.calls[1].headers["Idempotency-Key"]);
});

test("an account change ignores a stale import response and preserves the new workflow", async () => {
  const old = deferred();
  const env = setup(async () => old.promise);
  const pending = env.controller.start(url);
  env.controller.connect("second@example.com");
  assert.equal(env.controller.intent, null);
  old.resolve(item("ready"));
  await pending;
  assert.equal(env.controller.owner, "second@example.com");
  assert.equal(env.controller.item, null);
  assert.equal(env.jobs.length, 0);
  assert.ok(env.values.has("youtube.first@example.com"));
  assert.equal(env.values.has("youtube.second@example.com"), false);
});

test("a stale separation response after sign-out cannot reopen a recording", async () => {
  const response = deferred();
  const started = deferred();
  const env = setup(async (path) => {
    if (path !== "/jobs") return item("ready");
    started.resolve();
    return response.promise;
  });
  const pending = env.controller.start(url);
  await started.promise;
  assert.equal(env.controller.phase, "creating-job");
  env.controller.connect(null);
  response.resolve(job);
  await pending;
  assert.equal(env.jobs.length, 0);
  assert.equal(env.controller.job, null);
});

test("page suspension cancels polling and restoring checks the original import", async () => {
  const env = setup(async (path) => path.includes("?") ? [] : item());
  await env.controller.start(url);
  const staleTimer = env.timers.values().next().value;
  env.controller.pause();
  assert.equal(env.timers.size, 0);
  await staleTimer();
  assert.equal(env.calls.length, 1);
  await env.controller.restore();
  assert.equal(env.calls.filter((call) => call.path === "/imports").length, 1);
  assert.ok(env.calls.some((call) => call.path === `/imports/${id}`));
  assert.equal(env.controller.loading, false);
});

test("incomplete import or job responses retain the bookmark for a safe retry", async () => {
  for (const bad of [null, { ...item("ready"), upload_id: null }]) {
    const env = setup(async () => bad);
    await env.controller.start(url);
    assert.match(env.controller.error, /incomplete/);
    assert.equal(env.jobs.length, 0);
    assert.ok(env.values.size);
  }
  const env = setup(async (path) => path === "/jobs" ? { ...job, upload_id: "wrong" } : item("ready"));
  await env.controller.start(url);
  assert.match(env.controller.error, /incomplete/);
  assert.equal(env.controller.phase, "ready");
  assert.equal(env.jobs.length, 0);
});

test("list refresh can finish during submission without hiding the accepted import", async () => {
  const response = deferred();
  const env = setup(async (path) => path.includes("?") ? response.promise : item());
  const refresh = env.controller.refresh();
  await env.controller.start(url);
  response.resolve([]);
  await refresh;
  assert.equal(env.controller.loading, false);
  assert.deepEqual(env.controller.items, [item()]);
});

test("an old account's list cannot repopulate a new account", async () => {
  const response = deferred();
  const env = setup(async () => response.promise);
  const refresh = env.controller.refresh();
  env.controller.connect("second@example.com");
  response.resolve([item("ready")]);
  await refresh;
  assert.deepEqual(env.controller.items, []);
  assert.equal(env.controller.loading, false);
});

test("damaged saved state is ignored instead of blocking the application", () => {
  for (const value of ["not-json", JSON.stringify({ url, key: "valid", id: "../../anything" })]) {
    const env = setup(async () => [], new Map([["youtube.first@example.com", value]]));
    assert.equal(env.controller.intent, null);
    assert.equal(env.controller.phase, "idle");
  }
});
