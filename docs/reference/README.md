# Reference Documentation

Quick reference guides for egg.

## Available References

### [Log Persistence](log-persistence.md)
Container log persistence and correlation.

## Common Issues

**Slack not receiving notifications:**
1. Check service: `systemctl --user status slack-notifier`
2. Verify token: Check logs for authentication errors
3. Test manually: Write file to `~/.egg-sharing/notifications/`

**Claude Code not available:**
1. Authenticate: `claude auth login`
2. Check version: `claude --version`
3. Verify PATH: `which claude`

**Container issues:**
1. Rebuild: `bin/egg --rebuild`
2. Check Docker: `docker ps`
3. View logs: `docker logs egg-claude -f`

## See Also
- [Setup Guides](../setup/)
- [Architecture](../architecture/)
