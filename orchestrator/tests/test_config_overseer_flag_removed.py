"""PR D (issue #3364, task-3-5): ``overseer_owns_host_detection`` removal.

The calibration-window flag ``overseer_owns_host_detection`` is *removed*
outright (not flipped to ``True``) now that the host → overseer detector
migration is concluded — the host ``/sdlc`` skill no longer runs its
stall / silent-agent / NACK / stuck-pipeline detectors, so there is no
XOR selector left to gate. This pins AC-D2's model-side contract:

* the field is gone from :class:`PipelineConfig` (not merely defaulted),
* the model still constructs with defaults, and
* the sibling overseer threshold knobs the migration DID keep are intact.

``PipelineConfig`` uses ``extra='ignore'``, so a *persisted* config that
still carries the old key deserializes without error and the value is
dropped rather than retained — asserted below so a stale statefile can
never silently resurrect the flag.
"""

from __future__ import annotations

from models import PipelineConfig

_REMOVED_FIELD = "overseer_owns_host_detection"

# The overseer threshold knobs the migration KEEPS (per-pipeline
# configurable; surfaced in the status payload). Their continued
# presence guards against an over-broad deletion that also strips the
# retained tuning surface.
_RETAINED_OVERSEER_FIELDS = (
    "overseer_stuck_phase_transition_seconds",
    "overseer_agent_stall_seconds",
    "overseer_silent_agent_threshold_seconds",
    "overseer_long_running_phase_seconds",
    "overseer_nack_unresolved_seconds",
)


def test_flag_field_removed_from_model() -> None:
    """The field no longer exists on the pydantic model."""
    assert _REMOVED_FIELD not in PipelineConfig.model_fields


def test_default_instance_has_no_flag_attribute() -> None:
    """A default-constructed config exposes no such attribute — proving
    the field was deleted, not renamed or defaulted elsewhere."""
    cfg = PipelineConfig()
    sentinel = object()
    assert getattr(cfg, _REMOVED_FIELD, sentinel) is sentinel


def test_stale_persisted_flag_is_dropped_not_retained() -> None:
    """A persisted config carrying the old key still deserializes
    (``extra='ignore'``) but the value is dropped, never resurrected."""
    cfg = PipelineConfig(**{_REMOVED_FIELD: True})
    sentinel = object()
    assert getattr(cfg, _REMOVED_FIELD, sentinel) is sentinel
    # Round-tripping the dump must not re-emit the removed key either.
    assert _REMOVED_FIELD not in cfg.model_dump()


def test_retained_overseer_threshold_fields_intact() -> None:
    """The overseer threshold knobs the migration keeps are untouched."""
    for field in _RETAINED_OVERSEER_FIELDS:
        assert field in PipelineConfig.model_fields, field
    # And they carry through onto a default instance.
    cfg = PipelineConfig()
    for field in _RETAINED_OVERSEER_FIELDS:
        assert isinstance(getattr(cfg, field), int), field
