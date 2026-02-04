# Gateway Sidecar Integration Tests

## Running Tests in egg Container

### Quick Start

```bash
# From inside the egg container:
~/repos/egg/gateway/tests/integration_test.sh
```

### Output Location

Results are written to: `~/sharing/gateway-test-results.json`

This path is in the shared volume, so you can access it from the host.

### Copying Results

**Option 1: From inside container (paste into host terminal)**
```bash
cat ~/sharing/gateway-test-results.json
```

**Option 2: From host using docker cp**
```bash
docker cp sandbox:/home/jwies/sharing/gateway-test-results.json ./
```

**Option 3: Direct path on host** (if sharing volume mounted)
```bash
cat ~/.egg-sharing/gateway-test-results.json
```

### Command Line Options

```bash
./integration_test.sh [OPTIONS]

Options:
  --output FILE     Write results to FILE (default: ~/sharing/gateway-test-results.json)
  --repo PATH       Repository path (default: ~/repos/egg)
  --gateway URL     Gateway URL (default: http://egg-gateway:9847)
  -h, --help        Show help
```

### Test Categories

| Category | Description |
|----------|-------------|
| Environment | Checks gateway URL, session token, repo, wrappers |
| Connectivity | Health endpoint, token validity |
| Authentication | 401 for missing/invalid tokens, 200 for valid |
| Git Operations | remote, status, fetch, push (with policy) |
| gh Operations | auth status, repo view, pr list, issue list |
| Blocked Operations | pr merge, repo delete, repo create |
| Rate Limiting | Rate limit info, normal volume handling |
| Fail-Closed | Operations fail when gateway unavailable |
| Direct API | Raw API endpoint tests |

### Expected Results

For a working gateway sidecar setup:

- **Environment**: All pass (wrappers may skip if using system binaries)
- **Connectivity**: Must pass
- **Authentication**: Must pass
- **Git Operations**: Most pass; push to main may be blocked by policy (expected)
- **gh Operations**: All pass
- **Blocked Operations**: All must show "blocked" (security-critical)
- **Rate Limiting**: Pass or skip
- **Fail-Closed**: Must pass (security-critical)
- **Direct API**: Pass

### JSON Output Format

```json
{
  "timestamp": "2026-01-22T...",
  "gateway_url": "http://egg-gateway:9847",
  "repo_path": "/home/jwies/repos/egg",
  "summary": {
    "passed": 25,
    "failed": 0,
    "skipped": 3,
    "total": 28
  },
  "results": [
    {
      "name": "test_name",
      "status": "pass|fail|skip",
      "message": "Description",
      "details": "Raw output or error"
    }
  ]
}
```

### Troubleshooting

**Gateway connection refused**
- Check gateway container is running: `docker ps | grep egg-gateway`
- Check network: `docker network inspect egg-network`
- Verify GATEWAY_URL: default is `http://egg-gateway:9847`

**Authentication failures**
- Check session token exists: `ls -la ~/.config/egg/session-token`
- Session token is created during container registration with the gateway
- For launcher operations, verify launcher secret: `~/.config/egg/launcher-secret`

**Git/gh operations fail**
- Check wrappers are first in PATH: `which git`, `which gh`
- Verify wrappers are the egg scripts, not system binaries

**Blocked operations not blocking**
- This is a security issue - check gateway policy.py
- Verify gateway is actually handling requests (check logs)
