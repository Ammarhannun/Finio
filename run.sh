#!/usr/bin/env bash
# Finio — start backend + frontend with one command.
# Usage:  ./run.sh        (from the Finio/main folder)
# Stop:   press Ctrl+C

cd "$(dirname "$0")"   # always run from the project root (this folder)

echo "Finio — starting up..."

# 1. Free the ports in case an old server is still running.
lsof -ti tcp:8000 | xargs kill -9 2>/dev/null || true
lsof -ti tcp:5500 | xargs kill -9 2>/dev/null || true

# 2. Backend (FastAPI on :8000).
if [ ! -d venv ]; then
  echo "ERROR: no venv found. Create it first with:  python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
source venv/bin/activate
uvicorn main:app --reload --port 8000 > /tmp/finio_backend.log 2>&1 &
BACK=$!

# 3. Frontend (static files on :5500).
( cd frontend && python3 -m http.server 5500 > /tmp/finio_frontend.log 2>&1 ) &
FRONT=$!

# 4. Wait for the backend to answer.
printf "Waiting for backend"
for i in $(seq 1 40); do
  if curl -s -o /dev/null http://127.0.0.1:8000/ ; then break; fi
  printf "."; sleep 0.5
done
echo " ready."

echo ""
echo "  Backend:  http://127.0.0.1:8000"
echo "  App:      http://localhost:5500/login.html"
echo ""

# 5. Open the app in the default browser.
open "http://localhost:5500/login.html" 2>/dev/null || true

echo "Both servers are running. Press Ctrl+C here to stop them."
trap "echo; echo 'Stopping servers...'; kill $BACK $FRONT 2>/dev/null; exit 0" INT TERM
wait
