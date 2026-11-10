# OfferPilot backend

`app.main.create_app(data_dir, *, origin, bootstrap_code=None, clock=None, notifier=None,
mail_adapter=None, credential_store=None, start_workers=True, autostart_callback=None,
static_dir=None)` builds the local API. The desktop launcher supplies the actual loopback
origin, protected data directory, process lock and autostart callback. The factory serves
the built frontend and resolves packaged migrations from `backend/migrations`.

The factory exposes `app.state.sessions.issue_bootstrap()` for a fresh single-use,
60-second bootstrap link. Session cookies are HttpOnly/SameSite Strict, expire after
12 hours, and all private API reads require the session. Mutations require the exact
origin and CSRF header. API errors never include exception strings or input values.

## API contracts

- Base path `/api/v1`; entities `{data,request_id}` and lists `{data,next_cursor,request_id}`.
- Task `priority` is **1/2/3**; status is `pending/in_progress/done/cancelled`.
  Date-only values never acquire an invented time. Explicit offset local timestamps
  must agree with the selected IANA timezone. UTC timestamps use RFC3339 `Z`.
- Task create accepts `title,kind,priority,temporal,notes?,application_id?`.
  PATCH accepts `{expected_version,changes}`. Complete/cancel/reopen take `expected_version`.
- Applications support persistent GET/POST/PATCH and manual task ownership. No automatic
  company merge, stage progression, calendar, AI, or P1 reschedule workflow is enabled.
- Email detail includes `candidates[]` with `candidate_id,title,kind,priority,temporal,
  evidence,issues,state,task_id`. Review body is
  `{expected_version,decisions:[{candidate_id,action:'accept'|'edit'|'ignore',changes?,application_id?}]}`.
  `{candidate_id:'all',action:'ignore'}` explicitly ignores an entire message, preserving
  existing tasks. Reschedule/cancellation candidates require editing the original task
  and then ignoring the candidate; they cannot create a second active schedule.
- Accounts accept `provider:'qq',address,authorization_code,initial_scan:'14'|'30'|'now'`.
  Responses expose masked addresses. Credentials exist only in Windows Credential Manager;
  replacement stages and tests a new reference before switching from the old reference.
- Import/sync/export/backup APIs return `{job_id}`. Job detail is
  `{id,kind,status,progress,result,error_code,created_at,updated_at}`. Artifact jobs use
  `result.artifact_id`; EML import uses `result.message_id`.
- Restore validation returns a job with `result.restore_id`. GET `/restores/{id}` provides
  `confirmation_token` and manifest/counts. Apply returns HTTP 202 with
  `{job_id,restored:true,session_reset:true,reauthorization_required:true}` after replacement.
  The client must reopen via the desktop launcher; the old session is deliberately cleared.
- EML limit is 10 MiB. Backup upload and total uncompressed data limit are 128 MiB.
  Artifacts expire in 24 hours; restore and privacy confirmation expire in 10 minutes.

## Storage and worker behavior

SQLAlchemy sessions are per-use-case, SQLite uses WAL/FULL/foreign_keys/busy_timeout,
and Alembic owns schema revision. Version checks and task/reminder changes share one
immediate transaction. API command results for task creation, import, and review commit
in the same transaction as business changes. Per-task locks order cancellation against
delivery. An exclusive maintenance barrier drains active notification and IMAP operations
before restore or privacy deletion.

The reminder loop runs every 15 seconds. Definite non-delivery retries at 30 and 120
seconds, at most three attempts. Uncertain results and interrupted sends are durable
`delivery_unknown` and never automatically retried. Expired plans are collected into one
recovery summary, including batches larger than 50 reminders. Windows delivery means
handed to the channel, not read by the user. The in-app center retains each outcome.

QQ uses TLS-verified IMAPS, read-only INBOX, UID cursors, BODYSTRUCTURE, and bounded
BODY.PEEK text sections. It never STOREs, EXPUNGEs, or sends email. Network calls are
outside SQL transactions. The first scan captures UIDNEXT; "now" skips previous UIDs.
UIDVALIDITY rebuilds from the original scan boundary with content/occurrence deduplication.
Mid-batch errors never advance over unsaved mail. Account sync is serialized and job state
survives restart; authenticated accounts must be reauthorized after restoring a backup.

MIME imports are normalized into text, HTML is never rendered, scripts/active content are
removed, remote resources are not loaded, and attachments are metadata only. Full dates
with explicit timezone, clear actions, and company evidence can create tasks; uncertain
dates, missing timezone/company, forwards, and schedule changes remain review candidates.
Relative times expose Date/INTERNALDATE candidates without treating sync time as receipt.

Snapshots use SQLite backup, not a copy of the active WAL file. Downloadable copies remove
credential references, command/session tokens, active leases, and optionally source text;
VACUUM removes deleted-page remnants. Restore verifies ZIP names/sizes/hashes, SQLite
integrity/FKs, exact schema, and task temporal values before confirmation. Internal rollback
snapshots are retained locally. CSV values that could be spreadsheet formulas are escaped.
CSV includes `kind=application` rows when applications were selected; JSON preserves both
collections and task ownership.

## Validation and remaining real-environment gates

Run `.venv/Scripts/python.exe -m pytest backend/tests -q` and
`.venv/Scripts/python.exe -m ruff check backend` from the repository root. All tests use
temporary databases and synthetic mail; no test reads real credentials or sends real
notifications. Injected notifier callables must themselves enforce bounded I/O. The
Windows adapter uses the local OS API and treats an exception after dispatch as unknown.

Real QQ compatibility/7-day monitoring, Windows notification identity/permission behavior,
sleep and shutdown on a clean user account, installer upgrade/rollback, and nontechnical
user acceptance remain real-environment gates; simulated tests do not satisfy them.
Rules intentionally prefer review over broad natural-language guessing. Restore currently
accepts the exact current schema revision; importing older backup formats requires a
future explicitly tested upgrade path. Long maintenance jobs run on a request worker and
record persistent completed/failed jobs; they are not resumable background job executions.
While running, the backup worker creates a sanitized snapshot every 24 hours and retains
five automatic ZIPs; manual and pre-restore/pre-upgrade snapshots are retained separately.
Reminder templates, quiet hours, timezone and privacy are editable. Schedule changes drain
active deliveries and transactionally rebuild plans for unfinished tasks. Title-only edits
preserve snoozed plans. Generic Windows notifications hide task names by default.
Mail network failures persist 30/60/120/300-second backoff with 0–20% jitter across restart;
authentication failures require reauthorization. Connection tests can recover an account
without falsely recording a completed sync. Removed accounts can be reconnected safely.
Schema upgrades first snapshot and migrate a trial copy, validate integrity/foreign keys,
then upgrade live storage; failure restores the snapshot. Future versions are refused.
