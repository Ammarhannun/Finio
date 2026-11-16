# Manual encrypted backups on Supabase Free

The owner selected manual encrypted backups on 2 November 2026. The first real
encrypted backup and its isolated local database restore were verified that day.
No upgrade, scheduled job, public deployment or hosted-project restore was made.

`scripts/manual_backup.py` reads PostgreSQL and sends its custom-format dump
straight into age encryption. Only encrypted bytes reach the backup file. The
client verifies the database certificate and hostname, requests a read-only
session, hides the password prompt, and refuses to overwrite an existing file.
Failed runs remove the encrypted partial file. A completed file is mode 600 in
an owner-only directory. Passwords and database errors are not logged.

This is a database dump, not a complete copy of a Supabase project. Global
roles, dashboard/Auth configuration, API keys, Edge Functions, Storage file
contents, DNS and provider settings need separate recovery material. Database
records can include Auth credentials and session/factor information. Treat even
encrypted backups as sensitive. A lost private key makes the backup unusable.

## One-time preparation on your trusted computer

1. Install Docker Desktop and official [age tools](https://age-encryption.org/).
   Verify their release checksums. The tested tools were age 1.3.2 and a pinned
   PostgreSQL 17 client image. Do not run backup tools supplied by strangers or
   paste passwords into chat. Docker administrators on this computer can inspect
   a temporary client's password while it runs.
2. Create a private backup directory outside the repository, such as
   `$HOME/FinioBackups`, with permissions 700. Use a separate private location for
   the decryption identity. Avoid placing that identity in the backup directory,
   repository, web host or ordinary shared/cloud-sync folders.
3. Generate an age identity once. Replace these placeholder paths with your own:

   ```bash
   umask 077
   /PATH/TO/age-keygen -o /SEPARATE/PRIVATE/LOCATION/finio.agekey
   /PATH/TO/age-keygen -y /SEPARATE/PRIVATE/LOCATION/finio.agekey > /PRIVATE/CONFIG/finio-recipient.txt
   ```

   The `.agekey` file is secret. `finio-recipient.txt` contains only the public
   encryption recipient. Keep an additional protected offline copy of the
   private identity. Verify that both copies derive the same public recipient.
   If using an encrypted drive, keep its unlocking material separately too.
4. In your Supabase project, open Connect and copy the direct database hostname
   and project reference. Download the **server root certificate** from the
   project's database connection settings. This public PEM file must be readable
   by the non-root Docker client. Never substitute a certificate from an
   untrusted connection. Do not disable verification to make a failure go away.
5. Direct Free-plan connections may require IPv6. For IPv4, choose **Session
   pooler** in Connect and copy its hostname. This tool always uses port 5432.
   Transaction pooler port 6543 is not supported for this procedure.
6. Pull the exact tested client image once. The backup command uses
   `--pull=never`, so it will not silently fetch another image during a backup:

   ```bash
   docker pull pgvector/pgvector@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8
   ```

   Keep Docker and backup tools maintained. PostgreSQL 17 pg_dump supports
   servers through major version 17; review this client before a database major
   upgrade. This tool fails rather than backing up a newer unsupported server.

## Make a backup manually

From the Finio repository, in your own terminal:

```bash
python -m scripts.manual_backup \
  --project-ref YOUR_PROJECT_REFERENCE \
  --server-ca /PATH/TO/DOWNLOADED/server-ca.pem \
  --recipient-file /PRIVATE/CONFIG/finio-recipient.txt \
  --age /PATH/TO/age \
  --output "$HOME/FinioBackups/finio-YYYY-MM-DD-HHMM.dump.age"
```

For an IPv4 session-pooler connection, also supply:

```text
--db-host YOUR-SESSION-POOLER-HOST.pooler.supabase.com
```

Use the exact hostname from Connect. The tool derives the right database
username from the project reference. It asks for the **database password** in a
hidden terminal prompt. That is different from the Supabase account password,
Finio password and API keys. Do not put it in a command, `.env` file or message.
If you do not know it, stop and plan credential recovery separately. Do not reset
it casually, since other services may depend on it.

`--help` lists optional Docker host/config flags. The command has a ten-minute
limit. A permission, TLS, connectivity, server-version or tool failure gives a
generic message and nonzero exit. Check those details privately rather than
posting connection strings or raw diagnostics. After an abrupt power loss or
forced kill, check for this tool's `finio-backup-*` Docker client and encrypted
`.partial` file. Ordinary cancellation and termination clean them up.

A successful run prints the encrypted file size, SHA-256 checksum and creation
time, with `restore_verified: false`. Save that receipt privately. Copy the
encrypted file to a separate owner-controlled device or encrypted storage
location, verify its checksum there, and keep the private identity elsewhere.
The checksum detects accidental copy damage; it does not replace age's
authenticated decryption or a restore test.

Run this before each database migration and at least daily while accepting
customer uploads. Manual means you must remember to run it. Track the last
successful off-host copy and last successful recovery test. Data since the last
backup can be lost. This procedure does not provide point-in-time recovery.
Choose and document a retention period before public use. A starting operational
proposal is seven daily copies and four weekly copies, subject to your privacy
policy and deletion obligations. No automatic retention or deletion is configured.

## Verify recovery without risking the running database

The tested local drill uses invented records, the same encrypted-stream helper,
and an isolated container with no external network or published port:

```bash
python -m scripts.recovery_drill \
  --age-directory /PATH/TO/AGE-TOOLS \
  --output-directory /PRIVATE/NEW/DRILL-DIRECTORY
```

It compares 12 tables, checks restored owner/MFA/session policies and rejects
corrupted ciphertext. Its temporary test identity is deleted. Its encrypted
fixture is therefore test evidence, not a recoverable real backup.

The first real backup was restored in a disposable, offline container using the
matching Supabase PostgreSQL image. All nine financial tables matched the live
row counts and SHA-256 fingerprints, including 1,575 transactions. Readiness,
owner isolation, missing-session denial and revoked-session denial passed. The
container used tmpfs, no host mount and no published port, and was removed.
No plaintext dump file was written or live data modified.

The first verified copy is in the owner's `~/FinioBackups` directory. Its
`.verification.json` records the later restore result; the original creation
receipt retains `restore_verified: false`, its state when the dump was created.
A separately encrypted `.recovery-metadata.json.age` companion records source
roles, membership, extensions and fingerprints, bound to that dump's checksum.
Copy the dump, encrypted companion and receipts together. Keep the private key
separate. On 3 November 2026, the encrypted dump, companion and both receipts
were copied to an owner-only Google Drive folder. A downloaded dump matched its
local SHA-256 checksum. The private identity was not uploaded. An independent
protected key copy remains pending; losing the only identity makes the encrypted
copies unusable. Subsequent backups still require a fresh off-device copy.
The normal backup command does not automatically refresh this companion for
future dumps. Refresh recovery metadata and retest after role/schema changes.

The local recovery procedure kept the default public schema and recreated
roles without login credentials. Assigning Supabase's managed event-trigger
ownership required temporary postgres superuser status inside the disposable
container, followed by reinstating its source NOSUPERUSER flag before checking
access. Never apply that test-only privilege adjustment casually to a live
project. Fingerprint comparisons use UTC and explicit C collation so different
database locale sort orders do not produce false mismatches.

A hosted Supabase project recovery still requires separate verification.
Follow Supabase's current restore guidance and account for managed
schema/role/extension differences. A full raw PostgreSQL dump cannot simply be
restored over an existing managed project without conflicts. Do not execute
`pg_restore --clean` against the running project. Do not pipe a real decrypted
backup into a public preview, logs or an AI tool. Review restored privileges,
RLS, MFA, session behavior, row completeness and storage/settings inventories
before allowing access. Keep recovered customer data isolated, restrict who can
see it, and remove the test copy securely afterward. Measure recovery time and
record which backup was verified.

## References

- [Supabase connection options](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Verified TLS and the server root certificate](https://supabase.com/docs/guides/database/psql)
- [Supabase database backups](https://supabase.com/docs/guides/platform/backups)
- [Restoring to another Supabase project](https://supabase.com/docs/guides/platform/migrating-within-supabase/dashboard-restore)
- [age documentation and official releases](https://github.com/FiloSottile/age)
