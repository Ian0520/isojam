import assert from "node:assert/strict";
import test from "node:test";

import { authenticate } from "../app/static/auth.js";

const credentials = { email: "friend@example.com", password: "test-password" };

test("sign-in sends only credentials, even if an invitation value is present", async () => {
  const calls = [];
  const result = await authenticate(async (path, options) => {
    calls.push({ path, ...options });
    return { access_token: "login-token" };
  }, { ...credentials, invitationCode: "private-code" });

  assert.deepEqual(calls, [
    { path: "/login", method: "POST", body: credentials, auth: false },
  ]);
  assert.equal(result.access_token, "login-token");
});

test("invitation registration acknowledges account creation before login and never sends the code to login", async () => {
  const events = [];
  const calls = [];
  const code = "Case_Sensitive-code";
  const result = await authenticate(async (path, options) => {
    events.push(path);
    calls.push({ path, ...options });
    return path === "/register"
      ? { id: "new-user", email: credentials.email }
      : { access_token: "new-login-token" };
  }, { ...credentials, invitationCode: code }, {
    register: true,
    onRegistered: () => events.push("account-created"),
  });

  assert.deepEqual(events, ["/register", "account-created", "/login"]);
  assert.deepEqual(calls, [
    {
      path: "/register", method: "POST",
      body: { ...credentials, invitation_code: code }, auth: false,
    },
    { path: "/login", method: "POST", body: credentials, auth: false },
  ]);
  assert.equal(result.access_token, "new-login-token");
});

test("open registration omits the invitation field", async () => {
  const calls = [];
  await authenticate(async (path, options) => {
    calls.push({ path, ...options });
    return { access_token: "token" };
  }, credentials, { register: true });

  assert.deepEqual(calls[0], {
    path: "/register", method: "POST", body: credentials, auth: false,
  });
  assert.equal(calls.length, 2);
});

test("rejected registration preserves the generic error and does not attempt login", async () => {
  const calls = [];
  const denied = new Error("Invitation is invalid or unavailable");
  let registered = false;
  await assert.rejects(authenticate(async (path) => {
    calls.push(path);
    throw denied;
  }, { ...credentials, invitationCode: "wrong-code" }, {
    register: true,
    onRegistered: () => { registered = true; },
  }), (error) => error === denied);

  assert.deepEqual(calls, ["/register"]);
  assert.equal(registered, false);
});

test("failed automatic sign-in preserves the fact that registration committed and allows a login-only retry", async () => {
  const calls = [];
  let registered = false;
  let loginAttempts = 0;
  const request = async (path) => {
    calls.push(path);
    if (path === "/register") return { id: "created-user" };
    if (++loginAttempts === 1) throw new Error("Connection lost");
    return { access_token: "retry-token" };
  };
  await assert.rejects(authenticate(request, credentials, {
    register: true,
    onRegistered: () => { registered = true; },
  }), /Your account was created.*Sign in with your email and password/);
  assert.equal(registered, true);

  const result = await authenticate(request, credentials);
  assert.equal(result.access_token, "retry-token");
  assert.deepEqual(calls, ["/register", "/login", "/login"]);
});

for (const response of [{}, { access_token: null }, { access_token: "" }]) {
  test(`incomplete login response ${JSON.stringify(response)} does not report sign-in success`, async () => {
    await assert.rejects(
      authenticate(async () => response, credentials), /response was incomplete/,
    );
  });
}

test("ordinary login failures retain their original message", async () => {
  const denied = new Error("Invalid email or password");
  await assert.rejects(authenticate(async () => { throw denied; }, credentials),
    (error) => error === denied);
});
