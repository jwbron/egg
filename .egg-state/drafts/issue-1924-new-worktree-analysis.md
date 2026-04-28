# Analysis: Add write operations to Jira gateway (create/update/comment/link)

> Issue: #1924 | Phase: refine

## Problem Statement

Issue [#1556](https://github.com/jwbron/egg/issues/1556) landed the v1 read-only Jira gateway: sandboxed agents can fetch tickets, search via JQL, and read comments through `/api/v1/jira/*`, with Atlassian credentials held exclusively in the gateway, project allowlist enforcement, private-mode-only access, conservative JQL scope extraction, and structured audit logging. Writes were explicitly future-scope.

Issue [#1557](https://github.com/jwbron/egg/issues/1557) (Jira-epic SDLC pipelines) is now blocked on those writes. The refine phase needs to push the agent-authored analysis to the **epic's Description field** (`editJiraIssue`); the plan phase needs to create **child tickets under an epic** with parent / Epic Link set (`createJiraIssue`), update existing children when scope shifts (`editJiraIssue`), drop comments pointing at survivors during consolidation (`addCommentToJiraIssue`), and wire cross-task dependency edges (`createIssueLink`).

#1924 is the **bounded write extension** of #1556. It must:

1. Add four narrow write verbs: `createJiraIssue`, `editJiraIssue`, `addCommentToJiraIssue`, `createIssueLink`.
2. Inherit every v1 invariant: private-mode only, project allowlist, gateway-held credentials, structured audit, fail-closed on missing config.
3. Keep transitions, worklogs, attachments, and deletions **permanently denied** at the path / method validator (matching #1556's `JIRA_WRITE_VERBS_DENIED`). The write extension does **not** widen that escape hatch.
4. Land as a **pure extension** of the existing modules (`gateway/jira_client.py`, `gateway/jira_policy.py`, `gateway/gateway.py` route layer, `sandbox/scripts/jira`, `gateway/tests/test_jira_*`) — no re-architecting.
5. Preserve the zero-credential invariant: Atlassian creds never enter the sandbox; the new sandbox wrapper subcommands shell to the gateway over `EGG_SESSION_TOKEN`.

This analysis surveys the existing v1 surface, enumerates each new verb's Atlassian shape, names the design choices that need a human answer, and recommends an approach that keeps the surface tight, auditable, and reusable for #1557.

## Recommended Approach

**Adopt A1 + B1 + C1 + D1 + E1.** The combined design preserves every v1 invariant, keeps `/execute` GET-only, names a per-verb route per Atlassian write, validates request bodies up front, solves idempotency at the gateway, and makes the sandbox wrapper agent-friendly.

Concretely the surface becomes:

| New gateway route | Atlassian endpoint | Audit operation | Notes |
|---|---|---|---|
| `POST /api/v1/jira/ticket/create` | `POST /rest/api/3/issue` | `jira_ticket_create` | projectKey-allowlisted; idempotency-keyed; ADF wrapping; returns Atlassian's `{id, key, self}`. |
| `POST /api/v1/jira/ticket/edit` | `PUT /rest/api/3/issue/{key}` | `jira_ticket_edit` | ticket-allowlisted; supports `notifyUsers=false`; translates `addLabels`/`removeLabels` to Atlassian `update` block. |
| `POST /api/v1/jira/ticket/comment/add` | `POST /rest/api/3/issue/{key}/comment` | `jira_comment_add` | ticket-allowlisted; idempotency-keyed; ADF wrapping; **no visibility restriction in v1**. |
| `POST /api/v1/jira/issue-link/create` | `POST /rest/api/3/issueLink` | `jira_issue_link_create` | both `inwardIssue` and `outwardIssue` projects allowlisted; link `type.name` against a configurable allowlist (default `Blocks`, `Relates`). |

Permanent denylist unchanged (`transitions`, `worklog`, `attachments`, `watchers`, `DELETE`). `/execute` stays GET-only forever. `validate_jira_api_path` is unchanged.

`JiraClient` gains four methods: `create_issue`, `edit_issue`, `add_comment`, `create_issue_link`. They use `_request` directly with hardcoded paths; they do **not** auto-retry on 429 (already enforced by `_request` for non-GET). They translate the route-layer body schema to Atlassian's wire shape (e.g., `addLabels`/`removeLabels` → `update.labels: [{add},{remove}]`).

Idempotency cache lives in `gateway/jira_idempotency.py`. Module-level dict, threading lock, 5-minute TTL, evicted lazily. Per Q12 default, also extends to `createIssueLink` (D28) for symmetry — Atlassian does not dedupe identical (inward, outward, type) triples.

`sandbox/scripts/jira` grows the four new subcommands per E1, sharing the `call_gateway` helper. The wrapper validates flag presence client-side; semantic validation lives at the gateway.

## Decisions locked in (per issue body's "Decided Design" section)

All decisions D1-D28 and Q12-Q26 are resolved per the issue body. Re-runs of the SDLC pipeline must treat these as settled and not re-surface them.

- D1 — Custom fields: No customFields map in v1; only summary/description/labels/parent/epicLink shorthand.
- D2 — Epic Link: Per-instance dispatch via JiraPolicy.epic_link_field config (default "parent").
- D3 — idempotency_key: Optional with documented warning.
- D4 — Link-type allowlist: Operator-configurable via jira.link_types (default ["Blocks", "Relates"]).
- D5 — notifyUsers default: false (quiet update).
- D6 — Comment visibility: Hidden in v1.
- D7 — Body input format: Both plain text (gateway wraps to ADF) and pre-built ADF dict (passthrough).
- D8 — Issuetype identifier: Both name and numeric ID.
- D9 — createIssueLink allowlist: Strict (both inward and outward projects must be allowlisted).
- D10 — Per-role write gating: Defer to follow-up issue.
- D11 — Phase gating: Defer to follow-up issue.
- D13 — createJiraIssue response shape: Normalized envelope {key, id, browse_url, status: "created"}.
- D14 — editJiraIssue response shape: Envelope {status: "updated", key} (no extra Atlassian call).
- D16 — Idempotency cache scope: In-memory per-gateway-process, 5-minute TTL.
- D17 — Cross-project parent: Reject if parent.key project differs from new ticket project.
- D23 — createIssueLink optional comment: Surface via `comment` field in body.
- D24 — Dry-run mode: Live calls only; orchestrator handles preview.
- D27 — Documentation home: Extend docs/reference/jira-wrapper.md as single source of truth.
- D28 — createIssueLink idempotency: Extend the idempotency cache (D1) to createIssueLink.
- Q12 — 429 audit on writes: Yes, emit jira_upstream_rate_limited audit on any 429 (writes included).
- Q15 — Body size caps: summary ≤ 255, description ≤ 32 KiB, comment body ≤ 32 KiB, labels ≤ 30 with each ≤ 50 chars, customFields disabled.
- Q18 — Existing-issue probe before create/edit: No (idempotency key is sufficient).
- Q19 — Wrapper input flags: Confirmed --description / --description-file / --description-stdin.
- Q20 — Audit body redaction: Log structural metadata (field names changed, content lengths, label counts) AND label values AND link-type names. Body content (description/comment) never logged.
- Q21 — Adversarial body tests location: Same file as the route 403 grid (test_jira_routes.py).
- Q22 — Multi-tenant hooks: Single-tenant in v1, no multi-tenant work in #1924.
- Q25 — Failure recovery: Orchestrator's responsibility; gateway implements no transactions.
- Q26 — Error envelope: Reuse _jira_error_from_upstream verbatim for v1.

(Full analysis with Options Considered, Open Questions, and Complexity Assessment is preserved on branch egg/issue-1924 at .egg-state/drafts/1924-analysis.md and .egg-state/brc-history/1924-refine.md — 522 lines.)