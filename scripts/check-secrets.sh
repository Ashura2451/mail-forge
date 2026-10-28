#!/usr/bin/env bash
set -euo pipefail

# Offline, redacted guard for tracked files and staged additions. It emits only
# a generic failure message; matched values are never printed.
if git ls-files | awk '
  /(^|\/)\.env($|\.)/ && !/(^|\/)\.env\.example$/ { found=1 }
  END { exit found ? 0 : 1 }
'; then
  echo "secret check failed: tracked environment file detected" >&2
  exit 1
fi

aws_prefix="AK""IA"
github_prefix="gh""p_"
private_key_pattern='-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----'
credential_pattern="${aws_prefix}[0-9A-Z]{16}|${github_prefix}[A-Za-z0-9]{30,}"

if git grep --cached -I -q -E "$private_key_pattern|$credential_pattern" -- \
  . ':!scripts/check-secrets.sh' ':!**/*.md' 2>/dev/null; then
  echo "secret check failed: high-confidence credential pattern detected" >&2
  exit 1
fi

if git diff --cached --unified=0 --binary | rg -e "$private_key_pattern|$credential_pattern" >/dev/null; then
  echo "secret check failed: staged credential pattern detected" >&2
  exit 1
fi

echo "secret check passed"
