#!/bin/bash
# Block built-in WebSearch/WebFetch when routing through LiteLLM to non-Anthropic models.
#
# The Anthropic server-tool schemas for these are stripped by drop_params
# on the Anthropic->OpenAI translation, so they silently no-op and report
# "Did 0 searches". Direct the model to mcp__ddg__* equivalents instead.

if [[ -z "$ANTHROPIC_CUSTOM_MODEL_OPTION" ]]; then
  # First-party Claude route (or LiteLLM routed to a Claude alias):
  # built-in tools are live, allow.
  printf '{"decision":"allow"}\n'
  exit 0
fi

# LiteLLM non-Anthropic route: block with a hint so the model picks the DDG MCP tools.
printf '{"decision":"block","reason":"WebSearch and WebFetch do not work in this session (routing through LiteLLM to a non-Anthropic model; built-in schemas are stripped). Use mcp__ddg__search instead of WebSearch, and mcp__ddg__fetch_content instead of WebFetch. Retry your operation with those tools."}'
exit 0
