-- Row-level security for Postgres: a second lock behind the API's own hospital filter.
--
-- Run once after the tables exist (as the database owner):
--     psql "$DATABASE_URL_OWNER" -v app_password=change-me -f sql/rls.sql
-- Then point the API at the restricted role:
--     DATABASE_URL=postgresql+psycopg://aeternix_app:change-me@host/db
-- The API sets app.hospital_id on every request (app/db.py: scope_session). The worker sets
-- app.bypass_rls = 'on' because it works across hospitals.

SELECT format('CREATE ROLE aeternix_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aeternix_app') \gexec

GRANT USAGE ON SCHEMA public TO aeternix_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aeternix_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aeternix_app;

-- The audit trail is append-only for the application role.
REVOKE UPDATE, DELETE ON audit_log FROM aeternix_app;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['payer','claim_case','claim','document','check_result','payer_query','decision',
                           'payment_advice','bank_txn','allocation','escalation','clock','status_snapshot',
                           'inbound_email','desk_activity','audit_log']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS hospital_isolation ON %I', t);
    EXECUTE format($p$CREATE POLICY hospital_isolation ON %I
      USING (hospital_id = current_setting('app.hospital_id', true)
             OR current_setting('app.bypass_rls', true) = 'on')
      WITH CHECK (hospital_id = current_setting('app.hospital_id', true)
             OR current_setting('app.bypass_rls', true) = 'on')$p$, t);
  END LOOP;
END $$;

-- Sign-ins by Aeternix staff write audit rows that belong to no hospital.
DROP POLICY IF EXISTS hospital_isolation ON audit_log;
CREATE POLICY hospital_isolation ON audit_log
  USING (hospital_id = current_setting('app.hospital_id', true) OR current_setting('app.bypass_rls', true) = 'on')
  WITH CHECK (hospital_id IS NULL OR hospital_id = current_setting('app.hospital_id', true)
              OR current_setting('app.bypass_rls', true) = 'on');

-- The shared query library may hold rows with no hospital (Aeternix-wide replies).
ALTER TABLE query_library ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_library FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS hospital_isolation ON query_library;
CREATE POLICY hospital_isolation ON query_library
  USING (hospital_id IS NULL OR hospital_id = current_setting('app.hospital_id', true)
         OR current_setting('app.bypass_rls', true) = 'on')
  WITH CHECK (hospital_id = current_setting('app.hospital_id', true)
         OR current_setting('app.bypass_rls', true) = 'on');
