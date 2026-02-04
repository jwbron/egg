# Egg Container Log Persistence

This document describes the log persistence and correlation system for egg containers.

## Overview

Egg containers are ephemeral - they're removed after execution completes. To preserve logs for debugging and auditing, the system now:

1. **Persists container logs** to `~/.egg-sharing/container-logs/`
2. **Creates correlation links** between task IDs, thread timestamps, and container IDs
3. **Maintains a searchable log index** for quick lookups

## Log Locations

| Location | Contents | Persisted? |
|----------|----------|------------|
| `~/.egg-sharing/container-logs/` | Docker container stdout/stderr | Yes |
| `~/.egg-sharing/logs/` | Claude output streams (real-time) | Yes |
| Docker daemon logs | Internal Docker logs | Via json-file driver |

## Log Correlation

Every container execution is tagged with correlation IDs:

### Environment Variables (Inside Container)
- `CONTAINER_ID` - Unique container identifier (e.g., `egg-exec-20251129-222239-12345`)
- `EGG_TASK_ID` - Task identifier (e.g., `task-20251129-222239`)
- `EGG_THREAD_TS` - Thread timestamp (e.g., `1764483758.159619`)

### Docker Labels
- `egg.container_id` - Container ID label
- `egg.task_id` - Task ID label (if available)

### Log Index File
`~/.egg-sharing/container-logs/log-index.json` contains:
```json
{
  "task_to_container": {
    "task-20251129-222239": "egg-exec-20251129-222239-12345"
  },
  "thread_to_task": {
    "1764483758.159619": "task-20251129-222239"
  },
  "entries": [
    {
      "container_id": "egg-exec-20251129-222239-12345",
      "task_id": "task-20251129-222239",
      "thread_ts": "1764483758.159619",
      "log_file": "/home/user/.egg-sharing/container-logs/egg-exec-20251129-222239-12345.log",
      "timestamp": "2025-11-29T22:22:39.123456"
    }
  ]
}
```

## Using the egg-logs Utility

The `egg-logs` utility provides easy access to persisted logs:

### List Recent Logs
```bash
egg-logs                    # List last 20 logs
egg-logs --list 50          # List last 50 logs
```

### View Logs for a Task
```bash
egg-logs task-20251129-222239
```

### Search Logs
```bash
egg-logs --search "error"           # Search all logs
egg-logs --search "authentication"  # Case-insensitive regex
```

### Follow Logs (Tail)
```bash
egg-logs --tail task-20251129-222239
```

### Clean Up Old Logs
```bash
egg-logs --cleanup --days 7         # Remove logs older than 7 days
egg-logs --cleanup --days 30 --dry-run  # Preview what would be removed
```

## Log File Structure

Each container log file contains:

```
=== Container: egg-exec-20251129-222239-12345 ===
=== Saved: 2025-11-29T22:25:00.123456 ===
=== Task ID: task-20251129-222239 ===
=== Thread TS: 1764483758.159619 ===
==================================================

=== STDOUT ===
[Container stdout output...]

=== STDERR ===
[Container stderr output...]
```

## Correlation Flow

When a Slack message triggers a container:

1. **slack-receiver** writes message to `~/.egg-sharing/incoming/task-{timestamp}.md`
2. **egg --exec** extracts task_id and thread_ts from the task file
3. Container is started with correlation environment variables and Docker labels
4. On container exit, logs are captured via `docker logs` and saved
5. Log index is updated with correlation mappings
6. Symlink is created: `task-{id}.log -> egg-exec-{container-id}.log`

## Debugging a Slack Thread

To find all logs related to a Slack thread:

```bash
# If you have the thread timestamp
egg-logs --search "1764483758.159619"

# If you have the task ID
egg-logs task-20251129-222239

# Search by content
egg-logs --search "your error message"
```

## Storage Management

Logs are stored with rotation:
- Container logs: No automatic rotation (use `egg-logs --cleanup`)
- Claude output logs: Also use manual cleanup

Recommended cleanup policy:
```bash
# Add to crontab for weekly cleanup
0 0 * * 0 /path/to/egg-logs --cleanup --days 14
```

## Security Considerations

### Sensitive Data in Logs

**IMPORTANT**: Container logs may contain sensitive information:

- **API Keys and Tokens**: Environment variables, authentication headers
- **Credentials**: Database passwords, SSH keys, OAuth secrets
- **Personal Data**: User information, email addresses, API responses
- **Internal Details**: System paths, configuration details

### Security Recommendations

1. **Storage Encryption**: Logs are stored **unencrypted** in `~/.egg-sharing/container-logs/`
   - Ensure filesystem encryption if handling sensitive data
   - Consider encrypting the entire `.egg-sharing` directory

2. **Access Control**:
   - Logs are readable by the user running egg
   - Ensure proper file permissions on the `.egg-sharing` directory
   - Avoid sharing logs without sanitization

3. **Regular Cleanup**:
   ```bash
   # Clean up old logs to minimize exposure window
   egg-logs --cleanup --days 7
   ```

4. **Shared Environments**:
   - **DO NOT** use egg on shared systems without considering log exposure
   - Implement log sanitization if logs will be shared (e.g., for debugging)
   - Consider using separate egg instances for sensitive vs. non-sensitive work

5. **Log Retention Policy**:
   - Default: Manual cleanup only
   - Recommended: Automated weekly/monthly cleanup via cron
   - For compliance: Align retention with your organization's data retention policies

### Sanitizing Logs for Sharing

Before sharing logs with others:

```bash
# Create a sanitized copy
egg-logs task-20251129-222239 > /tmp/log.txt

# Manually review and redact:
# - API keys (look for "Bearer", "token", "key")
# - Passwords (look for "password", "secret")
# - Personal data (emails, names, IDs)
```

Consider creating a sanitization script if you frequently share logs.

## Troubleshooting

### Logs Not Being Saved
1. Check if `~/.egg-sharing/container-logs/` exists and is writable
2. Verify container completed (not killed mid-execution)
3. Check egg launcher output for errors

### Can't Find Logs for a Task
1. Try searching: `egg-logs --search "task-20251129"`
2. Check the log index: `cat ~/.egg-sharing/container-logs/log-index.json`
3. List all logs: `egg-logs --list 100`

### Old Logs Taking Up Space
```bash
# Check disk usage
du -sh ~/.egg-sharing/container-logs/
du -sh ~/.egg-sharing/logs/

# Clean up
egg-logs --cleanup --days 7
```
