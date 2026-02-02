# API Reference

This document describes the gateway REST API endpoints.

## Base URL

```
http://egg-gateway:9847/api/v1
```

## Authentication

All endpoints require a session token in the Authorization header:

```
Authorization: Bearer <session_token>
```

Session tokens are created via the session endpoints.

## Endpoints

### Health

#### GET /api/v1/health

Check gateway health status.

**Request:**
```bash
curl http://egg-gateway:9847/api/v1/health
```

**Response:**
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime_seconds": 3600
}
```

### Sessions

#### POST /api/v1/session/create

Create a new session for a container.

**Request:**
```json
{
  "container_id": "abc123",
  "repos": ["owner/repo1"]
}
```

**Response:**
```json
{
  "session_token": "eyJ...",
  "expires_at": "2026-02-03T12:00:00Z"
}
```

#### POST /api/v1/session/validate

Validate a session token.

**Request:**
```json
{
  "token": "eyJ..."
}
```

**Response:**
```json
{
  "valid": true,
  "container_id": "abc123",
  "repos": ["owner/repo1"]
}
```

#### DELETE /api/v1/session

End a session and clean up resources.

**Response:**
```json
{
  "status": "deleted"
}
```

### Git Operations

#### POST /api/v1/git/status

Get git status for a repository.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo"
}
```

**Response:**
```json
{
  "branch": "egg/abc123/work",
  "clean": false,
  "staged": ["src/main.py"],
  "unstaged": ["README.md"],
  "untracked": ["new_file.txt"]
}
```

#### POST /api/v1/git/diff

Get git diff.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "staged": false
}
```

**Response:**
```json
{
  "diff": "diff --git a/README.md b/README.md..."
}
```

#### POST /api/v1/git/add

Stage files.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "files": ["src/main.py", "README.md"]
}
```

**Response:**
```json
{
  "staged": ["src/main.py", "README.md"]
}
```

#### POST /api/v1/git/commit

Create a commit.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "message": "Add new feature"
}
```

**Response:**
```json
{
  "commit": "abc123def456",
  "message": "Add new feature"
}
```

#### POST /api/v1/git/push

Push to remote. Subject to branch ownership policy.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "branch": "egg/abc123/feature"
}
```

**Response:**
```json
{
  "pushed": true,
  "branch": "egg/abc123/feature",
  "remote": "origin"
}
```

**Error Response (Policy Violation):**
```json
{
  "error": "Branch ownership violation",
  "policy": "branch_ownership",
  "branch": "main",
  "allowed_prefixes": ["egg/"]
}
```

#### POST /api/v1/git/fetch

Fetch from remote.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo"
}
```

**Response:**
```json
{
  "fetched": true
}
```

#### POST /api/v1/git/execute

Execute a generic git command. Commands are validated against an allowlist.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "command": "log",
  "args": ["--oneline", "-5"]
}
```

**Response:**
```json
{
  "output": "abc123 Latest commit\ndef456 Previous commit...",
  "exit_code": 0
}
```

### GitHub CLI Operations

#### POST /api/v1/gh/pr/create

Create a pull request.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "title": "Add new feature",
  "body": "Description of changes",
  "base": "main",
  "head": "egg/abc123/feature"
}
```

**Response:**
```json
{
  "number": 123,
  "url": "https://github.com/owner/repo/pull/123"
}
```

#### POST /api/v1/gh/pr/comment

Add a comment to a pull request.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "pr_number": 123,
  "body": "Comment text"
}
```

**Response:**
```json
{
  "comment_id": 456,
  "url": "https://github.com/owner/repo/pull/123#issuecomment-456"
}
```

#### POST /api/v1/gh/execute

Execute a generic gh command. Commands are validated.

**Request:**
```json
{
  "repo_path": "/home/sandbox/repos/my-repo",
  "command": "pr",
  "args": ["view", "123"]
}
```

**Response:**
```json
{
  "output": "Title: Add new feature\nState: OPEN...",
  "exit_code": 0
}
```

**Note:** `gh pr merge` is blocked. Humans must merge via GitHub UI.

### Configuration

#### GET /api/v1/config

Get current configuration (sanitized, no secrets).

**Response:**
```json
{
  "git": {
    "branch_prefix": "egg/",
    "protected_branches": ["main", "master"]
  },
  "repositories": {
    "allowed": ["owner/repo1", "owner/repo2"]
  }
}
```

#### GET /api/v1/config/domains

Get allowed domains (private mode).

**Response:**
```json
{
  "allowed_domains": [
    "api.anthropic.com",
    "github.com"
  ]
}
```

## Error Responses

All errors follow this format:

```json
{
  "error": "Error message",
  "code": "ERROR_CODE",
  "details": {}
}
```

### Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `UNAUTHORIZED` | 401 | Invalid or missing session token |
| `FORBIDDEN` | 403 | Policy violation |
| `NOT_FOUND` | 404 | Resource not found |
| `VALIDATION_ERROR` | 400 | Invalid request |
| `INTERNAL_ERROR` | 500 | Server error |

### Policy Violation Errors

```json
{
  "error": "Branch ownership violation",
  "code": "FORBIDDEN",
  "policy": "branch_ownership",
  "details": {
    "branch": "main",
    "allowed_prefixes": ["egg/"]
  }
}
```

## Rate Limiting

The gateway enforces rate limits per session:

| Endpoint Type | Limit |
|---------------|-------|
| Read operations | 100/minute |
| Write operations | 30/minute |
| Push operations | 10/minute |

Rate limit headers are included in responses:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1706875200
```
