Generate a monitoring report showing recent agent activity and context usage.

Use the jib monitoring infrastructure to show:
1. API usage metrics (last 7 days)
2. Task completion statistics
3. Context source usage (which Confluence spaces, JIRA projects accessed)
4. Top context sources

Steps:
1. Run the monitoring report generator:
   ```bash
   python3 ~/repos/egg/lib/python/jib_monitor.py --days 7
   ```

2. Present the report in a clean, readable format

3. Add interpretation:
   - Highlight any concerning patterns (high API usage, low completion rate)
   - Suggest optimizations if needed
   - Note which context sources are most valuable

Example output format:

```
# jib Activity Report (Last 7 Days)

## Summary
- 45 API calls, 12.3 MB total prompts
- 15 tasks completed (93% success rate)
- Average task duration: 32 seconds

## Context Usage
Top sources:
1. confluence/ENG - 25 accesses
2. jira/WEBAPP - 18 accesses
3. confluence/INFRA - 12 accesses

## Insights
✅ High completion rate indicates good task clarity
📊 Context from ENG confluence most valuable
💡 Consider expanding INFRA documentation access
```

This helps you understand what the agent is doing and what context is most useful.
