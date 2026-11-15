-- Durable live effect journal only. No worker, adapter or migration auto-start.
-- Existing tasks remain synthetic; never infer live from a process flag.
ALTER TABLE onboarding_tasks ADD COLUMN execution_mode text NOT NULL DEFAULT 'synthetic'
 CHECK (execution_mode IN ('synthetic','live'));
ALTER TABLE onboarding_tasks ADD CONSTRAINT live_task_pool_scope
 CHECK (execution_mode <> 'live' OR execution_scope = 'pool');
ALTER TABLE onboarding_tasks ADD CONSTRAINT task_mode_identity UNIQUE (id,execution_mode);

CREATE TABLE live_effect_journal (
 id uuid PRIMARY KEY,
 task_id uuid NOT NULL,
 task_mode text NOT NULL DEFAULT 'live' CHECK (task_mode='live'),
 step_key text NOT NULL CHECK (step_key ~ '^[a-z][a-z0-9._:-]{0,127}$'),
 action text NOT NULL CHECK (action IN (
  'google.login.email.submit','google.login.password.submit','project.create',
  'billing.link','service.enable.vertex','service_account.create','project.iam.bind_sa',
  'service_account.key.create','sub2api.import_disabled')),
 generation bigint NOT NULL CHECK (generation>0),
 config_revision text NOT NULL REFERENCES global_configs(revision) ON DELETE RESTRICT,
 resource_revision char(64) NOT NULL CHECK (resource_revision ~ '^[a-f0-9]{64}$'),
 effect_digest char(64) NOT NULL CHECK (effect_digest ~ '^[a-f0-9]{64}$'),
 request_hash char(64) NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
 request_key text NOT NULL CHECK (request_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
 fence_bindings jsonb NOT NULL CHECK (jsonb_typeof(fence_bindings)='array'),
 phase text NOT NULL DEFAULT 'INTENT'
  CHECK (phase IN ('INTENT','UNKNOWN','SUCCEEDED','FAILED_CONFIRMED','CONFLICT')),
 result_code text CHECK (result_code IS NULL OR result_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
 evidence_ref text CHECK (evidence_ref IS NULL OR evidence_ref ~ '^evidence:[a-f0-9-]{36}$'),
 version bigint NOT NULL DEFAULT 1 CHECK (version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 observed_at timestamptz,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY (task_id,task_mode) REFERENCES onboarding_tasks(id,execution_mode) ON DELETE RESTRICT,
 UNIQUE (task_id,action,request_key),
 UNIQUE (task_id,generation,step_key,action)
);
CREATE INDEX live_effects_unresolved ON live_effect_journal(task_id,generation)
 WHERE phase IN ('INTENT','UNKNOWN','CONFLICT');
