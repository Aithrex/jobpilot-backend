# Sign-in (`/api/auth`), 2026-11-15

How a person gets a session: a password, a six-digit code sent to their email,
or Google. This is the contract the frontend is written against. Code:
`services/auth/blueprints/auth.py`, `services/auth/services/email_code_service.py`,
`services/auth/services/oauth_service.py`. Tests: `tests/test_sign_in.py`,
`tests/test_oauth_sign_in.py`.

Microsoft and Apple are coded and switched off (no credentials). Their open
items are at the end.

## The one rule

**No account exists, and no password works, for an address nobody has proven
they receive mail at.** An address is proven by entering an emailed code,
opening a reset link, or signing in with a provider that vouches for it.

Before this date `/register` created the account at once, unverified, and
nothing ever verified it. Anyone could register someone else's address and
keep a working password on it after the real owner arrived through Google.

What follows from the rule:

- `/register` stores nothing. It checks the request and mails a code;
  `/otp/verify` creates the account, and carries the password itself.
- A password account created before this date is unverified. Its next
  password sign-in answers `email_unverified`; one code later it is verified
  for good. Send the same password with the code and it is kept.
- When an unverified account's address is proven by someone who does **not**
  present its password (a code alone, or a Google sign-in), the password is
  removed: whoever set it never proved the address. The response says
  `password_removed: true`.

## Response shapes

Success is flat: `{access_token, email, role?, sector?}`. The JWT identity is
the email. `JWT_SECRET_KEY` is unchanged.

Every failure:

```json
{"error": "<message>", "code": "<machine code>", "message": "<message>"}
```

Switch on `code`, show `message`. Statuses are 400, 403, 409, 429 and 503.
**Never 401**: the frontend signs the person out on any 401.

Every 429, from every auth route:

```json
{"error": "rate_limited", "code": "rate_limited | account_locked | code_cooldown",
 "message": "...", "retry_after": 42}
```

with the same number in the `Retry-After` header.

## Endpoints

### `POST /otp/request`  `{email}`

Mails a code. `200 {ok, message, resend_after: 60, expires_in: 600}`, the same
for an address with an account and one without.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_email` | not an address |
| 403 | `account_closed` | the account was deleted and is being erased |
| 429 | `code_cooldown` | a code was sent under 60 seconds ago |
| 429 | `rate_limited` | 5 codes an hour for one address, 20 an hour for one IP |
| 503 | `email_send_failed` | the mail provider refused or the address is suppressed (it bounced before). Nothing was sent and there is no cooldown |

### `POST /otp/verify`  `{email, code, password?, age_confirmed?, terms_version?, name?, role?, fingerprint_hash?}`

`200 {access_token, email, role?, sector?, created, password_removed?}`.

- Existing account: signs in, marks the email verified, clears a password
  lockout.
- New address: creates the account. Needs `age_confirmed: true`; stores
  `terms_version`. `password` is optional (an account can live on codes).

| Status | `code` | When |
|---|---|---|
| 400 | `code_invalid` (`attempts_left`) | wrong code |
| 400 | `code_locked` | fifth wrong try; that code is dead |
| 400 | `code_expired` | no live code: older than 10 minutes, used, or replaced |
| 400 | `consent_required` | new address without `age_confirmed: true`. The code is untouched, send it again with the consent |
| 400 | `age_requirement` | `age_confirmed: false` (`error` is `age_requirement`, as before) |
| 400 | `weak_password` | new address, password fails the policy. The code is untouched |
| 403 | `account_closed` | |
| 429 | `rate_limited` | 40 tries per 10 minutes per IP |

The code: 6 digits, stored as an HMAC (never the digits), 10 minutes, single
use, 5 tries, one live code per address. Spaces and dashes are ignored.

### `POST /register`  `{email, password, age_confirmed?, terms_version?, ...}`

Step one of password sign-up. `202 {ok, verification_required: true, email,
resend_after, expires_in}`. No `access_token`, nothing stored. Then call
`/otp/verify` with the code, the same `password` and the consent fields.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_email`, `weak_password`, `age_requirement` | |
| 409 | `account_exists` (`methods`) | `methods` lists how that account signs in, e.g. `["password","google"]` |
| 403 | `account_closed` | |
| 429 / 503 | as `/otp/request` | |

### `POST /login`  `{email, password, fingerprint_hash?}`

| Status | `code` | When |
|---|---|---|
| 200 | | flat payload |
| 400 | `invalid_credentials` | wrong password or unknown address, one identical answer |
| 400 | `no_password` (`methods`, `auth_providers`, `social_only`) | the account signs in with a provider or a code |
| 403 | `email_unverified` | right password, address never proven. Request a code and verify it with the same password |
| 403 | `account_closed` | |
| 429 | `account_locked` | 8 wrong passwords, 15 minutes. A code still signs in and clears it |
| 429 | `rate_limited` | 30 per 10 minutes per IP |

### `POST /check-email`  `{email}`

For the email-first screen. `{exists: false}`, or
`{exists: true, has_password, methods}`, or `{exists: true, closed: true}`.
30 per 10 minutes per IP.

### `POST /request-password-reset`, `POST /reset-password`

Unchanged in shape (`new_password`). New: `/reset-password` is limited to 10
per 10 minutes per IP; a link is single use atomically; a completed reset
marks the email verified and burns every other link; one address gets 5 reset
mails an hour; closed accounts get none.

### `POST /validate-password`: removed

It answered whether a password was right for any address without touching the
lockout counter. `/login` is the only route that tests a password.

### Google: `GET /oauth/google/authorize`, `GET /oauth/google/callback`

`authorize?redirect=1` answers `302` to Google (use this: the browser stores
the binding cookie on a navigation in every setup). Without `redirect` it
answers `{authorize_url}` as before.

The callback redirects to `${FRONTEND_URL}/oauth/callback#token=<jwt>` or
`#error=<code>&provider=google`.

| `error` | Meaning |
|---|---|
| `invalid_state` | expired, already used, or finished in a different browser than started it |
| `access_denied` (and other provider codes) | the person cancelled, or the provider refused |
| `email_unverified` | the provider did not vouch for the address, so we neither link nor create |
| `no_email` | the provider returned no address |
| `link_not_allowed` | Apple, and the address already has an account |
| `account_closed` | |
| `rate_limited`, `oauth_failed`, `provider_not_configured`, `unknown_provider` | |

Linking, in order: (1) the provider subject is already linked: that account;
(2) the address has an account: link only when the provider vouches for the
address (Google `email_verified`; Microsoft only `xms_edov`), never Apple;
(3) otherwise create, again only for a vouched address.

The `state` is single use, lives 10 minutes, and is bound to the browser by
the HttpOnly cookie `aspirely_oauth` (`SameSite=Lax`, path `/api/auth/oauth`),
which stops a callback link started by someone else from signing a victim in
to the attacker's account. Google also gets PKCE (S256).

## Limits

All counted in MongoDB (`rate_limit_buckets`) through
`utils.security.check_rate_limit`, so they hold across workers and across the
old and new stack during a deploy. The earlier limits lived in each process.

## Data

- `users.email_verified`, `users.email_verified_at`.
- `users.auth_providers` may now be `["email_code"]` (created by a code, no
  password).
- `auth_email_codes`: `{email, code_hash, attempts, used, created_at,
  expires_at}`. TTL on `expires_at`. The app creates its indexes on first use.

## What the owner sets

**No new environment variable.** Codes use the mail settings already in
`/etc/jobpilot/stack.env` (`EMAIL_PROVIDER=zeptomail`, `ZEPTOMAIL_TOKEN`,
`EMAIL_FROM_ADDRESS`) and Google keeps `GOOGLE_SIGNIN_CLIENT_ID` /
`GOOGLE_SIGNIN_CLIENT_SECRET`. `BACKEND_PUBLIC_URL` must start with `https://`
in production so the state cookie is marked `Secure`.

One owner-run step after deploy, dry run first:

```
python scripts/migrate_auth_indexes.py            # prints the plan, changes nothing
python scripts/migrate_auth_indexes.py --apply
```

It makes each provider subject unique (it stops and prints if two accounts
already share one).

## Deploy order

Backend first, frontend straight after. In between, the old sign-up page gets
`202` from `/register`, tries to sign in, and tells the person to sign in to an
account that does not exist yet.

## Not done: Microsoft and Apple

Deliberately skipped on 2026-11-15. Before either is switched on:

- **Both:** carry their variables through `infra/compose/render_stack_env.py`
  (it drops them today; the Apple key must be one line with literal `\n`).
- **Microsoft:** the app registration must emit the optional ID-token claims
  `email` and `xms_edov`. Without `xms_edov` every Microsoft sign-in is
  refused with `email_unverified`, which is the safe failure.
- **Apple:** its callback is a cross-site POST, which the `SameSite=Lax`
  state cookie does not ride, so Apple's state is not bound to the browser
  (`oauth_service.binds_browser`). It needs its own `SameSite=None; Secure`
  cookie. The `user` form field (the name, sent once) is still ignored.
- **Both:** the terms and age step for a first-time provider sign-up. Today
  the page shows the agreement line and nothing is recorded.
