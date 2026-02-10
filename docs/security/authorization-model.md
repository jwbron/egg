# Workflow Authorization Model

This document describes the authorization model for egg's GitHub workflow triggers, ensuring only authorized users can invoke automated agents.

## Overview

egg implements a tiered authorization model that balances security with usability:

1. **High-risk operations** (SDLC pipeline, autofix) — require explicit authorization
2. **Read-only operations** (code review) — require authorization by default
3. **Explicit invocations** (workflow_dispatch) — bypass authorization checks (user must have repo permissions)

## Authorization Mechanism

All workflows use a shared authorization script (`.github/scripts/check-authorization.sh`) that:

- Accepts a comma-separated list of authorized usernames
- Supports GitHub organization membership checks (optional)
- Prevents bot self-triggering
- Outputs `authorized=true|false` for workflow conditions

### Configuration

Authorization is configured via the `authorized_users` input on each workflow:

```yaml
# Example: Allow multiple users
authorized_users: "jwbron, team-member, bot-account"

# Example: Allow organization members
authorized_users: "@myorg, jwbron"
```

**Default value**: `jwbron` (repository owner)

## Workflow Authorization Matrix

| Workflow | Trigger | Authorization Required | Skip Conditions |
|----------|---------|----------------------|-----------------|
| `sdlc-pipeline.yml` | `issues: [labeled]` | Yes (sender) | `workflow_dispatch`, `workflow_call` |
| `on-pull-request.yml` | `pull_request` | Yes (PR author) | `workflow_dispatch` |
| `on-check-failure.yml` | `workflow_run` | Yes (PR author) | `workflow_dispatch` |
| `on-mention.yml` | `issue_comment`, etc. | Yes (sender) | `workflow_call` |
| `sdlc-hitl.yml` | `issue_comment` | Yes (sender) | - |

### Existing Authorization (unchanged)

These workflows already had authorization before this change:

- **`on-mention.yml`**: Checks sender against `authorized_users` before responding to @mentions
- **`sdlc-hitl.yml`**: Checks sender against `authorized_users` before processing HITL decisions

### New Authorization (added by #419)

These workflows now have authorization checks:

- **`sdlc-pipeline.yml`**: Checks sender when triggered by label addition
- **`on-pull-request.yml`**: Checks PR author before triggering code review
- **`on-check-failure.yml`**: Checks PR author before triggering autofix

## Why Certain Triggers Bypass Authorization

### `workflow_dispatch` (manual trigger)

Users who can trigger `workflow_dispatch` already have repository write access. The GitHub UI provides the authorization gate.

### `workflow_call` (reusable workflow)

When a workflow is called by another workflow, authorization should be enforced by the caller, not the callee. This prevents double-checking and allows the caller to pass through pre-validated context.

## Adding New Authorized Users

### Per-Workflow (Repository Variables)

For repositories that consume egg as a reusable workflow:

```yaml
# In your workflow
uses: jwbron/egg/.github/workflows/sdlc-pipeline.yml@main
with:
  authorized_users: "your-team-member, another-user"
```

### Organization-Wide

If using organization membership checks, add users to the org and configure:

```yaml
authorized_users: "@your-org"
```

**Note**: Organization membership checks require `CHECK_ORG_MEMBERSHIP=true` and a `GH_TOKEN` with org read permissions.

## Security Considerations

### What Authorization Prevents

1. **Unauthorized agent invocation**: Random users cannot trigger expensive agent operations
2. **Resource exhaustion**: Prevents malicious actors from consuming compute/API quotas
3. **Pipeline manipulation**: Only authorized users can advance SDLC phases

### What Authorization Does NOT Prevent

1. **Code injection via PR content**: Authorization checks the author, not the content. Malicious code in PRs is mitigated by:
   - Trusted checkout pattern (prompt scripts run from main branch)
   - Gateway credential isolation
   - Human review before merge

2. **Privilege escalation**: Users with repo write access can bypass authorization by using `workflow_dispatch`. This is by design — if they have write access, they can run workflows anyway.

### Defense in Depth

Authorization is one layer in egg's security model:

1. **Authorization** (this document) — controls who can trigger workflows
2. **Trusted checkout** — prevents prompt injection via malicious PR code
3. **Gateway sidecar** — enforces phase transitions and credential isolation
4. **Human gates** — require approval before merge

## Troubleshooting

### "User 'X' is not authorized to trigger..."

The user attempting to trigger the workflow is not in the `authorized_users` list.

**Resolution**: Add the user to `authorized_users` input or use `workflow_dispatch` if they have repo write access.

### Authorization check not running

The workflow may have been triggered via `workflow_dispatch` or `workflow_call`, which bypass authorization by design.

### Org membership check failing

Ensure:
1. `CHECK_ORG_MEMBERSHIP=true` is set
2. `GH_TOKEN` has `read:org` scope
3. The org name is prefixed with `@` in `authorized_users`

## Related Documentation

- [SDLC Pipeline Guide](../guides/sdlc-pipeline.md)
- [Gateway Sidecar](../../gateway/README.md) — credential isolation
- [ADR: SDLC Pipeline](../adr/implemented/ADR-SDLC-Pipeline.md) — threat model
