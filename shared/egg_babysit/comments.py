"""Status comment lifecycle management for babysit-pr.

Manages PR status comments with HTML markers for identification,
minimizes stale comments as OUTDATED, and prevents duplicate comments
on the same commit.
"""

import json
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# HTML marker used to identify egg status comments.
STATUS_COMMENT_MARKER = "<!-- egg-status-comment -->"

# GraphQL mutation to minimize a comment as OUTDATED.
_MINIMIZE_COMMENT_MUTATION = """
mutation MinimizeComment($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {
    minimizedComment {
      isMinimized
    }
  }
}
"""


@dataclass
class StatusComment:
    """A PR status comment with metadata."""

    id: str  # GraphQL node ID
    body: str
    author_login: str
    created_at: str
    is_minimized: bool = False


def post_status_comment(
    pr_number: int,
    repo: str,
    body: str,
    head_sha: str = "",
    minimize_previous: bool = True,
) -> bool:
    """Post a status comment on a PR with egg markers.

    Optionally minimizes previous status comments before posting the new one.
    Includes commit SHA in the marker for deduplication.

    Args:
        pr_number: PR number.
        repo: Repository in owner/repo format.
        body: Comment body (markdown).
        head_sha: Current HEAD SHA for deduplication.
        minimize_previous: Whether to minimize prior status comments.

    Returns:
        True if the comment was posted successfully.
    """
    # Check for existing comment on same commit (deduplication)
    if head_sha:
        existing = find_status_comments(pr_number, repo)
        for comment in existing:
            if not comment.is_minimized and f"<!-- sha:{head_sha[:12]} -->" in comment.body:
                logger.info(
                    "Status comment already exists for SHA %s on PR #%d, skipping",
                    head_sha[:12],
                    pr_number,
                )
                return True

    # Minimize previous status comments
    if minimize_previous:
        _minimize_previous_comments(pr_number, repo)

    # Build the comment body with markers
    sha_marker = f"<!-- sha:{head_sha[:12]} -->" if head_sha else ""
    marked_body = f"{STATUS_COMMENT_MARKER}\n{sha_marker}\n{body}"

    try:
        subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", marked_body],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        logger.info("Posted status comment on PR #%d", pr_number)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("Failed to post status comment on PR #%d: %s", pr_number, exc)
        return False


def find_status_comments(pr_number: int, repo: str) -> list[StatusComment]:
    """Find all egg status comments on a PR.

    Args:
        pr_number: PR number.
        repo: Repository in owner/repo format.

    Returns:
        List of StatusComment objects with egg markers.
    """
    try:
        raw = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--jq",
                "[.[] | {id: .node_id, body: .body, author_login: .user.login,"
                " created_at: .created_at}]",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        comments_data = json.loads(raw.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch comments for PR #%d: %s", pr_number, exc)
        return []

    status_comments = []
    for c in comments_data:
        body = c.get("body", "")
        if STATUS_COMMENT_MARKER in body:
            status_comments.append(
                StatusComment(
                    id=c.get("id", ""),
                    body=body,
                    author_login=c.get("author_login", ""),
                    created_at=c.get("created_at", ""),
                )
            )

    return status_comments


def _minimize_previous_comments(pr_number: int, repo: str) -> None:
    """Minimize all previous egg status comments on a PR.

    Uses the GitHub GraphQL API to mark comments as OUTDATED.
    """
    comments = find_status_comments(pr_number, repo)

    for comment in comments:
        if comment.is_minimized:
            continue

        try:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={_MINIMIZE_COMMENT_MUTATION}",
                    "-f",
                    f"id={comment.id}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            logger.debug("Minimized status comment %s on PR #%d", comment.id, pr_number)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("Failed to minimize comment %s: %s", comment.id, exc)


__all__ = [
    "StatusComment",
    "find_status_comments",
    "post_status_comment",
    "STATUS_COMMENT_MARKER",
]
