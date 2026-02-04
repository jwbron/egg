# GitHub Auth in Long-Running Containers

## Problem Statement

Long-running containers may lose GitHub access after approximately 1 hour. Users see errors like:
- `HTTP 401: Bad credentials`
- `403 Resource not accessible by integration`

## Root Cause Analysis

### Token Lifecycle

1. **Token Generation**: GitHub App installation tokens expire after **1 hour**
2. **Token Refresh**: The gateway sidecar refreshes tokens automatically 15 minutes before expiry
3. **GitHub Access**: The `gh` CLI and `git` commands are routed through the gateway sidecar

### Architecture

```
+-----------------------------------------------------------------+
|                      GATEWAY SIDECAR                             |
+-----------------------------------------------------------------+
|  TokenRefresher (in-memory)                                      |
|  +- Refreshes tokens 15 minutes before expiry                    |
|  +- Caches tokens in memory (thread-safe)                        |
|  +- Falls back to cached token on refresh failure (up to 3x)     |
|  +- Clears cache after 3 consecutive failures (fail closed)      |
+-----------------------------------------------------------------+
                              |
                              | git/gh commands routed via gateway
                              v
+-----------------------------------------------------------------+
|                         CONTAINER                                |
+-----------------------------------------------------------------+
|  git/gh wrappers -> gateway sidecar API -> GitHub                |
+-----------------------------------------------------------------+
```

## Diagnosis Steps

### 1. Check Gateway Sidecar Health

```bash
# Check if gateway is running
curl http://egg-gateway:9847/api/v1/health
```

### 2. Check Gateway Logs

```bash
# On host
journalctl --user -u gateway -f

# Or view recent logs
journalctl --user -u gateway --since "1 hour ago"
```

### 3. Test GitHub Token

```bash
# In container - test via gateway
gh auth status
```

## Common Issues

### Gateway Sidecar Not Running

```bash
# On host - restart gateway
systemctl --user restart gateway
```

### Token Refresh Failures

If you see "Max refresh failures reached" in gateway logs:

1. Check GitHub App credentials in `~/.config/egg/`:
   - `github-app-id`
   - `github-app-installation-id`
   - `github-app.pem`

2. Verify the GitHub App is still installed on the target repositories

3. Restart the gateway sidecar to reset the failure counter:
   ```bash
   systemctl --user restart gateway
   ```

## Known Limitations

### GitHub App Token Limitations

GitHub App installation tokens have limited scope:
- `/user` endpoint returns `403 Resource not accessible by integration` (this is expected)
- Token only has access to repositories where the App is installed
- Use `/installation/repositories` to verify token validity

## See Also

- `docs/setup/github-app-setup.md` - GitHub App configuration
- `gateway/token_refresher.py` - In-memory token refresh implementation
