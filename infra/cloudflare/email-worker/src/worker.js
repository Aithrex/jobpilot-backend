/**
 * JobPilot managed-mailbox inbound Email Worker (managed-mailbox-v1 §4.1).
 *
 * Runs on the Cloudflare Email Routing catch-all for the neutral APPLY_MAIL
 * domain. It is DELIBERATELY THIN and CPU-light (Cloudflare free Workers have a
 * hard CPU cap): it does NO MIME parsing, holds NO AWS credentials, and never
 * touches message bodies beyond hashing the raw bytes. Its only job is the
 * two-phase handoff to the backend, which mints a one-use presigned S3 PUT.
 *
 *   Phase A (slot):   POST {MAIL_INBOUND_URL}/slot   -> {upload_url, put_headers, commit_token}
 *   PUT raw MIME   :  PUT  upload_url (direct to S3, presigned; Worker holds no creds)
 *   Phase B (commit): POST {MAIL_INBOUND_URL}/commit -> {ok}
 *
 * Auth to the backend is HMAC-SHA256 over a canonical string (envelope fields),
 * NOT the message body, so the backend can authenticate the request cheaply and
 * the body only ever transits the presigned S3 URL.
 *
 * Bindings (see wrangler.toml):
 *   env.MAIL_INBOUND_URL          (plain var)   base URL, e.g. https://aspirely.me/api/resume/mail-inbound/cloudflare
 *   env.MAIL_INBOUND_HMAC_SECRET  (secret)      shared with the backend SSM value
 *
 * LOGGING RULE: never log secrets, the raw body, addresses, or any PII. Only
 * sizes, the message-id, and HTTP status codes are safe breadcrumbs.
 */

const MAX_RAW_BYTES = 25 * 1024 * 1024; // 25 MiB — mirrors MAIL_MAX_RAW_BYTES (spec §6).

/** Hex-encode an ArrayBuffer / TypedArray. */
function toHex(buffer) {
  const bytes = new Uint8Array(buffer);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

/** HMAC-SHA256(secret, message) -> lowercase hex. Uses Web Crypto (no deps). */
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
  return toHex(sig);
}

/** SHA-256(bytes) -> lowercase hex. */
async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return toHex(digest);
}

export default {
  /**
   * @param {ForwardableEmailMessage} message
   * @param {{ MAIL_INBOUND_URL: string, MAIL_INBOUND_HMAC_SECRET: string }} env
   * @param {ExecutionContext} ctx
   */
  async email(message, env, ctx) {
    const messageId = message.headers.get("message-id") || "";
    const rawSize = message.rawSize;

    // 1. Size gate BEFORE reading any bytes into memory (spec §4.1.1).
    if (rawSize > MAX_RAW_BYTES) {
      console.log(
        `reject oversize message_id=${messageId} raw_size=${rawSize} limit=${MAX_RAW_BYTES}`,
      );
      message.setReject("552 message too large");
      return;
    }

    if (!env.MAIL_INBOUND_URL || !env.MAIL_INBOUND_HMAC_SECRET) {
      // Misconfigured binding: throw so Cloudflare retries (transient from our
      // side) rather than silently dropping mail. No secret is logged.
      console.log(`error missing_binding message_id=${messageId}`);
      throw new Error("worker misconfigured: missing MAIL_INBOUND_URL / HMAC secret");
    }

    const base = env.MAIL_INBOUND_URL.replace(/\/+$/, "");

    // envelope recipient is the authoritative SMTP recipient (NOT the MIME To).
    const envelopeTo = message.to;
    const envelopeFrom = message.from;

    // 2. HMAC over the canonical string (spec §4.1.2): to\nfrom\nid\nsize.
    const slotCanonical = `${envelopeTo}\n${envelopeFrom}\n${messageId}\n${rawSize}`;
    const slotSig = await hmacHex(env.MAIL_INBOUND_HMAC_SECRET, slotCanonical);

    // 3. Phase A — slot.
    const slotResp = await fetch(`${base}/slot`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-jp-signature": slotSig,
      },
      body: JSON.stringify({
        envelope_to: envelopeTo,
        envelope_from: envelopeFrom,
        message_id: messageId,
        raw_size: rawSize,
      }),
    });

    if (slotResp.status === 404) {
      // Unknown / retired alias — the backend never reveals ownership.
      console.log(`reject no_such_user message_id=${messageId}`);
      message.setReject("550 5.1.1 no such user");
      return;
    }
    if (slotResp.status === 429) {
      // Rate-limited by the backend (P1-RATE-IN): the alias / source-domain is
      // over its inbound ingest cap. Treat as a HARD reject — do NOT throw for a
      // Cloudflare retry (retrying would just hammer the same cap and amplify a
      // flood). A 4xx SMTP code tells the sending MTA to stop for this message.
      console.log(`reject rate_limited status=429 message_id=${messageId}`);
      message.setReject("450 4.7.1 rate limited, try again later");
      return;
    }
    if (slotResp.status !== 200) {
      console.log(`slot non200 status=${slotResp.status} message_id=${messageId}`);
      throw new Error(`slot failed: ${slotResp.status}`); // Cloudflare retries (5xx).
    }

    const { upload_url: uploadUrl, put_headers: putHeaders, commit_token: commitToken } =
      await slotResp.json();
    if (!uploadUrl || !commitToken) {
      console.log(`slot bad_payload message_id=${messageId}`);
      throw new Error("slot returned an incomplete payload");
    }

    // 4. Read raw bytes ONCE (bounded by the size gate above) and hash them.
    const rawBuffer = await new Response(message.raw).arrayBuffer();
    const rawBytes = new Uint8Array(rawBuffer);
    const rawSha256 = await sha256Hex(rawBytes);

    // PUT straight to S3 via the presigned URL. The Worker's only capability is
    // this one-use, single-object URL — it carries no AWS creds.
    const putResp = await fetch(uploadUrl, {
      method: "PUT",
      headers: putHeaders || {},
      body: rawBytes,
    });
    if (!putResp.ok) {
      console.log(`s3_put non2xx status=${putResp.status} message_id=${messageId}`);
      throw new Error(`s3 put failed: ${putResp.status}`); // Cloudflare retries.
    }

    // 5. Phase B — commit. HMAC over commit_token\nraw_sha256 (spec §4.1.5).
    const commitCanonical = `${commitToken}\n${rawSha256}`;
    const commitSig = await hmacHex(env.MAIL_INBOUND_HMAC_SECRET, commitCanonical);

    const commitResp = await fetch(`${base}/commit`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-jp-signature": commitSig,
      },
      body: JSON.stringify({
        commit_token: commitToken,
        raw_sha256: rawSha256,
      }),
    });

    // 2xx -> done. 409 (already committed) -> treat as done (idempotent).
    // Anything else -> throw so Cloudflare retries.
    if (!commitResp.ok && commitResp.status !== 409) {
      console.log(`commit non2xx status=${commitResp.status} message_id=${messageId}`);
      throw new Error(`commit failed: ${commitResp.status}`);
    }

    console.log(
      `ok message_id=${messageId} raw_size=${rawSize} commit_status=${commitResp.status}`,
    );
  },
};
