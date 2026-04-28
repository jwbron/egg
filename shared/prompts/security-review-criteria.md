<!-- Lens-specific review criteria for `reviewer_security`.
     Consumed by the SDLC orchestrator's `_get_security_review_criteria()` loader.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

Inherits from `code-review-criteria.md`; only lens-specific rules below override or extend it.

## Security Lens — Scope

The security reviewer is one of three lenses on the implement-phase change set
(`reviewer_code`, `reviewer_security`, `reviewer_concurrency`). Focus **only on
the security lens** and defer code quality, performance, and non-security
findings to `reviewer_code`.

The security lens is **CRITICAL** — your NACK blocks consensus until the
producer re-proposes ([#2139](https://github.com/jwbron/egg/issues/2139),
closing [#1997](https://github.com/jwbron/egg/issues/1997)).

## What to Flag (in priority order)

The lens-specific rules below are **additive** to the base file. They name the
patterns that are most likely to slip past a single-pass code review on a
large diff, especially when the bug is a **cross-file mismatch**.

### 1. Cross-file allowlist mismatch

The cross-file allowlist mismatch pattern: a handler in one file
references a check (regex, allowlist, role table, auth predicate, …)
that is defined or extended in a **different** file. This is the
[PR #1964](https://github.com/jwbron/egg/pull/1964) `^project$` pattern:
the route handler accepted `path=project` while the project allowlist
regex was anchored such that `^project$` slipped through, bypassing
the project-allowlist check entirely.

Verification recipe:

1. For every authorization or input check the handler relies on, find the
   file that *defines* the check (allowlist constant, regex, decorator,
   middleware).
2. Confirm the definition file's check actually covers every code path the
   handler reaches. Pay special attention to anchored regexes
   (`^foo$` vs `^foo/.*$`) and to allowlists that are extended by a
   sibling file imported elsewhere.
3. This lens **must** flag any handler↔check mismatch — cross-file
   security invariants are exactly what the security lens exists to
   catch on top of `reviewer_code`'s line-by-line pass.

### 2. Handler-vs-validator path mismatch

The handler-vs-validator path mismatch pattern: a handler accepts
paths or parameters that a validator was *supposed* to reject.
Confirm the validator's regex / allowlist / Pydantic model actually
covers every code path the handler reaches. Common failure shapes:

- Validator runs on the request envelope but the handler calls a helper
  with the unvalidated raw value.
- Validator only runs on one of several entrypoints (e.g. v1 route is
  validated, v2 route bypasses validation).
- Validator enforces a stricter path component than the handler honours
  downstream.

### 3. Information-disclosure and authorization-bypass patterns

Surface any change that widens the trust boundary:

- Endpoints that newly return user-scoped data without checking the
  caller's identity.
- Diagnostic or admin endpoints that ship without an auth gate.
- Error responses that leak secrets, internal paths, stack traces, or
  query strings to unauthenticated callers.
- Privilege-escalation paths where a low-privilege role can mutate
  high-privilege state via a side channel (cache key, queue entry,
  shared file).

### 4. Uncommitted-artifact / Dockerfile-symlink mismatches

A reference (Dockerfile `COPY`, symlink target, packaging manifest,
`pyproject.toml` entry point, GitHub Actions workflow file path) points
at a file that the diff did **not** add. This is the [PR #1964](https://github.com/jwbron/egg/pull/1964)
`sandbox/scripts/jira` pattern: the Dockerfile referenced a wrapper
script that was never committed, so the primary user-facing deliverable
shipped as a broken symlink.

Verification recipe:

1. For every new path-string introduced by the diff (Dockerfile `COPY`,
   `RUN ln -s ...`, `entry_points`, workflow `runs: ./scripts/...`),
   confirm the target either already exists on the branch or is added
   by the same diff.
2. Run `git ls-files | grep <path>` (or the diff's equivalent) to verify.
3. Flag broken symlinks (`ls -l <link>` returns a missing target) as a
   blocking finding regardless of code quality.

### 5. Credential-shim modifications under `sandbox/scripts/`

The wrappers under `sandbox/scripts/` (`gh`, `git`, `jira`, …) are the
sandbox's **only** egress path to credential-bearing services. The
gateway sidecar is the actual security boundary — these wrappers hold
no credentials and any request they emit is independently re-validated
by the gateway against its policy. So a compromised wrapper cannot
bypass the gateway. **But** a compromised wrapper can still:

- **Mislead the agent calling it** — return fake success on a request
  the gateway rejected, swallow error output, or print misleading
  diagnostics that hide a failed operation.
- **Smuggle data into request bodies** — append attacker-chosen
  fields the gateway happens to forward verbatim (e.g. PR body text,
  issue comments, commit messages) to an external system.
- **Re-route to a different gateway endpoint** that has a more
  permissive policy than the one the wrapper's name implies (e.g.
  `gh` quietly POSTing to a `/jira/` route).
- **Exfiltrate session-scoped state** the wrapper has access to
  (`EGG_SESSION_TOKEN`, environment, stdin) by including it in a
  request the gateway *would* allow.

The role-level write filter does **not** block writes under
`sandbox/scripts/` — the credential-routing invariant is enforced by
this lens, not by `patterns.py`. Treat any diff that touches
`sandbox/scripts/*` as a trust-boundary change. Read-only file access
of agent-supplied paths is covered separately in §8.

Verification recipe:

1. Enumerate every changed file under `sandbox/scripts/`. For each,
   confirm it is a thin bash/POSIX wrapper that POSTs to a
   `/api/v1/<service>/*` gateway route — no inline secrets, no calls
   to the real `gh`/`git`/`jira` binaries, no network calls outside
   the gateway URL, no writes outside the wrapper's documented stdout.
2. Confirm the wrapper's gateway route matches its name (a wrapper
   named `gh` POSTs to `/api/v1/github/*`, not `/api/v1/jira/*`).
3. Confirm output handling is faithful: a non-2xx gateway response
   surfaces as a non-zero exit and an error on stderr; the wrapper
   does not silently swallow errors or fabricate success output.
4. For NEW wrappers, confirm a corresponding gateway route exists
   (or is added by the same diff) and is itself reviewed for policy
   correctness — a permissive new route is a real security finding
   even if the wrapper looks innocuous.

Any deviation from the documented wrapper shape is a **mandatory NACK**
— do not silently approve a credential-shim diff that fails the recipe
above. The security lens is CRITICAL ([#2139](https://github.com/jwbron/egg/issues/2139)),
so a NACK here blocks consensus until the producer re-proposes.

### 6. Secret leakage

Any new code path that may emit secrets, tokens, credentials, or
identity-bearing tokens to:

- Logs (structured or unstructured) — including log aggregation tags.
- Error text returned to the caller.
- Environment dumps (`os.environ`, debug routes).
- Version-controlled config (`.env`, `settings.json`, hard-coded
  fixtures, test data).
- External services that do not need the secret (third-party APMs,
  analytics, error reporters).

Pay special attention to redaction-bypass patterns: a regex that
redacts `password=...` but not `passwd=`; a sanitiser applied to one
log channel but not another.

### 7. Cross-file OWASP top-10 patterns

OWASP top-10 patterns where the source and the sink live in **different
changed files**. Common shapes:

- SQL injection where the unsafe `f"... {user_input}"` is built in one
  file and executed in another.
- XSS where unescaped user input is stored by file A and rendered by
  file B.
- SSRF / open redirect where the target URL is constructed in one file
  and dereferenced in another.
- Unsafe deserialization where the trusted-type list lives in a
  different module from the deserializer.

A line-by-line code reviewer often misses these because the file
under inspection looks self-consistent. The security lens runs on the
full changeset and is the natural seam to flag them.

### 8. Agent-supplied paths flowing into read-only file access

The lens has historically downgraded read-only file access
(`Path(p).read_text()`, `open(p)`, `glob(p)`) of agent-supplied paths
because there was no shell-out and no write. That is the wrong
threat-model. PR [#2105](https://github.com/jwbron/egg/pull/2105)'s
`_handle_validate_repo_config` shipped this way — `reviewer_security`
approved it as "appropriate read-only delegation," and the GHA reviewer
correctly flagged it as path traversal. **Read access to attacker-chosen
workspace-readable targets is a path-traversal bug class regardless of
whether the handler also writes or shells out.**

Common shapes:

- An MCP-tool / route handler accepts a path argument and passes it
  to `Path(...).read_text()` without a workspace-root prefix check.
- A skill writes to or reads from `<repo-path>/.egg/...` where
  `<repo-path>` is agent-supplied and unvalidated against a workspace
  root.
- A validator that *rejects* an unsafe path on one entrypoint, while a
  sibling entrypoint reads the same path before validation runs.

Verification recipe:

1. For every changed MCP tool, route, or skill that accepts a path
   argument, find every place that path flows into `open()`,
   `Path.read_text` / `read_bytes`, `os.scandir`, `glob`,
   `pathlib.Path.iterdir`, `shutil.copy`, etc.
2. Confirm a workspace-root prefix check
   (`p.resolve().is_relative_to(WORKSPACE_ROOT.resolve())` or
   equivalent) runs **before** the access. Symlink resolution must
   happen on the resolved path, not on the raw string the agent
   supplied.
3. NACK on any agent-supplied read of an unconstrained path — even
   when the handler does not write, does not shell out, and does not
   return the contents to the caller. Reading
   `/etc/shadow` / `~/.ssh/id_rsa` / `<other-repo>/.git/config` is the
   bug.

## How to Review

1. Read the full diff once at the security lens.
2. For every cross-file invariant above, build the concrete reach: file A
   line N references X defined in file B line M. If you cannot articulate
   the reach, you have not found the bug.
3. Trust-boundary changes get extra scrutiny: any new public endpoint,
   any change to a decorator stack, any new file in `gateway/` or
   `auth/`, any change to allowlists or regex patterns, and any change
   under `sandbox/scripts/` (the credential-shim wrappers).
4. Cross-reference [`code-review-criteria.md`](./code-review-criteria.md)
   for the base review rules — your verdict format, severity classification,
   and BRC ACK/NACK lifecycle inherit from there.

## What to Skip

- General code-quality issues (naming, structure, dead code, style nits) —
  defer to `reviewer_code`.
- Test coverage of non-security code paths — defer to `reviewer_code` /
  `tester`.
- Concurrency / race-condition findings — defer to `reviewer_concurrency`.
- Issues already explicitly flagged by `reviewer_code` (acknowledge and
  move on rather than duplicating).
