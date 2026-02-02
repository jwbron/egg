"""Unit tests for shared module."""


def test_shared_imports():
    """Test that shared modules can be imported."""
    import shared
    import shared.egg_config
    import shared.egg_logging

    assert shared is not None
    assert shared.egg_config is not None
    assert shared.egg_logging is not None
