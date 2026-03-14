"""Tests for dynamic subnet allocation and context-aware network creation."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.context import AUTO, RuntimeContext, set_context
from egg_lib.docker import _allocate_dynamic_subnet, ensure_gateway_networks, teardown_networks

MODULE = "egg_lib.docker"


class TestAllocateDynamicSubnet:
    """Tests for _allocate_dynamic_subnet() helper."""

    @patch(f"{MODULE}.subprocess")
    def test_returns_first_available_subnet(self, mock_subprocess):
        """Should return 172.28.0.0/24 when no networks exist."""
        # docker network ls returns empty
        ls_result = MagicMock(stdout="\n", returncode=0)
        mock_subprocess.run.return_value = ls_result

        result = _allocate_dynamic_subnet()
        assert result == "172.28.0.0/24"

    @patch(f"{MODULE}.subprocess")
    def test_skips_used_subnets(self, mock_subprocess):
        """Should skip subnets already in use."""
        # First call: docker network ls returns one network ID
        ls_result = MagicMock(stdout="net123\n", returncode=0)
        # Second call: docker network inspect returns subnet
        inspect_result = MagicMock(stdout="172.28.0.0/24", returncode=0)

        mock_subprocess.run.side_effect = [ls_result, inspect_result]

        result = _allocate_dynamic_subnet()
        assert result == "172.28.1.0/24"

    @patch(f"{MODULE}.subprocess")
    def test_raises_when_all_used(self, mock_subprocess):
        """Should raise RuntimeError when no subnet is available."""
        # Simulate all subnets used
        all_subnets = set()
        for major in range(28, 64):
            for minor in range(0, 256):
                all_subnets.add(f"172.{major}.{minor}.0/24")

        # docker network ls returns many IDs
        ids = "\n".join(f"net{i}" for i in range(len(all_subnets)))
        ls_result = MagicMock(stdout=ids, returncode=0)

        # Each inspect returns a different subnet
        inspects = [MagicMock(stdout=s, returncode=0) for s in sorted(all_subnets)]
        mock_subprocess.run.side_effect = [ls_result] + inspects

        try:
            _allocate_dynamic_subnet()
            raise AssertionError("Should have raised RuntimeError")
        except RuntimeError as e:
            assert "No unused subnet" in str(e)


class TestEnsureGatewayNetworksWithContext:
    """Tests for ensure_gateway_networks() reading from context."""

    def setup_method(self):
        """Save original context."""
        import egg_lib.context as ctx_mod

        self._original = ctx_mod._context

    def teardown_method(self):
        """Restore original context."""
        import egg_lib.context as ctx_mod

        ctx_mod._context = self._original

    @patch(f"{MODULE}._create_network", return_value=True)
    def test_uses_context_network_names(self, mock_create):
        """Should use network names from context, not hardcoded values."""
        ctx = RuntimeContext(
            isolated_network="test-iso",
            external_network="test-ext",
            isolated_subnet="10.0.0.0/24",
            external_subnet="10.1.0.0/24",
        )
        set_context(ctx)

        result = ensure_gateway_networks()

        assert result is True
        # Verify the correct network names were passed
        calls = mock_create.call_args_list
        assert calls[0] == call("test-iso", "10.0.0.0/24", internal=True)
        assert calls[1] == call("test-ext", "10.1.0.0/24", internal=False)

    @patch(f"{MODULE}._create_network", return_value=True)
    @patch(f"{MODULE}._allocate_dynamic_subnet")
    def test_auto_subnet_allocates_dynamically(self, mock_alloc, mock_create):
        """When subnet is 'auto', should call _allocate_dynamic_subnet."""
        mock_alloc.side_effect = ["172.28.0.0/24", "172.28.1.0/24"]

        ctx = RuntimeContext(
            isolated_network="test-iso",
            external_network="test-ext",
            isolated_subnet=AUTO,
            external_subnet=AUTO,
        )
        set_context(ctx)

        result = ensure_gateway_networks()

        assert result is True
        assert mock_alloc.call_count == 2
        # Verify context was updated with allocated values
        assert ctx.isolated_subnet == "172.28.0.0/24"
        assert ctx.external_subnet == "172.28.1.0/24"
        assert ctx.gateway_isolated_ip == "172.28.0.2"
        assert ctx.gateway_external_ip == "172.28.1.2"

    @patch(f"{MODULE}._create_network")
    def test_returns_false_on_isolated_failure(self, mock_create):
        """Should return False if isolated network creation fails."""
        mock_create.return_value = False
        ctx = RuntimeContext()
        set_context(ctx)

        result = ensure_gateway_networks()

        assert result is False


class TestTeardownNetworks:
    """Tests for teardown_networks()."""

    def setup_method(self):
        import egg_lib.context as ctx_mod

        self._original = ctx_mod._context

    def teardown_method(self):
        import egg_lib.context as ctx_mod

        ctx_mod._context = self._original

    @patch(f"{MODULE}.subprocess")
    def test_removes_both_networks(self, mock_subprocess):
        """Should docker network rm both networks from context."""
        ctx = RuntimeContext(
            isolated_network="iso-net",
            external_network="ext-net",
        )
        set_context(ctx)

        mock_subprocess.run.return_value = MagicMock(returncode=0)
        teardown_networks()

        calls = mock_subprocess.run.call_args_list
        assert len(calls) == 2
        assert "iso-net" in calls[0][0][0]
        assert "ext-net" in calls[1][0][0]
