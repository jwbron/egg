# egg-litellm — stock LiteLLM proxy + egg's prompt-cache patches.
#
# egg routes non-Claude agents (the cq-6 OpenRouter/Qwen pilot, #2799)
# through this proxy: Claude Code -> gateway -> LiteLLM -> OpenRouter. The
# stock image's Anthropic->OpenAI translation drops prompt-cache hits on
# Qwen/DeepSeek, billing full input rate every turn. patch_litellm_cache.py
# closes the three gaps at build time (see that script for the diagnosis);
# cost_callback.py surfaces the resulting cost/cache stats into the pod log
# stream. Both mirror the host-side dotfiles setup that took Qwen cache hit
# rate from 0% to ~99.99%.
#
# Pinned to v1.86.2 — the version the patches and cost_callback were
# validated against, and the same release the dotfiles host install pins.
# The patch script fails the build loudly if a needle stops matching, so a
# future bump can't silently ship an unpatched (full-input-rate) image.
FROM ghcr.io/berriai/litellm:v1.86.2

# Patches edit the installed litellm package (root-owned site-packages); the
# build runs as root. The runtime pod still drops to UID 1000 via the
# Deployment's securityContext.
USER root

COPY patch_litellm_cache.py /egg/patch_litellm_cache.py
RUN python3 /egg/patch_litellm_cache.py

# Custom cost/cache logger, registered as `cost_callback.cost_logger` under
# litellm_settings.callbacks (litellm-configmap.yaml). LiteLLM resolves a
# config-registered callback by FILE PATH relative to the config directory
# (litellm/proxy/types_utils/utils.py::get_instance_fn loads
# `<dirname(--config)>/cost_callback.py`) — NOT via PYTHONPATH/import. The
# Deployment passes `--config /app/config.yaml`, so this must sit at
# /app/cost_callback.py, alongside the mounted config. If that mount path
# ever changes, move this with it. Logs to stdout only — no file writes, so
# it's compatible with the read-only root filesystem.
COPY cost_callback.py /app/cost_callback.py

# ENTRYPOINT/CMD inherited from the base image (docker/prod_entrypoint.sh);
# the Deployment supplies --config and --port as args.
