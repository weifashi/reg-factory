-- Non-secret immutable plans and actual adapter observations. No mode promotion.
-- No provider I/O, automatic startup, grants to callers or secret plaintext.
CREATE TABLE live_run_plans (
 task_id uuid PRIMARY KEY,
 task_mode text NOT NULL DEFAULT 'live' CHECK (task_mode='live'),
 generation bigint NOT NULL CHECK (generation>0),
 config_revision text NOT NULL REFERENCES global_configs(revision) ON DELETE RESTRICT,
 owner_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
 plan jsonb NOT NULL CHECK (jsonb_typeof(plan)='object'),
 plan_digest char(64) NOT NULL CHECK (plan_digest ~ '^[a-f0-9]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY (task_id,task_mode) REFERENCES onboarding_tasks(id,execution_mode) ON DELETE RESTRICT
);
CREATE TABLE live_effect_observations (
 id uuid PRIMARY KEY,
 receipt_id uuid NOT NULL REFERENCES live_effect_journal(id) ON DELETE RESTRICT,
 task_id uuid NOT NULL REFERENCES live_run_plans(task_id) ON DELETE RESTRICT,
 action text NOT NULL,
 incoming_phase text NOT NULL CHECK (incoming_phase IN
   ('PENDING','BLOCKED','UNKNOWN','CONFLICT','SUCCEEDED','FAILED_CONFIRMED')),
 reason text NOT NULL CHECK (reason ~ '^[A-Z][A-Z0-9_]{0,63}$'),
 external_ref text CHECK (external_ref IS NULL OR length(external_ref) BETWEEN 1 AND 256),
 operation text CHECK (operation IS NULL OR operation ~ '^operations/[A-Za-z0-9._-]{1,180}$'),
 secret_ref uuid REFERENCES secret_objects(id) ON DELETE RESTRICT,
 account_id bigint CHECK (account_id IS NULL OR account_id>0),
 logout text CHECK (logout IS NULL OR logout IN ('NOT_REQUIRED','PASSED','FAILED')),
 scheduling text CHECK (scheduling IS NULL OR scheduling IN ('NOT_VERIFIED','DISABLED')),
 verification text CHECK (verification IS NULL OR verification='NOT_SENT'),
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX live_observations_receipt ON live_effect_observations(receipt_id,observed_at DESC,id DESC);
