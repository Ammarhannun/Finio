#!/usr/bin/env bash
# Finio — start backend + frontend with one command.
# Usage:  ./run.sh        (from the Finio/main folder)
# Stop:   press Ctrl+C

cd "$(dirname "$0")"   # always run from the project root (this folder)

echo "Finio — starting up..."

# 1. Never kill an unrelated process to claim a port.
if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 || lsof -nP -iTCP:5500 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8000 or 5500 is already in use. Stop your existing Finio servers first."
  exit 1
fi

# 2. Backend (FastAPI on :8000).
if [ ! -d venv ]; then
  echo "ERROR: create a Python 3.11+ venv, then install requirements.lock. See DEPLOY.md."
  exit 1
fi
source venv/bin/activate
python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "This release needs Python 3.11+. Your existing venv is older; see DEPLOY.md."
  exit 1
}
uvicorn main:app --reload --port 8000 > /tmp/finio_backend.log 2>&1 &
BACK=$!

# 3. Frontend (static files on :5500).
python frontend/serve.py 5500 > /tmp/finio_frontend.log 2>&1 &
FRONT=$!
cleanup() { kill "$BACK" "$FRONT" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# 4. Wait for the backend to answer.
printf "Waiting for backend"
for i in $(seq 1 40); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/ 2>/dev/null; then break; fi
  printf "."; sleep 0.5
done
if ! curl -fsS -o /dev/null http://127.0.0.1:8000/ 2>/dev/null || ! kill -0 "$FRONT" 2>/dev/null; then
  echo "Startup failed. Check /tmp/finio_backend.log and /tmp/finio_frontend.log."
  exit 1
fi
echo " ready."

echo ""
echo "  Backend:  http://127.0.0.1:8000"
echo "  App:      http://localhost:5500/login.html"
echo ""

# 5. Open the app in the default browser.
open "http://localhost:5500/login.html" 2>/dev/null || true

echo "Both servers are running. Press Ctrl+C here to stop them."
wait
