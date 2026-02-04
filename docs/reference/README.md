# Reference Documentation

Quick reference guides for egg.

## Available References

### [Beads Task Tracking](beads.md)
Persistent task memory system for autonomous agents.

**Essential for:**
- Tracking work across container restarts
- Finding existing tasks before creating new ones
- Recording decisions and progress
- Linking related work (Slack threads, PRs, JIRA)

**Quick start:** Always run `bd --allow-stale list --status in_progress` before starting work.

### [Slack Quick Reference](slack-quick-reference.md)
Common Slack operations and commands.

**Quick answers for:**
- Sending test notifications
- Checking service status
- Troubleshooting connection issues
- Managing channels and tokens

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
3. View logs: `docker logs jib-claude -f`

## See Also
- [Setup Guides](../setup/)
- [Architecture](../architecture/)
