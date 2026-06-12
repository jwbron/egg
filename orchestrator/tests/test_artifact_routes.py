"""Pins the slice-4 gateway artifact-read endpoint contract on the orchestrator side
(#3077 TASK-4-4 / task-4-1).

Slice-4 of #3077 adds a *strict, spec-resolving* orchestrator route at
``orchestrator/routes/artifacts.py`` that the gateway blueprint forwards to (see
``gateway/artifact_api.py`` and ``gateway/tests/test_artifact_api.py``).  The
endpoint never accepts a repo path — it resolves a spec-registered ``name`` via
:func:`egg_contracts.artifact_spec.resolve_artifact_path` and serves the
content of ``git show <ref>:<path>`` from the orchestrator's authoritative
repo, with an output cap and a ``truncated`` flag.

These tests pin the rejection branches and the byte-equality / cap contract
that the slice-4 HITL Q2 ("strict") decision turns into a wire-level
guarantee.  The tests exercise the Flask blueprint with mocked seams
(``routes.get_state_store_for_pipeline``,
``contract_store.resolve_pipeline_worktree``,
``routes.artifacts.subprocess.run``) so they are independent of the
orchestrator's runtime repo layout and runnable inside the container sandbox
where ``git init`` is blocked.

Conventions verified against the slice-4 coder's implementation:

* The blueprint URL prefix mirrors ``contracts.py`` — ``/api/v1/artifacts`` —
  with a single ``POST /api/v1/artifacts/get`` action endpoint.
* The request body carries ``name`` + ``ref`` + ``pipeline_id``; the
  orchestrator looks up ``issue_number`` from the pipeline state and runs
  the spec's ``path_template`` through
  :func:`routes.pipelines._pipeline_identifier`.
* On success the response is ``{"success": True, "message": "Artifact
  retrieved", "data": {...}}`` with a ``truncated`` boolean and the resolved
  ``path`` returned alongside ``content``.  The sandbox helper
  ``egg-artifact`` and the gateway forwarder both rely on this envelope.
* ``subprocess.run`` is invoked with ``capture_output=True`` so ``stdout``
  is *bytes*; the route decodes with UTF-8 replacement so a non-UTF-8 blob
  never 500s.  Tests pass bytes accordingly.

The cap-boundary test pinches ``_ARTIFACT_MAX_BYTES`` down via
``monkeypatch`` so we don't need to materialise a ~256 KB response just to
exercise the cap branch — the test only cares about the *boundary
semantics*, not the production byte value.
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Resolve orchestrator + shared imports the same way the rest of the test
# suite does (``conftest.py`` already extends ``sys.path``, but keep this
# block so the file is also runnable as a script for local debugging).
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pipeline():
    """Minimal pipeline state the artifact route loads to resolve identifiers.

    Mirrors the shape ``handle_consensus_propose_signal`` sees in
    ``test_signals.py``: a ``Pipeline`` with ``issue_number`` and ``branch``
    so the spec's ``path_template`` renders against the bare ``issue-<N>``
    identifier (the common case for production-style pipeline IDs).
    """
    from models import Pipeline

    return Pipeline(
        id="issue-3077",
        issue_number=3077,
        repo="owner/repo",
        branch="egg/issue-3077",
    )


@pytest.fixture
def app():
    """Build a Flask app with only the artifacts blueprint registered.

    Avoids loading the rest of ``orchestrator.api`` so the test stays focused
    on the artifact route surface; matches the per-blueprint isolation used
    by ``test_signals.py`` and ``test_anchors_routes.py``.
    """
    from flask import Flask
    from routes.artifacts import artifacts_bp

    app = Flask(__name__)
    app.register_blueprint(artifacts_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_subprocess_result(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> MagicMock:
    """``subprocess.CompletedProcess`` stand-in matching the route's call shape.

    The route invokes ``subprocess.run(..., capture_output=True)`` (no
    ``text=True``), so ``stdout`` and ``stderr`` are *bytes*.  Tests must
    match the byte shape — passing strings hides a real-world divergence
    between the test and production decode paths.
    """
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _post(client, body: dict, **kwargs) -> tuple[int, dict]:
    """``client.post`` JSON helper returning ``(status_code, payload)``."""
    response = client.post(
        "/api/v1/artifacts/get",
        data=json.dumps(body),
        content_type="application/json",
        **kwargs,
    )
    return response.status_code, response.get_json() or {}


class _ArtifactRouteSeams:
    """Composite patcher for the three lazy-imported seams in ``routes.artifacts``.

    The route imports ``get_state_store_for_pipeline`` from the ``routes``
    package (its package ``__init__``), and ``resolve_pipeline_worktree``
    from ``contract_store``.  ``subprocess.run`` is patched at the route
    module attribute (the only direct module-level import the route has).
    Combining the three into one context manager keeps the test bodies
    readable and matches the lazy-import contract the route relies on.
    """

    def __init__(
        self,
        *,
        pipeline,
        worktree: Path = Path("/tmp/orch-worktree"),
        subprocess_result: MagicMock | None = None,
        get_state_store_side_effect: Exception | None = None,
        worktree_override: Path | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.worktree = worktree
        self.subprocess_result = subprocess_result or _make_subprocess_result(stdout=b"ok")
        self.get_state_store_side_effect = get_state_store_side_effect
        self.worktree_override = worktree_override
        self.subprocess_mock: MagicMock | None = None
        self.worktree_mock: MagicMock | None = None

    def __enter__(self) -> _ArtifactRouteSeams:
        # Imported here so the ``patch.object`` target exists in the test
        # process even when the route hasn't yet been touched in the
        # current test.
        import contract_store
        import routes
        import routes.artifacts as artifacts_mod

        self._stack = ExitStack()

        store_mock = MagicMock()
        if self.get_state_store_side_effect is not None:
            store_patch = patch.object(
                routes,
                "get_state_store_for_pipeline",
                side_effect=self.get_state_store_side_effect,
            )
        else:
            store_patch = patch.object(
                routes,
                "get_state_store_for_pipeline",
                return_value=(store_mock, self.pipeline),
            )
        self._stack.enter_context(store_patch)

        self.worktree_mock = self._stack.enter_context(
            patch.object(
                contract_store,
                "resolve_pipeline_worktree",
                return_value=self.worktree_override
                if self.worktree_override is not None
                else self.worktree,
            )
        )

        self.subprocess_mock = self._stack.enter_context(
            patch.object(
                artifacts_mod.subprocess,
                "run",
                return_value=self.subprocess_result,
            )
        )
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()


# ---------------------------------------------------------------------------
# Happy path — byte-equality and envelope shape
# ---------------------------------------------------------------------------


class TestArtifactGetHappyPath:
    """Pins the wire-level shape of a successful spec-resolved read."""

    def test_happy_path_byte_equality(self, client, mock_pipeline):
        """Registered name + valid ref returns the committed content byte-identical.

        The route resolves ``plan-draft`` to ``.egg-state/drafts/3077-plan.md``
        via :func:`egg_contracts.artifact_spec.resolve_artifact_path`, runs
        ``git show <ref>:<path>`` and surfaces the decoded blob unchanged in
        the ``content`` field.  No transformation, no extra whitespace
        stripping — byte-equality is the contract.
        """
        committed_blob = "# Plan for #3077\n\nBody with intentional trailing\nblank line.\n\n"
        ref = "a" * 40
        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            subprocess_result=_make_subprocess_result(stdout=committed_blob.encode()),
        ) as seams:
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": ref, "pipeline_id": "issue-3077"},
            )

        assert status == 200, payload
        assert payload.get("success") is True
        data = payload.get("data") or {}
        assert data["content"] == committed_blob, (
            "content must be byte-identical to git show stdout"
        )
        # Echo the resolved path + name so the gateway and sandbox helper can
        # surface them without re-running the spec.
        assert data.get("name") == "plan-draft"
        assert data.get("path") == ".egg-state/drafts/3077-plan.md"
        assert data.get("ref") == ref
        # ``truncated`` MUST be present on every success — consumers must not
        # have to guess whether the field is missing because no cap applied
        # or because the field was forgotten.
        assert data.get("truncated") is False

        # Sanity check the git show shape so reviewers can pin the
        # authoritative-repo invariant: the route runs against a worktree
        # the orchestrator owns (not a per-agent worktree).
        cmd = seams.subprocess_mock.call_args.args[0]
        assert cmd[0:2] == ["git", "-C"]
        assert cmd[2] == str(seams.worktree)
        assert cmd[3] == "show"
        assert cmd[4] == f"{ref}:.egg-state/drafts/3077-plan.md"

    def test_qualified_pipeline_id_uses_string_identifier(self, client):
        """A re-run pipeline_id (``issue-3077-replan``) resolves to the qualified path.

        ``_pipeline_identifier`` returns the bare issue number for
        ``pipeline_id == "issue-3077"`` and the full pipeline id for
        ``pipeline_id == "issue-3077-replan"``.  Pinning the qualified shape
        prevents accidental collisions between concurrent runs on the same
        issue — the same risk slice-2 ``_pipeline_identifier`` mitigates for
        ``_get_draft_path``.
        """
        from models import Pipeline

        pipeline = Pipeline(
            id="issue-3077-replan",
            issue_number=3077,
            repo="owner/repo",
            branch="egg/issue-3077",
        )
        ref = "b" * 40
        with _ArtifactRouteSeams(
            pipeline=pipeline,
            subprocess_result=_make_subprocess_result(stdout=b"re-run plan\n"),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": ref, "pipeline_id": "issue-3077-replan"},
            )

        assert status == 200, payload
        data = payload["data"]
        assert data["path"] == ".egg-state/drafts/issue-3077-replan-plan.md"

    def test_non_utf8_blob_does_not_500(self, client, mock_pipeline):
        """A non-UTF-8 byte sequence in git show output decodes with replacement.

        Documents the route's defense against a binary-ish artifact (a YAML
        that smuggled a Latin-1 byte, or a Markdown with a stray 0x80) —
        the decoder uses ``errors='replace'`` so the response is always
        valid UTF-8 JSON and a non-UTF-8 commit never 500s.
        """
        non_utf8 = b"head\n\xff\xfe\nrest\n"
        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            subprocess_result=_make_subprocess_result(stdout=non_utf8),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": "a" * 40, "pipeline_id": "issue-3077"},
            )

        assert status == 200, payload
        # The content round-trips through UTF-8-replace so the unrepresentable
        # bytes survive as U+FFFD ('�').  The bytes around them stay
        # intact, so the test asserts the structural shape rather than
        # pinning the exact replacement character placement.
        content = payload["data"]["content"]
        assert content.startswith("head\n")
        assert content.endswith("\nrest\n")
        assert payload["data"]["truncated"] is False


# ---------------------------------------------------------------------------
# Strict-resolution rejections (HITL Q2)
# ---------------------------------------------------------------------------


class TestArtifactGetRejections:
    """Strict-mode rejection branches that the gateway propagates verbatim."""

    def test_unregistered_name_400_lists_registered_names(self, client, mock_pipeline):
        """Unknown artifact name -> 400 listing every registered alternative.

        The error MUST enumerate ``spec_by_name``'s registered names so an
        agent CLI (``egg-artifact``) can surface a usable hint instead of a
        bare "unknown name" string.  This is the slice-4 HITL Q2 commitment
        that there is *no* path escape hatch: the only way to read an
        artifact is by its registered name.
        """
        with _ArtifactRouteSeams(pipeline=mock_pipeline):
            status, payload = _post(
                client,
                {
                    "name": "definitely-not-registered",
                    "ref": "c" * 40,
                    "pipeline_id": "issue-3077",
                },
            )

        assert status == 400, payload
        # The error must name the offending value (so the caller sees what
        # they sent) and at least one registered name (so the caller can
        # self-correct without a docs round-trip).
        body_text = json.dumps(payload).lower()
        assert "definitely-not-registered" in body_text
        # All five spec-registered names must appear so a hand-rolled curl
        # caller gets a discoverable error.
        registered = {
            "analysis-draft",
            "plan-draft",
            "architect-output",
            "architect-slices",
            "risk-analyst-output",
        }
        missing = {n for n in registered if n not in body_text}
        assert not missing, (
            f"unregistered-name 400 must list every registered name; missing={sorted(missing)}"
        )

    @pytest.mark.parametrize(
        "bad_ref",
        [
            "main",  # branch name
            "HEAD",  # symbolic ref
            "deadbeef!",  # invalid hex
            "abc1234; rm -rf /",  # shell metachar attack
            "../../../etc/passwd",  # path traversal style
            "",  # empty
            "g" * 40,  # 40 chars but not hex
            "ab",  # too short (regex requires 7-40)
        ],
    )
    def test_non_hex_ref_400(self, client, mock_pipeline, bad_ref):
        """Any non-hex / non-commit-sha ``ref`` -> 400 before ``git show`` runs.

        The hex validation is the cheap pre-flight that lets the rest of the
        route assume the ref cannot inject shell metacharacters or invoke
        symbolic resolution against the authoritative repo.  Each case here
        would slip past a "just call git show with the input" implementation
        — that's the failure mode this branch exists to catch.
        """
        with _ArtifactRouteSeams(pipeline=mock_pipeline) as seams:
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": bad_ref, "pipeline_id": "issue-3077"},
            )

            assert status == 400, (bad_ref, payload)
            # Hex validation MUST run before subprocess invocation — a
            # malformed ref must never reach ``git show`` as a string.
            assert not seams.subprocess_mock.called, (
                f"git show must not run for malformed ref {bad_ref!r}"
            )

    def test_absent_at_ref_returns_structured_4xx_not_500(self, client, mock_pipeline):
        """``git show`` non-zero -> structured 4xx (never a 500 / never an empty body).

        A reviewer calling ``egg-artifact get architect-output --ref <sha>``
        for a slice where the architect never ran must see a structured 4xx
        ("path absent at this ref") instead of a 500 / opaque "internal
        error" / orchestrator stack trace.  The error envelope must include
        a ``success: false`` boolean so the gateway forwarder and sandbox
        helper can branch on it.
        """
        absent_stderr = b"fatal: Path '.egg-state/drafts/3077-plan.md' does not exist in 'abcdef'\n"
        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            subprocess_result=_make_subprocess_result(
                returncode=128,
                stderr=absent_stderr,
            ),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": "d" * 40, "pipeline_id": "issue-3077"},
            )

        # 404 is the natural choice (resource not found at ref).  Accept the
        # full structured-4xx band so the coder can pick 404 vs 422 without
        # blocking the contract — what matters is "no 500" and "structured".
        assert 400 <= status < 500, payload
        assert payload.get("success") is False
        assert payload.get("message"), "absent-at-ref response must include a 'message'"

    def test_unresolvable_ref_returns_4xx(self, client, mock_pipeline):
        """``git show`` 'invalid object name' -> structured 4xx.

        Git's stderr for an unresolvable commit ('invalid object name' /
        'unknown revision' / 'bad object') is structurally different from
        path-absent-at-ref: the ref itself does not resolve, regardless of
        path.  The coder's choice of 4xx code (422 vs 404) is open; the
        contract is "structured 4xx, not 500".
        """
        unresolvable_stderr = b"fatal: invalid object name 'abcdef'.\n"
        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            subprocess_result=_make_subprocess_result(
                returncode=128,
                stderr=unresolvable_stderr,
            ),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": "abcdef0", "pipeline_id": "issue-3077"},
            )

        assert 400 <= status < 500, payload
        assert payload.get("success") is False

    def test_subprocess_timeout_returns_503_not_500(self, client, mock_pipeline):
        """``git show`` timeout -> 503 (transient, retryable) — never a 500.

        A wedged git invocation must not bubble up as a 500 (which the
        gateway 502-wraps as "orchestrator unreachable" — wrong category).
        503 tells the sandbox CLI to retry.  Pinning this guards the
        "no 500" promise from the absent-at-ref test against the second
        infrastructure-failure path.
        """
        import subprocess as subprocess_mod

        import routes.artifacts as artifacts_mod

        with _ArtifactRouteSeams(pipeline=mock_pipeline):
            # Override the subprocess mock to raise instead of returning.
            with patch.object(
                artifacts_mod.subprocess,
                "run",
                side_effect=subprocess_mod.TimeoutExpired(cmd=["git"], timeout=15),
            ):
                status, payload = _post(
                    client,
                    {"name": "plan-draft", "ref": "a" * 40, "pipeline_id": "issue-3077"},
                )

        assert status == 503, payload
        assert payload.get("success") is False

    def test_path_field_is_rejected_400(self, client, mock_pipeline):
        """Strict per HITL Q2: a ``path`` field in the body is rejected at the wire.

        The orchestrator MUST refuse a body that carries ``path`` even when
        the field would otherwise be harmless — defense in depth against a
        future bug that lets the gateway forward an extra field.  The
        rejection must (a) be 400, (b) name the forbidden field, (c) never
        reach ``git show`` with the attacker-supplied value.
        """
        with _ArtifactRouteSeams(pipeline=mock_pipeline) as seams:
            status, payload = _post(
                client,
                {
                    "name": "plan-draft",
                    "ref": "e" * 40,
                    "pipeline_id": "issue-3077",
                    "path": "/etc/passwd",
                },
            )

            assert status == 400, payload
            assert "path" in (payload.get("message") or "").lower()
            # subprocess must never run on a path-rejected body.
            assert not seams.subprocess_mock.called, (
                "request body 'path' must not trigger a git show"
            )

    @pytest.mark.parametrize(
        "bad_identifier",
        [
            "../../../etc/passwd",  # traversal style
            "..",  # bare parent ref
            "issue/../3077",  # embedded traversal
            "a/b",  # path separator
            "issue 3077",  # whitespace
            "issue;rm",  # shell metachar
            "",  # empty string
            ".hidden",  # leading dot (no alnum start)
            "issue-3077\n",  # trailing newline (fullmatch, not $-anchored match)
            "3077\n",  # all-digit with trailing newline
        ],
    )
    def test_unsafe_explicit_identifier_400(self, client, mock_pipeline, bad_identifier):
        """An explicit ``identifier`` outside the safe slug shape -> 400.

        The identifier is interpolated into the spec path template, so a
        defense-in-depth shape check rejects ``..``, ``/``, and other
        unexpected characters at the wire — the strict no-path guarantee
        should not have to lean on ``git show``'s pathspec semantics.  The
        rejection must happen before any subprocess runs.
        """
        with _ArtifactRouteSeams(pipeline=mock_pipeline) as seams:
            status, payload = _post(
                client,
                {
                    "name": "plan-draft",
                    "ref": "e" * 40,
                    "pipeline_id": "issue-3077",
                    "identifier": bad_identifier,
                },
            )

            assert status == 400, payload
            assert "identifier" in (payload.get("message") or "").lower()
            assert not seams.subprocess_mock.called, (
                "an unsafe 'identifier' must not trigger a git show"
            )


# ---------------------------------------------------------------------------
# Cap + truncated flag at the boundary
# ---------------------------------------------------------------------------


class TestArtifactGetCap:
    """Pins the output cap boundary semantics — the contract the sandbox CLI prints."""

    def test_content_at_cap_is_intact_truncated_false(
        self,
        client,
        mock_pipeline,
        monkeypatch,
    ):
        """Content exactly at the cap returns intact with ``truncated: false``.

        The boundary is a deliberate equality, not a strict inequality:
        consumers that see ``truncated: false`` must be able to trust that
        the bytes returned are the complete blob.  Pinching the cap via
        monkeypatch keeps the test fast — it exercises the *boundary
        semantics* without forcing the test to materialise hundreds of KB.
        """
        import routes.artifacts as artifacts_mod

        # The coder named the cap ``_ARTIFACT_MAX_BYTES`` — this monkeypatch
        # asserts the name (``raising=True``) so a future rename ratchets
        # both the test and any docs that reference the constant.
        monkeypatch.setattr(artifacts_mod, "_ARTIFACT_MAX_BYTES", 16, raising=True)

        exactly_at_cap = b"x" * 16
        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            subprocess_result=_make_subprocess_result(stdout=exactly_at_cap),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": "f" * 40, "pipeline_id": "issue-3077"},
            )

        assert status == 200, payload
        data = payload["data"]
        assert data["content"] == "x" * 16
        assert data["truncated"] is False

    def test_content_over_cap_is_truncated_flag_true(
        self,
        client,
        mock_pipeline,
        monkeypatch,
    ):
        """Content over the cap returns first-N bytes with ``truncated: true``.

        The contract: ``content`` length is exactly the cap (no off-by-one
        either side), ``truncated`` is ``true``, status stays 200.  The
        sandbox helper relies on this to print a "...(truncated, see
        artifact at ref ...)" hint without a second probe call.
        """
        import routes.artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "_ARTIFACT_MAX_BYTES", 16, raising=True)

        oversized = b"y" * 64
        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            subprocess_result=_make_subprocess_result(stdout=oversized),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": "a" * 40, "pipeline_id": "issue-3077"},
            )

        assert status == 200, payload
        data = payload["data"]
        assert data["truncated"] is True
        # Cap is bytes; the ASCII payload makes byte length == char length.
        assert len(data["content"]) == 16, (
            f"capped content length must equal _ARTIFACT_MAX_BYTES, got {len(data['content'])}"
        )
        assert data["content"] == "y" * 16


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestArtifactGetSchema:
    """Body-level schema rejections that don't depend on git or pipeline state."""

    def test_missing_body_400(self, client):
        """No JSON body -> 400 (mirrors ``contracts.mutate_contract``)."""
        response = client.post(
            "/api/v1/artifacts/get",
            data="",
            content_type="application/json",
        )
        assert response.status_code in (400, 415)

    @pytest.mark.parametrize("missing_field", ["name", "ref", "pipeline_id"])
    def test_missing_required_field_400(self, client, missing_field):
        """Each required body field is individually required — none can default silently."""
        body = {
            "name": "plan-draft",
            "ref": "a" * 40,
            "pipeline_id": "issue-3077",
        }
        body.pop(missing_field)
        status, payload = _post(client, body)
        assert status == 400, (missing_field, payload)
        # The error must name the missing field so a hand-rolled caller can
        # self-correct without diffing against a sample request.
        message = (payload.get("message") or "").lower()
        assert missing_field in message, (
            f"missing-field error must name '{missing_field}'; got {payload!r}"
        )

    def test_unknown_pipeline_returns_4xx(self, client, mock_pipeline):
        """Unknown pipeline_id -> structured 4xx (not 500 / not 200 with empty body)."""
        from state_store import PipelineNotFoundError

        with _ArtifactRouteSeams(
            pipeline=mock_pipeline,
            get_state_store_side_effect=PipelineNotFoundError("issue-9999"),
        ):
            status, payload = _post(
                client,
                {"name": "plan-draft", "ref": "a" * 40, "pipeline_id": "issue-9999"},
            )

        assert 400 <= status < 500, payload
        assert payload.get("success") is False
