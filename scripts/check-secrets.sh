#!/usr/bin/env bash
# Grep the tracked tree for things that must never be published. Exit 1 when anything matches.
# Run it before every commit (`bash scripts/check-secrets.sh`); CI runs it too.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

status=0

# Tokens, cloud access keys, personal addresses.
pattern='eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|@gmail\.com'

# Patterns that would themselves say too much if published (your network ranges, your
# customers' phone format, your home directory) go in .check-secrets.local, one extended
# regex per line. That file is gitignored, so they are checked without being shipped.
if [ -f .check-secrets.local ]; then
    while IFS= read -r line; do
        case "$line" in '' | '#'*) continue ;; esac
        pattern="$pattern|$line"
    done < .check-secrets.local
fi

# --untracked: a new file is checked before it is added, not only once it is committed. Ignored
# files (.check-secrets.local itself, build output) stay out.
if git grep --untracked -nIE "$pattern" -- . ':!uv.lock' ':!scripts/check-secrets.sh' ':!web/pnpm-lock.yaml'; then
    echo "check-secrets: the lines above look like secrets or personal paths." >&2
    status=1
fi

# Key material must never be tracked, whatever it is called.
if git ls-files | grep -nE '\.(pem|key|p12|pfx)$|(^|/)id_(rsa|ed25519|ecdsa|dsa)(\.pub)?$|(^|/)secrets\.env$'; then
    echo "check-secrets: the files above look like key material." >&2
    status=1
fi

# ...and whatever it is called, a key is a header alone on its line. Test strings that mention
# one sit inside quotes, so they do not match.
if git grep --untracked -nIE '^[[:space:]]*(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|PuTTY-User-Key-File-[0-9]+:)' -- . ':!scripts/check-secrets.sh'; then
    echo "check-secrets: the lines above start a private key." >&2
    status=1
fi

if [ "$status" -eq 0 ]; then
    echo "check-secrets: clean"
fi
exit "$status"
