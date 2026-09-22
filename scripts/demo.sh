#!/usr/bin/env sh
set -eu

base_url="${BASE_URL:-http://localhost:8000/v1}"
workflow_payload='{"name":"Demo writer","description":"Draft and normalize text","definition":{"steps":[{"key":"draft","type":"llm","provider":"fake","input":"Write about ${input.topic}"},{"key":"publish","type":"tool","tool":"uppercase","input":"${steps.draft.output}"}]}}'

workflow_response=$(curl --fail --silent --show-error -X POST "$base_url/workflows" -H 'Content-Type: application/json' -d "$workflow_payload")
workflow_id=$(printf '%s' "$workflow_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl --fail --silent --show-error -X POST "$base_url/workflows/$workflow_id/runs" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-run-1' \
  -d '{"inputs":{"topic":"reliable AI workflows"}}' | python3 -m json.tool

