-- P1b B0 foundation only. No real mailbox/card/cloud data and no live adapters.
-- All objects are created in the validated transaction-local search_path.
CREATE TABLE schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    checksum char(64) NOT NULL CHECK (checksum ~ '^[a-f0-9]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE operators (
    id uuid PRIMARY KEY,
    username_norm text NOT NULL UNIQUE CHECK (length(username_norm) BETWEEN 1 AND 128),
    password_hash text NOT NULL,
    permissions text[] NOT NULL DEFAULT '{}',
    disabled boolean NOT NULL DEFAULT false,
    auth_epoch bigint NOT NULL DEFAULT 1 CHECK (auth_epoch > 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE global_configs (
    id uuid PRIMARY KEY,
    revision text NOT NULL UNIQUE CHECK (length(revision) BETWEEN 1 AND 128),
    nonsecret_config jsonb NOT NULL CHECK (jsonb_typeof(nonsecret_config) = 'object'),
    secret_refs jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(secret_refs) = 'object'),
    changed_by uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE onboarding_batches (
    id uuid PRIMARY KEY,
    selection_mode text NOT NULL CHECK (selection_mode IN ('specified', 'automatic')),
    requested_count integer NOT NULL CHECK (requested_count > 0),
    selected_mailbox_refs jsonb NOT NULL CHECK (jsonb_typeof(selected_mailbox_refs) = 'array'),
    config_id uuid NOT NULL REFERENCES global_configs(id) ON DELETE RESTRICT,
    created_by uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (jsonb_array_length(selected_mailbox_refs) = requested_count)
);

CREATE TABLE onboarding_tasks (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES onboarding_batches(id) ON DELETE RESTRICT,
    mailbox_ref text NOT NULL CHECK (length(mailbox_ref) BETWEEN 1 AND 256),
    status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN
      ('QUEUED','PREFLIGHT','RUNNING','PAUSED','WAIT_HUMAN','WAIT_ADMIN','WAIT_RESOURCE',
       'RECONCILING','FAILED_CONFIRMED','CANCELLED_SAFE','SUCCEEDED','CONFLICT')),
    reason_code text,
    current_step text,
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    config_id uuid NOT NULL REFERENCES global_configs(id) ON DELETE RESTRICT,
    cancel_requested boolean NOT NULL DEFAULT false,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task_steps (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
    step_key text NOT NULL CHECK (length(step_key) BETWEEN 1 AND 128),
    generation bigint NOT NULL CHECK (generation > 0),
    state text NOT NULL DEFAULT 'NOT_SENT' CHECK (state IN
      ('NOT_SENT','RUNNING','INTENT','UNKNOWN','SUCCEEDED','FAILED_CONFIRMED','CONFLICT','CANCELLED_SAFE')),
    fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
    observation_code text,
    started_at timestamptz,
    finished_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX steps_one_generation ON task_steps(task_id, step_key, generation);

CREATE TABLE resource_leases (
    resource_kind text NOT NULL CHECK (resource_kind IN ('mailbox','card','fixture')),
    resource_id text NOT NULL CHECK (length(resource_id) BETWEEN 1 AND 256),
    task_id uuid REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
    owner_id text,
    fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
    lease_until timestamptz,
    hold_reason text,
    stopped_evidence_ref text,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(resource_kind, resource_id),
    CHECK (owner_id IS NULL OR (task_id IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK (task_id IS NOT NULL OR (owner_id IS NULL AND hold_reason IS NULL))
);

CREATE TABLE operation_receipts (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
    action text NOT NULL CHECK (length(action) BETWEEN 1 AND 128),
    resource_revision text NOT NULL CHECK (length(resource_revision) BETWEEN 1 AND 128),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    phase text NOT NULL CHECK (phase IN ('NOT_SENT','INTENT','UNKNOWN','SUCCEEDED','FAILED_CONFIRMED','CONFLICT')),
    fence bigint NOT NULL CHECK (fence >= 0),
    result_code text,
    external_ref text,
    generation bigint NOT NULL CHECK (generation > 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX receipts_request_unique ON operation_receipts(task_id, action, idempotency_key);
CREATE UNIQUE INDEX receipts_one_unresolved ON operation_receipts(task_id, action, resource_revision)
    WHERE phase IN ('INTENT','UNKNOWN','CONFLICT');

CREATE TABLE audit_events (
    id bigserial PRIMARY KEY,
    actor_id uuid REFERENCES operators(id) ON DELETE RESTRICT,
    task_id uuid REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
    action text NOT NULL,
    object_ref text NOT NULL,
    outcome_code text NOT NULL,
    correlation_id text NOT NULL,
    before_summary jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(before_summary) = 'object'),
    after_summary jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(after_summary) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE operator_sessions (
    id uuid PRIMARY KEY,
    operator_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
    token_hash char(64) NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
    csrf_hash char(64) NOT NULL CHECK (csrf_hash ~ '^[a-f0-9]{64}$'),
    auth_epoch bigint NOT NULL CHECK (auth_epoch > 0),
    expires_at timestamptz NOT NULL,
    idle_expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (idle_expires_at <= expires_at)
);

CREATE TABLE auth_throttles (
    bucket_key text PRIMARY KEY CHECK (length(bucket_key) BETWEEN 1 AND 256),
    window_start timestamptz NOT NULL,
    failures integer NOT NULL DEFAULT 0 CHECK (failures >= 0),
    blocked_until timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE secret_objects (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('fixture','google_credential','pan','service_account_json')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    key_version text NOT NULL CHECK (length(key_version) BETWEEN 1 AND 128),
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) >= 16),
    access_policy text NOT NULL,
    expires_at timestamptz,
    revoked_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX secret_nonce_unique ON secret_objects(key_version, nonce);

CREATE TABLE approvals (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
    action text NOT NULL CHECK (action IN ('test','enable','download')),
    resource_revision text NOT NULL CHECK (length(resource_revision) BETWEEN 1 AND 128),
    config_revision text NOT NULL REFERENCES global_configs(revision) ON DELETE RESTRICT,
    actor_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    revoked_at timestamptz,
    receipt_id uuid REFERENCES operation_receipts(id) ON DELETE RESTRICT,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (consumed_at IS NULL OR receipt_id IS NOT NULL)
);
CREATE UNIQUE INDEX approvals_one_open ON approvals(task_id, action, resource_revision)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE TABLE download_grants (
    id uuid PRIMARY KEY,
    approval_id uuid NOT NULL UNIQUE REFERENCES approvals(id) ON DELETE RESTRICT,
    secret_id uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
    secret_revision bigint NOT NULL CHECK (secret_revision > 0),
    operator_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
    token_hash char(64) NOT NULL UNIQUE CHECK (token_hash ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    revoked_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
