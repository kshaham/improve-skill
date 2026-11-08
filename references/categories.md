# Improvement categories

Read this when `run.json.focus.board_areas.themes` (or a pending `focus` request) names
categories. Each category prefers its listed areas and tells you what kind of work to look
for inside them. Categories narrow where you look; they never widen intake scope,
authorization, or exclusions, and every change still needs normal verification.

Rank work matching a chosen category ahead of other work in the same priority tier.
When several categories are chosen, rotate discovery between them instead of draining one.

| Id | Category | Areas | Look for |
|---|---|---|---|
| `ux` | UI/UX polish | ui, accessibility | Visual hierarchy, inconsistent spacing/type, missing empty/loading/error states, confusing flows, unclear copy, dead-end screens |
| `animation` | Animation & transitions | ui, performance | Abrupt state changes, missing feedback on press/submit, layout jumps, janky (non-compositor) animations, missing `prefers-reduced-motion` handling, durations over ~300 ms for small UI |
| `security` | Security hardening | security | Unvalidated input, missing authorization checks, injectable queries/commands, secrets in code or logs, unsafe deserialization, known-vulnerable dependencies |
| `scale` | Scaling & production | performance, resilience, concurrency | N+1 or unbounded queries, missing pagination/limits, no timeouts or retries with backoff, missing caching, unbounded memory/queues, single points of failure, missing graceful shutdown, config not externalized |
| `performance` | Speed & performance | performance, gate-speed | Slow startup/load, heavy bundles, unnecessary re-renders, hot-path allocations, blocking I/O on main thread; measure before and after |
| `accessibility` | Accessibility | accessibility | Keyboard traps, missing labels/roles, low contrast, focus order, missing alt text, small touch targets |
| `reliability` | Reliability & errors | resilience, quality | Swallowed errors, crashes on bad input, missing retries for transient failures, inconsistent state after partial failure |
| `observability` | Logging & monitoring | resilience, quality | Unstructured or missing logs at failure points, no health/readiness checks, errors without context, missing metrics for key operations; never log secrets or personal data |
| `tests` | Test coverage | coverage, quality | Untested critical paths, assertion-free tests, flaky tests, missing edge cases for recent bugs |
| `features` | Feature completeness | features | Half-built features, TODO-gated paths, missing edge cases the user's outcomes imply |
| `mobile` | Mobile & responsive | ui, accessibility | Overflow at 320–430 px, tiny touch targets, hover-only interactions, viewport/safe-area issues, slow mobile loads |
| `dx` | Developer experience | gate-speed, docs, quality | Slow or flaky local gates, unclear scripts, missing setup steps, confusing errors for contributors |
| `docs` | Documentation | docs | Outdated instructions, undocumented commands/config, wrong examples |
| `api` | APIs & data contracts | contracts | Unvalidated payloads, undocumented breaking changes, missing versioning, loose types at boundaries |
| `code-health` | Code health | quality | Dead code, duplication, oversized functions, misleading names; refactor only with tests that pin behavior |
| `assets` | Assets & media | assets, performance | Oversized images, missing responsive variants, unoptimized fonts/icons, layout shift from media |
| `privacy` | Privacy & compliance | security, contracts | Personal data in logs/analytics, missing retention limits, data sent to third parties without need |
| `i18n` | Internationalization | ui, features | Hard-coded user-facing strings, locale-unaware dates/numbers/currency, layouts that break with longer text or right-to-left |
| `seo` | SEO & discoverability | ui, performance | Missing titles/meta descriptions, non-semantic markup, missing link previews, broken sitemaps/robots rules (web projects only) |
| `concurrency` | Concurrency & races | concurrency | Shared mutable state, check-then-act races, missing locks, async ordering bugs, unsafe cancellation |

If a category does not apply to this project (for example `seo` in a CLI), record that
once in the journal and continue with the other chosen categories.
