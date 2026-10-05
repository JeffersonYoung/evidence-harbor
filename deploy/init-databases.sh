#!/bin/sh
set -eu
: "${RESEARCH_DB_PASSWORD:?RESEARCH_DB_PASSWORD is required}"
: "${TEMPORAL_DB_PASSWORD:?TEMPORAL_DB_PASSWORD is required}"
# psql quoted variables escape operator-supplied passwords as SQL literals.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=research_password="$RESEARCH_DB_PASSWORD" \
  --set=temporal_password="$TEMPORAL_DB_PASSWORD" <<'SQL'
CREATE ROLE research LOGIN PASSWORD :'research_password';
CREATE DATABASE research OWNER research;
CREATE ROLE temporal LOGIN PASSWORD :'temporal_password';
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
\connect research
CREATE EXTENSION IF NOT EXISTS vector;
SQL
