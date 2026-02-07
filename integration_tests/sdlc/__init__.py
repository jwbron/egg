"""SDLC pipeline integration tests.

These tests verify the structurally-enforced agent checkpoint system,
including:
- Happy path: Full pipeline success from refine to PR
- Review rejection: Reviewer rejects tasks, implementer fixes them
- Circuit breaker: Escalation triggers when thresholds are exceeded
- HITL flow: Human decision pauses and resumes the pipeline
- Role enforcement: Gateway blocks unauthorized mutations
"""
