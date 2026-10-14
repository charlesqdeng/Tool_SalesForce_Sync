#!/usr/bin/env bash
# Launch a dedicated Chrome instance with remote debugging on :9222.
# Uses an isolated profile at ~/chrome-sf-debug so it doesn't disturb your
# regular Chrome. Log into Salesforce once here — the profile persists.

set -euo pipefail

PORT=9222
PROFILE_DIR="$HOME/chrome-sf-debug"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN_URL="${SALESFORCE_LOGIN_URL:-https://login.salesforce.com/}"

if curl -s --max-time 2 "http://localhost:${PORT}/json/version" >/dev/null 2>&1; then
    echo "✅ Chrome is already running on :${PORT}."
    # On macOS, closing the last window leaves Chrome running with no pages.
    # Open a tab via the DevTools API so there's something visible.
    if ! curl -s --max-time 2 "http://localhost:${PORT}/json/list" | grep -q '"type": *"page"'; then
        echo "   No open windows — opening ${LOGIN_URL}"
        curl -s --max-time 5 -X PUT "http://localhost:${PORT}/json/new?${LOGIN_URL}" >/dev/null
        osascript -e 'tell application "Google Chrome" to activate' >/dev/null 2>&1 || true
    fi
    exit 0
fi

if [ ! -x "$CHROME" ]; then
    echo "❌ Chrome not found at: $CHROME"
    exit 1
fi

echo "🚀 Launching Chrome on :${PORT} with profile ${PROFILE_DIR}..."
"$CHROME" \
    --remote-debugging-port="${PORT}" \
    --user-data-dir="${PROFILE_DIR}" \
    "${LOGIN_URL}" \
    >/dev/null 2>&1 &

# Wait up to 10s for the debug port to come up.
for _ in $(seq 1 20); do
    if curl -s --max-time 1 "http://localhost:${PORT}/json/version" >/dev/null 2>&1; then
        echo "✅ Chrome is up on :${PORT}."
        echo "   → If this is your first launch, complete Salesforce login in the new window."
        echo "   → Then run: ./run.sh --dry-run"
        exit 0
    fi
    sleep 0.5
done

echo "⚠️  Chrome was launched but :${PORT} didn't come up within 10s."
echo "   Check that Chrome opened; then try: curl -s http://localhost:${PORT}/json/version"
exit 1
