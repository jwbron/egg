<!-- Shared review criteria: consumed by GHA prompt scripts AND orchestrator pipelines.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

## Default Review Rules

**Be extremely thorough.** This is critical infrastructure. Identify ALL issues in the first pass—do not stop after finding a few. A false negative (missing a bug) is far worse than extra scrutiny.

### What to Review

**Security** (highest priority):
- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)
- Authentication/authorization flaws
- Credential exposure, hardcoded secrets
- Insecure cryptography or randomness
- SSRF, open redirects, unsafe deserialization

**Correctness**:
- Logic errors, off-by-one, boundary conditions
- Race conditions, deadlocks, concurrency bugs
- Null/undefined handling, missing error paths
- Resource leaks (connections, file handles, memory)
- Incorrect algorithm complexity for data size
- **End-to-end feature functionality**: For new features, verify the feature actually works in its real execution environment, not just that the code is well-structured. Trace the full path from trigger to effect. If a feature's core functionality is broken (e.g., config is read at build time but only available at runtime), that is a blocking correctness issue regardless of code quality.

**Robustness**:
- Missing input validation at trust boundaries
- Unhandled exceptions that could crash the system
- Missing retry logic for transient failures
- Inadequate timeouts for external calls
- State corruption scenarios

**Design issues**:
- Violations of existing codebase patterns
- Breaking changes to public interfaces
- Missing or incorrect abstractions
- Tight coupling that will hinder future changes

**Testing**:
- Are there tests for new functionality?
- Do existing tests still pass?
- Are edge cases covered?

**Documentation**:
- Are significant changes documented?
- Are public API changes reflected in docs?

### How to Review

1. **Examine every changed file systematically**. Do not skim.
2. **Read surrounding context**—check how changed code integrates with the rest of the codebase. Use file reads and grep liberally.
3. **Trace data flow** from input to output, especially for security-sensitive paths.
4. **Verify end-to-end functionality**: For new features, trace the complete execution path in the real deployment environment. Check that config files, environment variables, and dependencies are actually available where the code runs. A feature that reads config from a path that doesn't exist in its runtime environment is non-functional, not just suboptimal.
5. **Consider edge cases** the author may not have tested.
6. **Research when uncertain**—look up library behavior, check documentation, verify assumptions.

### Severity Classification

**Blocking** (request changes):
- Security vulnerabilities
- Non-functional features — the feature's core purpose does not work end-to-end
- Logic errors that produce incorrect results
- Breaking changes to existing functionality
- Resource leaks or crashes
- Pre-existing broken or inconsistent behavior in code the PR modifies — if the PR touches code that already has bugs, incorrect behavior, or inconsistencies (e.g., different code paths producing different results for the same input), request changes to fix it. The PR is already in the area; this is the right time.

**Non-blocking** (suggestions):
- Code quality improvements (naming, structure, duplication)
- Defense-in-depth additions
- Missing edge case handling that doesn't affect the core feature
- Documentation gaps
- Style or convention deviations not caught by linters

**Do not dismiss issues as "not a regression"**: If a PR modifies code that has existing broken or inconsistent behavior, the issue is blocking even if the PR didn't introduce it. A PR that adds a new code path through already-inconsistent logic makes the inconsistency worse — it's not acceptable to ship it just because the bug was there before. The fact that the PR is already changing this code makes it the natural place to fix it.

**Beware of false analogies**: When comparing new code to existing patterns, verify the analogy holds at the execution-model level. Two features may look structurally similar in config but have completely different execution paths. If the existing pattern works via mechanism A but the new code relies on mechanism B that doesn't exist, the comparison is invalid — classify based on actual functionality, not superficial similarity.

### Skip

- Style issues handled by linters (formatting, import order)
- Type annotation completeness (type checkers handle this)
- Auto-generated files (migrations, lock files)
