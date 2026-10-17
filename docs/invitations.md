# Registration invitations

The owner can create email-bound invitations through a database command.
Issuance does not create an account or change registration mode. The API can
redeem invitations in `invite` mode through the browser or API. Registration
still defaults to `closed`.

## Run the owner command

Use the same Python environment, database path and filesystem permissions as the
API. From `backend`, apply migrations to the selected database first:

```bash
export ISOJAM_DATABASE_PATH=/var/lib/isojam/metadata/isojam.db
python -m alembic upgrade head
python -m app.invitations create friend@example.com
```

Leave `ALEMBIC_DATABASE_URL` unset for normal use so the migration and command
select the same path. The command follows `ISOJAM_DATABASE_PATH`, independent of
the current working directory. It does not start FastAPI, load inference packages
or require `ISOJAM_JWT_SECRET_KEY`.

The default lifetime is seven days. To choose a different lifetime:

```bash
python -m app.invitations create friend@example.com --expires-in-days 2
```

The accepted range is 1-30 whole days. Invalid email addresses or lifetimes exit
with code 2 before initializing the database path. Emails use the same Pydantic
`EmailStr` validation and lowercase normalization as registration. An address
that is already registered is rejected with exit code 1.

For an existing API container with the migrated persistent volume attached:

```bash
docker exec isojam-api python -m app.invitations create friend@example.com
```

This is an owner command requiring direct database access. There is no public
invitation-creation endpoint or email-delivery integration.

## Output and stored data

After committing, the command prints one JSON line containing `id`, normalized
`email`, `code`, `created_at` and `expires_at`. The timestamps include UTC offsets.
The `code` value is the secret to share privately with the intended recipient.
The command does not send it anywhere.

Each code uses 32 cryptographically random bytes, encoded as 43 URL-safe
characters. Only its SHA-256 hash is persisted. The raw code is not recoverable
from the invitation record, and is excluded from the issued object's normal
Python representation. This differs from a human-chosen password: invitation
codes already have high randomness, while account passwords retain Argon2 hashing.

The new `registration_invitations` table stores:

| Field | Meaning |
| --- | --- |
| `id` | Invitation UUID |
| `email` | Normalized intended account address |
| `code_hash` | Unique lowercase SHA-256 digest of the code |
| `created_at` | Time the invitation was issued |
| `expires_at` | Exclusive redemption deadline |
| `used_at` | Initially null; recorded with successful account creation |

Database constraints enforce normalized email storage, digest format/uniqueness,
expiry after creation, and a use timestamp within the validity interval. Python
returns timezone-aware UTC dates through the existing `UTCDateTime` type.

An expiration timestamp does not delete a row or run scheduled work. Redemption
checks the deadline and unused state atomically when creating the user. Issuance
and redemption do not verify email ownership.

Each command creates an independent invitation. Multiple invitations for one
email are allowed; issuing a new code does not invalidate earlier ones. If a
code is lost, another can be issued, while older codes retain their own expiry.
Revocation and automatic removal of expired records are not implemented yet.

## Redeem in the browser

Start the API with `ISOJAM_REGISTRATION_MODE=invite`. On the studio page, select
**Create account**, enter the invited email, choose a password and paste the code
into **Invitation code**. Codes are case-sensitive and must be pasted exactly.
The field appears only during invitation-only registration and is required there.
Open registration needs no code; closed registration hides account creation while
keeping sign-in available. The page reads this policy from `/ui-info`; reload
after the server's registration policy changes. The API enforces the policy on
every registration request, regardless of the browser controls.

The browser sends the code only to `/register`, keeps it out of application browser
storage, and clears the field after account creation, on returning to sign-in,
on sign-out and when leaving the page. Invalid registration preserves the field
for correction and shows the API's generic error. Successful registration normally
signs in automatically. If that login fails, the page confirms account creation
and switches to sign-in, so retrying does not attempt to consume the code again.
If the registration response itself is lost, try signing in before registering again.

## Redeem through the API

Start or restart the API with `ISOJAM_REGISTRATION_MODE=invite`, retaining the
usual JWT secret, database and storage settings. Then submit:

```json
{
  "email": "friend@example.com",
  "password": "your-password",
  "invitation_code": "<code-from-the-owner>"
}
```

Use `POST /register`, including the complete, case-sensitive code. Success returns
`201` with the account's ID and normalized email. Sign in through `POST /login`
as usual. The code does not appear in the registration response or the request
model's normal representation.

For well-formed requests, missing, incorrect, expired, used or email-mismatched
codes return `403` with `{"detail":"Invitation is invalid or unavailable"}`.
The same response applies if another account already owns the email. Malformed
request fields still receive normal `422` validation errors; oversized bodies
receive `413`. Retry after successful registration does not create another
account; use login if the original response was lost.

The preliminary lookup rejects invalid invitations before password hashing.
Its read transaction is ended before hashing. The final operation then obtains
SQLite's write lock with `BEGIN IMMEDIATE`, reads the current UTC time after any
wait, and conditionally updates a matching unused invitation whose creation time
is not in the future and whose expiry is strictly later than that time. The user
INSERT follows in the same transaction. Neither change commits independently.

This final check is required even after a successful preflight: the invitation
could expire or another request could redeem it while hashing or waiting.
Different invitations for one email are also protected by the user's unique
email constraint; a losing transaction rolls back its own invitation claim.

`open` registration works without a code and ignores a supplied one without
consuming it. `closed` registration rejects requests regardless of their code.
Existing users can log in in all three modes. No additional migration is needed
for redemption; it uses the existing invitation table.

## Transaction and migration behavior

The repository inserts and flushes within its caller's transaction. The issuing
service never commits. The CLI commits before printing the code; database errors
return exit code 1 without printing a code or SQL parameters. Check the configured
path, permissions and applied migrations when that happens.

Migration `c8f2a6139d40` adds only the invitation table after `b5d8f3027a91`.
Existing accounts, uploads, jobs, attempts, receipts and audio remain intact.
Downgrading removes invitation history; re-upgrading creates an empty invitation
table and cannot recover previously issued codes.
