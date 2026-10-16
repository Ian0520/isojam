import assert from "node:assert/strict";
import test from "node:test";

import { JobHistory } from "../app/static/history.js";

function item(id, status = "completed") {
  return {
    id,
    upload_id: "upload",
    filename: `${id}.wav`,
    status,
    created_at: "2026-10-16T08:00:00Z",
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

test("loads the first page and passes an opaque cursor unchanged", async () => {
  const paths = [];
  const cursor = "opaque+/=bookmark";
  const pages = [
    { items: [item("a"), item("b")], next_cursor: cursor },
    { items: [item("c")], next_cursor: null },
  ];
  const history = new JobHistory(async (path) => {
    paths.push(path);
    return pages.shift();
  }, () => {});

  await history.load();
  assert.equal(history.loaded, true);
  assert.equal(history.nextCursor, cursor);
  assert.deepEqual(history.items.map((job) => job.id), ["a", "b"]);
  await history.load({ more: true });
  assert.deepEqual(history.items.map((job) => job.id), ["a", "b", "c"]);
  assert.equal(history.nextCursor, null);
  assert.equal(paths[0], "/jobs?limit=20");
  assert.equal(new URL(paths[1], "http://localhost").searchParams.get("cursor"), cursor);
  await history.load({ more: true });
  assert.equal(paths.length, 2);
});

test("refresh replaces previously loaded pages", async () => {
  const pages = [
    { items: [item("old")], next_cursor: "older" },
    { items: [item("older")], next_cursor: null },
    { items: [item("new")], next_cursor: "fresh-boundary" },
  ];
  const history = new JobHistory(async () => pages.shift(), () => {});
  await history.load();
  await history.load({ more: true });
  await history.load();

  assert.deepEqual(history.items.map((job) => job.id), ["new"]);
  assert.equal(history.nextCursor, "fresh-boundary");
});

test("failed load-more preserves the list and bookmark for retry", async () => {
  let calls = 0;
  const history = new JobHistory(async () => {
    calls += 1;
    if (calls === 1) return { items: [item("a")], next_cursor: "after-a" };
    if (calls === 2) throw new Error("Offline");
    return { items: [item("b")], next_cursor: null };
  }, () => {});
  await history.load();
  await history.load({ more: true });

  assert.deepEqual(history.items.map((job) => job.id), ["a"]);
  assert.equal(history.nextCursor, "after-a");
  assert.equal(history.error, "Offline");
  assert.equal(history.loading, false);
  await history.load({ more: true });
  assert.deepEqual(history.items.map((job) => job.id), ["a", "b"]);
  assert.equal(history.error, "");
});

test("repeated load-more clicks cannot append the same pending page twice", async () => {
  const pending = deferred();
  let calls = 0;
  const history = new JobHistory(async () => {
    calls += 1;
    if (calls === 1) return { items: [item("a")], next_cursor: "after-a" };
    return pending.promise;
  }, () => {});
  await history.load();
  const loading = history.load({ more: true });
  await history.load({ more: true });
  assert.equal(calls, 2);
  pending.resolve({ items: [item("b")], next_cursor: null });
  await loading;
  assert.deepEqual(history.items.map((job) => job.id), ["a", "b"]);
});

test("account reset discards an old response without clearing the new request's loading state", async () => {
  const oldRequest = deferred();
  const newRequest = deferred();
  let calls = 0;
  const history = new JobHistory(
    () => (++calls === 1 ? oldRequest.promise : newRequest.promise),
    () => {},
  );
  const oldLoad = history.load();
  history.clear();
  const newLoad = history.load();
  oldRequest.resolve({ items: [item("old-account")], next_cursor: "old-cursor" });
  await oldLoad;

  assert.deepEqual(history.items, []);
  assert.equal(history.nextCursor, null);
  assert.equal(history.loading, true);
  newRequest.resolve({ items: [item("new-account")], next_cursor: null });
  await newLoad;
  assert.deepEqual(history.items.map((job) => job.id), ["new-account"]);
  assert.equal(history.loading, false);
});

test("an old account's delayed error cannot overwrite the new account's list", async () => {
  const oldRequest = deferred();
  let calls = 0;
  const history = new JobHistory(
    () => ++calls === 1
      ? oldRequest.promise
      : Promise.resolve({ items: [item("new-account")], next_cursor: null }),
    () => {},
  );
  const oldLoad = history.load();
  history.clear();
  await history.load();
  oldRequest.reject(new Error("Old account request failed"));
  await oldLoad;

  assert.deepEqual(history.items.map((job) => job.id), ["new-account"]);
  assert.equal(history.error, "");
});

test("refresh wins over an older load-more response", async () => {
  const moreRequest = deferred();
  let calls = 0;
  const history = new JobHistory(async () => {
    calls += 1;
    if (calls === 1) return { items: [item("a")], next_cursor: "after-a" };
    if (calls === 2) return moreRequest.promise;
    return { items: [item("new"), item("a")], next_cursor: "new-boundary" };
  }, () => {});
  await history.load();
  const moreLoad = history.load({ more: true });
  await history.load();
  moreRequest.resolve({ items: [item("stale-page")], next_cursor: "stale-cursor" });
  await moreLoad;

  assert.deepEqual(history.items.map((job) => job.id), ["new", "a"]);
  assert.equal(history.nextCursor, "new-boundary");
});

test("invalid response can be retried without replacing the displayed list", async () => {
  const pages = [
    { items: [item("a")], next_cursor: null },
    { items: [{ ...item("broken"), created_at: "invalid" }], next_cursor: null },
    { items: [], next_cursor: null },
  ];
  const history = new JobHistory(async () => pages.shift(), () => {});
  await history.load();
  await history.load();
  assert.deepEqual(history.items.map((job) => job.id), ["a"]);
  assert.match(history.error, /incomplete/);
  await history.load();
  assert.deepEqual(history.items, []);
  assert.equal(history.error, "");
  assert.equal(history.loaded, true);
});

test("job polling updates a displayed status without refetching history", async () => {
  let calls = 0;
  const history = new JobHistory(async () => {
    calls += 1;
    return { items: [item("a", "processing")], next_cursor: null };
  }, () => {});
  await history.load();
  history.updateJob({ id: "a", status: "completed" });

  assert.equal(history.items[0].status, "completed");
  assert.equal(calls, 1);
});
