# Analysis: Coder file-boundary policy — replace extension allowlist with role-complement blocklist

> Issue: #1901 | Phase: refine

## Problem Statement

The `coder` agent's git-push file policy (`CODER_PATTERNS` in `shared/egg_restrictions/patterns.py`) is an **extension-based allowlist**. It enumerates source-code extensions (`**/*.py`, `**/*.ts`, `**/*.sh`, …) plus a hand-maintained list of extensionless literals (`Makefile`, `Dockerfile`, `Procfile`, `.python-version`, …). Any file that does not match any of those patterns is rejected at push time.

This breaks when the implement phase has a legitimate task on a file that is not in the enumerated set. The concrete failure observed in `issue-1762-membump` (PR #1900) was that the plan assigned the coder the task of deleting `bin/egg` (an extensionless symlink → `../sandbox/egg`) and `sandbox/egg` (an extensionless shell script). Neither matches any `CODER_PATTERNS.allowed_patterns` entry, so every push attempt came back with `Role 'coder' cannot modify: bin/egg, sandbox/egg`. The coder had no recourse, the overseer hit the same block from its own worktree, and the implement phase stalled for over an hour before a human advanced the pipeline.

The desired outcome is a file-boundary model where adding a new language, new config format, or new top-level script does **not** require editing `CODER_PATTERNS`. Specifically, the three implement-phase producer roles (`coder`, `tester`, `documenter`) should be **mutually exclusive by construction**: the coder's scope is defined as "everything the tester and documenter don't own," not as an enumerated list of extensions.

## Current Behavior

### Where restrictions live

The source of truth for agent file access is `shared/egg_restrictions/patterns.py`:

- **`AgentFilePattern.can_write(file_path)`** (patterns.py:59–97) is the matcher. Logic:
  1. Reject path-traversal attempts (`..`).
  2. If `file_path` matches any `blocked_patterns` **and** does not match any `block_exempt_patterns`, deny.
  3. If `allowed_patterns` is empty, **deny** (deny-by-default — see `test_no_allowed_patterns_denies_all` in `gateway/tests/test_agent_restrictions_patterns.py:139–141`).
  4. If `file_path` matches any `allowed_patterns`, allow; otherwise deny.
- **`CODER_PATTERNS`** (patterns.py:170–249) currently has ~25 allow patterns (extensions + literal build files + `skills/` + `.egg-state/agent-outputs/`) and a parallel blocklist for docs/tests/contracts, plus `block_exempt_patterns` for `.md` files in `sandbox/agent-config/{rules,commands}/` and `skills/` (functional-code markdown).
- **`TESTER_PATTERNS`** (patterns.py:251–291) allows `tests/`, `test/`, `**/tests/`, `**/test/`, test-file name patterns (`**/*_test.py`, `**/*.test.ts`, …), `**/conftest.py`, plus config/lock files needed for test env. Blocks docs and `.egg-state/contracts/`.
- **`DOCUMENTER_PATTERNS`** (patterns.py:293–324) allows `docs/`, `**/*.md`, `**/README.md`, `.egg-state/agent-outputs/`. Blocks source extensions and tests.

### Where the gateway enforces it

- `shared/egg_restrictions/checker.py::validate_agent_push()` drives the per-agent validation at push time.
- `gateway/phase_filter.py::check_file_restrictions()` (phase_filter.py:591–635) applies role-based restrictions from `.egg/phase-permissions.json` (distinct from the per-agent patterns above — that file only lists `role: implementer, blocked_patterns: ['.egg-state/contracts/']`). It is layered on top of the agent-pattern check.
- When a push is rejected, the error message is `"Role '{role}' cannot modify: {comma-joined files}"`.

### The extensionless-file problem

Inside the repo today there are several extensionless executable files that the coder cannot touch via the current allowlist:

| Path | Current coder access |
|---|---|
| `bin/egg` (symlink → `../sandbox/egg`) | ❌ blocked |
| `bin/egg-deploy`, `bin/egg-onboarding-docs`, `bin/egg-sdlc`, `bin/egg-status` | ❌ blocked |
| `sandbox/egg` | ❌ blocked |
| `sandbox/bin/egg-health-inspect`, `sandbox/bin/egg-onboarding-docs`, `sandbox/bin/egg-pipeline-watch`, `sandbox/bin/egg-sdlc` | ❌ blocked |
| `sandbox/scripts/gh`, `sandbox/scripts/git`, `sandbox/scripts/git-credential-github-token` | ❌ blocked |
| `LICENSE`, `.dockerignore` | ❌ blocked |

So the observed failure is the tip of a larger iceberg — any task that needs to delete, rename, or edit any of the above extensionless files stalls today.

### Parallel allowlists in other roles

The same pattern (extension-based allowlist) recurs elsewhere and has the same latent risk:

- **`AUTOFIXER_PATTERNS`** (patterns.py:511–555) — enumerates extensions; will fail if auto-fixers are asked to operate on extensionless files.
- **`CONFLICT_RESOLVER_PATTERNS`** (patterns.py:561–625) — enumerates extensions; same caveat.
- **`OVERSEER_PATTERNS`** (patterns.py:481–504) — uses a *blocklist* style, but with an empty allowlist it falls through to "deny all" per the matcher's step 3 semantics. The issue's reporter notes overseer hit the same wall when trying to delete `bin/egg`/`sandbox/egg` from its own worktree.

### Pre-existing matcher quirk (found during analysis)

`AgentFilePattern._matches_pattern` (patterns.py:122–164) does not correctly handle directory patterns of the form `**/tests/`. A pattern like `**/tests/` matches neither `gateway/tests/__init__.py` nor `tests/__init__.py` via its `**`-aware branch (it tries to match `__init__.py` against the suffix `tests/` via `fnmatch`, which fails). The current CODER_PATTERNS blocklist relies on the file-name patterns (`**/test_*.py`, `**/*_test.py`, `**/conftest.py`) to catch nested test files — which works for conventionally-named files but leaks for helpers like `__init__.py` inside nested `tests/` dirs. This is **not** what the issue asks us to fix, but any blocklist-complement rewrite will inherit (or choose to resolve) this limitation.

### Gateway phase-filter parallel

`.egg/phase-permissions.json` has a separate `file_restrictions` section (implementer role blocked from `.egg-state/contracts/`) and a phase-level `blocked_patterns` section for the `implement` phase. These are enforced by `gateway/phase_filter.py` in a separate code path from `validate_agent_push`. The rewrite does not obviously need to touch these files, but there is a sync comment in `shared/egg_container/__init__.py:131` (`"Must stay in sync with .egg/phase-permissions.json blocked_patterns for 'implement'"`) indicating that three surfaces (patterns.py, phase-permissions.json, egg_container/_IMPLEMENT_READONLY_DIRS) all attempt to describe overlapping concepts today.

## Constraints

- **Matcher semantics are load-bearing.** `test_no_allowed_patterns_denies_all` (in `gateway/tests/test_agent_restrictions_patterns.py:139`) explicitly pins the behavior that an empty `allowed_patterns` denies everything. Flipping that semantic is a breaking change to every role that currently relies on it (ARCHITECT, TASK_PLANNER, RISK_ANALYST, REFINER, reviewers, OVERSEER, INSPECTOR — all of which intentionally set a short allowlist and rely on it being authoritative). Either the coder needs an explicit catch-all pattern (`**`), or the matcher needs a new opt-in flag.
- **Security: deny-by-default is the current posture.** The blocklist-complement approach flips the coder to "allow-by-default" within the non-blocked space. This is by design (it's the whole point of the issue) but means the blocklist becomes the only thing preventing coder from overwriting files in scopes we care about. The blocklist must be comprehensive for all current and future sensitive areas.
- **Path-traversal defence must be preserved.** `_normalize_path` still rejects `..` segments; that guard is orthogonal to the allowlist/blocklist question and must be retained.
- **Mutual exclusion with tester/documenter must remain airtight.** Any scope the tester and documenter own must be represented in coder's blocklist, and any block-exempt carve-outs (`sandbox/agent-config/{rules,commands}/*.md`, `skills/`) must continue to work correctly.
- **Gateway phase-filter layer is independent.** `.egg/phase-permissions.json` file_restrictions and implement-phase blocked_patterns are enforced in `gateway/phase_filter.py` and apply on top of the agent-pattern check. The rewrite does not need to touch them to fix the reported bug, but diverging surfaces will drift.
- **Test surface is large.** The patterns are exercised in at least `shared/tests/test_egg_restrictions.py` (664 lines), `gateway/tests/test_agent_restrictions_patterns.py` (798 lines), `gateway/tests/test_agent_restrictions_comprehensive.py`, `gateway/tests/test_agent_restrictions_enforce.py`, `gateway/tests/test_push_error_enrichment.py`, and `gateway/tests/test_phase_filter_restrictions.py`. At least one test (`test_no_allowed_patterns_denies_all`) will change behavior under Option B below and needs to be re-expressed.
- **Documentation has the allowlist baked in.** `docs/reference/agent-roles.md:134` enumerates the current coder allow list by extension; it must be rewritten.
- **Backward compat with in-flight pipelines is not at risk** — pattern changes apply at push time, no migration needed.

## Options Considered

### Option A: Explicit catch-all in coder's allowlist (keep matcher semantics)

**Approach**: Rewrite `CODER_PATTERNS.allowed_patterns = ["**"]` (catch-all) and rely entirely on `blocked_patterns` + `block_exempt_patterns` to carve out the tester, documenter, and pipeline-state scopes. Matcher semantics are unchanged. Coder-specific result:

```python
CODER_PATTERNS = AgentFilePattern(
    role=AgentRole.CODER,
    description="Coder: everything except tester/documenter/pipeline-state scope",
    allowed_patterns=["**"],
    blocked_patterns=[
        # Pipeline state (agent-outputs carved back via block_exempt_patterns)
        ".egg-state/",
        # Documenter's scope
        "docs/",
        "**/*.md",
        "**/README.md",
        # Tester's scope
        "tests/",
        "test/",
        "**/tests/",
        "**/test/",
        "**/*_test.py", "**/test_*.py",
        "**/*_test.go", "**/test_*.go",
        "**/*.test.ts", "**/*.test.tsx", "**/*.test.js", "**/*.test.jsx",
        "**/*.spec.ts", "**/*.spec.tsx", "**/*.spec.js", "**/*.spec.jsx",
        "**/conftest.py",
    ],
    block_exempt_patterns=[
        ".egg-state/agent-outputs/",          # coder's handoff dir
        "sandbox/agent-config/rules/*.md",    # functional-code markdown
        "sandbox/agent-config/commands/*.md",
        "skills/",                            # SKILL.md is functional code
    ],
)
```

I verified this shape against the current matcher: `bin/egg`, `sandbox/egg`, `Makefile`, `.gitignore`, `LICENSE`, and `pyproject.toml` all return `True`; `docs/*.md`, `**/README.md`, `tests/test_*.py`, `.egg-state/contracts/*`, `.egg-state/drafts/*` all return `False`; `.egg-state/agent-outputs/*` and `sandbox/agent-config/rules/*.md` still return `True` via the exemption path.

**Pros**:
- Minimum change. Matcher behavior, other roles, other tests unchanged.
- The sentinel `["**"]` reads self-evidently as "coder gets everything not listed below."
- Does not touch the load-bearing `test_no_allowed_patterns_denies_all` invariant.
- `skills/` and `sandbox/agent-config/*/*.md` exceptions continue to use the existing `block_exempt_patterns` mechanism — no new machinery.

**Cons**:
- `["**"]` as a catch-all looks slightly odd next to the real allow lists other roles have. A reader might assume coder's `allowed_patterns` is meaningful when it's really a shim around the "empty-means-deny-all" matcher invariant.
- Does not address the parallel problem in `AUTOFIXER_PATTERNS` / `CONFLICT_RESOLVER_PATTERNS` / `OVERSEER_PATTERNS` unless those are migrated in the same change.

### Option B: Change matcher semantics — empty `allowed_patterns` means "allow by default"

**Approach**: Modify `AgentFilePattern.can_write` so that an empty `allowed_patterns` is interpreted as "allow anything not blocked." Then rewrite `CODER_PATTERNS` with an empty `allowed_patterns` list (as the issue body suggests). Example:

```python
def can_write(self, file_path: str) -> bool:
    ...
    if any(self._matches_pattern(normalized, p) for p in self.blocked_patterns):
        if not any(self._matches_pattern(normalized, p) for p in self.block_exempt_patterns):
            return False
    # New behavior: empty allowed_patterns = permissive default
    if not self.allowed_patterns:
        return True
    return any(self._matches_pattern(normalized, p) for p in self.allowed_patterns)
```

**Pros**:
- Semantically cleaner. The data mirrors the intent: "here are the things I don't own; everything else is mine."
- Makes it mechanical to apply the same rewrite to autofixer, conflict_resolver, and potentially overseer by simply clearing their allowlists.
- Smaller pattern file overall.

**Cons**:
- **High blast radius.** Every role today uses the "deny-all when allowed is empty" semantic as its backstop. Analysis roles (architect, task_planner, risk_analyst, refiner), reviewer roles, inspector, and overseer all have short allowlists; they'd continue to behave correctly only because their allowlists are non-empty. But any future refactor that accidentally empties one of those lists would silently flip from deny-all to allow-all — the exact failure mode of this issue's `CODER_PATTERNS`, but with more sensitive roles.
- Invalidates `test_no_allowed_patterns_denies_all` and any downstream tests that assume the deny-all behavior.
- Harder to reason about per-role safety without reading the matcher.

### Option C: New `allow_by_default: bool` flag on `AgentFilePattern`

**Approach**: Add a field:

```python
@dataclass
class AgentFilePattern:
    role: str
    allowed_patterns: list[str] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    block_exempt_patterns: list[str] = field(default_factory=list)
    allow_by_default: bool = False  # NEW
    description: str = ""
```

`can_write` consults the flag only after `allowed_patterns` is exhausted:

```python
if not self.allowed_patterns:
    return self.allow_by_default
return any(self._matches_pattern(normalized, p) for p in self.allowed_patterns)
```

Coder sets `allow_by_default=True`, empties `allowed_patterns`, keeps the blocklist. Other roles keep today's deny-all safety.

**Pros**:
- Explicit opt-in: each role self-declares whether it wants allowlist or complement semantics.
- No accidental flipping of other roles if their allowlists are emptied.
- Tests remain valid (the new path is only taken when the flag is set).

**Cons**:
- Adds a third axis of pattern configuration that future readers must know about.
- Marginal gain over Option A since Option A already achieves the policy change with zero new machinery.

### Option D: Reverse-define the coder from the union of tester + documenter

**Approach**: At module load time, compute `CODER_PATTERNS.blocked_patterns` as `TESTER_PATTERNS.allowed_patterns ∪ DOCUMENTER_PATTERNS.allowed_patterns ∪ [".egg-state/contracts/", ".egg-state/drafts/", ...]`. The coder is literally "not-tester and not-documenter."

**Pros**:
- Mutual exclusion is mechanical — it cannot drift. If tester grows a new pattern, coder automatically shrinks.
- Matches the conceptual framing in the issue body exactly.

**Cons**:
- Ordering/initialization complexity: module-level mutation of module-level constants is fragile under Python import semantics.
- Tester's allowlist contains non-scope items (`.python-version`, `**/*.lock`, `**/requirements*.txt`, `.egg-state/agent-outputs/`) that are shared between coder and tester — direct union would incorrectly block coder from those.
- The tester/documenter allowlists are not currently structured as "my scope" — they include handoff paths (agent-outputs) and shared config. They would first need to be refactored to split "my scope" from "allowed to touch."
- Higher scope of change for marginal ergonomic win.

## Recommended Approach

**Option A (explicit catch-all in coder's allowlist).** It is the smallest viable change that fixes the reported bug and matches the issue's proposed model, without disturbing the load-bearing deny-all backstop that every other role relies on. Specifically:

1. Rewrite `CODER_PATTERNS` with `allowed_patterns=["**"]`, `blocked_patterns=[ …docs, tests, .egg-state/ … ]`, and the existing `block_exempt_patterns` list extended with `.egg-state/agent-outputs/`.
2. Leave the matcher semantics (`can_write`, `_matches_pattern`, `_normalize_path`) untouched.
3. Leave other roles' patterns untouched **unless** the human directs otherwise via decision-2 (autofixer/conflict_resolver scope) or decision-4 (overseer scope).
4. Update `docs/reference/agent-roles.md` to describe the new model (the documenter owns this, but the refined analysis captures the intent).
5. Add tests for the concrete failure modes from the issue (`bin/egg`, `sandbox/egg`, arbitrary new path, top-level dotfiles), plus regression tests for the existing allowed/blocked cases.

Option C (opt-in flag) is a reasonable second choice if the human wants the cleaner data model now at the cost of a small machinery change. Option B is not recommended — the blast radius of flipping default semantics for every role is disproportionate to the bug being fixed. Option D is not recommended — initialization ordering and the need to first refactor tester/documenter allowlists makes it the wrong shape for a bug fix.

## Open Questions

All open questions have been registered via `egg-contract` and appear as separate decision/feedback comments on the issue for the human to answer. They are summarized here for context:

<!-- egg-hitl-decision id=decision-1 -->

**How should the matcher treat an empty allowed_patterns list for the coder role?**

- [ ] Keep deny-all semantics; add an explicit catch-all pattern ('**') to CODER_PATTERNS.allowed_patterns
- [ ] Change semantics: empty allowed_patterns means 'allow by default' (blocklist-only mode) across all roles
- [ ] Add a new sentinel flag (e.g., allow_by_default=True) on AgentFilePattern; coder opts in
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-2 -->

**Should autofixer and conflict_resolver (which also use extension-based allowlists) get the same blocklist-complement treatment in this issue?**

- [ ] Yes — rewrite all three utility roles now for consistency (coder, autofixer, conflict_resolver)
- [ ] Coder only — keep this issue scoped to the reported bug; file follow-ups for autofixer/conflict_resolver
- [ ] Coder + autofixer (both apply automated fixes to source) — leave conflict_resolver for a follow-up
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**How should the coder's .egg-state/ access be carved?**

- [ ] Block all of .egg-state/ and use block_exempt_patterns=['.egg-state/agent-outputs/'] to carve out the handoff dir (what the issue proposes)
- [ ] Enumerate blocked .egg-state/ subdirs explicitly: contracts/, drafts/, pipelines/, reviews/, oversight/, checkpoints/, brc-history/, agent-anchors/ — and leave agent-outputs/ implicitly allowed
- [ ] Keep today's approach: allowed_patterns includes '.egg-state/agent-outputs/' and blocked_patterns includes '.egg-state/contracts/' only
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**Overseer hit the same wall per the issue's 'out of scope' note. Should we include overseer in this fix?**

- [ ] Out of scope — leave overseer for a separate issue as the reporter suggested
- [ ] In scope — give overseer the same treatment (allow everything except .egg-state/ non-oversight subdirs, tester's scope, documenter's scope)
- [ ] Middle ground — tighten overseer's access only enough to unblock the specific deletion path that stalled the pipeline (list extensionless scripts)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**Should we also fix the pre-existing matcher bug where '**/tests/' does NOT match nested test directories as a directory (nested .py files under test dirs are only blocked via file-name patterns like '**/test_*.py', so nested test __init__.py leaks through)?**

- [ ] Yes — fix the '**/tests/' directory pattern matching as part of this issue (coder would then be strictly blocked from any file under a tests/ subdirectory)
- [ ] No — keep the pre-existing behavior; file a separate issue for the matcher bug
- [ ] Partial — add explicit file-type patterns covering the leak (e.g., '**/tests/**') without changing the general matcher
- [ ] Other (explain in reply)

<!-- egg-feedback id=feedback-1 -->

**Open-ended questions** (registered via `egg-contract add-feedback`):

- **Q1**: The `.egg/phase-permissions.json` file has a separate `file_restrictions` section (role=`implementer`, blocked_patterns=`['.egg-state/contracts/']`) enforced by `gateway/phase_filter.py::check_file_restrictions`. Is that role-based phase restriction expected to stay as-is, or should it also reflect the new three-role model (e.g., split into coder/tester/documenter)?
- **Q2**: Comment in `shared/egg_container/__init__.py:131` says `_IMPLEMENT_READONLY_DIRS` "Must stay in sync with .egg/phase-permissions.json blocked_patterns for 'implement'". After this rewrite, which file is authoritative, and should the container side be simplified to derive its readonly dirs from the blocklist (rather than hardcoded)?
- **Q3**: Under the new model, coder gains write access to any new top-level path or file type — including hypothetical `.egg-state/` subdirs that don't exist yet. Are there future `.egg-state/` subdirectories you'd want coder pre-emptively blocked from (e.g., a future `.egg-state/secrets/`)?
- **Q4**: Currently `CODER_PATTERNS.allowed_patterns` includes `skills/` (a directory prefix), while `DOCUMENTER_PATTERNS` blocks `**/*.md` implicitly. Under the new blocklist-complement model, should `skills/` .md files still be coder-owned (as today) or are they documenter-owned? (`SKILL.md` is arguably functional code describing agent behavior, but it's still markdown.)

---

*Authored-by: egg*

<!-- metadata -->
```yaml
# metadata
complexity_tier: medium
parallel_phases: false
```
