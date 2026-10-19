import assert from "node:assert/strict";
import test from "node:test";

import { browserPage, settle } from "./browser_harness.mjs";

const importId = "11111111-1111-4111-8111-111111111111";
const uploadId = "22222222-2222-4222-8222-222222222222";
const jobId = "33333333-3333-4333-8333-333333333333";
const url = "https://youtu.be/BaW_jenozKc";
const item = {
  id: importId, video_id: "BaW_jenozKc", status: "ready", upload_id: uploadId,
  filename: "<img src=x onerror=alert(1)>.wav", error_code: null,
  created_at: "2026-10-20T12:00:00Z",
};
const job = { id: jobId, status: "pending", upload_id: uploadId, outputs: {} };
const info = {
  processing_mode: "queued", registration_mode: "open", youtube_imports_enabled: true,
  max_upload_bytes: 209715200, max_audio_duration_seconds: 600,
  audio_upload_types: { ".wav": "audio/wav", ".mp3": "audio/mpeg" },
};
const ok = (body) => ({ status: 200, body });
function respond(path) {
  if (path === "/ui-info") return ok(info);
  if (path.startsWith("/jobs?")) return ok({ items: [], next_cursor: null });
  if (path.startsWith("/imports?")) return ok([]);
  if (path === "/jobs") return ok(job);
  if (path.startsWith("/imports")) return ok(item);
  throw new Error(`Unexpected request: ${path}`);
}

test("the real page wires import controls, sends authenticated retry keys and opens separation", async () => {
  const page = await browserPage(respond);
  try {
    assert.equal(page.nodes.get("source-tabs").hidden, false);
    await page.submit(url);
    const importCall = page.calls.find((call) => call.path === "/imports");
    assert.equal(importCall.headers.Authorization, "Bearer test-token");
    assert.match(importCall.headers["Idempotency-Key"], /^[0-9a-f-]{36}$/);
    assert.deepEqual(JSON.parse(importCall.body), { url });
    const jobCall = page.calls.find((call) => call.path === "/jobs");
    assert.equal(jobCall.headers["Idempotency-Key"], `youtube-import:${importId}`);
    assert.equal(page.nodes.get("session-title").textContent, item.filename);
    assert.equal(page.nodes.get("job-status").textContent, "pending");
    assert.equal(page.nodes.get("notice").hidden, true);
    assert.equal(page.nodes.get("upload-disclosure").open, false);
    assert.equal(page.values.get("isojam.job"), jobId);
  } finally { await page.close(); }
});

test("a lost POST response survives page reload with the same key and original URL", async () => {
  const first = await browserPage((path) => {
    if (path === "/imports") throw new TypeError("Connection lost");
    return respond(path);
  });
  let values, key;
  try {
    await first.submit(url);
    assert.equal(first.nodes.get("youtube-submit").textContent, "Retry import");
    assert.equal(first.nodes.get("youtube-submit").disabled, false);
    assert.equal(first.nodes.get("youtube-url").disabled, true);
    assert.match(first.nodes.get("youtube-message").textContent, /Could not reach/);
    values = first.values;
    key = first.calls.find((call) => call.path === "/imports").headers["Idempotency-Key"];
  } finally { await first.close(); }
  const next = await browserPage(respond, values);
  try {
    const replay = next.calls.find((call) => call.path === "/imports");
    assert.equal(replay.headers["Idempotency-Key"], key);
    assert.equal(next.nodes.get("job-status").textContent, "pending");
    assert.equal(next.nodes.get("youtube-panel").hidden, false);
  } finally { await next.close(); }
});

test("an HTTP admission error shows server feedback and keeps a usable retry button", async () => {
  const page = await browserPage((path) => path === "/imports"
    ? { status: 429, body: { detail: "The audio import queue is full" } } : respond(path));
  try {
    await page.submit(url);
    assert.match(page.nodes.get("youtube-message").textContent, /queue is full/);
    assert.equal(page.nodes.get("youtube-url").disabled, false);
    assert.equal(page.nodes.get("youtube-submit").disabled, false);
    assert.equal(page.nodes.get("youtube-reset").hidden, false);
  } finally { await page.close(); }
});

test("sign-out during a download response clears the UI and does not submit separation", async () => {
  let finish;
  const response = new Promise((resolve) => { finish = resolve; });
  const page = await browserPage((path) => path === "/imports" ? response : respond(path));
  try {
    await page.submit(url);
    page.nodes.get("logout").click();
    finish(ok(item));
    await settle();
    assert.equal(page.calls.some((call) => call.path === "/jobs"), false);
    assert.equal(page.nodes.get("session-panel").hidden, true);
    assert.equal(page.nodes.get("youtube-url").value, "");
    assert.equal(page.nodes.get("account-panel").hidden, false);
    assert.equal(page.values.has("isojam.token"), false);
  } finally { await page.close(); }
});

test("expired credentials cannot leave an import spinner or account data visible", async () => {
  const page = await browserPage((path) => path === "/imports"
    ? { status: 401, body: { detail: "Not authenticated" } } : respond(path));
  try {
    await page.submit(url);
    assert.equal(page.nodes.get("youtube-progress").hidden, true);
    assert.equal(page.nodes.get("account-panel").hidden, false);
    assert.match(page.nodes.get("notice").textContent, /sign-in expired/);
    assert.equal(page.nodes.get("youtube-submit").disabled, true);
  } finally { await page.close(); }
});

test("local audio uploads still submit correctly when the import feature is disabled", async () => {
  const page = await browserPage((path) => {
    if (path === "/ui-info") return ok({ ...info, youtube_imports_enabled: false });
    if (path === "/uploads") return ok({ id: uploadId });
    return respond(path);
  });
  try {
    assert.equal(page.nodes.get("source-tabs").hidden, true);
    const file = new File(["data"], "recording.mp3", { type: "audio/mpeg" });
    page.nodes.get("audio-file").files = [file];
    page.nodes.get("audio-file").dispatch("change");
    await page.nodes.get("upload-form").dispatch("submit");
    assert.equal(page.calls.find((call) => call.path === "/uploads").body.get("audio_file").name, file.name);
    assert.equal(page.nodes.get("job-status").textContent, "pending");
    assert.equal(page.nodes.get("notice").hidden, true);
  } finally { await page.close(); }
});

test("finishing an import preserves an explicitly opened history recording", async () => {
  const oldId = "44444444-4444-4444-8444-444444444444";
  const old = { id: oldId, status: "completed", upload_id: uploadId, outputs: {} };
  const page = await browserPage((path, options) => {
    if (path.startsWith("/jobs?")) return ok({ items: [{
      ...old, filename: "Earlier recording.wav", created_at: "2026-10-19T12:00:00Z",
    }], next_cursor: null });
    if (path === `/jobs/${oldId}`) return ok(old);
    if (path === `/jobs/${jobId}`) return ok(job);
    if (path === "/imports" && options.method === "POST")
      return ok({ ...item, status: "pending", upload_id: null, filename: null });
    return respond(path);
  });
  try {
    await page.submit(url);
    const historyButton = page.nodes.get("job-history").children[0].children[0];
    historyButton.click();
    await settle();
    assert.equal(page.nodes.get("session-title").textContent, "Earlier recording.wav");
    const importPoll = [...page.timers].find(([, callback]) => callback.toString().includes("advance"));
    assert.ok(importPoll);
    page.timers.delete(importPoll[0]);
    await importPoll[1]();
    await settle();
    assert.equal(page.nodes.get("job-id").textContent, oldId);
    assert.equal(page.nodes.get("youtube-open-job").hidden, false);
    page.nodes.get("youtube-open-job").click();
    await settle();
    assert.equal(page.nodes.get("job-id").textContent, jobId);
    assert.equal(page.nodes.get("youtube-open-job").hidden, true);
  } finally { await page.close(); }
});

test("disabled admission still lets prepared imports open while failed retries stay disabled", async () => {
  const failed = { ...item, id: "55555555-5555-4555-8555-555555555555",
    status: "failed", upload_id: null, filename: null, error_code: "interrupted" };
  const page = await browserPage((path) => {
    if (path === "/ui-info") return ok({ ...info, youtube_imports_enabled: false });
    if (path.startsWith("/imports?")) return ok([item, failed]);
    return respond(path);
  });
  try {
    assert.equal(page.nodes.get("source-tabs").hidden, false);
    const buttons = page.nodes.get("youtube-imports").children.map((row) => row.children[0]);
    assert.equal(buttons[0].disabled, false);
    assert.equal(buttons[1].disabled, true);
    buttons[0].click();
    await settle();
    assert.equal(page.nodes.get("job-id").textContent, jobId);
    assert.equal(page.calls.some((call) => call.path === "/imports"), false);
  } finally { await page.close(); }
});
