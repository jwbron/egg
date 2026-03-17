"""
Tests for anchor constants in egg_config.constants.

Covers:
- All anchor constants are defined with correct values
- Constants are importable from egg_config
- Constants match issue spec values
"""


class TestAnchorConstants:
    """Tests for anchor-related constants."""

    def test_soft_limit_bytes(self):
        """ANCHOR_SOFT_LIMIT_BYTES is 2048."""
        from egg_config.constants import ANCHOR_SOFT_LIMIT_BYTES

        assert ANCHOR_SOFT_LIMIT_BYTES == 2048

    def test_hard_limit_bytes(self):
        """ANCHOR_HARD_LIMIT_BYTES is 3072."""
        from egg_config.constants import ANCHOR_HARD_LIMIT_BYTES

        assert ANCHOR_HARD_LIMIT_BYTES == 3072

    def test_team_soft_limit_bytes(self):
        """ANCHOR_TEAM_SOFT_LIMIT_BYTES is 4096."""
        from egg_config.constants import ANCHOR_TEAM_SOFT_LIMIT_BYTES

        assert ANCHOR_TEAM_SOFT_LIMIT_BYTES == 4096

    def test_team_hard_limit_bytes(self):
        """ANCHOR_TEAM_HARD_LIMIT_BYTES is 6144."""
        from egg_config.constants import ANCHOR_TEAM_HARD_LIMIT_BYTES

        assert ANCHOR_TEAM_HARD_LIMIT_BYTES == 6144

    def test_redis_prefix(self):
        """ANCHOR_REDIS_PREFIX is 'anchor'."""
        from egg_config.constants import ANCHOR_REDIS_PREFIX

        assert ANCHOR_REDIS_PREFIX == "anchor"

    def test_max_progress_items(self):
        """ANCHOR_MAX_PROGRESS_ITEMS is 10."""
        from egg_config.constants import ANCHOR_MAX_PROGRESS_ITEMS

        assert ANCHOR_MAX_PROGRESS_ITEMS == 10

    def test_max_decisions(self):
        """ANCHOR_MAX_DECISIONS is 8."""
        from egg_config.constants import ANCHOR_MAX_DECISIONS

        assert ANCHOR_MAX_DECISIONS == 8

    def test_max_key_context(self):
        """ANCHOR_MAX_KEY_CONTEXT is 5."""
        from egg_config.constants import ANCHOR_MAX_KEY_CONTEXT

        assert ANCHOR_MAX_KEY_CONTEXT == 5

    def test_max_errors(self):
        """ANCHOR_MAX_ERRORS is 5."""
        from egg_config.constants import ANCHOR_MAX_ERRORS

        assert ANCHOR_MAX_ERRORS == 5

    def test_max_files(self):
        """ANCHOR_MAX_FILES is 15."""
        from egg_config.constants import ANCHOR_MAX_FILES

        assert ANCHOR_MAX_FILES == 15

    def test_constants_in_all_export(self):
        """All anchor constants are in __all__."""
        from egg_config import constants

        anchor_constants = [
            "ANCHOR_SOFT_LIMIT_BYTES",
            "ANCHOR_HARD_LIMIT_BYTES",
            "ANCHOR_TEAM_SOFT_LIMIT_BYTES",
            "ANCHOR_TEAM_HARD_LIMIT_BYTES",
            "ANCHOR_REDIS_PREFIX",
            "ANCHOR_MAX_PROGRESS_ITEMS",
            "ANCHOR_MAX_DECISIONS",
            "ANCHOR_MAX_KEY_CONTEXT",
            "ANCHOR_MAX_ERRORS",
            "ANCHOR_MAX_FILES",
        ]
        for const_name in anchor_constants:
            assert const_name in constants.__all__, f"{const_name} should be in constants.__all__"

    def test_importable_from_egg_config(self):
        """Anchor constants importable from egg_config."""
        from egg_config.constants import (
            ANCHOR_HARD_LIMIT_BYTES,
            ANCHOR_MAX_DECISIONS,
            ANCHOR_MAX_ERRORS,
            ANCHOR_MAX_FILES,
            ANCHOR_MAX_KEY_CONTEXT,
            ANCHOR_MAX_PROGRESS_ITEMS,
            ANCHOR_REDIS_PREFIX,
            ANCHOR_SOFT_LIMIT_BYTES,
            ANCHOR_TEAM_HARD_LIMIT_BYTES,
            ANCHOR_TEAM_SOFT_LIMIT_BYTES,
        )

        # All should be defined
        assert all(
            v is not None
            for v in [
                ANCHOR_SOFT_LIMIT_BYTES,
                ANCHOR_HARD_LIMIT_BYTES,
                ANCHOR_TEAM_SOFT_LIMIT_BYTES,
                ANCHOR_TEAM_HARD_LIMIT_BYTES,
                ANCHOR_REDIS_PREFIX,
                ANCHOR_MAX_PROGRESS_ITEMS,
                ANCHOR_MAX_DECISIONS,
                ANCHOR_MAX_KEY_CONTEXT,
                ANCHOR_MAX_ERRORS,
                ANCHOR_MAX_FILES,
            ]
        )
