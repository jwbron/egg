"""Single declarative registry of coordination artifacts (#3077 slice-2).

A small, pure-Python registry describing the per-phase coordination
artifacts that producer roles commit and consumer roles read.  Rows here
are the single source of truth for:

* propose-time validation that a producer has actually committed its
  registered artifacts at the proposed SHA (slice-3 of #3077 consumes
  this, replacing the bespoke ``_validate_producer_draft_present`` and
  the hard-coded ``_get_draft_path`` knowledge that today lives in
  ``orchestrator/routes/signals.py`` and ``orchestrator/routes/pipelines.py``);
* the gateway artifact-read endpoint added in slice-4, which never lets
  agents pass a repo path — instead resolving an allow-listed ``name``
  through :func:`resolve_artifact_path`.

The registry mirrors the conventions baked into today's code:

* draft files under ``.egg-state/drafts/`` keyed by
  :func:`orchestrator.routes.pipelines._get_draft_path`
  (``{identifier}-analysis.md`` and ``{identifier}-plan.md``);
* agent-output files under ``.egg-state/agent-outputs/``
  (``{identifier}-architect-output.json``,
  ``{identifier}-architect-slices.yaml``,
  ``{identifier}-risk_analyst-output.json``).

Crucially this module has *no* orchestrator/gateway imports.  It must
stay importable from any process — sandbox CLI helpers, the gateway
blueprint added in slice-4, and orchestrator routes alike — without
dragging in the orchestrator package or its dependencies.  No new
config format either: the registry is plain Python data, and the
declarative-config rewrite tracked in #3017 will consume the rows here
rather than re-introducing a parser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One coordination artifact: identity, path template, and audience.

    Instances are *frozen* — every row is constructed at module import
    time and must never mutate after.  Reviewers (slice-3) and the
    gateway endpoint (slice-4) treat the registry as immutable shared
    state.

    Attributes:
        name: Stable artifact identifier (e.g. ``"plan-draft"``).  This
            is the only handle agents and downstream code pass around;
            paths are derived from it via :meth:`resolve_path`.
        path_template: Repo-relative path with a single
            ``{identifier}`` placeholder, rendered by
            :meth:`resolve_path` against either an issue number or a
            qualified pipeline id (see
            ``orchestrator.routes.pipelines._pipeline_identifier``).
        phase: Lifecycle phase the producer runs in.  Used by
            :func:`specs_for` to look up artifacts at propose time.
        producer_role: Exactly one role writes this artifact.  Multiple
            producers per artifact would defeat the validation slice-3
            layers on top of this registry.
        consumer_roles: Roles that read the artifact downstream.  Must
            be non-empty — an artifact with no consumer is just a
            disk file, not a coordination point.
    """

    name: str
    path_template: str
    phase: str
    producer_role: str
    consumer_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        # Cheap structural invariants that catch typos in the registry
        # at import time rather than at the first ``specs_for`` call.
        if not self.name:
            raise ValueError("ArtifactSpec.name must be non-empty")
        if "{identifier}" not in self.path_template:
            raise ValueError(
                "ArtifactSpec.path_template must contain '{identifier}'; "
                f"got {self.path_template!r}"
            )
        if not self.phase:
            raise ValueError("ArtifactSpec.phase must be non-empty")
        if not self.producer_role:
            raise ValueError("ArtifactSpec.producer_role must be non-empty")
        if not self.consumer_roles:
            raise ValueError(
                f"ArtifactSpec.consumer_roles must be non-empty (artifact={self.name!r})"
            )

    def resolve_path(self, identifier: str | int) -> str:
        """Render :attr:`path_template` against ``identifier``.

        ``identifier`` may be an integer issue number (e.g. ``3077``)
        or a qualified pipeline id (e.g. ``"issue-3077-replan"``);
        :func:`orchestrator.routes.pipelines._pipeline_identifier`
        picks between the two and this module renders whatever it
        returns.
        """
        return self.path_template.format(identifier=identifier)


# ---------------------------------------------------------------------------
# Registry
#
# Paths must match the conventions in:
#   * ``orchestrator.routes.pipelines._get_draft_path``
#     (``.egg-state/drafts/{identifier}-analysis.md``,
#     ``.egg-state/drafts/{identifier}-plan.md``)
#   * the agent-output prompt templates in ``routes/pipelines.py``
#     (``.egg-state/agent-outputs/{identifier}-architect-output.json``,
#     ``-architect-slices.yaml``, ``-risk_analyst-output.json``).
#
# Note the disk filename for the risk analyst uses an underscore
# (``risk_analyst-output.json``) — that matches today's prompt prose;
# the *artifact name* uses a hyphen (``risk-analyst-output``) to stay
# consistent with the other hyphenated artifact names.
# ---------------------------------------------------------------------------

_SPECS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec(
        name="analysis-draft",
        path_template=".egg-state/drafts/{identifier}-analysis.md",
        phase="refine",
        producer_role="refiner",
        consumer_roles=(
            "reviewer_refine",
            "architect",
            "task_planner",
            "risk_analyst",
        ),
    ),
    ArtifactSpec(
        name="plan-draft",
        path_template=".egg-state/drafts/{identifier}-plan.md",
        phase="plan",
        producer_role="task_planner",
        consumer_roles=(
            "reviewer_plan",
            "reviewer_contract",
            "coder",
            "tester",
            "documenter",
        ),
    ),
    ArtifactSpec(
        name="architect-output",
        path_template=".egg-state/agent-outputs/{identifier}-architect-output.json",
        phase="plan",
        producer_role="architect",
        consumer_roles=(
            "task_planner",
            "risk_analyst",
            "reviewer_plan",
        ),
    ),
    ArtifactSpec(
        name="architect-slices",
        path_template=".egg-state/agent-outputs/{identifier}-architect-slices.yaml",
        phase="plan",
        producer_role="architect",
        consumer_roles=(
            "task_planner",
            "reviewer_plan",
        ),
    ),
    ArtifactSpec(
        name="risk-analyst-output",
        path_template=".egg-state/agent-outputs/{identifier}-risk_analyst-output.json",
        phase="plan",
        producer_role="risk_analyst",
        consumer_roles=(
            "task_planner",
            "reviewer_plan",
        ),
    ),
)


# Name -> Spec lookup, frozen at import time.  ``Mapping`` (not
# ``dict``) for the annotation so callers can't rely on mutation.
_BY_NAME: Mapping[str, ArtifactSpec] = {spec.name: spec for spec in _SPECS}


def all_specs() -> tuple[ArtifactSpec, ...]:
    """Return every registered spec as an immutable tuple.

    Useful for tests that want to round-trip the whole registry
    through :func:`spec_by_name`, or for the gateway error path that
    lists registered names back to the caller.
    """
    return _SPECS


def spec_by_name(name: str) -> ArtifactSpec:
    """Return the spec registered under ``name``.

    Raises:
        KeyError: ``name`` is not registered.  The error message names
            the registered alternatives so the gateway can surface a
            structured 4xx (slice-4 task-4-1).
    """
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown artifact name: {name!r}; registered names: {sorted(_BY_NAME)}"
        ) from exc


def specs_for(phase: str, producer_role: str) -> tuple[ArtifactSpec, ...]:
    """Return every spec written by ``producer_role`` in ``phase``.

    Returns an empty tuple when the pair has no registered artifacts —
    this is the signal slice-3's propose-time validation uses to skip
    role/phase combinations that don't commit a coordination artifact
    (e.g. roles in the implement phase, or refine-phase reviewers).
    """
    return tuple(
        spec for spec in _SPECS if spec.phase == phase and spec.producer_role == producer_role
    )


def resolve_artifact_path(name: str, identifier: str | int) -> str:
    """Resolve ``name`` to its repo-relative path for ``identifier``.

    Thin convenience over ``spec_by_name(name).resolve_path(identifier)``.

    Both identifier shapes are supported: integer issue numbers
    (e.g. ``3077``) and string pipeline ids (e.g.
    ``"issue-3077-replan"``).  The caller is expected to have already
    picked between the two via
    ``orchestrator.routes.pipelines._pipeline_identifier``.
    """
    return spec_by_name(name).resolve_path(identifier)


def name_for_path(path: str) -> str | None:
    """Reverse-resolve a concrete repo-relative ``path`` to its registered
    artifact name, or ``None`` when no spec matches.

    This is the inverse of :meth:`ArtifactSpec.resolve_path`: the
    served-read consumers (the event-prompt first-review renderer in
    ``orchestrator/routes/event_prompt.py``, #3216 WS1 of #3209) know a
    producer's *changed paths* but the served-read endpoint and the
    ``egg-artifact`` verb address artifacts by *name*. This maps the path
    back so reviewers are handed ``egg-artifact get <name> --ref <sha>``
    instead of a path-bearing ``git show <sha>:<path>``.

    Each ``path_template`` has exactly one ``{identifier}`` placeholder
    (enforced at construction), so it reduces to a ``prefix`` /
    ``suffix`` pair around a single path segment. The identifier never
    contains a ``/`` (it is an issue number or a hyphenated pipeline id),
    so the placeholder matches one non-empty, slash-free run. The
    templates have disjoint prefix+suffix shapes, so at most one spec
    matches; the first match wins.
    """
    candidate = path.strip()
    for spec in _SPECS:
        prefix, _, suffix = spec.path_template.partition("{identifier}")
        pattern = re.compile(f"{re.escape(prefix)}[^/]+{re.escape(suffix)}")
        if pattern.fullmatch(candidate):
            return spec.name
    return None


__all__ = [
    "ArtifactSpec",
    "all_specs",
    "name_for_path",
    "resolve_artifact_path",
    "spec_by_name",
    "specs_for",
]
