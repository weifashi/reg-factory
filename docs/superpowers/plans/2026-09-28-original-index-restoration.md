# Original index restoration — implementation plan

**Goal:** Correct the rejected replacement-page approach: serve the original index, preserve all original task groups and original parameter renderer, and add onboarding links instead of replacing the application.
**Architecture:** protected root renders the repository's original index with a protected-mode marker and local CSS/JS adaptation. Reuse original app.js/selectScript/renderForm and scripts.py schema. A dedicated authenticated read-only catalog exposes form metadata, never runtime credentials. Keep legacy execution middleware and direct-handler blocking; original local off-mode operations stay unchanged. Do not claim restored public execution simply because menus are restored.
**Tech stack:** existing FastAPI/plain JS/CSS/unittest/Playwright; current isolated worktree fix/onboarding-minors at 6b6d723; preserve unrelated dirty changes. User approved original-home additive direction with “做完 告诉我”. No commit/push, DB migration or real provider/payment calls.

## Tasks
- [x] Backend RED: assert original-root HTML not pool redirect; pure renderer preserves original IDs, extracts inline styles without CSP weakening; schema includes every original task/group but excludes executable file metadata and runtime secrets; authenticated exact assets/catalog and no legacy unblocking.
- [x] Backend GREEN: new webui/onboarding_console.py renderer/catalog/routes; root calls renderer for ready protected pool. Three exact assets plus catalog/styles and static support image require onboarding:read + ready pool. Pool-off diagnostics and off original index remain unchanged.
- [x] Frontend RED/GREEN: original app.js protected-aware bootstrap, all original catalog categories and parameters, additive new links, session/logout, explicit unavailable actions, no unsafe automatic probes/readers/iframes/POSTs. No copied task form implementation. Existing local-mode initialization and actions preserved.
- [x] Keep existing pool page/API; add return-to-original-console link. No new tables/fields or fake success/log output.
- [ ] Independent diff/security review; run affected backend/DOM tests, original off-mode regression tests, full Web suite and offline suite. Preserve any failed-run evidence.
- [x] Identity-check preview restart without changing its credentials/data/mode; live local TLS login/root browser, all task menus/forms, pool links/return, responsive widths and negative API/auth checks. Public authenticated Coder chain still requires user access.

## Acceptance boundary
Original task menu/forms must be genuinely driven by original schema and renderer, not links to replacement placeholders. Protected view explicitly identifies read-only original task parameters and inaccessible execution/tools; no real Google/bank/subprocess activity as a side effect of viewing. Existing original off-mode features remain in source and unchanged in execution behavior. Completing this correction is not completing integration of every legacy executor, the cloud workflow, or public release readiness.

## Final verification status
- Final Web run (`web-verified.json`): 247 tests, zero assertion failures, one error during isolated schema migration; PostgreSQL logged statement timeout at 06:48:14. This run is NOT a fully green suite. The exact failed case passed independently in 3.197s (`db-scope-recheck.log`), without changing timeout or migration code.
- All original-console DOM tests passed in that final run; full suite's mobile-tab regression now passes with both directions including the additive home link.
- Offline + 73 named original regressions: 175 passed, no failures/errors/skips (`offline-legacy-final.json`).
- Live actual TLS/API browser checked every original task and original six categories, pool navigation, responsive sizes; no JS/CSP errors or legacy API calls. No real execution.
- Remaining checkbox is deliberately not marked complete: no clean single full-suite run under current shared-host conditions, and restoring public old execution was not accomplished by this UI restoration.
