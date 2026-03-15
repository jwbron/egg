# Analysis: Add Integration Tests for Worktree Management

> Issue: #553 | Phase: refine

## Problem Statement

The worktree management system is a critical component of the egg gateway that provides isolated git worktrees for each container. While the system has comprehensive unit tests (`gateway/tests/test_worktree_manager.py` with 449 lines of tests) and some host-side integration tests (`tests/sandbox/test_git_isolation.py` with 249 lines), there are gaps in **realistic end-to-end integration testing** that exercises the full worktree lifecycle through the gateway API in a containerized environment.

The current state:
- Unit tests mock subprocess calls and file operations
- Host-side tests verify git worktree structure but don't test the gateway API
- No integration tests that exercise the worktree API endpoints through the full stack

The desired outcome:
- Integration tests that validate worktree creation, listing, and deletion through the gateway API
- Multi-container isolation verification (multiple containers can't interfere with each other's worktrees)
- Crash recovery testing (orphaned worktree cleanup)
- Realistic container lifecycle testing with actual Docker containers

## Current Behavior

### Worktree Manager Implementation (`gateway/worktree_manager.py:771 lines`)

The `WorktreeManager` class provides:
- `create_worktree()`: Creates isolated worktrees with container-specific branches (`egg/{container_id}/work`)
- `remove_worktree()`: Removes worktrees and associated branches with uncommitted changes detection
- `list_worktrees()`: Enumerates all active worktrees
- `cleanup_orphaned_worktrees()`: Removes worktrees from crashed containers

### Gateway API Endpoints (`gateway/gateway.py:2239-2449`)

```
POST /api/v1/worktree/create  - Creates worktrees for a container (launcher_auth)
POST /api/v1/worktree/delete  - Deletes worktrees for a container (launcher_auth)
GET  /api/v1/worktree/list    - Lists all active worktrees (launcher_auth)
```

### Session Integration (`gateway/gateway.py:2457+`)

The session creation endpoint (`POST /api/v1/sessions/create`) atomically:
1. Queries repository visibility
2. Filters repos based on mode (private/public)
3. Creates worktrees for filtered repos
4. Registers session with filtered repo list

### Existing Tests

| Location | Type | Coverage |
|----------|------|----------|
| `gateway/tests/test_worktree_manager.py` | Unit | Identifier validation, data classes, manager operations, Docker pre-created .git handling |
| `tests/sandbox/test_git_isolation.py` | Host Integration | Git worktree structure, gitdir/commondir backup/restore cycles |
| `tests/sandbox/test_gateway_helpers.py` | Unit | API call mocking for create_worktrees/delete_worktrees |
| `integration_tests/test_stack_lifecycle.py` | Integration | Session creation with worktree info (but doesn't verify worktree content) |

## Constraints

### Technical Constraints
- **Docker required**: Integration tests need Docker to spawn containers and gateway
- **Repository setup**: Tests need a real git repository to create worktrees from (current test config uses `test-owner/test-repo`)
- **Network isolation**: Tests use 172.40.x/172.41.x subnets to avoid collision with production
- **Authentication**: Worktree endpoints require `launcher_auth` (shared secret)
- **File permissions**: Worktree operations require proper uid/gid handling (default: 1000)

### Dependencies
- `egg_stack` fixture in `integration_tests/conftest.py` provides gateway lifecycle
- Docker Compose configuration in `integration_tests/docker-compose.yml`
- Volume mounts for worktrees: `worktrees:/home/egg/.egg-worktrees`

### Scope Boundaries
- Tests should NOT require GitHub connectivity (worktree management is local-only)
- Tests should NOT modify production worktree directories
- Tests should clean up all created containers and worktrees

## Options Considered

### Option A: Extend `test_stack_lifecycle.py` with Worktree-Specific Tests

**Approach**: Add worktree integration tests to the existing session lifecycle test file, leveraging the `egg_stack` and `gateway_session` fixtures.

**Pros**:
- Minimal new infrastructure needed
- Reuses existing test patterns
- Natural fit since worktrees are created as part of session lifecycle
- Single test file to run for related functionality

**Cons**:
- File could become too large/unfocused
- Worktree tests may need different fixtures (e.g., real git repos)
- Mixing concerns between session and worktree testing

### Option B: Create Dedicated `test_worktree_integration.py`

**Approach**: Create a new integration test file specifically for worktree management, with specialized fixtures for git repository setup.

**Pros**:
- Clear separation of concerns
- Can have specialized fixtures for git repo creation
- Easier to run/debug worktree tests in isolation
- Better organization for comprehensive test coverage

**Cons**:
- Some duplication of fixture setup
- New file to maintain
- May need to extract common fixtures to conftest.py

### Option C: Agent-Led Testing with Claude Code

**Approach**: Use the existing `run_claude_structured()` infrastructure to have an agent verify worktree behavior from inside a container.

**Pros**:
- Tests from the agent's perspective (realistic usage)
- Can verify git operations work in the worktree
- Leverages existing structured output framework

**Cons**:
- Expensive (API costs)
- Slower test execution
- May be overkill for basic worktree lifecycle tests
- Better suited for end-to-end agent workflows

## Recommended Approach

**Option B: Create Dedicated `test_worktree_integration.py`** is recommended.

**Rationale**:
1. **Clear scope**: Worktree management is distinct from session lifecycle and deserves focused testing
2. **Specialized fixtures**: Creating real git repositories for worktree tests requires custom fixtures that don't belong in the session tests
3. **Comprehensive coverage**: A dedicated file allows systematic testing of all worktree scenarios without cluttering session tests
4. **Maintainability**: Easier to understand, debug, and extend worktree-specific tests

### Proposed Test Structure

```python
# integration_tests/test_worktree_integration.py

@pytest.fixture
def test_git_repo(egg_stack):
    """Create a real git repository for worktree testing."""
    # Create temp repo in gateway's repos volume
    # Initialize with git init, add commit
    # Yield repo info
    # Cleanup on exit

class TestWorktreeAPI:
    def test_create_worktree_returns_paths(egg_stack)
    def test_create_worktree_for_nonexistent_repo_fails(egg_stack)
    def test_list_worktrees_shows_created(egg_stack, test_git_repo)
    def test_delete_worktree_removes_paths(egg_stack, test_git_repo)
    def test_delete_nonexistent_worktree_succeeds(egg_stack)

class TestMultiContainerIsolation:
    def test_two_containers_get_different_branches(egg_stack, test_git_repo)
    def test_container_cannot_access_other_worktree(egg_stack, test_git_repo)
    def test_concurrent_worktree_creation(egg_stack, test_git_repo)

class TestWorktreeLifecycle:
    def test_session_creation_creates_worktrees(egg_stack, test_git_repo)
    def test_session_deletion_removes_worktrees(egg_stack, test_git_repo)
    def test_orphaned_worktree_cleanup_on_startup(egg_stack, test_git_repo)

class TestWorktreePermissions:
    def test_worktree_owned_by_specified_uid_gid(egg_stack, test_git_repo)
    def test_worktree_content_is_writable(egg_stack, test_git_repo)

class TestErrorHandling:
    def test_invalid_container_id_rejected(egg_stack)
    def test_path_traversal_blocked(egg_stack)
    def test_delete_with_uncommitted_changes(egg_stack, test_git_repo)
```

### Implementation Notes

1. **Repository creation**: The test needs to create a git repo inside the gateway's `/home/egg/repos/` volume. This can be done by:
   - Exec into the gateway container to create the repo
   - Use a shared volume mounted by the test harness

2. **Verification**: Tests should verify:
   - API response correctness
   - Actual filesystem state (paths exist, correct permissions)
   - Git branch creation (correct branch names)

3. **Cleanup**: Each test should clean up:
   - Created worktrees via API
   - Test containers
   - Test git repositories

## Open Questions

```
egg-contract add-decision --question "Should we require actual container spawning for worktree tests, or is API-level testing sufficient?" \
  --options "API-level only (Recommended)" "Container spawning for some tests" "Full container spawning for all tests" --format markdown
```

The tradeoff is between test realism and test speed/complexity. API-level tests verify the gateway behavior but don't validate that containers can actually use the worktrees. Container-spawning tests are more realistic but slower and more complex.

**Open-ended questions:**
- Are there specific worktree failure scenarios from production that should be prioritized for testing?
- Should we include performance benchmarks (e.g., time to create 10 worktrees)?

---

*Authored-by: egg*
