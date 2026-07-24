#!/bin/bash
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
PROTECTED=("uv.lock" ".github/workflows/" "tach.toml" ".pre-commit-config.yaml")
for pattern in "${PROTECTED[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]]; then
    echo "Blocked: $FILE_PATH is a protected gate file — ask the human to change it" >&2
    exit 2
  fi
done
exit 0
