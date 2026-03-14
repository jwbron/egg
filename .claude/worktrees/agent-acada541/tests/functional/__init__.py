"""
Functional tests for egg gateway components.

Functional tests sit between unit tests and full integration tests:
- Use Docker containers for realistic testing
- Lighter-weight fixtures than integration_tests/
- Focus on component pairs rather than full system
- Target ~5-10s startup vs ~30s for full stack

Test modules:
- test_git_wrappers.py: Git command routing and validation
- test_session_lifecycle.py: Session create/heartbeat/delete flow
- test_network_modes.py: Private vs public mode behavior
"""
