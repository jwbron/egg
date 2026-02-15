# Non-Interactive Mode & Notifications
# Audience: GHA execution only — agents running in CI/GitHub Actions

## Notifications

Slack: `from notifications import slack_notify; slack_notify("Topic", "Details")`
File-based: `cat > ~/sharing/notifications/$(date +%Y%m%d-%H%M%S)-topic.md`
