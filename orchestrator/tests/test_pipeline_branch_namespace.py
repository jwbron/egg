"""Regression tests for #2399 — pipeline-branch / slice-branch ref-namespace.

The orchestrator pushes the pipeline tip to ``<branch>/work`` so the
``<branch>/`` namespace can hold slice integration branches as siblings
(``<branch>/slice-N``) without git's ``directory file conflict``
rejection. A leaf ref at ``<branch>`` and a child at
``<branch>/slice-N`` cannot coexist on origin.

These tests pin the contract so a future refactor can't quietly
re-introduce the leaf-ref shape.
"""

from routes.pipelines import _ensure_pipeline_work_ref, _slice_namespace_root


class TestEnsurePipelineWorkRef:
    def test_appends_work_to_egg_branch(self) -> None:
        assert _ensure_pipeline_work_ref("egg/issue-2261-v6") == "egg/issue-2261-v6/work"

    def test_appends_work_to_qualified_egg_branch(self) -> None:
        # Qualifier suffixes (-v3, -backend) propagate; ``/work`` lives one
        # level deeper.
        assert _ensure_pipeline_work_ref("egg/issue-100-backend") == "egg/issue-100-backend/work"

    def test_idempotent_when_already_work_suffixed(self) -> None:
        assert _ensure_pipeline_work_ref("egg/issue-2261-v6/work") == "egg/issue-2261-v6/work"

    def test_passthrough_for_none(self) -> None:
        assert _ensure_pipeline_work_ref(None) is None

    def test_passthrough_for_non_egg_branch(self) -> None:
        # Non-``egg/`` branches are foreign refs; the orchestrator does
        # not own the namespace below them and must not rewrite them.
        assert _ensure_pipeline_work_ref("feature/foo") == "feature/foo"
        assert _ensure_pipeline_work_ref("main") == "main"

    def test_single_segment_egg_work_is_not_treated_as_normalised(self) -> None:
        # Degenerate input: ``egg/work`` ends in ``/work`` but is a
        # single-segment id, not an already-normalised pipeline branch.
        # Plain ``endswith("/work")`` would return it unchanged and the
        # slice DAG would push to ``egg/work/slice-N`` under the leaf
        # ``egg/work`` — back to the directory/file conflict #2399 fixes.
        # Structural check (≥2 slashes, last segment ``work``) treats it
        # as needing normalisation.
        assert _ensure_pipeline_work_ref("egg/work") == "egg/work/work"

    def test_strips_trailing_slash_before_appending_work(self) -> None:
        # The branch validation regex permits trailing slashes; an
        # input like ``egg/issue-1/`` must not collapse to a
        # double-slash ``egg/issue-1//work``.
        assert _ensure_pipeline_work_ref("egg/issue-1/") == "egg/issue-1/work"

    def test_degenerate_egg_slash_does_not_double_slash(self) -> None:
        # ``egg/`` is degenerate (no id segment). Pre-fix the helper
        # produced ``egg//work``; we now strip the trailing slash and
        # leave the bare ``egg`` unchanged (it does not start with
        # ``egg/``). The branch validation regex upstream should
        # reject this shape — the helper just refuses to make it worse.
        assert _ensure_pipeline_work_ref("egg/") == "egg"


class TestSliceNamespaceRoot:
    def test_strips_work_suffix(self) -> None:
        assert _slice_namespace_root("egg/issue-2261-v6/work") == "egg/issue-2261-v6"

    def test_passthrough_when_no_work_suffix(self) -> None:
        # Legacy / non-normalised callers: the branch itself is the root.
        assert _slice_namespace_root("egg/issue-2261-v6") == "egg/issue-2261-v6"

    def test_strips_only_trailing_work(self) -> None:
        # ``/work`` mid-path is NOT a suffix and must not be stripped.
        assert _slice_namespace_root("egg/work-stream/v1/work") == "egg/work-stream/v1"

    def test_qualifier_preserved(self) -> None:
        assert _slice_namespace_root("egg/issue-100-backend/work") == "egg/issue-100-backend"

    def test_single_segment_egg_work_is_root_itself(self) -> None:
        # Mirror of the structural check in ``_ensure_pipeline_work_ref``:
        # ``egg/work`` is a single-segment id, not a normalised pipeline
        # branch, so the namespace root is the branch itself rather than
        # collapsing to ``egg``.
        assert _slice_namespace_root("egg/work") == "egg/work"


class TestNamespaceCoexistence:
    """Pin the design property that solves the conflict.

    The pipeline tip ``<root>/work`` and slice integration branches
    ``<root>/slice-N`` must share a single parent path ``<root>/`` —
    that's the whole point of the #2399 fix. A regression that starts
    pushing the tip to ``<root>`` (a leaf ref) would re-introduce the
    ``directory file conflict`` from GitHub.
    """

    def test_pipeline_tip_and_slice_share_namespace_parent(self) -> None:
        pipeline_branch = _ensure_pipeline_work_ref("egg/issue-2261-v6")
        assert pipeline_branch == "egg/issue-2261-v6/work"

        namespace_root = _slice_namespace_root(pipeline_branch)
        slice_branch = f"{namespace_root}/slice-1"

        # Both refs share the parent ``egg/issue-2261-v6/`` and live as
        # siblings — neither is a prefix of the other.
        assert pipeline_branch.rsplit("/", 1)[0] == slice_branch.rsplit("/", 1)[0]
        assert not slice_branch.startswith(pipeline_branch + "/")
        assert not pipeline_branch.startswith(slice_branch + "/")

    def test_pipeline_tip_is_not_a_prefix_of_slice_branch(self) -> None:
        # Regression: the pre-fix shape had ``pipeline.branch ==
        # 'egg/issue-2261-v6'`` and slice branches at
        # ``'egg/issue-2261-v6/slice-N'``, making the slice path a child
        # of the pipeline ref. Git's ref storage rejects that with
        # ``directory file conflict``.
        pipeline_branch = _ensure_pipeline_work_ref("egg/issue-2261-v6")
        namespace_root = _slice_namespace_root(pipeline_branch)
        slice_branch = f"{namespace_root}/slice-1"
        assert not slice_branch.startswith(pipeline_branch + "/"), (
            "Slice integration branch must not live under the pipeline tip's path "
            "— that's the directory/file conflict #2399 fixes."
        )
