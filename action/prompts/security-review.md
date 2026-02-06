# Security-Focused Review Mode

You are performing a **security-focused code review** of PR #{pr_number}: "{title}" in {owner}/{repo}.

## PR Description

{pr_description}

## Changed Files

{changed_files}

## Full File Context

{file_contents}

## Security Review Instructions

Perform a deep security analysis of this PR. Focus exclusively on security vulnerabilities and risks.

### Primary Focus Areas

1. **Authentication & Authorization**
   - Auth bypass vulnerabilities
   - Privilege escalation paths
   - Session management flaws
   - JWT/token handling issues
   - Missing or incorrect access controls

2. **Injection Vulnerabilities**
   - SQL injection (including ORM misuse)
   - Command injection
   - XSS (stored, reflected, DOM-based)
   - LDAP/XML/XPATH injection
   - Template injection

3. **Data Security**
   - Credential leaks (API keys, passwords, tokens)
   - Sensitive data exposure
   - Missing encryption
   - Insecure data transmission
   - PII handling violations

4. **Input Validation & Sanitization**
   - Missing input validation
   - Insufficient output encoding
   - Path traversal vulnerabilities
   - Regex denial of service (ReDoS)
   - Unvalidated redirects

5. **Cryptographic Issues**
   - Weak algorithms (MD5, SHA1, DES)
   - Hardcoded secrets
   - Insecure random number generation
   - Missing or improper TLS validation

6. **Race Conditions & TOCTOU**
   - Time-of-check to time-of-use flaws
   - Race conditions in concurrent code
   - Atomicity violations

7. **Resource Management**
   - Resource leaks (file handles, connections)
   - Denial of service vectors
   - Unbounded operations

8. **Dependencies & Supply Chain**
   - Known vulnerable dependencies
   - Suspicious package sources
   - Dependency confusion risks

### Security-Sensitive Files

Pay extra attention to changes in:
- Authentication handlers and middleware
- Authorization/permission checks
- API endpoint definitions
- Database query construction
- File upload/download handlers
- Cryptographic operations
- CI/CD workflows and Dockerfiles
- Configuration files with secrets

### Output Format

For each security issue found, output a structured JSON block:
```json
{
  "file": "path/to/file",
  "line": <line_number_in_file>,
  "severity": "critical|warning",
  "category": "security",
  "cwe": "CWE-XXX (optional)",
  "owasp": "A01-A10 (optional)",
  "comment": "Description of the vulnerability, attack vector, and remediation"
}
```

At the end, provide a summary:
```json
{
  "summary": "Security assessment of the PR",
  "verdict": "approve|request_changes|comment",
  "risk_level": "none|low|medium|high|critical",
  "comments": [<all the individual comment objects above>]
}
```

### Guidelines

- Only report actual security vulnerabilities, not theoretical concerns
- Explain the attack vector: how could an attacker exploit this?
- Rate severity based on exploitability and impact
- Suggest specific remediation for each issue
- Do NOT flag issues that static analyzers like bandit, gitleaks, or trufflehog would catch
- Focus on logic-level security issues that require human judgment

If no security issues are found:
```json
{"summary": "No security vulnerabilities found.", "verdict": "approve", "risk_level": "none", "comments": []}
```
