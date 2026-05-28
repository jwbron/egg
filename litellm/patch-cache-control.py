# Vendored-image patcher: adds qwen/deepseek to the two LiteLLM allowlists
# that gate cache_control passthrough. See litellm/Dockerfile and #2839.
#
# We patch by string-replacement (not whole-file COPY) so the patches
# survive minor upstream edits to surrounding code. The asserts below fail
# the build loudly if either anchor goes missing, instead of producing an
# unpatched image that silently re-bills the bootstrap on every turn.

import pathlib

LITELLM_ROOT = pathlib.Path("/app/litellm")

OPENROUTER_TRANSFORM = LITELLM_ROOT / "llms/openrouter/chat/transformation.py"
ANTHROPIC_ADAPTER = (
    LITELLM_ROOT / "llms/anthropic/experimental_pass_through/adapters/transformation.py"
)


def patch_openrouter_enum() -> None:
    needle = '    ZAI = "z-ai"\n'
    addition = '    QWEN = "qwen"\n    DEEPSEEK = "deepseek"\n'
    s = OPENROUTER_TRANSFORM.read_text()
    if addition in s:
        raise SystemExit(f"{OPENROUTER_TRANSFORM}: patch already present")
    if needle not in s:
        raise SystemExit(
            f'{OPENROUTER_TRANSFORM}: anchor `ZAI = "z-ai"` not found; '
            f"upstream LiteLLM layout shifted -- update the patcher"
        )
    OPENROUTER_TRANSFORM.write_text(s.replace(needle, needle + addition, 1))


def patch_anthropic_adapter() -> None:
    old = 'return "anthropic" in model_lower or "claude" in model_lower'
    new = 'return any(s in model_lower for s in ("anthropic", "claude", "qwen", "deepseek"))'
    s = ANTHROPIC_ADAPTER.read_text()
    if new in s:
        raise SystemExit(f"{ANTHROPIC_ADAPTER}: patch already present")
    if old not in s:
        raise SystemExit(
            f"{ANTHROPIC_ADAPTER}: anchor `is_anthropic_claude_model` "
            f"return line not found; upstream LiteLLM layout shifted -- "
            f"update the patcher"
        )
    ANTHROPIC_ADAPTER.write_text(s.replace(old, new, 1))


def verify() -> None:
    s1 = OPENROUTER_TRANSFORM.read_text()
    if 'QWEN = "qwen"' not in s1 or 'DEEPSEEK = "deepseek"' not in s1:
        raise SystemExit(f"verification failed: enum patch missing in {OPENROUTER_TRANSFORM}")
    s2 = ANTHROPIC_ADAPTER.read_text()
    if '"qwen"' not in s2 or '"deepseek"' not in s2:
        raise SystemExit(f"verification failed: adapter patch missing in {ANTHROPIC_ADAPTER}")


def main() -> None:
    patch_openrouter_enum()
    patch_anthropic_adapter()
    verify()
    print("LiteLLM cache_control passthrough patches applied (qwen, deepseek)")


if __name__ == "__main__":
    main()
