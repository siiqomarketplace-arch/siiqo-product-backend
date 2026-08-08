-- Admin Audit Log Table
-- Run this SQL manually or let Flask-Migrate handle it

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id SERIAL PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    admin_email VARCHAR(255) NOT NULL,
    admin_role VARCHAR(50) NOT NULL,
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50) NOT NULL,
    resource_id VARCHAR(100) NOT NULL,
    details JSONB,
    ip_address VARCHAR(45) NOT NULL,
    user_agent TEXT,
    request_id VARCHAR(50),
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes for fast querying
CREATE INDEX IF NOT EXISTS idx_audit_admin_action ON admin_audit_logs(admin_id, action);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON admin_audit_logs(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON admin_audit_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ip ON admin_audit_logs(ip_address);
CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_logs(action);

-- Comment for documentation
COMMENT ON TABLE admin_audit_logs IS 'Immutable audit trail for all admin actions. Used for security investigations, compliance, and dispute resolution.';
