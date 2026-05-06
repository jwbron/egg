"""Canonical sandbox-side ``slice-<N>`` regex.

The sandbox imports the canonical ``slice_id`` shape from a single
module so the handler-side validation in
``egg_agent_tools.handlers.{brc,progress}`` and the CLI-side validation
in ``egg_lib.orch_cli`` can't drift apart. The orchestrator-side
canonical source is ``orchestrator.slice_id_validation.SLICE_ID_PATTERN``;
sandbox code does not import from the orchestrator package, so this
mirror is kept in step by the parity check in
``tests/sandbox/test_slice_id_pattern_parity.py``.
"""

from __future__ import annotations

import re

SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")
