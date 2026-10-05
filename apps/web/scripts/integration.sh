#!/usr/bin/env bash
# Creates throwaway accounts, launches both servers in one network namespace,
# and proves the production Next proxy against the real FastAPI implementation.
set -euo pipefail
WEB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$WEB_DIR/../.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
TMPDIR_TEST=$(mktemp -d /tmp/evidence-ui-integration.XXXXXX)
ARTIFACT_DIR="$WEB_DIR/test-results"
mkdir -p "$ARTIFACT_DIR"
API_PID='' WEB_PID=''
cleanup() {
  local status=$?
  for pid in "$API_PID" "$WEB_PID"; do
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
  done
  # Preserve diagnostics, never the temporary account database or credentials.
  for service in api web; do
    if [[ -f "$TMPDIR_TEST/$service.log" ]]; then
      cp "$TMPDIR_TEST/$service.log" "$ARTIFACT_DIR/$service.log"
    fi
  done
  exit "$status"
}
trap cleanup EXIT
export DATABASE_URL="sqlite:///$TMPDIR_TEST/app.db" BLOB_DIR="$TMPDIR_TEST/blobs" API_TOKENS_JSON='{}' DEMO_MODE=false
export STORAGE_BACKEND=local RAW_STORAGE_DIR="$TMPDIR_TEST/blobs" ENABLE_EXTERNAL_PROVIDERS=false LOCAL_WORKER=true
export ISOLATED_RENDERER_EGRESS_ATTESTED=false
# This generated secret is used only for temporary local test accounts.
export TEST_ACCOUNT_PASSWORD="$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(24))')"
export TEST_ADMIN_USERNAME=ui-admin TEST_READER_USERNAME=ui-reader
unset API_TOKEN
cd "$ROOT"
"$PYTHON" - <<'PY'
import os
from backend.db import init_db,SessionLocal
from backend.models import Workspace
from backend.auth import UserAccount,hash_password
init_db()
with SessionLocal() as s:
    s.add(Workspace(id='ui-test',name='UI integration test'));s.flush()
    for name,role in [('ui-admin','admin'),('ui-reader','reader')]:
        s.add(UserAccount(username=name,workspace_id='ui-test',role=role,password_hash=hash_password(os.environ['TEST_ACCOUNT_PASSWORD'])))
    s.commit()
PY
"$PYTHON" -m uvicorn backend.api:app --host 127.0.0.1 --port 8012 > "$TMPDIR_TEST/api.log" 2>&1 & API_PID=$!
cd "$WEB_DIR"
API_BASE_URL=http://127.0.0.1:8012 API_TOKEN=must-not-enable-anonymous-access SINGLE_USER_MODE=false node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3012 > "$TMPDIR_TEST/web.log" 2>&1 & WEB_PID=$!
ready=false
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:3012/api/health > /dev/null; then ready=true; break; fi
  if ! kill -0 "$API_PID" 2>/dev/null || ! kill -0 "$WEB_PID" 2>/dev/null; then break; fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  cat "$TMPDIR_TEST/api.log" "$TMPDIR_TEST/web.log"
  echo 'Production test servers failed readiness checks' >&2
  exit 1
fi
npm test
npm run test:integration
if [[ "${RUN_BROWSER_TESTS:-false}" == "true" ]]; then
  mkdir -p "$TMPDIR_TEST/browser-home"
  HOME="$TMPDIR_TEST/browser-home" npm run test:e2e
fi
printf '\nTemporary test data and logs: %s\n' "$TMPDIR_TEST"
