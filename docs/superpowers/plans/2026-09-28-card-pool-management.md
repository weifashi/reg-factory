# Credit-card pool management implementation plan

> For agentic workers: use subagent-driven-development, TDD, then independent review. No commit, migration, production operation or provider call is authorized here.

**Goal:** Replace the card placeholder inside the original reg-factory console with owner-scoped persisted list, manual/CSV add, masked preview, metadata editing and enable/disable management.
**Architecture:** Reuse cards.import_csv/preview_import, current tables, vault policy, session/CSRF and operation receipts. Dedicated card service, router and JavaScript; share existing pool runtime so busy/pending/expiry apply across modules. Existing synthetic-only vault is retained and explicitly labelled; do not imply real card storage or payment integration.
**Stack:** Python/FastAPI/PostgreSQL and original vanilla JS/CSS.

## Approved UI and rules
- Same credit-card entry; tabs list / manual add / CSV import. No new shell.
- List shows alias, brand, last4, enablement, cap, confirmed links, reservations, reconciliation flag, version, availability observation. Search alias/last4; filter enabled; bounded keyset pages.
- Manual form serializes to the existing CSV preview/confirm pipeline, including user-entered billing identity/address. Never fabricate addresses or query balances. No CVV/OTP fields or persistence.
- Update permits alias, account_limit, sms_channel_ref, enabled only; PAN immutable. List exposes sms_configured only; replacement channel reference is explicit input. Secret/billing revisions not editable in this delivery.
- account_limit cannot be less than linked_count+reserved_count. Disable does not release reservations or links. Counters and reconciliation cannot be edited. No physical deletion of historical cards.
- Same-owner listing/update, cards:manage for all card HTTP routes. CAS+idempotent receipt on update. Commit before success, no automatic replay. Unknown import clears plaintext; same digest/key required to retry after explicit user action.
- The actual binding allocator/worker remains unconnected. Show the intended least-linked-first and one-active-binding rules; never show an observed available slot as an actual reservation.

## Task 1 — service and database tests (backend agent)
Create onboarding/card_management.py and tests/test_onboarding_card_management.py, add audit action card.update only. Implement list_page(settings,actor,search='',enabled=None,cursor=None,limit=50,policy=...,mac=...) -> Page(items,next_cursor), and update(settings,actor,card_id,expected_version,changes,request_key,policy=...,mac=...). Write and run failing tests first, including owner/permission, cursor/input, cap/counters, CAS/replay/conflict, audit rollback and revocation. Reuse tables without schema changes.

## Task 2 — original-shell UI and DOM tests (UI agent)
Create webui/static/onboarding-cards.js, replace only #card-pool contents, extend scoped CSS. Create tests/test_onboarding_card_ui.py. Use root-supplied RFPoolRuntime adapter, not another session/fetch/pending store. Cover list/empty/error, manual/CSV preview-confirm, metadata edit/toggle, uncertain outcome, navigation plaintext clearing, forbidden/expiry, no storage and mobile overflow. No fixture-only success substitution.

## Task 3 — HTTP and shared-runtime integration (root)
Create webui/onboarding_card_routes.py; register through pool routes. Exact allowlist routes GET cards / cards/template, POST cards/import-preview / cards/import, PATCH cards/{uuid}; reject unknown fields/queries and secret output. Existing five not_committed proof endpoints unchanged. Add JS to composite and template script ordering; exact static allowlist. Share api/run/mutate/session/pending handling through RFPoolRuntime and addon callbacks. Add HTTP tests and runner modules.

## Task 4 — final evidence and preview
Run targeted tests, security/DOM regressions and full affected suites. Independently review final diff and original business rules. Only restart existing local TLS preview after verification; preserve schema and keys. Actual browser checks read-only against preview; synthetic writes confined to disposable test schemas. Report tests, table fields, remaining real-provider limits and current URL; never claim full product launch readiness.
