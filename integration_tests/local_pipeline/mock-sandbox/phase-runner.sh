#!/bin/sh
echo "Mock sandbox: phase=$EGG_PIPELINE_PHASE pipeline_id=$EGG_PIPELINE_ID"
echo "Mode: $EGG_PIPELINE_MODE | Prompt: $EGG_PIPELINE_PROMPT"
sleep ${MOCK_SLEEP:-1}
exit ${MOCK_EXIT_CODE:-0}
