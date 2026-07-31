/**
 * Unit tests for the inbound Email Worker (managed-mailbox-v1 §4.1).
 * Runs on Node's built-in test runner (`npm test` -> `node --test`), which
 * exercises the same Web Crypto / fetch / Response globals the Workers runtime
 * exposes. No wrangler, no network, no cloud.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.js";

const SECRET = "test-shared-hmac-secret";
const BASE = "https://backend.test/api/resume/mail-inbound/cloudflare";

async function hmacHex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function makeMessage(rawBytes, overrides = {}) {
  const rejects = [];
  return {
    to: overrides.to ?? "harshith.manne@pilotmail.co",
    from: overrides.from ?? "no-reply@greenhouse.io",
    rawSize: overrides.rawSize ?? rawBytes.length,
    headers: new Map([["message-id", overrides.messageId ?? "<abc123@greenhouse.io>"]]),
    get raw() {
      return rawBytes; // Response accepts a Uint8Array body.
    },
    setReject(reason) {
      rejects.push(reason);
    },
    _rejects: rejects,
  };
}

// Programmable fetch that records calls and returns queued responses.
function installFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init, calls.length);
  };
  return calls;
}

const env = { MAIL_INBOUND_URL: BASE, MAIL_INBOUND_HMAC_SECRET: SECRET };

test("oversize message is rejected before any fetch", async () => {
  const calls = installFetch(() => {
    throw new Error("fetch must not be called");
  });
  const msg = makeMessage(new Uint8Array(0), { rawSize: 25 * 1024 * 1024 + 1 });
  await worker.email(msg, env, {});
  assert.equal(calls.length, 0);
  assert.deepEqual(msg._rejects, ["552 message too large"]);
});

test("slot 404 rejects with no-such-user and does not PUT/commit", async () => {
  const calls = installFetch((url) => {
    if (url.endsWith("/slot")) return new Response(null, { status: 404 });
    throw new Error(`unexpected call to ${url}`);
  });
  const msg = makeMessage(new TextEncoder().encode("raw mime here"));
  await worker.email(msg, env, {});
  assert.equal(calls.length, 1);
  assert.deepEqual(msg._rejects, ["550 5.1.1 no such user"]);
});

test("slot 429 (rate limited) hard-rejects with a 4xx and does not throw/PUT/commit", async () => {
  const calls = installFetch((url) => {
    if (url.endsWith("/slot")) return new Response(null, { status: 429 });
    throw new Error(`unexpected call to ${url}`);
  });
  const msg = makeMessage(new TextEncoder().encode("raw mime here"));
  await worker.email(msg, env, {}); // must NOT throw (distinct from 5xx)
  assert.equal(calls.length, 1); // no PUT, no commit
  assert.deepEqual(msg._rejects, ["450 4.7.1 rate limited, try again later"]);
});

test("happy path: correct HMACs, presigned PUT, then commit", async () => {
  const raw = new TextEncoder().encode("From: x\r\nSubject: hi\r\n\r\nbody");
  const msg = makeMessage(raw);
  const expectedSha = await sha256Hex(raw);
  const commitToken = "opaque-commit-token";
  const uploadUrl = "https://s3.test/presigned-put";

  const seen = {};
  const calls = installFetch(async (url, init) => {
    if (url.endsWith("/slot")) {
      seen.slotSig = init.headers["x-jp-signature"];
      seen.slotBody = JSON.parse(init.body);
      return new Response(
        JSON.stringify({
          upload_url: uploadUrl,
          put_headers: { "content-type": "message/rfc822" },
          commit_token: commitToken,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    if (url === uploadUrl) {
      seen.putMethod = init.method;
      seen.putHeaders = init.headers;
      return new Response(null, { status: 200 });
    }
    if (url.endsWith("/commit")) {
      seen.commitSig = init.headers["x-jp-signature"];
      seen.commitBody = JSON.parse(init.body);
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }
    throw new Error(`unexpected call to ${url}`);
  });

  await worker.email(msg, env, {});

  assert.equal(calls.length, 3);
  assert.deepEqual(msg._rejects, []);

  // Slot: envelope recipient authoritative, canonical to\nfrom\nid\nsize.
  assert.equal(seen.slotBody.envelope_to, "harshith.manne@pilotmail.co");
  assert.equal(seen.slotBody.envelope_from, "no-reply@greenhouse.io");
  assert.equal(seen.slotBody.raw_size, raw.length);
  const expectSlotSig = await hmacHex(
    SECRET,
    `${msg.to}\n${msg.from}\n<abc123@greenhouse.io>\n${raw.length}`,
  );
  assert.equal(seen.slotSig, expectSlotSig);

  // PUT: presigned URL used verbatim with returned headers.
  assert.equal(seen.putMethod, "PUT");
  assert.equal(seen.putHeaders["content-type"], "message/rfc822");

  // Commit: sha of raw + HMAC over commit_token\nraw_sha256.
  assert.equal(seen.commitBody.commit_token, commitToken);
  assert.equal(seen.commitBody.raw_sha256, expectedSha);
  const expectCommitSig = await hmacHex(SECRET, `${commitToken}\n${expectedSha}`);
  assert.equal(seen.commitSig, expectCommitSig);
});

test("commit 409 (already committed) is treated as success", async () => {
  const raw = new TextEncoder().encode("dup");
  const msg = makeMessage(raw);
  installFetch(async (url) => {
    if (url.endsWith("/slot"))
      return new Response(
        JSON.stringify({ upload_url: "https://s3.test/p", put_headers: {}, commit_token: "t" }),
        { status: 200 },
      );
    if (url === "https://s3.test/p") return new Response(null, { status: 200 });
    if (url.endsWith("/commit")) return new Response(null, { status: 409 });
    throw new Error(`unexpected ${url}`);
  });
  await worker.email(msg, env, {}); // must not throw
  assert.deepEqual(msg._rejects, []);
});

test("slot 500 throws so Cloudflare retries", async () => {
  installFetch((url) => {
    if (url.endsWith("/slot")) return new Response(null, { status: 500 });
    throw new Error(`unexpected ${url}`);
  });
  const msg = makeMessage(new TextEncoder().encode("x"));
  await assert.rejects(() => worker.email(msg, env, {}));
});
