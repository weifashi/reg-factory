# Original reg-factory Console Integration Plan

**Goal:** Use the existing dark reg-factory shell (not the standalone light diagnostics UI) for protected onboarding, preserving working pool/config/task actions and the current security boundary.
**Architecture:** Reuse the repository's original `style.css` and shell/brand/sidebar/task-workbench structure; add only a scoped protected UI adapter. Never load legacy `app.js` or permit its unsafe bootstrap/legacy routes. Protected homepage is the business workbench when pool-ready; diagnostics remains a maintenance URL. Mailbox list/import use the existing actual APIs. Not-yet-integrated card/history/proxy/executors must be explicit, never simulated successful actions.
**Tech stack:** existing FastAPI, plain HTML/CSS/JS, unittest + Playwright, isolated PostgreSQL.

## Baseline
User requested latest source first: origin/main=6b6d723. Development worktree fast-forwarded to this base; prior dirty/untracked changes backed up with a retained stash. Only conflict was old guard test byte-identical to 46bca24; retained latest M3 strengthened test. Main untouched; no push/production deployment. New UI changes stay in this same reg-factory repository, not /workspace/gcloud product sources.

## Execution
1. Frontend owner: adapt `onboarding-pools.html` and add a small scoped adapter CSS/JS only if needed; re-use original style.css. Retain all existing DOM IDs/form semantics and API command logic, field permissions, import secret clearing, pending-receipt behavior. Dark header/sidebar, Google task entry, mailbox/tool entries, task parameters/workbench and state pane. Keep real task state distinct from not-yet-available worker runtime logs. Avoid duplicate copies of handlers or credential persistence.
2. Frontend tests before implementation: existing page asset tests plus dedicated DOM/navigation/responsive checks; do not bypass hidden panels or weaken tests to get green. Keep existing full pool tests meaningful; internal hash navigation must not recreate credentials or lose pending receipt. Original legacy index/style/app JS remain untouched.
3. Backend/root owner: allowlist only exact original CSS and new local shell assets under protected mode with appropriate public/read permissions, never old app.js or new broad static wildcard. Ready protected root routes to business page, diagnostics remains /onboarding. Unready/missing pool dependencies remain fail-closed with no anonymous fallback. Add routing/static contract tests before code.
4. Root integration: independently verify diff, security, old-page off-mode compatibility and final source. Run full web suite plus affected offline tests; no skip treated as success. Actual local TLS browser: external entry/login/root business workspace; mailbox tabs, config/tasks navigation, responsive widths; negative API/CSRF/Host checks.
5. Restart only identity-checked preview, preserve runtime secrets/schema and audit. Coder public sharing unchanged. Deliver exact URL and screenshot evidence; state original consumers/storage/external workers not yet unified, no credit-card or Google backend readiness claims for UI-only work.

## Acceptance and business boundaries
- Page visually uses the user's original dark sidebar/task-workbench hierarchy; not renamed light diagnostics.
- Homepage no longer treats diagnostics as product landing when business dependencies ready.
- Existing pool API actions and error/pending/secret-handling rules work; no fake data, no new unguarded legacy access.
- No table/field/migration changes in this UI integration. No merge to main, commit/push or production actions without separate authorization.

## Execution evidence — this UI stage only
- [x] Baseline aligned; previous work backed up and preserved.
- [x] Original dark-style shell and fixed business landing integrated; original four frontend files unchanged.
- [x] Independent frontend/backend reviews completed; final-source security and navigation regressions verified.
- [x] Final full Web suite: 226 passed, zero failures/errors/skips; offline suite: 102 passed, zero failures/errors/skips. Initial Web classification-test error preserved and corrected without removing the 60 legacy execution-block assertions.
- [x] Live local TLS/API browser checks, 390/768/1440 responsive checks, negative authorization/transport checks, and identity-checked preview restart completed.
- [x] No new database fields or migrations; no real provider, fee, scheduling or production actions.
- [ ] External authenticated Coder end-to-end verification remains outside local-browser evidence.
- [ ] Full product completion remains pending: card management UI/API integration, protected network adapters, old storage/consumer unification, real Google/Vertex/Sub2API execution and approved fee validation.

Evidence: private `/workspace/gcloud/.local/preview-launch/ui-integration/`; summary in `docs/operations/onboarding-original-console.md`. This does not mark the overall onboarding goal complete.
