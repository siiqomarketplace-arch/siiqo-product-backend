"""
audit.py — Admin audit logging for compliance and forensics

Tracks all sensitive admin operations:
- User modifications (status changes, deletions)
- Escrow operations (releases, refunds)
- Category management
- Storefront verifications
"""
from datetime import datetime, timezone
from app.extensions import db


class AdminAuditLog(db.Model):
    """
    Immutable audit trail for all admin actions.
    Used for security investigations, compliance, and dispute resolution.
    """
    __tablename__ = 'admin_audit_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Who performed the action
    admin_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'), nullable=False)
    admin_email = db.Column(db.String(255), nullable=False)  # Denormalized for immutability
    admin_role = db.Column(db.String(50), nullable=False)     # Denormalized
    
    # What action was performed
    action = db.Column(db.String(100), nullable=False, index=True)
    # Examples: USER_DELETE, USER_STATUS_UPDATE, ESCROW_RELEASE, 
    #           ESCROW_REFUND, CATEGORY_CREATE, CATEGORY_DELETE
    
    # What resource was affected
    resource_type = db.Column(db.String(50), nullable=False, index=True)
    # Examples: User, Storefront, EscrowTransaction, Category, Order
    
    resource_id = db.Column(db.String(100), nullable=False, index=True)
    # ID of the affected resource (can be string or int)
    
    # Context & metadata
    details = db.Column(db.JSON)  # Additional context (old vs new values, reason, etc.)
    
    # Request metadata for forensics
    ip_address = db.Column(db.String(45), nullable=False, index=True)  # IPv6 support
    user_agent = db.Column(db.Text)
    request_id = db.Column(db.String(50))  # For tracing across logs
    
    # Timestamps
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    
    # Relationships
    admin = db.relationship('AdminUser', backref='audit_logs')
    
    def __repr__(self):
        return f"<AdminAuditLog {self.id}: {self.action} by {self.admin_email} on {self.resource_type}#{self.resource_id}>"
    
    def to_dict(self):
        return {
            'id': self.id,
            'admin_id': self.admin_id,
            'admin_email': self.admin_email,
            'admin_role': self.admin_role,
            'action': self.action,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'details': self.details,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }
    
    @staticmethod
    def log_action(admin, action: str, resource_type: str, resource_id, details=None, ip_address=None, user_agent=None):
        """
        Create an audit log entry.
        
        Args:
            admin: AdminUser object
            action: Action performed (e.g., 'USER_DELETE')
            resource_type: Type of resource (e.g., 'User')
            resource_id: ID of affected resource
            details: Optional dict with additional context
            ip_address: IP address of request
            user_agent: User-Agent header
        """
        from flask import request as flask_request
        
        # Auto-detect IP and User-Agent if not provided
        if ip_address is None:
            try:
                from flask_limiter.util import get_remote_address
                ip_address = get_remote_address()
            except:
                ip_address = flask_request.remote_addr if flask_request else 'unknown'
        
        if user_agent is None:
            user_agent = flask_request.headers.get('User-Agent', 'unknown') if flask_request else 'unknown'
        
        log = AdminAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            admin_role=admin.role,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        
        db.session.add(log)
        db.session.commit()
        
        return log


# Index for fast querying
db.Index('idx_audit_admin_action', AdminAuditLog.admin_id, AdminAuditLog.action)
db.Index('idx_audit_resource', AdminAuditLog.resource_type, AdminAuditLog.resource_id)
db.Index('idx_audit_timestamp', AdminAuditLog.timestamp.desc())
