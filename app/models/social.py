"""
social.py — Social/Community Models
Handles: Posts, Likes, Comments, Follows, User Activity
"""
from app.extensions import db
from datetime import datetime, timezone
from sqlalchemy import Index


def utcnow():
    return datetime.now(timezone.utc)


class Post(db.Model):
    """User-generated posts for community feed"""
    __tablename__ = 'posts'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    # Content
    post_type = db.Column(db.String(50), default='GENERAL', index=True)
    # GENERAL, DEAL, NEWS, QUESTION, ANNOUNCEMENT, PRODUCT_LAUNCH, EVENT
    content = db.Column(db.Text, nullable=False)
    images = db.Column(db.JSON, nullable=True)  # Array of image URLs
    
    # Location (for hyper-local feed)
    city = db.Column(db.String(100), nullable=True, index=True)
    state = db.Column(db.String(100), nullable=True, index=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    
    # Metadata
    is_pinned = db.Column(db.Boolean, default=False, index=True)
    is_featured = db.Column(db.Boolean, default=False, index=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    
    # Engagement metrics (denormalized for performance)
    likes_count = db.Column(db.Integer, default=0)
    comments_count = db.Column(db.Integer, default=0)
    shares_count = db.Column(db.Integer, default=0)
    views_count = db.Column(db.Integer, default=0)
    
    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    author = db.relationship('User', backref='posts', foreign_keys=[user_id])
    likes = db.relationship('PostLike', back_populates='post', cascade='all, delete-orphan', lazy='dynamic')
    comments = db.relationship('PostComment', back_populates='post', cascade='all, delete-orphan', lazy='dynamic')
    
    # Composite indexes for common queries
    __table_args__ = (
        Index('idx_post_feed', 'is_active', 'created_at'),
        Index('idx_post_location', 'city', 'is_active', 'created_at'),
        Index('idx_post_type', 'post_type', 'is_active', 'created_at'),
    )
    
    def to_dict(self, include_author=True, include_stats=True):
        """Convert post to dictionary"""
        data = {
            'id': self.id,
            'user_id': self.user_id,
            'post_type': self.post_type,
            'content': self.content,
            'images': self.images or [],
            'city': self.city,
            'state': self.state,
            'is_pinned': self.is_pinned,
            'is_featured': self.is_featured,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        
        if include_author and self.author:
            # Display name: storefront name for vendors, full name for buyers.
            # Never expose raw email — full_name falls back to a friendly
            # username derived from the email prefix (e.g. "john.doe" → "John Doe")
            # so the email itself is never shown.
            author_obj = self.author
            display_name = author_obj.full_name  # always safe — never empty, never raw email
            if author_obj.role == 'VENDOR' and author_obj.storefront:
                display_name = author_obj.storefront.store_name or display_name
            data['author'] = {
                'id': author_obj.id,
                'name': display_name,
                'role': author_obj.role,
                'avatar': author_obj.profile_pic,
            }
        
        if include_stats:
            data['stats'] = {
                'likes': self.likes_count,
                'comments': self.comments_count,
                'shares': self.shares_count,
                'views': self.views_count,
            }
        
        return data


class PostLike(db.Model):
    """Likes/reactions on posts"""
    __tablename__ = 'post_likes'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    reaction_type = db.Column(db.String(20), default='LIKE')
    # LIKE, LOVE, HAHA, WOW, SAD, ANGRY
    
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    
    # Relationships
    post = db.relationship('Post', back_populates='likes')
    user = db.relationship('User')
    
    # Unique constraint: one reaction per user per post
    __table_args__ = (
        db.UniqueConstraint('post_id', 'user_id', name='unique_post_like'),
        Index('idx_post_likes', 'post_id', 'user_id'),
    )


class PostComment(db.Model):
    """Comments on posts (supports nested replies)"""
    __tablename__ = 'post_comments'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('post_comments.id'), nullable=True, index=True)
    
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    
    # Engagement
    likes_count = db.Column(db.Integer, default=0)
    
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    
    # Relationships
    post = db.relationship('Post', back_populates='comments')
    author = db.relationship('User', backref='post_comments')
    parent = db.relationship('PostComment', remote_side=[id], backref='replies')
    
    def to_dict(self, include_author=True, include_replies=False):
        """Convert comment to dictionary"""
        data = {
            'id': self.id,
            'post_id': self.post_id,
            'user_id': self.user_id,
            'parent_id': self.parent_id,
            'content': self.content,
            'likes_count': self.likes_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        
        if include_author and self.author:
            author_obj = self.author
            display_name = author_obj.full_name
            if author_obj.role == 'VENDOR' and author_obj.storefront:
                display_name = author_obj.storefront.store_name or display_name
            data['author'] = {
                'id': author_obj.id,
                'name': display_name,
                'role': author_obj.role,
                'avatar': author_obj.profile_pic,
            }
        
        if include_replies:
            data['replies'] = [r.to_dict(include_author=True, include_replies=False) 
                              for r in self.replies if r.is_active]
        
        return data


class Follow(db.Model):
    """User following relationships"""
    __tablename__ = 'follows'

    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    following_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    
    # Relationships
    follower = db.relationship('User', foreign_keys=[follower_id], backref='following')
    following = db.relationship('User', foreign_keys=[following_id], backref='followers')
    
    # Unique constraint: can't follow same user twice
    __table_args__ = (
        db.UniqueConstraint('follower_id', 'following_id', name='unique_follow'),
        Index('idx_follower', 'follower_id', 'created_at'),
        Index('idx_following', 'following_id', 'created_at'),
    )


class PostView(db.Model):
    """Track post views for analytics"""
    __tablename__ = 'post_views'

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    
    # Track anonymous views too
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    
    viewed_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    
    # Unique constraint: one view per user per post per day
    __table_args__ = (
        Index('idx_post_view', 'post_id', 'user_id', 'viewed_at'),
    )


class UserActivity(db.Model):
    """Track user activity for gamification and analytics"""
    __tablename__ = 'user_activities'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    activity_type = db.Column(db.String(50), nullable=False, index=True)
    # POST_CREATED, COMMENT_ADDED, LIKE_GIVEN, FOLLOW, PURCHASE, REVIEW, etc.
    
    points_earned = db.Column(db.Integer, default=0)
    reference_id = db.Column(db.Integer, nullable=True)  # ID of related object
    reference_type = db.Column(db.String(50), nullable=True)  # POST, COMMENT, ORDER, etc.
    
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)
    
    # Relationships
    user = db.relationship('User', backref='activities')
    
    __table_args__ = (
        Index('idx_user_activity', 'user_id', 'activity_type', 'created_at'),
    )
