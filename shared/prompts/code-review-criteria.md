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

### How to Review

1. **Examine every changed file systematically**. Do not skim.
2. **Read surrounding context**—check how changed code integrates with the rest of the codebase. Use file reads and grep liberally.
3. **Trace data flow** from input to output, especially for security-sensitive paths.
4. **Consider edge cases** the author may not have tested.
5. **Research when uncertain**—look up library behavior, check documentation, verify assumptions.

### Skip

- Style issues handled by linters (formatting, import order)
- Type annotation completeness (type checkers handle this)
- Auto-generated files (migrations, lock files)
