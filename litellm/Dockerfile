# Vendored LiteLLM image: bases on upstream and patches in cache_control
# passthrough for qwen/deepseek upstreams (issue #2839).
#
# Two stale allowlists in upstream LiteLLM strip cache_control before the
# request reaches OpenRouter, even though OpenRouter has supported caching
# for Qwen and DeepSeek since early 2026:
#
#   1. litellm/llms/openrouter/chat/transformation.py:
#      CacheControlSupportedModels enum -- adds QWEN, DEEPSEEK.
#   2. litellm/llms/anthropic/experimental_pass_through/adapters/
#      transformation.py: is_anthropic_claude_model() -- extends the
#      substring check to also pass through for qwen and deepseek.
#
# Net effect today: each turn re-bills the full bootstrap (~30k tokens of
# CLAUDE.md + tools + skills + agent prompt) at full price instead of
# cache_read. Empirically host-side this was a ~5x per-turn cost driver on
# non-Claude routes.
#
# Long-term path is upstream-merged, not vendored -- file an upstream PR
# alongside.
FROM ghcr.io/berriai/litellm:v1.86.2

USER root
COPY litellm/patch-cache-control.py /tmp/patch-cache-control.py
RUN python /tmp/patch-cache-control.py \
    && rm /tmp/patch-cache-control.py

# Drop back to a non-root UID -- the k8s podSpec sets runAsUser: 1000 via
# securityContext anyway, but ending the image on a non-root USER keeps
# hadolint (DL3002) happy and matches the runtime contract. Patched files
# are world-readable.
USER 1000
