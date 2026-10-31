#!/bin/sh
# Create tables as the database owner, switch on row-level security, and add the demo hospital if asked.
# Needs: DATABASE_URL (owner, SQLAlchemy form), DB_APP_PASSWORD, optional DEMO_MODE=true.
set -eu
python -m app.manage init
PSQL_URL=$(echo "$DATABASE_URL" | sed 's#^postgresql+psycopg://#postgresql://#')
psql "$PSQL_URL" -q -v ON_ERROR_STOP=1 -v app_password="$DB_APP_PASSWORD" -f sql/rls.sql
echo "Row-level security on; the API and worker connect as aeternix_app."
if [ "${DEMO_MODE:-false}" = "true" ]; then
  python -m app.manage seed-demo
fi
