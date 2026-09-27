-- P1c isolated synthetic pools. No production policy or external consumers.
-- Applied only by the reviewed versioned migrator in its validated search_path.

CREATE TABLE mailbox_registry (
 id uuid PRIMARY KEY, owner_operator_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
 email_norm text NOT NULL UNIQUE CHECK(length(email_norm) BETWEEN 3 AND 320),
 source_type text NOT NULL CHECK(source_type IN ('outlook','icloud','other')),
 group_ref text NOT NULL DEFAULT '' CHECK(length(group_ref)<=128),
 credential_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 credential_version bigint NOT NULL DEFAULT 1 CHECK(credential_version>0),
 health text NOT NULL DEFAULT 'UNKNOWN' CHECK(health IN ('UNKNOWN','HEALTHY','NEEDS_REVIEW','DISABLED')),
 disabled boolean NOT NULL DEFAULT false,
 ever_registration_attempted boolean NOT NULL DEFAULT false,
 sale_eligibility text NOT NULL DEFAULT 'UNVERIFIED' CHECK(sale_eligibility IN ('UNVERIFIED','ELIGIBLE','INELIGIBLE')),
 pool_status text NOT NULL DEFAULT 'AVAILABLE' CHECK(pool_status IN ('AVAILABLE','EXPORTED','QUARANTINED')),
 source_fingerprint char(64), last_used_at timestamptz,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(NOT ever_registration_attempted OR sale_eligibility='INELIGIBLE'),
 CHECK(email_norm=lower(email_norm) AND email_norm !~ '[[:cntrl:]]')
);
CREATE INDEX mailboxes_owner_filters ON mailbox_registry(owner_operator_id,disabled,health,group_ref,id);
CREATE TABLE mailbox_platform_states (
 id uuid PRIMARY KEY, mailbox_id uuid NOT NULL REFERENCES mailbox_registry(id) ON DELETE RESTRICT,
 platform text NOT NULL CHECK(platform IN ('google','claude','chatgpt','grok','kiro','github','k12')),
 identity_status text NOT NULL DEFAULT 'UNKNOWN' CHECK(identity_status IN ('UNKNOWN','EXISTING','NEW_CONFIRMED')),
 usage_status text NOT NULL DEFAULT 'UNUSED' CHECK(usage_status IN ('UNUSED','RESERVED','SUCCEEDED','FAILED_CONFIRMED','UNKNOWN','CONFLICT','HISTORY_UNRECONCILED')),
 credential_ref uuid REFERENCES secret_objects(id) ON DELETE RESTRICT,
 credential_version bigint NOT NULL DEFAULT 1 CHECK(credential_version>0),
 last_task_id uuid REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
 evidence_ref text CHECK(evidence_ref IS NULL OR length(evidence_ref)<=256), checked_at timestamptz,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(mailbox_id,platform)
);
CREATE INDEX platform_filter ON mailbox_platform_states(platform,usage_status,mailbox_id);
CREATE TABLE billing_identities (
 id uuid PRIMARY KEY, owner_operator_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
 holder_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 address_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 country char(2) NOT NULL CHECK(country ~ '^[A-Z]{2}$'),
 validation_status text NOT NULL DEFAULT 'UNVERIFIED' CHECK(validation_status IN ('UNVERIFIED','USER_ATTESTED','NEEDS_REVIEW')),
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE payment_cards (
 id uuid PRIMARY KEY, owner_operator_id uuid NOT NULL REFERENCES operators(id) ON DELETE RESTRICT,
 alias text NOT NULL CHECK(length(alias) BETWEEN 1 AND 80),
 brand text NOT NULL CHECK(brand IN ('visa','mastercard','amex','other')),
 last4 char(4) NOT NULL CHECK(last4 ~ '^[0-9]{4}$'),
 expiry_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 pan_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 billing_identity_ref uuid NOT NULL REFERENCES billing_identities(id) ON DELETE RESTRICT,
 pan_fingerprint char(64) NOT NULL CHECK(pan_fingerprint ~ '^[a-f0-9]{64}$'),
 enabled boolean NOT NULL DEFAULT true, account_limit integer NOT NULL CHECK(account_limit BETWEEN 1 AND 10000),
 sms_channel_ref text CHECK(sms_channel_ref IS NULL OR length(sms_channel_ref)<=128),
 linked_count integer NOT NULL DEFAULT 0 CHECK(linked_count>=0),
 reserved_count integer NOT NULL DEFAULT 0 CHECK(reserved_count>=0),
 reconciliation_required boolean NOT NULL DEFAULT false, last_assigned_at timestamptz,
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(pan_fingerprint),
 CHECK(reconciliation_required OR linked_count+reserved_count<=account_limit)
);
CREATE INDEX cards_candidates ON payment_cards(owner_operator_id,enabled,reconciliation_required,linked_count,last_assigned_at,id);
CREATE TABLE card_reservations (
 id uuid PRIMARY KEY, card_id uuid NOT NULL REFERENCES payment_cards(id) ON DELETE RESTRICT,
 task_id uuid NOT NULL REFERENCES onboarding_tasks(id) ON DELETE RESTRICT,
 account_ref uuid NOT NULL REFERENCES mailbox_platform_states(id) ON DELETE RESTRICT,
 phase text NOT NULL CHECK(phase IN ('NOT_SENT','INTENT','UNKNOWN','SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE','CONFLICT')),
 conflict_detected boolean NOT NULL DEFAULT false,
 operation_id uuid NOT NULL REFERENCES operation_receipts(id) ON DELETE RESTRICT,
 card_revision bigint NOT NULL CHECK(card_revision>0), billing_revision bigint NOT NULL CHECK(billing_revision>0),
 pan_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 expiry_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 billing_identity_ref uuid NOT NULL REFERENCES billing_identities(id) ON DELETE RESTRICT,
 holder_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 address_secret_ref uuid NOT NULL REFERENCES secret_objects(id) ON DELETE RESTRICT,
 evidence_ref text CHECK(evidence_ref IS NULL OR length(evidence_ref)<=256),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(operation_id), UNIQUE(id,card_id,account_ref)
);
CREATE UNIQUE INDEX card_one_open_binding ON card_reservations(card_id)
 WHERE phase IN ('NOT_SENT','INTENT','UNKNOWN','CONFLICT');
CREATE INDEX reservations_task ON card_reservations(task_id,created_at,id);
CREATE TABLE card_account_links (
 id uuid PRIMARY KEY, card_id uuid NOT NULL REFERENCES payment_cards(id) ON DELETE RESTRICT,
 account_ref uuid NOT NULL REFERENCES mailbox_platform_states(id) ON DELETE RESTRICT,
 reservation_id uuid NOT NULL UNIQUE,
 billing_resource_ref text NOT NULL CHECK(length(billing_resource_ref) BETWEEN 1 AND 256),
 evidence_ref text NOT NULL CHECK(length(evidence_ref) BETWEEN 1 AND 256),
 confirmed_at timestamptz NOT NULL,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(card_id,account_ref),
 FOREIGN KEY(reservation_id,card_id,account_ref) REFERENCES card_reservations(id,card_id,account_ref) ON DELETE RESTRICT
);
CREATE INDEX links_account ON card_account_links(account_ref,confirmed_at,id);

ALTER TABLE global_configs ADD COLUMN scope text NOT NULL DEFAULT 'fixture' CHECK(scope IN ('fixture','pool'));
ALTER TABLE onboarding_tasks ADD COLUMN execution_scope text NOT NULL DEFAULT 'fixture' CHECK(execution_scope IN ('fixture','pool'));
ALTER TABLE onboarding_tasks ADD COLUMN mailbox_id uuid REFERENCES mailbox_registry(id) ON DELETE RESTRICT;
ALTER TABLE onboarding_tasks ADD COLUMN platform text CHECK(platform IN ('google','claude','chatgpt','grok','kiro','github','k12','combined'));
ALTER TABLE onboarding_tasks ADD COLUMN platform_plan jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(platform_plan)='array');
ALTER TABLE onboarding_tasks ADD COLUMN credential_version bigint CHECK(credential_version>0);
ALTER TABLE onboarding_tasks ADD COLUMN mailbox_credential_ref uuid REFERENCES secret_objects(id) ON DELETE RESTRICT;
ALTER TABLE onboarding_tasks ADD COLUMN platform_credential_pins jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(platform_credential_pins)='object');
ALTER TABLE onboarding_tasks ADD CONSTRAINT pool_task_identity CHECK(
 (execution_scope='fixture' AND mailbox_id IS NULL AND platform IS NULL AND credential_version IS NULL AND mailbox_credential_ref IS NULL AND platform_plan='[]'::jsonb AND platform_credential_pins='{}'::jsonb) OR
 (execution_scope='pool' AND mailbox_id IS NOT NULL AND platform IS NOT NULL AND credential_version IS NOT NULL AND mailbox_credential_ref IS NOT NULL));
CREATE INDEX mailbox_active_tasks ON onboarding_tasks(mailbox_id,id)
 WHERE execution_scope='pool' AND status NOT IN ('SUCCEEDED','FAILED_CONFIRMED','CANCELLED_SAFE');
ALTER TABLE operation_receipts ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE operation_receipts ADD COLUMN scope_operator_id uuid REFERENCES operators(id) ON DELETE RESTRICT;
ALTER TABLE operation_receipts ADD COLUMN result_summary jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(result_summary)='object');
ALTER TABLE operation_receipts ADD CONSTRAINT receipt_scope_exact CHECK(
 (task_id IS NOT NULL AND scope_operator_id IS NULL) OR (task_id IS NULL AND scope_operator_id IS NOT NULL));
ALTER TABLE operation_receipts ADD CONSTRAINT admin_receipt_terminal CHECK(
 task_id IS NOT NULL OR phase IN ('SUCCEEDED','FAILED_CONFIRMED'));
CREATE UNIQUE INDEX admin_receipt_request ON operation_receipts(scope_operator_id,action,idempotency_key) WHERE task_id IS NULL;
ALTER TABLE secret_objects DROP CONSTRAINT secret_objects_kind_check;
ALTER TABLE secret_objects ADD CONSTRAINT secret_objects_kind_check CHECK(kind IN
 ('fixture','google_credential','pan','service_account_json','mailbox_credential','platform_credential','billing_holder','billing_address','card_expiry'));

-- Shape validators are pure; ownership and referenced secret kinds are also
-- checked by pool services, not delegated to client-controlled JSON.
CREATE FUNCTION valid_platform_plan(platform_value text, plan jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE item jsonb; seen text[] := '{}'; name text;
BEGIN
 IF platform_value IS NULL OR plan IS NULL OR jsonb_typeof(plan) <> 'array' THEN RETURN false; END IF;
 IF platform_value IN ('google','claude','chatgpt','grok','kiro','github','k12') THEN
  RETURN plan = jsonb_build_array(platform_value);
 END IF;
 IF platform_value <> 'combined' OR jsonb_array_length(plan) NOT BETWEEN 2 AND 6 THEN RETURN false; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(plan) LOOP
  IF jsonb_typeof(item) <> 'string' THEN RETURN false; END IF;
  name := item #>> '{}';
  IF name NOT IN ('claude','chatgpt','grok','kiro','github','k12') OR name = ANY(seen) THEN RETURN false; END IF;
  seen := array_append(seen, name);
 END LOOP;
 RETURN true;
END;
$$;

CREATE FUNCTION valid_platform_credential_pins(plan jsonb, pins jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE item jsonb; pin jsonb; platform_name text; revision_text text;
 uuid_pattern constant text := '^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$';
BEGIN
 IF plan IS NULL OR pins IS NULL OR jsonb_typeof(plan) <> 'array' OR jsonb_typeof(pins) <> 'object' THEN RETURN false; END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(pins)) <> jsonb_array_length(plan) THEN RETURN false; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(plan) LOOP
  IF jsonb_typeof(item) <> 'string' THEN RETURN false; END IF;
  platform_name := item #>> '{}';
  IF NOT (pins ? platform_name) THEN RETURN false; END IF;
  pin := pins -> platform_name;
  IF jsonb_typeof(pin) <> 'object' THEN RETURN false; END IF;
  IF NOT (pin ?& ARRAY['state_id','secret_ref','revision','identity_status']) THEN RETURN false; END IF;
  IF (SELECT count(*) FROM jsonb_object_keys(pin)) <> 4 THEN RETURN false; END IF;
  IF jsonb_typeof(pin->'state_id') <> 'string' OR (pin->>'state_id') !~ uuid_pattern THEN RETURN false; END IF;
  IF jsonb_typeof(pin->'secret_ref') <> 'null' THEN
   IF jsonb_typeof(pin->'secret_ref') <> 'string' OR (pin->>'secret_ref') !~ uuid_pattern THEN RETURN false; END IF;
  END IF;
  IF jsonb_typeof(pin->'identity_status') <> 'string' OR (pin->>'identity_status') NOT IN ('UNKNOWN','EXISTING','NEW_CONFIRMED') THEN RETURN false; END IF;
  IF jsonb_typeof(pin->'revision') <> 'number' THEN RETURN false; END IF;
  revision_text := pin->>'revision';
  IF revision_text !~ '^[1-9][0-9]{0,18}$' THEN RETURN false; END IF;
  IF revision_text::numeric > 9223372036854775807 THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END;
$$;

ALTER TABLE onboarding_tasks ADD CONSTRAINT pool_task_platform_plan CHECK(
 execution_scope='fixture' OR valid_platform_plan(platform,platform_plan));
ALTER TABLE onboarding_tasks ADD CONSTRAINT pool_task_credential_pins CHECK(
 execution_scope='fixture' OR valid_platform_credential_pins(platform_plan,platform_credential_pins));

CREATE FUNCTION mailbox_history_monotonic()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (OLD.ever_registration_attempted AND NOT NEW.ever_registration_attempted)
    OR (OLD.sale_eligibility='INELIGIBLE' AND NEW.sale_eligibility<>'INELIGIBLE') THEN
  RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='mailbox history is irreversible';
 END IF;
 IF NEW.ever_registration_attempted THEN NEW.sale_eligibility := 'INELIGIBLE'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER mailbox_history_monotonic BEFORE UPDATE ON mailbox_registry
 FOR EACH ROW EXECUTE FUNCTION mailbox_history_monotonic();

CREATE FUNCTION card_identity_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(OLD.owner_operator_id,OLD.pan_fingerprint,OLD.pan_secret_ref)
    IS DISTINCT FROM ROW(NEW.owner_operator_id,NEW.pan_fingerprint,NEW.pan_secret_ref) THEN
  RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='card identity is immutable';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER card_identity_immutable BEFORE UPDATE ON payment_cards
 FOR EACH ROW EXECUTE FUNCTION card_identity_immutable();

CREATE FUNCTION reservation_identity_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(OLD.card_id,OLD.task_id,OLD.account_ref,OLD.operation_id,OLD.card_revision,OLD.billing_revision,
        OLD.pan_secret_ref,OLD.expiry_secret_ref,OLD.billing_identity_ref,OLD.holder_secret_ref,OLD.address_secret_ref)
    IS DISTINCT FROM
    ROW(NEW.card_id,NEW.task_id,NEW.account_ref,NEW.operation_id,NEW.card_revision,NEW.billing_revision,
        NEW.pan_secret_ref,NEW.expiry_secret_ref,NEW.billing_identity_ref,NEW.holder_secret_ref,NEW.address_secret_ref) THEN
  RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reservation identity is immutable';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER reservation_identity_immutable BEFORE UPDATE ON card_reservations
 FOR EACH ROW EXECUTE FUNCTION reservation_identity_immutable();

CREATE FUNCTION reservation_count_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_count integer := 0; new_count integer := 0; target_card uuid;
BEGIN
 IF TG_OP <> 'INSERT' THEN
  old_count := CASE WHEN OLD.phase IN ('NOT_SENT','INTENT','UNKNOWN','CONFLICT') THEN 1 ELSE 0 END;
  target_card := OLD.card_id;
 END IF;
 IF TG_OP <> 'DELETE' THEN
  new_count := CASE WHEN NEW.phase IN ('NOT_SENT','INTENT','UNKNOWN','CONFLICT') THEN 1 ELSE 0 END;
  target_card := NEW.card_id;
 END IF;
 PERFORM id FROM payment_cards WHERE id=target_card FOR UPDATE;
 IF old_count <> new_count THEN
  UPDATE payment_cards SET reserved_count=reserved_count+new_count-old_count,
   version=version+1, updated_at=clock_timestamp() WHERE id=target_card;
 END IF;
 RETURN NULL;
END;
$$;
CREATE TRIGGER reservation_count_projection AFTER INSERT OR UPDATE OF phase OR DELETE ON card_reservations
 FOR EACH ROW EXECUTE FUNCTION reservation_count_projection();

CREATE FUNCTION link_count_projection()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM id FROM payment_cards WHERE id=NEW.card_id FOR UPDATE;
 UPDATE payment_cards SET linked_count=linked_count+1, version=version+1,
  updated_at=clock_timestamp() WHERE id=NEW.card_id;
 RETURN NULL;
END;
$$;
CREATE TRIGGER link_count_projection AFTER INSERT ON card_account_links
 FOR EACH ROW EXECUTE FUNCTION link_count_projection();

-- The versioned migrator grants only the two pure CHECK helpers to the app.
-- Trigger invocation requires no public directly callable trigger functions.
REVOKE ALL ON FUNCTION valid_platform_plan(text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION valid_platform_credential_pins(jsonb,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION mailbox_history_monotonic() FROM PUBLIC;
REVOKE ALL ON FUNCTION card_identity_immutable() FROM PUBLIC;
REVOKE ALL ON FUNCTION reservation_identity_immutable() FROM PUBLIC;
REVOKE ALL ON FUNCTION reservation_count_projection() FROM PUBLIC;
REVOKE ALL ON FUNCTION link_count_projection() FROM PUBLIC;
