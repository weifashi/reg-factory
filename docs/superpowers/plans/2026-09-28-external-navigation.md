# External HTML Navigation Implementation Plan

> For agentic workers: use executing-plans with independent security review. User approved continuation after the narrow design; no commit/merge/push/production deployment.

**Goal:** Normal external browser navigation reaches the static login screen, never an authenticated API or business page without the existing checks.
**Architecture:** Extend only the existing ProtectionMiddleware transport decision. Return a boolean after all transport and cookie checks; for external navigation to the three known management pages, use a fixed 303 /login before authentication. Only /login renders anonymous static content. No new service, forwarding-header trust, DB migration or schema changes.
**Tech Stack:** Existing FastAPI/Starlette middleware, unittest/TestClient, actual TLS Chromium preview.

## Scope and preconditions
- Existing reg-factory worktree `fix/onboarding-minors`; preserve all previous dirty files and main.
- Evidence: user correlation b5502271-9c48-4bcb-8ba7-e20a1f3ca043, exact Host and HTTPS, external Referer + cross-site metadata. Controlled browser reproduced403; direct new-tab login200.
- Exception requires GET, one of `/login`, `/`, `/onboarding`, `/onboarding/pools`, no Origin header, Accept token text/html, Sec-Fetch-Mode navigate, Sec-Fetch-Dest document, Sec-Fetch-Site same-site or cross-site.
- Check all duplicate headers (including new decision headers), HTTPS/Host, duplicate protected Cookie, and raw path restrictions. Reject malformed Referer, unknown fetch values, iframe/fetch/HEAD/POST, APIs, assets and arbitrary paths. Never use next/return_to as a redirect destination. Off mode unchanged.

## Tasks
- [x] Add `ExternalNavigationTests` to existing `tests/test_onboarding_route_guard.py` so existing web runner discovers it without new suite registration. Use real middleware and fixed static/management stub handlers; assert forbidden cases never authenticate or run business handlers. Include pool_mode off and synthetic.
- [x] Run RED: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests .venv-onboarding/bin/python -m unittest test_onboarding_route_guard.ExternalNavigationTests -v`; expected positive navigation cases403 instead of200/303.
- [x] Modify only `webui/onboarding_auth.py`: compute the narrowly bounded navigation flag, retain base/cookie checks, return it; convert only known external HTML page requests to `external_navigation` policy after normal policy selection but before raw-path denial. Add a fixed 303 `/login` branch before auth. `/login` stays public; no cookies or auth calls for redirect.
- [x] Run whole route_guard including real isolated DB tests, then existing web suite with no skips; run no unrelated build. Preserve all failure outputs and fix causes instead of suppressing tests.
- [x] Independent code/security review of exact diff and boundary tests.
- [x] Restart only identity-checked preview PID, retaining schema/config/credentials; don't alter Coder sharing or production. Run controlled real Chromium external link → redirect → static login → bootstrap → login → pools, plus negative HTTP/API checks. Report actual Coder end-to-end still requires user's confirmation.
- [x] Add operations note: old/new business rule, zero field/table changes, unchanged credential/permission protections, exact rollback to saved pre-change auth file only after identity-checked preview stop. Do not revert other dirty changes.

## Acceptance
All selected tests execute, no errors/failures/skips. Cross-site management navigation cannot invoke auth or business routes even with cookies; only static login is accessible. Existing same-origin auth/API/CSRF and fail-closed behavior unchanged. Real browser login successful without JS errors. Product source resides in reg-factory worktree, preview launch utilities remain private operational files.
