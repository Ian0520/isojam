# Registration invitation issuance

The owner can create email-bound invitations through a database command.
Issuance does not create an account or change registration mode. Redemption and
the browser invitation field are the next slice; `invite` is not an accepted
registration mode yet. Registration still defaults to `closed`.

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
The `code` value is the secret to share privately with the intended recipient
once invitation redemption is available. The command does not send it anywhere.

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
| `expires_at` | Deadline future redemption must check |
| `used_at` | Initially null; reserved for successful redemption |

Database constraints enforce normalized email storage, digest format/uniqueness,
expiry after creation, and a use timestamp within the validity interval. Python
returns timezone-aware UTC dates through the existing `UTCDateTime` type.

An expiration timestamp does not delete a row or run scheduled work. Future
redemption must check the deadline and unused state atomically when creating the
user. Issuance alone does not verify email ownership.

Each command creates an independent invitation. Multiple invitations for one
email are allowed; issuing a new code does not invalidate earlier ones. If a
code is lost, another can be issued, while older codes retain their own expiry.
Revocation and automatic removal of expired records are not implemented yet.

## Transaction and migration behavior

The repository inserts and flushes within its caller's transaction. The issuing
service never commits. The CLI commits before printing the code; database errors
return exit code 1 without printing a code or SQL parameters. Check the configured
path, permissions and applied migrations when that happens.

Migration `c8f2a6139d40` adds only the invitation table after `b5d8f3027a91`.
Existing accounts, uploads, jobs, attempts, receipts and audio remain intact.
Downgrading removes invitation history; re-upgrading creates an empty invitation
table and cannot recover previously issued codes.
