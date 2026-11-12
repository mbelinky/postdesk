-- Store created through the 0.2.3 CLI, before posts.held existed.
BEGIN TRANSACTION;
CREATE TABLE metrics (
	id INTEGER NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	receipt_id INTEGER NOT NULL,
	channel VARCHAR(30) NOT NULL,
	name VARCHAR(200) NOT NULL,
	value_json TEXT NOT NULL,
	raw_json TEXT NOT NULL,
	fetched_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(receipt_id) REFERENCES receipts (id)
);
CREATE TABLE posts (
	id INTEGER NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	external_ref VARCHAR(200) NOT NULL,
	channel VARCHAR(30) NOT NULL,
	kind VARCHAR(30) NOT NULL,
	media_json TEXT NOT NULL,
	caption TEXT,
	first_comment TEXT,
	collaborators_json TEXT NOT NULL,
	link TEXT,
	title TEXT,
	scheduled_at DATETIME NOT NULL,
	status VARCHAR(30) NOT NULL,
	claim_token VARCHAR(64),
	claim_expires_at DATETIME,
	error TEXT,
	notify_error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant, external_ref)
);
INSERT INTO "posts" VALUES(1,'demo','legacy-post','facebook','text','[]','Existing scheduled post',NULL,'[]',NULL,NULL,'2099-10-09 10:00:00.000000','approved',NULL,NULL,NULL,NULL,'2026-11-12 08:22:36.963545','2026-11-12 08:22:37.242289');
CREATE TABLE publish_attempts (
	id INTEGER NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	post_id INTEGER NOT NULL,
	claim_token VARCHAR(64) NOT NULL,
	external_ref VARCHAR(200) NOT NULL,
	state VARCHAR(30) NOT NULL,
	remote_ids_json TEXT NOT NULL,
	pending_json TEXT,
	error TEXT,
	started_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	completed_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(post_id) REFERENCES posts (id),
	UNIQUE (claim_token)
);
CREATE TABLE receipts (
	id INTEGER NOT NULL,
	tenant VARCHAR(100) NOT NULL,
	post_id INTEGER NOT NULL,
	attempt_id INTEGER NOT NULL,
	channel VARCHAR(30) NOT NULL,
	external_id VARCHAR(200) NOT NULL,
	permalink TEXT,
	raw_json TEXT NOT NULL,
	published_at DATETIME NOT NULL,
	deleted_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (tenant, channel, external_id),
	FOREIGN KEY(post_id) REFERENCES posts (id),
	UNIQUE (attempt_id),
	FOREIGN KEY(attempt_id) REFERENCES publish_attempts (id)
);
CREATE INDEX ix_posts_status ON posts (status);
CREATE INDEX ix_posts_claim_token ON posts (claim_token);
CREATE INDEX ix_posts_tenant ON posts (tenant);
CREATE INDEX ix_posts_scheduled_at ON posts (scheduled_at);
CREATE INDEX ix_posts_channel ON posts (channel);
CREATE INDEX ix_publish_attempts_external_ref ON publish_attempts (external_ref);
CREATE INDEX ix_publish_attempts_state ON publish_attempts (state);
CREATE INDEX ix_publish_attempts_post_id ON publish_attempts (post_id);
CREATE INDEX ix_publish_attempts_tenant ON publish_attempts (tenant);
CREATE INDEX ix_receipts_post_id ON receipts (post_id);
CREATE INDEX ix_receipts_tenant ON receipts (tenant);
CREATE INDEX ix_metrics_fetched_at ON metrics (fetched_at);
CREATE INDEX ix_metrics_tenant ON metrics (tenant);
CREATE INDEX ix_metrics_receipt_id ON metrics (receipt_id);
COMMIT;
