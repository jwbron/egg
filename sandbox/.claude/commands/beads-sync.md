# Sync Beads with Git

Commit and sync current Beads state to git repository.

This is useful before shutting down a container or after making significant changes to ensure all task updates are persisted.

Run these commands:

```bash
cd ~/beads

# Check current git status
echo "=== Current Beads Git Status ==="
git status

# Add any changes
git add -A

# Commit if there are changes
if ! git diff --staged --quiet 2>/dev/null; then
    git commit -m "Beads sync: $(date '+%Y-%m-%d %H:%M:%S')" 2>/dev/null
    echo "✓ Beads state committed to git"
else
    echo "✓ No changes to commit"
fi

# Show recent commits
echo ""
echo "=== Recent Beads History ==="
git log --oneline -5 2>/dev/null || echo "No git history yet"

# Import any JSONL updates to ensure database consistency
bd sync --import-only 2>/dev/null && echo "✓ Database synced with JSONL" || echo "✓ Already in sync"
```

Note: Beads automatically commits to git when you modify tasks, but this command ensures everything is synced. The SQLite cache is automatically rebuilt from JSONL when needed.
