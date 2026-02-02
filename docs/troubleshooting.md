# Troubleshooting

Common issues and their solutions.

## Docker Issues

### Docker daemon not running

**Symptom:**
```
Cannot connect to the Docker daemon at unix:///var/run/docker.sock
```

**Solution:**
```bash
# Linux
sudo systemctl start docker

# macOS
# Start Docker Desktop from Applications
```

### Permission denied

**Symptom:**
```
permission denied while trying to connect to the Docker daemon socket
```

**Solution:**
```bash
# Add user to docker group (Linux)
sudo usermod -aG docker $USER

# Log out and back in, or run:
newgrp docker
```

## Authentication Issues

### GitHub authentication failed

**Symptom:**
```
Error: Authentication failed for 'https://github.com/owner/repo'
```

**Solutions:**

1. **Check credentials file:**
   ```bash
   cat ~/.config/egg/secrets.yaml
   # Verify github_app or pats section is correct
   ```

2. **For GitHub App:**
   - Verify app is installed on the repository
   - Check private key file exists and is readable
   - Ensure app_id is correct

3. **For PAT:**
   - Verify token hasn't expired
   - Check token has required scopes (`repo`)

### Anthropic API authentication failed

**Symptom:**
```
Error: 401 Unauthorized from Anthropic API
```

**Solutions:**

1. **API Key:**
   ```bash
   # Test API key directly
   curl https://api.anthropic.com/v1/messages \
     -H "x-api-key: YOUR_KEY" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     -d '{"model":"claude-sonnet-4-20250514","max_tokens":10,"messages":[{"role":"user","content":"Hi"}]}'
   ```

2. **OAuth Token:**
   - Token may have expired
   - Run `claude auth login` to refresh

## Gateway Issues

### Gateway not responding

**Symptom:**
```
Connection refused to egg-gateway:9847
```

**Solutions:**

1. **Check container is running:**
   ```bash
   docker ps | grep egg-gateway
   ```

2. **Check gateway logs:**
   ```bash
   docker logs egg-gateway
   ```

3. **Verify network:**
   ```bash
   docker network inspect egg-isolated
   ```

### Gateway health check failing

**Symptom:**
```bash
curl http://localhost:9847/api/v1/health
# Returns error or timeout
```

**Solutions:**

1. **Check gateway startup:**
   ```bash
   docker logs egg-gateway 2>&1 | tail -50
   ```

2. **Verify configuration:**
   ```bash
   egg config validate
   ```

## Policy Violations

### Branch ownership violation

**Symptom:**
```
Error: Branch ownership violation - cannot push to 'main'
```

**Explanation:** The gateway only allows pushing to branches with the configured prefix (default: `egg/`).

**Solutions:**

1. Create a branch with the correct prefix:
   ```bash
   git checkout -b egg/my-feature
   ```

2. Or adjust the `branch_prefix` in egg.yaml (not recommended for security)

### Merge blocked

**Symptom:**
```
Error: Merge operations are blocked
```

**Explanation:** By design, agents cannot merge PRs. Humans must review and merge via GitHub UI.

**Solution:** This is intended behavior. Have a human review and merge the PR.

## Network Issues

### Cannot access external URLs (private mode)

**Symptom:**
```
Connection refused / Connection timed out
```

**Explanation:** In private mode, only allowed domains are accessible.

**Solutions:**

1. Check if the domain is in the allowlist:
   ```bash
   curl http://egg-gateway:9847/api/v1/config/domains
   ```

2. Switch to public mode if needed:
   ```bash
   egg stop
   egg start  # Without --private flag
   ```

### WebSearch/WebFetch not working (private mode)

**Symptom:**
```
WebSearch tool call returned error
```

**Explanation:** WebSearch and WebFetch are blocked in private mode to prevent data exfiltration.

**Solution:** Use public mode if you need web search capabilities.

## Session Issues

### Session expired

**Symptom:**
```
Error: Session expired or invalid
```

**Solution:**
```bash
# Restart the sandbox to create a new session
egg stop
egg start
```

### Session not found after gateway restart

**Symptom:** Commands fail after restarting the gateway.

**Explanation:** Sessions are stored in `~/.egg/sessions.json`. If this file is missing or corrupted, sessions are lost.

**Solution:**
```bash
# Restart the sandbox container
egg stop
egg start
```

## Workspace Issues

### Uncommitted changes lost

**Symptom:** Changes disappeared after container restart.

**Explanation:** Only committed changes are preserved. Uncommitted changes in the working directory are lost if the container stops unexpectedly.

**Best Practice:** Commit frequently.

### Cannot see other branches

**Symptom:** `git branch -a` only shows the current branch.

**Explanation:** The container doesn't have access to git metadata. Branch information is managed by the gateway.

**Solution:** Use `git log --oneline -10` through the git wrapper to see commit history.

## Getting Help

If you're still having issues:

1. Check the gateway logs:
   ```bash
   docker logs egg-gateway
   ```

2. Enable debug logging in egg.yaml:
   ```yaml
   egg:
     logging:
       level: "DEBUG"
   ```

3. Open an issue with:
   - Error message
   - Gateway logs
   - Steps to reproduce
