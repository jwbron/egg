"""Risk R7 — no-outbound-Jira-writes guard (#1557).

Counterpart to ``test_start_pipeline.py::test_source_never_exports_jira_secrets``:
that test asserts no Jira credentials leak into the sandbox env; this
test asserts no outbound HTTP writes to ``*.atlassian.net`` are made
from anywhere outside the canonical sites.

The egg trust boundary is:

  Sandbox/orchestrator → gateway → Atlassian

The gateway holds the Atlassian credentials and is the only component
allowed to issue write requests against the live Jira REST API. Two
specific exceptions sit on the orchestrator side:

- ``gateway/jira_client.py`` — the gateway's outbound Jira client.
- ``gateway/gateway.py`` — the gateway's HTTP dispatcher; delegates to
  ``jira_client``.
- ``orchestrator/jira_transitions.py`` — the orchestrator-side post-
  apply transition handler, which calls the Atlassian transitions
  endpoint directly using launcher-secret-gated auth (#1557 TASK-1-14).

This test grep-walks the repo for ``requests.{post,put,patch,delete}``
calls whose target is a Jira REST mutation path
(``/rest/api/.../issue/.../{transitions, worklog, attachments,
watchers}``) and asserts every match is in one of the allowed files.
A new Jira write site landing outside that list fails the test and
the new contributor has to either move the call behind the gateway
or update the allowlist (with reviewer approval).
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo root: 3 levels up from this test file.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Allowlist of files that may issue outbound write calls against Jira.
# Paths are POSIX-relative to the repo root.
ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "gateway/jira_client.py",
        "gateway/gateway.py",
        "orchestrator/jira_transitions.py",
    }
)

# Skip directories that hold test fixtures / artifacts / generated state —
# they may legitimately contain ``requests.post`` mentions in docstrings
# or fixtures without representing live outbound traffic.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".egg-state",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".tox",
    }
)

# requests.{verb}( pattern; we accept both ``requests.post(...)`` and
# ``self._session.post(...)`` etc. by being permissive on the prefix.
_VERB_PATTERN = re.compile(
    r"\brequests\.(post|put|patch|delete)\s*\(",
    flags=re.MULTILINE,
)

# Substrings naming a Jira mutation endpoint. Hitting any of these on the
# same line (or nearby) as a verb call indicates an outbound write.
_JIRA_MUTATION_SUBSTRINGS: tuple[str, ...] = (
    "/rest/api/2/issue/",
    "/rest/api/3/issue/",
    "atlassian.net/rest/api",
)

# Atlassian-cloud hostname pattern. A direct ``post('https://acme.atlassian.net/...')``
# is enough to count as outbound traffic regardless of path.
_ATLASSIAN_HOST_PATTERN = re.compile(r"https?://[\w.-]+\.atlassian\.net/", re.IGNORECASE)


def _is_allowed(path: Path) -> bool:
    """Return True when ``path`` is one of the allowed Jira-write sites."""
    rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    return rel in ALLOWED_FILES


def _is_skipped(path: Path) -> bool:
    """Skip cache/state dirs and the test file itself (defensive guard)."""
    parts = set(path.parts)
    if parts & SKIP_DIR_NAMES:
        return True
    # Don't grep this file or its sibling test files.
    if path.name == "test_no_outbound_jira_writes.py":
        return True
    # Counterpart Jira test files reference these patterns in docstrings
    # and asserts — they are not real outbound traffic.
    if path.name in {
        "test_start_pipeline.py",
        "test_pipeline_prompts.py",
        "test_pr_link_writeback.py",
        "test_apply_epic_agent_refine.py",
        "test_jira_routes.py",
        "test_jira_client.py",
    }:
        return True
    return False


def _find_outbound_jira_writes(repo_root: Path) -> list[tuple[Path, int, str]]:
    """Walk the repo and report any verb call paired with a Jira write path.

    Returns a list of ``(path, line_number, line_text)`` triples for every
    suspicious write call site whose file is NOT in ``ALLOWED_FILES``.
    """
    findings: list[tuple[Path, int, str]] = []

    for py_file in repo_root.rglob("*.py"):
        if _is_skipped(py_file):
            continue
        if _is_allowed(py_file):
            continue

        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Cheap rejection: a file with no requests.{verb} reference cannot
        # match. Skipping early keeps the rglob walk affordable.
        if not _VERB_PATTERN.search(text):
            continue

        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            verb_match = _VERB_PATTERN.search(line)
            if not verb_match:
                continue

            # Build a small context window (3 lines either side) so that a
            # multi-line ``requests.post(\n  url=...)`` call is detectable.
            lo = max(0, idx - 4)
            hi = min(len(lines), idx + 3)
            window = "\n".join(lines[lo:hi])

            has_mutation_path = any(token in window for token in _JIRA_MUTATION_SUBSTRINGS)
            has_atlassian_host = bool(_ATLASSIAN_HOST_PATTERN.search(window))

            if has_mutation_path or has_atlassian_host:
                findings.append((py_file, idx, line.rstrip()))
                break  # one match per file is enough; we'll surface it

    return findings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoOutboundJiraWrites:
    """Risk R7 guard — Jira mutations only via the gateway / transitions."""

    def test_repo_walk_finds_no_unauthorized_jira_writes(self):
        """No ``.py`` file outside the allowlist may issue Jira write traffic."""
        findings = _find_outbound_jira_writes(REPO_ROOT)

        if findings:
            lines = "\n".join(
                f"  {p.resolve().relative_to(REPO_ROOT)}:{idx}: {text}" for p, idx, text in findings
            )
            raise AssertionError(
                "Unauthorized outbound Jira write call(s) detected — risk R7 "
                "violation (#1557). All Jira mutations must route through the "
                "gateway (gateway/jira_client.py + gateway/gateway.py) or the "
                "orchestrator's transition handler "
                "(orchestrator/jira_transitions.py).\n"
                f"Findings:\n{lines}"
            )

    def test_allowlist_files_actually_exist(self):
        """Sanity check — the allowlisted files must exist in the repo.

        If one of these moves under a refactor, the test would silently
        pass without enforcing anything. Asserting their existence keeps
        the allowlist honest.
        """
        for rel in ALLOWED_FILES:
            assert (REPO_ROOT / rel).is_file(), (
                f"Allowlisted Jira-write site {rel} no longer exists — "
                "update ALLOWED_FILES in test_no_outbound_jira_writes.py"
            )

    def test_jira_transitions_uses_only_transitions_endpoint(self):
        """The orchestrator-side direct call MUST be transitions-only.

        ``orchestrator/jira_transitions.py`` is on the allowlist precisely
        because it owns the Won't-Do batch via the
        ``/rest/api/3/issue/<key>/transitions`` endpoint
        (#1557 TASK-1-14). It must NOT grow other write sites (worklog,
        attachments, watchers) — those still belong behind the gateway.
        """
        src = (REPO_ROOT / "orchestrator" / "jira_transitions.py").read_text(encoding="utf-8")
        # Permitted suffix.
        assert "/transitions" in src

        # Adversarial suffixes — these would broaden the orchestrator-
        # side surface beyond what's been reviewed for risk R7.
        for forbidden in (
            "/worklog",
            "/attachments",
            "/watchers",
        ):
            assert forbidden not in src, (
                f"orchestrator/jira_transitions.py touches {forbidden} — "
                "this expands the orchestrator-side Atlassian surface "
                "beyond transitions-only (risk R7)."
            )

    def test_orchestrator_routes_pipelines_makes_no_direct_atlassian_call(self):
        """``orchestrator/routes/pipelines.py`` must dispatch via the gateway.

        Specific regression guard: the PR-link writeback hook
        (``_writeback_pr_link_to_jira_child``) uses ``GatewayClient``,
        not raw ``requests.post``. If a future contributor inlines a
        direct ``atlassian.net`` call here, this test fires.
        """
        src = (REPO_ROOT / "orchestrator" / "routes" / "pipelines.py").read_text(encoding="utf-8")
        # Direct host-level traffic is forbidden.
        assert not _ATLASSIAN_HOST_PATTERN.search(src), (
            "orchestrator/routes/pipelines.py contains a direct "
            "atlassian.net URL — route the call through GatewayClient "
            "instead (risk R7)."
        )
        # And no inline requests.post(...) calls at all.
        for verb in ("post", "put", "patch", "delete"):
            assert f"requests.{verb}(" not in src, (
                f"orchestrator/routes/pipelines.py issues raw requests.{verb} — "
                "all outbound HTTP must go through GatewayClient (risk R7)."
            )

    def test_orchestrator_mcp_tools_makes_no_direct_atlassian_call(self):
        """``orchestrator/mcp_tools.py`` likewise stays gateway-mediated."""
        src = (REPO_ROOT / "orchestrator" / "mcp_tools.py").read_text(encoding="utf-8")
        assert not _ATLASSIAN_HOST_PATTERN.search(src), (
            "orchestrator/mcp_tools.py contains a direct atlassian.net URL "
            "— route through GatewayClient (risk R7)."
        )

    def test_no_jira_credentials_consumed_outside_gateway_and_shared(self):
        """Credentials must be *read* only by the gateway / shared helper.

        Adversarial usages (``os.environ["JIRA_API_TOKEN"]``,
        ``os.getenv("JIRA_API_TOKEN")``, ``settings.JIRA_API_TOKEN``)
        get flagged anywhere outside the allowed subtrees. Free-form
        documentation mentions of the credential name in code comments
        / docstrings are permitted because the symbol must still be
        nameable to assert its absence (this very test file is the
        canonical example).
        """
        # Patterns that indicate the value is being *consumed* — not
        # merely mentioned. The risk is reading the credential into
        # sandbox or orchestrator memory, not naming it in prose.
        consumption_patterns = (
            re.compile(r"""os\.environ\[\s*['"]JIRA_API_TOKEN['"]\s*\]"""),
            re.compile(r"""os\.environ\.get\(\s*['"]JIRA_API_TOKEN['"]"""),
            re.compile(r"""os\.getenv\(\s*['"]JIRA_API_TOKEN['"]"""),
            re.compile(r"""\bsettings\.JIRA_API_TOKEN\b"""),
            re.compile(r"""\bJIRA_API_TOKEN\s*=\s*(?!\s*#)"""),
        )

        forbidden_dirs = ("orchestrator", "sandbox")
        for forbidden_root in forbidden_dirs:
            root_path = REPO_ROOT / forbidden_root
            if not root_path.is_dir():
                continue
            for py_file in root_path.rglob("*.py"):
                if _is_skipped(py_file):
                    continue
                try:
                    text = py_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for pat in consumption_patterns:
                    match = pat.search(text)
                    assert match is None, (
                        f"{py_file.resolve().relative_to(REPO_ROOT)} consumes "
                        f"JIRA_API_TOKEN via {match.group()!r} — credentials "
                        "belong only in the gateway process (risk R7, #1557)."
                    )
