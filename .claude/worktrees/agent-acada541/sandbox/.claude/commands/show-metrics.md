Generate a monitoring report showing recent agent activity and context usage.

Gather the following information and present it in a clean, readable format:

1. **Git activity** (last 7 days):
   ```bash
   git -C ~/repos/egg log --oneline --since="7 days ago" --all
   ```

2. **Context source usage**: Check which Confluence spaces and JIRA projects exist:
   ```bash
   ls ~/context-sync/confluence/ ~/context-sync/jira/ 2>/dev/null
   ```

3. **Sharing activity**: Recent notifications and context saves:
   ```bash
   ls -lt ~/sharing/notifications/ ~/sharing/context/ 2>/dev/null | head -20
   ```

Present the report with interpretation:
- Highlight any concerning patterns
- Suggest optimizations if needed
- Note which context sources are most valuable

Example output format:

```
# Egg Activity Report (Last 7 Days)

## Git Activity
- 12 commits across 3 branches
- 4 PRs created

## Context Sources
Available:
- confluence/ENG
- jira/WEBAPP

## Recent Notifications
- 3 notifications sent
- Last: 2026-02-04 task-update.md
```
