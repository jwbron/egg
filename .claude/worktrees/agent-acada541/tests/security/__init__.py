"""
Security test suite for egg.

This module contains security-focused tests organized by attack category:
- Credential isolation: Tests ensuring credentials don't leak to sandbox
- Policy bypass: Tests for injection, encoding, and race condition attacks
- Input fuzzing: Property-based testing with hypothesis for edge cases

All security tests:
1. Document the attack vector being tested
2. Include a "defense validation" assertion
3. Are tagged with @pytest.mark.security
4. Reference relevant CWE/OWASP categories where applicable
"""
