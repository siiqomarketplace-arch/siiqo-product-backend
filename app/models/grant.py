from app.extensions import db
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import ARRAY


def utcnow():
    return datetime.now(timezone.utc)


class Grant(db.Model):
    """
    Grant model for managing funding opportunities.
    Stores comprehensive grant information for SEO-optimized grants hub.
    """
    __tablename__ = 'grants'

    id = db.Column(db.Integer, primary_key=True)
    
    # Basic Information
    slug = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.String(100), nullable=False)  # e.g., "₦5,000,000" or "₦100,000 - ₦5,000,000"
    
    # Categorization - stored as PostgreSQL array
    category = db.Column(ARRAY(db.String(50)), nullable=False)  # ["Women", "Tech", "Startups"]
    country = db.Column(db.String(100), nullable=False)  # "Nigeria" or "Africa"
    
    # Grant Details
    eligibility = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, nullable=False)  # Full description (markdown supported)
    application_tips = db.Column(db.Text, nullable=True)  # Optional tips for applying
    
    # Dates and Status
    deadline = db.Column(db.String(100), nullable=False)  # ISO date string or "Rolling"
    status = db.Column(db.String(20), nullable=False, default='upcoming')  # 'open', 'upcoming', 'closed'
    last_verified = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    
    # URLs and Media
    official_url = db.Column(db.String(500), nullable=False)  # Application URL
    cover_image = db.Column(db.String(255), nullable=True)
    
    # Display Options
    featured = db.Column(db.Boolean, default=False, index=True)
    is_published = db.Column(db.Boolean, default=True)
    
    # SEO Fields
    meta_title = db.Column(db.String(255), nullable=True)
    meta_description = db.Column(db.String(500), nullable=True)
    
    # Admin tracking
    admin_author_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'), nullable=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    admin_author = db.relationship('AdminUser', backref='grants')
    
    def __repr__(self):
        return f'<Grant {self.id}: {self.name}>'
    
    def to_dict(self):
        """Convert grant to dictionary for JSON responses."""
        return {
            'id': self.id,
            'slug': self.slug,
            'name': self.name,
            'amount': self.amount,
            'category': self.category,
            'country': self.country,
            'eligibility': self.eligibility,
            'description': self.description,
            'application_tips': self.application_tips,
            'deadline': self.deadline,
            'status': self.status,
            'last_verified': self.last_verified.isoformat() if self.last_verified else None,
            'official_url': self.official_url,
            'cover_image': self.cover_image,
            'featured': self.featured,
            'is_published': self.is_published,
            'meta_title': self.meta_title,
            'meta_description': self.meta_description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
    
    @staticmethod
    def get_valid_categories():
        """Return list of valid grant categories."""
        return [
            'Women',
            'Youth',
            'Startups',
            'Small Business',
            'Tech',
            'Agriculture',
            'Education',
            'Healthcare',
            'Creative Arts',
            'Environment',
            'Manufacturing',
            'Export',
            'Innovation',
            'Research',
        ]
    
    @staticmethod
    def get_valid_statuses():
        """Return list of valid grant statuses."""
        return ['open', 'upcoming', 'closed']
    
    @staticmethod
    def get_valid_countries():
        """Return list of valid countries."""
        return ['Nigeria', 'Africa', 'Ghana', 'Kenya', 'South Africa', 'Rwanda', 'International']
