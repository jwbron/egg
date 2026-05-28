#!/bin/bash
# Block built-in WebSearch/WebFetch when routing through LiteLLM to non-Anthropic models.
#
# The Anthropic server-tool schemas for these are stripped by drop_params
# on the Anthropic->OpenAI translation, so they silently no-op and report
# "Did 0 searches". Direct the model to mcp__ddg__* equivalents instead.
#
# This emits the PreToolUse output schema Claude Code actually honors: the
# decision is carried in hookSpecificOutput.permissionDecision (allow|deny|ask).
# The deprecated top-level {"decision":...} field is not read for PreToolUse,
# and only the modern permissionDecision:"deny" fires *before* the permission-
# mode check — so it overrides the bypassPermissions mode egg launches agents
# with (shared/egg_agent/client.py). See https://github.com/jwbron/egg/issues/2856
# and https://code.claude.com/docs/en/hooks.

if [[ -z "$ANTHROPIC_CUSTOM_MODEL_OPTION" ]]; then
  # First-party Claude route (or LiteLLM routed to a Claude alias): the built-in
  # tools are live. Exit 0 with no output so the call proceeds normally.
  exit 0
fi

# LiteLLM non-Anthropic route: deny with a reason so the model picks the DDG MCP
# tools (registered on the same path by run_agent_async).
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"WebSearch and WebFetch do not work in this session (routing through LiteLLM to a non-Anthropic model; the Anthropic built-in tool schemas are stripped). Use mcp__ddg__search instead of WebSearch, and mcp__ddg__fetch_content instead of WebFetch. Retry your operation with those tools."}}'
exit 0
