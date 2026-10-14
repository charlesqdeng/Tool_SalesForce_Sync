#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 Starting Web AI Launcher..."

if [ ! -d "venv" ]; then
    echo "📦 Creating a brand new virtual environment..."
    python3 -m venv venv
else
    echo "✓ Virtual environment already exists."
fi

VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"

CERT_PATH=$("$VENV_PYTHON" -c "import certifi; print(certifi.where())")
export SSL_CERT_FILE="$CERT_PATH"
export REQUESTS_CA_BUNDLE="$CERT_PATH"
export CURL_CA_BUNDLE="$CERT_PATH"
export ANONYMIZED_TELEMETRY=false

echo "🛡️ SSL certificate bundles locked to: $CERT_PATH"

echo "📥 Installing Python dependencies..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install python-dotenv playwright certifi simple-salesforce \
    google-api-python-client google-auth google-auth-oauthlib

echo "🌐 Checking Playwright browser binaries..."
"$VENV_PYTHON" -m playwright install chromium

SCRIPT="sf_agent.py"
case "${1:-}" in
    sheet) SCRIPT="sf_to_sheet.py"; shift ;;
    sync)  SCRIPT="sheet_sync.py"; shift ;;
esac

echo "🧠 Starting $SCRIPT..."
echo "----------------------------------------"
"$VENV_PYTHON" "$SCRIPT" "$@"

echo "----------------------------------------"
echo "✅ Agent run finished successfully!"
