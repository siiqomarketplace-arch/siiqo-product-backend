from app.extensions import db
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


class BlogAuthor(db.Model):
    __tablename__ = 'blog_authors'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    title = db.Column(db.String(150), nullable=True)  # e.g., "Senior E-Commerce Analyst"
    bio = db.Column(db.Text, nullable=True)
    avatar = db.Column(db.String(255), nullable=True)
    twitter_handle = db.Column(db.String(100), nullable=True)
    linkedin_url = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Article(db.Model):
    __tablename__ = 'articles'

    id = db.Column(db.Integer, primary_key=True)
    # author_id points to admin_users — nullable so system articles work too
    admin_author_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'), nullable=True)
    author_id = db.Column(db.Integer, db.ForeignKey('blog_authors.id'), nullable=True)

    title = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    excerpt = db.Column(db.String(500), nullable=True)

    cover_image = db.Column(db.String(255), nullable=True)
    category = db.Column(db.String(100), nullable=True)
    sub_category = db.Column(db.String(100), nullable=True)
    author_name = db.Column(db.String(100), nullable=True)
    is_published = db.Column(db.Boolean, default=False)

    # SEO
    meta_title = db.Column(db.String(255), nullable=True)
    meta_description = db.Column(db.String(500), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # Relationships
    admin_author = db.relationship('AdminUser', backref='articles')
    author = db.relationship('BlogAuthor', backref='articles')
    comments = db.relationship('Comment', back_populates='article', cascade="all, delete-orphan")


class Comment(db.Model):
    __tablename__ = 'comments'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    content = db.Column(db.Text, nullable=False)
    is_approved = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    article = db.relationship('Article', back_populates='comments')
    user = db.relationship('User')


class Review(db.Model):
    """Proper review model — replaces the Notification hack."""
    __tablename__ = 'reviews'
    __table_args__ = (
        db.UniqueConstraint('order_id', 'buyer_id', name='uq_review_order_buyer'),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)

    vendor_rating = db.Column(db.Integer, nullable=False)   # 1–5
    product_rating = db.Column(db.Integer, nullable=True)   # 1–5
    review_text = db.Column(db.Text, nullable=True)

    is_approved = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Relationships
    buyer = db.relationship('User', foreign_keys=[buyer_id])
    vendor = db.relationship('User', foreign_keys=[vendor_id])
    order = db.relationship('Order')
    product = db.relationship('Product', back_populates='reviews')
