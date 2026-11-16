# Gmail sync: what only the owner can do in Google Cloud

Written 2026-11-15 with the `fix/gmail-sync` change. The code now tells users
the truth about each state. It cannot change the Google project. This page is
the list of things that need the project owner, and what users see until each
one is done.

## Where things stand

The Google OAuth consent screen for the Gmail client is in **Testing**
(`infra/terraform/lambda.tf`, the `GMAIL_OAUTH_ENABLED` note). Testing has two
effects, both Google's rules and neither something code can work around:

1. **Only people on the test-user list can connect.** Anyone else is stopped on
   Google's screen ("Access blocked"). The list holds at most 100 accounts.
2. **Every connection ends 7 days after consent.** Google expires the refresh
   token of a Testing app that asks for more than name, email and profile.
   `gmail.readonly` is more than that.

The second one is why sync "stopped working" about once a week. Before this
change the app kept saying "Gmail connected" while every sync failed.

## What users see until it is fixed

- Before connecting, the card says that Google will ask them to reconnect
  after 7 days while Aspirely waits for Google's review.
- After connecting, the card shows the date the connection ends.
- When Google ends it, the next sync (manual, or the background one that now
  runs every 30 minutes) marks the connection as needing a reconnect. The
  tracker shows **Reconnect Gmail** instead of "connected". Reconnecting the
  same mailbox continues from the last finished scan; it does not re-read 60
  days.
- A person who is not on the test-user list cannot connect at all. Google's
  screen stops them, so Aspirely cannot explain it there. Keep the list
  current, or expect support mail.

## What to do, in order

### 1. Today: confirm the server settings

On the box (read-only, prints setting names and three non-secret values):

```bash
sudo grep -E '^(GMAIL_OAUTH_ENABLED|GOOGLE_OAUTH_REDIRECT_URI|GOOGLE_OAUTH_CONSENT_MODE)=' /etc/jobpilot/stack.env
sudo grep -oE '^(GMAIL_TOKEN_ENCRYPTION_KEY|GOOGLE_OAUTH_CLIENT_ID|GOOGLE_OAUTH_CLIENT_SECRET)=' /etc/jobpilot/stack.env
```

Expected:

| Setting | Must be |
|---|---|
| `GMAIL_OAUTH_ENABLED` | `true`. Unset means OFF in production, and the tracker says Gmail is unavailable. |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://aspirely.me/oauth/gmail/callback`, and the same string under **Authorized redirect URIs** on the OAuth client in Google Cloud. A mismatch fails on Google's screen with `redirect_uri_mismatch`. |
| `GMAIL_TOKEN_ENCRYPTION_KEY` | Present, and **the same value AWS used**. Stored tokens were encrypted with it. Missing: Connect is hidden. Different: every existing connection fails to sync with "a problem on Aspirely's side" until the old value is restored (connections are deliberately not marked dead for this). |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Present. These are the Gmail client, not the Google sign-in client. |
| `GOOGLE_OAUTH_CONSENT_MODE` | Leave unset (means `testing`) until step 3 is done. |

After this branch is deployed, one command reports the same plus how the
connections stand (read-only, no values):

```bash
docker compose --env-file /etc/jobpilot/compose.env exec worker python -c \
  "from services.gmail_service import print_health; print_health()"
```

### 2. Today: keep the test-user list current

Google Cloud console, project of the Gmail OAuth client, **Google Auth
Platform > Audience > Test users**. Add the Google account of every person who
should be able to connect. Remove nobody who is connected.

Since 2026-11-16 the app keeps its own copy of this list, so people ask
before they are sent to Google:

1. A person presses **Request access** in the tracker and gives the Google
   address they will connect. If `GMAIL_ACCESS_NOTIFY_EMAIL` is set, that
   address gets an email.
2. **Admin portal > Gmail access** lists who is waiting. Copy the address, add
   it under **Test users** in Google Cloud, then press **Mark as added**.
3. The person is emailed, **Connect Gmail** appears for them, and Google's
   screen opens on the account you added.

The panel shows places used out of 100 and refuses another approval at the
cap. Use **Record an address you already added** for anyone on Google's list
who has not asked in the app. Anyone already connected is let through without
a record. `GMAIL_ACCESS_GATE=open` switches the asking off.

Two things Google's pages say, worth knowing before the list fills:

- A test user "consumes a project's test user quota once added". The page does
  not say removing one frees the place, so treat the 100 as spent when used.
- An app that is **In production but not verified** is also capped at 100 new
  users, counted "over the entire lifetime of the project", and shows an
  "unverified app" screen before consent. The 7-day expiry is described only
  for Testing. So publishing without verification would end the weekly
  reconnect and the hand-adding, at the price of that warning screen and the
  same ceiling. Confirm on the Audience page before choosing; it is your call.

### 3. Soon: publish the consent screen

Same page, **Publishing status > Publish app** (Testing to In production).

What I understand from Google's documentation, to be confirmed on that page
before pressing the button:

- The 7-day expiry applies to Testing. Publishing ends it for new consents.
  Existing connections still end on their original date and need one more
  reconnect.
- Until verification is finished, a published app that asks for a restricted
  scope shows Google's "unverified app" warning before consent and is capped
  at 100 users.

Then set `GOOGLE_OAUTH_CONSENT_MODE=production` in `/etc/jobpilot/stack.env`
and restart the stack. That one setting removes the 7-day wording and the
end date from the app. Do not set it while the screen is still in Testing: the
app would stop warning people about an expiry that still happens.

### 4. Required for a public launch: restricted-scope verification

`gmail.readonly` is a restricted scope. Google requires, for an app that keeps
that data on a server:

- a verified domain, a homepage, and a privacy policy that states the Limited
  Use commitments (the app's Privacy and Trust pages already carry them; check
  they match what is submitted);
- a written justification for the scope: read application emails to update the
  user's own tracker, read-only, no sending, no deleting, no advertising use,
  no human reading;
- a demo video showing the consent screen with the client ID in the address
  bar and the feature using the data;
- a security assessment, **CASA Tier 2**, by a lab Google authorises, repeated
  every year. This costs money and takes weeks; start it first.

Submit from **Google Auth Platform > Verification Center**.

A narrower scope would avoid the assessment only if the feature stopped
reading message bodies, which is what the status detection uses. That is a
product decision, not a configuration one.

## What changed in the code (so the two lists are not confused)

Fixed in `fix/gmail-sync`, no owner action needed: the dead-connection state
and Reconnect, one press running the whole scan, background sync every 30
minutes for connected users (`GMAIL_AUTO_SYNC_ENABLED=false` turns it off),
the empty-tracker message, the tracker's "outcome needs review" 400, clear
errors on connect, the encryption-key check, and the cleanup of abandoned
consent attempts. Contract: `docs/specs/gmail-tracker-contract.md`.
