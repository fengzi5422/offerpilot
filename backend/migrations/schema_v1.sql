CREATE TABLE api_commands (
	route VARCHAR NOT NULL,
	request_key VARCHAR NOT NULL,
	request_hash VARCHAR NOT NULL,
	status_code INTEGER NOT NULL,
	response JSON NOT NULL,
	expires_at INTEGER NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX uq_api_command ON api_commands (route, request_key);

CREATE TABLE artifacts (
	name VARCHAR NOT NULL,
	media_type VARCHAR NOT NULL,
	expires_at INTEGER NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE audit_events (
	entity_type VARCHAR NOT NULL,
	entity_id VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	detail JSON NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE email_messages (
	canonical_digest VARCHAR NOT NULL,
	message_id_header VARCHAR,
	subject VARCHAR NOT NULL,
	from_addr VARCHAR NOT NULL,
	header_date VARCHAR,
	internal_date VARCHAR,
	text_excerpt TEXT NOT NULL,
	text_hash VARCHAR NOT NULL,
	text_truncated BOOLEAN NOT NULL,
	links JSON NOT NULL,
	attachments JSON NOT NULL,
	state VARCHAR NOT NULL,
	ignored_by_user BOOLEAN NOT NULL,
	classification JSON NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_email_messages_canonical_digest ON email_messages (canonical_digest);

CREATE INDEX ix_email_messages_state ON email_messages (state);

CREATE TABLE mail_accounts (
	provider VARCHAR NOT NULL,
	address VARCHAR NOT NULL,
	credential_ref VARCHAR,
	state VARCHAR NOT NULL,
	initial_scan VARCHAR NOT NULL,
	scan_from INTEGER,
	uidvalidity VARCHAR,
	last_uid INTEGER NOT NULL,
	initial_upper_uid INTEGER,
	last_sync_at INTEGER,
	next_sync_at INTEGER,
	last_error_code VARCHAR,
	last_job_id VARCHAR,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (address)
);

CREATE TABLE maintenance_jobs (
	kind VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	progress JSON NOT NULL,
	result JSON,
	error_code VARCHAR,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE maintenance_tokens (
	kind VARCHAR NOT NULL,
	token_hash VARCHAR NOT NULL,
	expires_at INTEGER NOT NULL,
	value JSON NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE notification_items (
	reminder_id VARCHAR,
	task_id VARCHAR,
	kind VARCHAR NOT NULL,
	state VARCHAR NOT NULL,
	title VARCHAR NOT NULL,
	detail JSON NOT NULL,
	dismissed_at INTEGER,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (reminder_id)
);

CREATE TABLE settings (
	value JSON NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE mail_occurrences (
	message_id VARCHAR NOT NULL,
	occurrence_key VARCHAR NOT NULL,
	account_id VARCHAR,
	folder VARCHAR,
	uidvalidity VARCHAR,
	uid INTEGER,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(message_id) REFERENCES email_messages (id) ON DELETE CASCADE,
	UNIQUE (occurrence_key)
);

CREATE TABLE review_items (
	message_id VARCHAR NOT NULL,
	candidate JSON NOT NULL,
	state VARCHAR NOT NULL,
	task_id VARCHAR,
	action_key VARCHAR NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(message_id) REFERENCES email_messages (id) ON DELETE CASCADE,
	UNIQUE (action_key)
);

CREATE INDEX ix_review_items_message_id ON review_items (message_id);

CREATE INDEX ix_review_items_state ON review_items (state);

CREATE TABLE tasks (
	title VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	priority INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	temporal JSON NOT NULL,
	due_at INTEGER,
	starts_at INTEGER,
	ends_at INTEGER,
	due_date VARCHAR,
	confirmed BOOLEAN NOT NULL,
	manual_fields JSON NOT NULL,
	notes TEXT NOT NULL,
	source JSON,
	action_key VARCHAR,
	schedule_revision INTEGER NOT NULL,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	CHECK (version >= 1),
	CHECK (status IN ('pending','in_progress','done','cancelled')),
	CHECK (priority IN (1,2,3)),
	UNIQUE (action_key)
);

CREATE INDEX ix_tasks_due_at ON tasks (due_at);

CREATE INDEX ix_tasks_starts_at ON tasks (starts_at);

CREATE INDEX ix_tasks_status ON tasks (status);

CREATE TABLE reminders (
	task_id VARCHAR NOT NULL,
	schedule_revision INTEGER NOT NULL,
	offset_seconds INTEGER NOT NULL,
	scheduled_for INTEGER NOT NULL,
	fire_at INTEGER NOT NULL,
	expires_at INTEGER NOT NULL,
	state VARCHAR NOT NULL,
	channel VARCHAR NOT NULL,
	next_attempt_at INTEGER,
	lease_until INTEGER,
	attempt_count INTEGER NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	parent_reminder_id VARCHAR,
	error_code VARCHAR,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE,
	UNIQUE (idempotency_key)
);

CREATE INDEX ix_reminders_fire_at ON reminders (fire_at);

CREATE INDEX ix_reminders_state ON reminders (state);

CREATE INDEX ix_reminders_task_id ON reminders (task_id);

CREATE TABLE notification_attempts (
	reminder_id VARCHAR NOT NULL,
	attempt_no INTEGER NOT NULL,
	started_at INTEGER NOT NULL,
	finished_at INTEGER,
	result VARCHAR,
	id VARCHAR NOT NULL,
	created_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(reminder_id) REFERENCES reminders (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_attempt ON notification_attempts (reminder_id, attempt_no);
