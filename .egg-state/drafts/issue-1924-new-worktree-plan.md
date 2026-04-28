# Plan: Add write operations to Jira gateway (create/update/comment/link)

> Issue: [#1924](https://github.com/jwbron/egg/issues/1924) | Phase: plan
> Single PR — phases organise commits inside the same workflow.

## Approach

Implement the bounded write extension recommended by the architect
analysis (`A1 + B1 + C1 + D1 + E1`):

- **A1 — four dedicated narrow routes** under `/api/v1/jira/*`, one per
  Atlassian write verb. `/api/v1/jira/execute` stays GET-only forever.
- **B1 — split the method allowlist**. `validate_jira_api_path` keeps
  `ALLOWED_METHODS={"GET"}`; the new `JiraClient` write methods bypass
  the validator and call `_request` directly with hardcoded paths.
  Path-segment denylist (`transitions`, `worklog`, `attachments`,
  `watchers`, `DELETE`/`PUT`/`PATCH` on `/execute`) stays unchanged.
- **C1 — curated body schema with field allowlist**, rejecting
  arbitrary `fields:` passthrough. ADF wrapping for plain-text
  `description`/`comment` bodies; raw ADF dicts pass through.
- **D1 — caller-supplied idempotency key + in-process cache** (5-min
  TTL, per-process). Cache is keyed by `(verb, project, key)` for
  `createJiraIssue` / `addCommentToJiraIssue` / `createIssueLink` (the
  link verb gets cached too because Atlassian does not dedupe identical
  `(inward, outward, type)` triples — see Open Q28). `editJiraIssue` is
  naturally idempotent and skips the cache.
- **E1 — agent-friendly sandbox wrapper** with `--summary`,
  `--description`, `--description-file`, `--description-stdin`,
  `--labels a,b`, `--parent`, `--epic-link`, `--add-labels`,
  `--remove-labels`, `--no-notify`, `--idempotency-key`, etc. Bash
  subcommands shell out to the existing `call_gateway` helper.

The work decomposes into six phases inside a single PR. Phases 1 and 2
are the shared foundation (idempotency cache, ADF helper, method
allowlist split, JiraClient write methods); Phase 3 is the four
gateway routes; Phase 4 is the sandbox wrapper; Phase 5 is tests;
Phase 6 is documentation. Each phase commits independently so review
can be targeted (e.g., reviewing just the gateway routes without
re-reading the foundation), and the BRC reviewer can replay
phase-by-phase if needed.

### HITL defaults baked into this plan

Nineteen HITL decisions and nine feedback questions from the refine
phase remained unresolved at plan-time. To unblock implementation we
**bake in the architect's recommended defaults** (matching the
"Recommended Approach" in `1924-analysis.md`); if the human answers
the contract differently before implement starts, the affected task
acceptance criteria and small slices of code will be updated by the
coder agent then. The defaults are:

| Decision | Default chosen |
|----------|----------------|
| D1 — Custom fields | **No customFields map** in v1; only `summary` / `description` / `labels` / `parent` / `epicLink` shorthand. |
| D2 — Epic Link | **Per-instance dispatch via config knob.** `JiraPolicy` loads `jira.epic_link_field: "parent" \| "customfield_10014"` (default `"parent"`). The `epicLink` shorthand emits **only the configured field** — never both. If a caller passes both an explicit `parent` and `epicLink`, the route returns 400 (conflicting fields); explicit `parent` wins only when `epicLink` is absent. |
| D3 — `idempotency_key` | **Optional, with documented warning** for createJiraIssue / addCommentToJiraIssue / createIssueLink. |
| D4 — Link-type allowlist source | **Operator-configurable** via `config/context-filters.yaml jira.link_types: [...]`; default `["Blocks", "Relates"]`. |
| D5 — `notifyUsers` default | **`false`** (quiet update — opt-in to notify) for editJiraIssue. |
| D6 — Comment visibility | **Hidden in v1** (no `visibility` field exposed). |
| D7 — Body input format | **Both** — plain text gets wrapped to ADF; pre-built ADF dicts pass through. |
| D8 — Issuetype identifier | **Both** name (`"Task"`) and numeric ID. |
| D9 — `createIssueLink` allowlist | **Strict** — both `inwardIssue` and `outwardIssue` projects must be allowlisted. |
| D10 — Per-role write gating | **Defer** to a follow-up issue (unrestricted within allowlisted projects in v1). |
| D11 — Phase gating | **Defer** to a follow-up issue (unrestricted across phases in v1). |
| D13 — `createJiraIssue` response | **Normalized envelope** `{key, id, browse_url, status: "created"}`. |
| D14 — `editJiraIssue` response | **Envelope** `{status: "updated", key}` (no extra Atlassian call). |
| D16 — Idempotency cache scope | **In-memory per-gateway-process, 5-min TTL**. |
| D17 — Cross-project parent | **Reject** if `parent.key` project differs from the new ticket's project. |
| D23 — `createIssueLink` comment | **Surface via `comment` field** in body (single round-trip; idempotent with link cache). |
| D24 — Dry-run mode | **Live calls only** — orchestrator handles preview. |
| D27 — Documentation home | **Extend `docs/reference/jira-wrapper.md`** (single source of truth). |
| D28 — `createIssueLink` idempotency | **Extend the idempotency cache** (D1) to `createIssueLink` for symmetry. |
| Q12 — 429 audit on writes | **Yes** — emit `jira_upstream_rate_limited` audit on any 429, including writes. |
| Q15 — Body size caps | summary ≤ 255 (Atlassian limit), description ≤ 32 KiB, comment body ≤ 32 KiB, labels count ≤ 30 with each ≤ 50 chars, customFields map disabled (per D1). |
| Q18 — Existing-issue probe | **No** — idempotency key is sufficient; orchestrator owns higher-level dedup. |
| Q19 — Wrapper input flags | Confirmed `--description` / `--description-file` / `--description-stdin` (and analogous `--body*` for comments). |
| Q20 — Audit body redaction | Log structural metadata (field-names changed, content lengths) **and label values** (small enumerated strings) **and link-type name**. Body content (description / comment) is never logged — only its length. |
| Q21 — Adversarial body tests | **Same file** as the route 403 grid (`test_jira_routes.py`) for grep-by-route convenience. |
| Q22 — Multi-tenant hooks | **Single-tenant** in v1, no multi-tenant work in #1924. |
| Q25 — Failure recovery | **Orchestrator's responsibility**; gateway implements no transactions or compensating actions. |
| Q26 — Error envelope | **Reuse `_jira_error_from_upstream` verbatim** for v1. Field-level Atlassian error surfacing is a follow-up. |

```yaml
# yaml-tasks
pr:
  title: |-
    Add bounded Jira write verbs to the gateway (create/edit/comment/link)
  description: |
    Issue [#1556](https://github.com/jwbron/egg/issues/1556) shipped the
    v1 read-only Jira gateway. Issue
    [#1557](https://github.com/jwbron/egg/issues/1557) (Jira-epic SDLC
    pipelines) is now blocked on the **write** counterpart. This PR is
    the bounded write extension that unblocks #1557.

    **Changes:**

    1. **Four new gateway routes** under `/api/v1/jira/*` — `ticket/create`
       (`POST /rest/api/3/issue`), `ticket/edit` (`PUT
       /rest/api/3/issue/{key}`), `ticket/comment/add` (`POST
       /rest/api/3/issue/{key}/comment`), and `issue-link/create`
       (`POST /rest/api/3/issueLink`).
    2. **Four new `JiraClient` methods** (`create_issue`, `edit_issue`,
       `add_comment`, `create_issue_link`) that bypass
       `validate_jira_api_path` and use `_request` directly.
    3. **Per-verb body schema** — every write route validates a tight
       schema. Custom fields are not exposed in v1; only `summary` /
       `description` / `labels` / `parent` / `epicLink` shorthand.
    4. **ADF wrapping helper** (`gateway/jira_adf.py`) so callers can
       send plain text and the gateway wraps it in ADF; pre-built ADF
       dicts pass through.
    5. **Idempotency cache** (`gateway/jira_idempotency.py`) — a
       per-gateway-process in-memory dict with a 5-minute TTL.
    6. **Sandbox wrapper subcommands** (`sandbox/scripts/jira ticket
       create | edit | comment add | link create`) with
       `--description` / `--description-file` / `--description-stdin`.
    7. **Tests** — per-route 403 grid extended to the four new routes;
       new `test_jira_idempotency.py` and `test_jira_adf.py`.
    8. **Docs** — `docs/reference/jira-wrapper.md` gains a "Write
       verbs" section.

    The v1 read-only invariants are preserved verbatim — `*.atlassian.net`
    stays out of the Squid allowlist, Atlassian credentials remain
    gateway-only, the permanent denylist (`transitions`, `worklog`,
    `attachments`, `watchers`, `DELETE`) is unchanged, and `/execute`
    stays GET-only. Body content is never written to audit logs.
  test_plan: |
    - Automated: `make test` runs every gateway and sandbox test, including
      the new `test_jira_idempotency.py`, `test_jira_adf.py`, and
      the extended `test_jira_routes.py` 403 grid. The route-enumeration
      regression in `test_jira_routes.py` now asserts
      `__egg_requires_private_mode__` on each of the four new routes.
      `test_allowed_domains.py` continues to exclude `*.atlassian.net`.
    - Manual: reviewer reads the per-route 403 grid in
      `test_jira_routes.py` to confirm coverage; reviewer skims
      `docs/reference/jira-wrapper.md` write-verbs section. Integration
      smoke against live Atlassian deferred to #1557.
  manual_steps: |
    Pre-merge: optional — operator may add `jira.link_types: [...]` to
    `config/context-filters.yaml` if they want link types beyond the
    default `["Blocks", "Relates"]`.
    Post-merge: none. Routes inert until #1557 ships.
phases:
  - id: 1
    name: |-
      Foundation modules
    goal: |-
      Land the two reusable utilities that later phases import: an
      in-process idempotency cache and an ADF wrapper helper.
    tasks:
      - id: TASK-1-1
        description: |-
          Add `gateway/jira_idempotency.py`. The module exposes
          `get_or_run(verb: str, project: str, key: str | None,
          fn: Callable[[], tuple[int, dict]])` and `clear_cache()`.
          Internal storage is a module-level dict keyed by
          `(verb, project, key)` mapping to
          `(monotonic_seconds, status_code, response_json)` with a
          5-min TTL constant. Eviction is lazy at lookup time;
          a threading lock guards mutations.
        acceptance: |-
          File `gateway/jira_idempotency.py` exists exporting
          `get_or_run`, `clear_cache`, and `IDEMPOTENCY_TTL_SECONDS`.
          `make lint` passes.
        role: coder
        files:
          - gateway/jira_idempotency.py
      - id: TASK-1-2
        description: |-
          Add `gateway/jira_adf.py`. The module exposes
          `wrap_text_as_adf(text: str) -> dict` returning the minimal
          ADF doc shape and `is_adf_dict(value: object) -> bool`.
        acceptance: |-
          File `gateway/jira_adf.py` exists exporting
          `wrap_text_as_adf` and `is_adf_dict`. `make lint` passes.
        role: coder
        files:
          - gateway/jira_adf.py
  - id: 2
    name: |-
      JiraClient write methods + method-allowlist separation
    goal: |-
      Extend `gateway/jira_client.py` with four new write methods that
      bypass `validate_jira_api_path`, hardcoding their upstream paths.
    tasks:
      - id: TASK-2-1
        description: |-
          Add `JiraClient.create_issue(*, project_key, issuetype,
          summary, description, labels, parent, epic_link,
          epic_link_field, idempotency_key)`. Builds the Atlassian body
          and calls `_request("POST", "issue", body=body)`. Consults
          `jira_idempotency` when `idempotency_key` is provided.
        acceptance: |-
          The method exists, exported in `__all__`, with unit tests in
          `test_jira_client.py` using `httpx.MockTransport` to assert
          wire shape. `make test` passes.
        role: coder
        files:
          - gateway/jira_client.py
      - id: TASK-2-2
        description: |-
          Add `JiraClient.edit_issue(*, key, summary, description,
          labels, add_labels, remove_labels, notify_users)`. Builds
          Atlassian's body in two modes: replace mode (`labels` only)
          and incremental mode (`add_labels`/`remove_labels` only).
          Mutually exclusive at the client method level. Sends
          `notifyUsers=false` query string only when
          `notify_users=False`.
        acceptance: |-
          The method exists. Unit tests assert body shape for
          replace-only, incremental-only, combined raises ValueError,
          and notifyUsers query param behavior. `make test` passes.
        role: coder
        files:
          - gateway/jira_client.py
      - id: TASK-2-3
        description: |-
          Add `JiraClient.add_comment(*, key, body, idempotency_key)`.
          ADF-wraps text or passes ADF dict through. Calls
          `_request("POST", f"issue/{key}/comment", body=body)`.
          Consults idempotency cache when key provided.
        acceptance: |-
          The method exists. Unit tests assert the wire shape for
          plain-text body (ADF-wrapped), pre-built ADF (passes
          through), and idempotency cache hit/miss. `make test` passes.
        role: coder
        files:
          - gateway/jira_client.py
      - id: TASK-2-4
        description: |-
          Add `JiraClient.create_issue_link(*, link_type, inward_key,
          outward_key, comment, idempotency_key)`. Builds body with
          `type.name`, `inwardIssue.key`, `outwardIssue.key`. When
          `comment` provided, ADF-wraps and adds. Idempotency cache
          keyed by canonical `(inward, outward, type)` triple per
          decision-28.
        acceptance: |-
          The method exists. Unit tests assert body shape with and
          without comment, idempotency hit/miss for the same triple,
          and that same opaque key against different triples produces
          distinct cache entries. `make test` passes.
        role: coder
        files:
          - gateway/jira_client.py
      - id: TASK-2-5
        description: |-
          Three touch-ups in `gateway/jira_client.py`: (1) update
          docstring + comment block above `validate_jira_api_path` to
          clarify execute-passthrough vs write-method bypass; (2) move
          the `jira_upstream_rate_limited` audit-emit out of the
          GET-only retry loop in `_request` so it fires on every 429
          (writes included); (3) update `__all__`.
        acceptance: |-
          Docstring + comment present. 429 audit emit fires for write
          methods (asserted in TASK-5-3). Denylist still enforced for
          `/execute`. `make test` passes.
        role: coder
        files:
          - gateway/jira_client.py
  - id: 3
    name: |-
      Gateway routes + body validation + audit
    goal: |-
      Land the four new Flask routes with curated body schema, project
      allowlist, ADF wrapping, JiraClient dispatch, and structured
      audit (no body content logged).
    tasks:
      - id: TASK-3-1
        description: |-
          Add `POST /api/v1/jira/ticket/create` to `gateway/gateway.py`.
          Body schema with size caps (summary ≤ 255, description ≤ 32
          KiB, labels ≤ 30 × 50 chars), reject custom fields, project
          allowlist, cross-project parent reject, reject if both
          `parent` and `epicLink` are present, ADF wrap of text
          description. Calls `JiraClient.create_issue(...)`. Returns
          normalized envelope `{key, id, browse_url, status: "created"}`.
        acceptance: |-
          Route exists, decorated with `@require_session_auth` +
          `@require_private_mode`. Audit log fields match Q20
          redaction. Route in route-enumeration regression. `make
          test` passes.
        role: coder
        files:
          - gateway/gateway.py
      - id: TASK-3-2
        description: |-
          Add `POST /api/v1/jira/ticket/edit`. Body schema validates
          ticket key, allowlist, no custom fields, size caps, rejects
          400 if both `labels` (replace) and `addLabels`/`removeLabels`
          (incremental) present. Returns `{status: "updated", key}`.
        acceptance: |-
          Route exists, decorated, in route-enumeration regression.
          Mixed labels mode returns 400. `make test` passes.
        role: coder
        files:
          - gateway/gateway.py
      - id: TASK-3-3
        description: |-
          Add `POST /api/v1/jira/ticket/comment/add`. Body schema:
          ticket, body (str | ADF, ≤ 32 KiB), idempotencyKey.
          Visibility rejected if present. Body content NEVER logged.
          Returns Atlassian comment object verbatim.
        acceptance: |-
          Route exists, decorated, in route-enumeration regression.
          Body content not in audit log. `make test` passes.
        role: coder
        files:
          - gateway/gateway.py
      - id: TASK-3-4
        description: |-
          Add `POST /api/v1/jira/issue-link/create`. Body schema: type
          (in jira_policy.link_types allowlist), inwardIssue,
          outwardIssue, comment?, idempotencyKey?. Strict allowlist on
          both projects. Returns `{status: "created", inwardIssue,
          outwardIssue, type}` envelope.
        acceptance: |-
          Route exists, decorated, in route-enumeration regression.
          Strict allowlist enforced. Response envelope matches.
          `make test` passes.
        role: coder
        files:
          - gateway/gateway.py
      - id: TASK-3-5
        description: |-
          Extend `gateway/jira_policy.py` with `jira.link_types: list[str]`
          (default `["Blocks", "Relates"]`) and
          `jira.epic_link_field: "parent" | "customfield_10014"`
          (default `"parent"`). Both fail-closed-on-malformed.
        acceptance: |-
          `JiraPolicy` exposes `link_types`, `link_type_allowed(name)`,
          `epic_link_field`. Unit tests in `test_jira_policy.py`.
          `make test` passes.
        role: coder
        files:
          - gateway/jira_policy.py
      - id: TASK-3-6
        description: |-
          Update `gateway/gateway.py` module docstring to list four new
          write routes alongside existing read routes.
        acceptance: |-
          Docstring lists the four new routes. `make lint` passes.
        role: coder
        files:
          - gateway/gateway.py
      - id: TASK-3-7
        description: |-
          Edit `config/context-filters.yaml` with commented example
          blocks for `jira.link_types` and `jira.epic_link_field` knobs.
        acceptance: |-
          Both knobs documented with comment blocks. `make lint`
          passes.
        role: coder
        files:
          - config/context-filters.yaml
  - id: 4
    name: |-
      Sandbox wrapper subcommands
    goal: |-
      Extend `sandbox/scripts/jira` with four new subcommands matching
      the gateway routes. Atlassian credentials never enter the sandbox.
    tasks:
      - id: TASK-4-1
        description: |-
          Add `jira ticket create --project KEY --type Task
          --summary "..." [--description ...] [--labels a,b]
          [--parent FOO-1] [--epic-link FOO-2] [--idempotency-key K]`.
          POSTs to `/api/v1/jira/ticket/create`.
        acceptance: |-
          Subcommand exists. `show_usage` updated. `make lint`
          passes.
        role: coder
        files:
          - sandbox/scripts/jira
      - id: TASK-4-2
        description: |-
          Add `jira ticket edit TICKET [--summary "..."]
          [--description ...] [--labels a,b] [--add-labels x]
          [--remove-labels y] [--no-notify]`. POSTs to
          `/api/v1/jira/ticket/edit`.
        acceptance: |-
          Subcommand exists. Mutually exclusive description flags
          enforced. `make lint` passes.
        role: coder
        files:
          - sandbox/scripts/jira
      - id: TASK-4-3
        description: |-
          Add `jira ticket comment add TICKET [--body ...]
          [--idempotency-key K]`. POSTs to
          `/api/v1/jira/ticket/comment/add`.
        acceptance: |-
          Subcommand exists. Exclusive body flags enforced.
          `make lint` passes.
        role: coder
        files:
          - sandbox/scripts/jira
      - id: TASK-4-4
        description: |-
          Add `jira link create --type Blocks --inward FOO-1
          --outward FOO-2 [--comment ...] [--idempotency-key K]`.
          POSTs to `/api/v1/jira/issue-link/create`.
        acceptance: |-
          Subcommand exists. `show_usage` updated. `make lint`
          passes.
        role: coder
        files:
          - sandbox/scripts/jira
  - id: 5
    name: |-
      Tests
    goal: |-
      Land the test surface that locks in policy invariants, ADF
      wrapping, idempotency cache, per-route 403 grid, adversarial
      bodies.
    tasks:
      - id: TASK-5-1
        description: |-
          Create `gateway/tests/test_jira_idempotency.py`: cache
          miss/hit, TTL expiry, distinct keys, distinct verbs sharing
          a key, missing-key bypass, threading-lock race-safety, link
          cache aliasing test (same opaque key against different
          triples must produce distinct cache entries).
        acceptance: |-
          File exists with named tests. Coverage of
          `gateway/jira_idempotency.py` ≥ 95%. `make test` passes.
        role: tester
        files:
          - gateway/tests/test_jira_idempotency.py
      - id: TASK-5-2
        description: |-
          Create `gateway/tests/test_jira_adf.py`: plain text wrap,
          empty string wrap, multi-line, `is_adf_dict` true on
          Atlassian samples, false on plain dicts/lists/strings.
        acceptance: |-
          File exists. Coverage ≥ 95%. `make test` passes.
        role: tester
        files:
          - gateway/tests/test_jira_adf.py
      - id: TASK-5-3
        description: |-
          Extend `gateway/tests/test_jira_client.py` with per-method
          tests for `create_issue`, `edit_issue`, `add_comment`,
          `create_issue_link`. Use `httpx.MockTransport`. Bound the
          matrix at ≥ 1 success per option pair plus failure cases.
          Required cases: parent only, epic_link with parent dispatch,
          epic_link with customfield_10014 dispatch, name-vs-ID
          issuetype, replace labels, incremental labels, combined
          raises ValueError, notify_users false vs true, ADF
          passthrough vs text wrap, idempotency hit/miss, 429 audit
          emit on each write verb.
        acceptance: |-
          New test functions added. Coverage ≥ 95%. `make test`
          passes.
        role: tester
        files:
          - gateway/tests/test_jira_client.py
      - id: TASK-5-4
        description: |-
          Extend `gateway/tests/test_jira_routes.py` with per-route 403
          grid for the four new routes. Each: public mode → 403,
          missing creds → 503, non-allowlisted project → 403, malformed
          body → 400, oversized → 400, unknown issuetype → 400,
          cross-project parent → 400, both `parent` and `epicLink` set
          → 400, mixed labels mode → 400, custom-field smuggling → 400,
          non-allowlisted link-type → 400, unicode-in-keys → 400,
          HTTP-method tunnelling → 400, success → 2xx with audit
          assertion, issue-link response envelope shape. Update route
          enumeration regression.
        acceptance: |-
          The 403/400 grid covers every cell for each new route. Route
          enumeration includes four extra routes. `make test` passes.
        role: tester
        files:
          - gateway/tests/test_jira_routes.py
      - id: TASK-5-5
        description: |-
          Extend `gateway/tests/test_jira_policy.py` with tests for
          `link_types` config knob: missing key → defaults; explicit
          list overrides; mtime-cache invalidation; case-sensitive
          lookup. Fail-closed when malformed.
        acceptance: |-
          Tests pass. Coverage ≥ 95% for new branch. `make test`
          passes.
        role: tester
        files:
          - gateway/tests/test_jira_policy.py
      - id: TASK-5-6
        description: |-
          Extend `tests/sandbox/test_jira_wrapper.py` with smoke tests
          for the four new subcommands against a mocked gateway:
          success path; mutually-exclusive body flags rejected;
          missing required flag rejected; output JSON to stdout. Run
          via subprocess.
        acceptance: |-
          New tests pass. `make test` passes.
        role: tester
        files:
          - tests/sandbox/test_jira_wrapper.py
  - id: 6
    name: |-
      Documentation
    goal: |-
      Land user-facing docs for the new write verbs (per decision-27).
    tasks:
      - id: TASK-6-1
        description: |-
          Extend `docs/reference/jira-wrapper.md` with a "Write verbs"
          section documenting each subcommand, body schema, ADF
          wrapping rules, idempotency-key semantics, the
          `jira.link_types` config knob, audit-log redaction rules, and
          cross-project parent reject. Cross-link from v1 read-verbs
          section.
        acceptance: |-
          The "Write verbs" section exists end-to-end. Markdown lints
          clean. The existing v1 read section is untouched.
        role: documenter
        files:
          - docs/reference/jira-wrapper.md
      - id: TASK-6-2
        description: |-
          If `docs/reference/sandbox-tools.md` (or equivalent index)
          exists, add the four new `jira ticket create | edit | comment
          add | link create` subcommands. Otherwise document as a no-op.
        acceptance: |-
          Either index updated or task documented as no-op. `make
          lint` passes.
        role: documenter
        files:
          - docs/reference/jira-wrapper.md
      - id: TASK-6-3
        description: |-
          Add a "Phase rollback" sub-section to the new "Write verbs"
          doc spelling out that each phase is independently revertible.
        acceptance: |-
          The "Phase rollback" sub-section exists. `make lint` passes.
        role: documenter
        files:
          - docs/reference/jira-wrapper.md
```

---

*Authored-by: egg (task_planner) — full plan with risks/dependencies and complete prose-narrative is preserved on branch egg/issue-1924 at .egg-state/drafts/1924-plan.md (1043 lines).*