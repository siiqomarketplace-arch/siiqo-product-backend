"""
community.py — Community/Social Feed Routes
Handles: Posts, Likes, Comments, Follows, Feed
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db, limiter
from app.models.social import Post, PostLike, PostComment, Follow, PostView, UserActivity
from app.models.user import User
from app.models.communication import Notification
from datetime import datetime, timezone
from sqlalchemy import or_, and_, func
from app.utils.algolia_sync import sync_post_to_algolia, delete_post_from_algolia

community_bp = Blueprint('community', __name__, url_prefix='/api/community')


def utcnow():
    return datetime.now(timezone.utc)


def _track_activity(user_id: int, activity_type: str, points: int = 0, ref_id: int = None, ref_type: str = None):
    """Helper to track user activity for gamification"""
    activity = UserActivity(
        user_id=user_id,
        activity_type=activity_type,
        points_earned=points,
        reference_id=ref_id,
        reference_type=ref_type
    )
    db.session.add(activity)


def _send_notification(user_id: int, title: str, message: str, notif_type: str = 'COMMUNITY', ref_id: int = None):
    """Helper to send notifications"""
    notif = Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=notif_type,
        order_id=ref_id if notif_type == 'ORDER' else None
    )
    db.session.add(notif)


# ---------------------------------------------------------------------------
# MY POSTS — Get posts by the authenticated user
# ---------------------------------------------------------------------------

@community_bp.route('/my-posts', methods=['GET'])
@jwt_required()
@limiter.limit("30 per minute")
def get_my_posts():
    """Get posts created by the authenticated user"""
    user_id = get_jwt_identity()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)

    query = Post.query.filter_by(
        user_id=user_id, is_active=True
    ).order_by(Post.created_at.desc())

    paginated = query.paginate(page=page, per_page=per_page, error_out=False)

    posts = []
    for post in paginated.items:
        post_data = post.to_dict(include_author=True, include_stats=True)
        post_data['user_liked'] = False  # own posts — no need to check
        posts.append(post_data)

    return jsonify({
        'posts': posts,
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'pages': paginated.pages
    }), 200


# ---------------------------------------------------------------------------
# FEED — Get personalized feed
# ---------------------------------------------------------------------------

@community_bp.route('/feed', methods=['GET'])
@limiter.limit("60 per minute")
def get_feed():
    """Get personalized community feed (public or authenticated)"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)
    feed_type = request.args.get('type', 'all')  # all, following, nearby
    
    # Get current user if authenticated
    user_id = None
    user_city = None
    try:
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if user:
            user_city = user.city
    except:
        pass
    
    # Base query
    query = Post.query.filter_by(is_active=True)
    
    # Filter by feed type
    if feed_type == 'following' and user_id:
        # Get posts from users the current user follows
        following_ids = db.session.query(Follow.following_id).filter_by(follower_id=user_id).all()
        following_ids = [f[0] for f in following_ids]
        if following_ids:
            query = query.filter(Post.user_id.in_(following_ids))
        else:
            # No following, return empty
            return jsonify({'posts': [], 'total': 0, 'page': page, 'per_page': per_page}), 200
    
    elif feed_type == 'nearby' and user_city:
        # Get posts from same city
        query = query.filter_by(city=user_city)
    
    # Order by pinned first, then featured, then recent
    query = query.order_by(
        Post.is_pinned.desc(),
        Post.is_featured.desc(),
        Post.created_at.desc()
    )
    
    # Paginate
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    # Serialize posts
    posts = []
    for post in paginated.items:
        post_data = post.to_dict(include_author=True, include_stats=True)
        
        # Add user-specific data if authenticated
        if user_id:
            # Check if user liked this post
            liked = PostLike.query.filter_by(post_id=post.id, user_id=user_id).first()
            post_data['user_liked'] = bool(liked)
            post_data['user_reaction'] = liked.reaction_type if liked else None
        
        posts.append(post_data)
    
    return jsonify({
        'posts': posts,
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'pages': paginated.pages
    }), 200


# ---------------------------------------------------------------------------
# POSTS — Create, Read, Update, Delete
# ---------------------------------------------------------------------------

@community_bp.route('/posts', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def create_post():
    """Create a new post"""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'message': 'User not found'}), 404
    
    data = request.get_json() or {}
    
    # Validate
    content = (data.get('content') or '').strip()
    if not content or len(content) < 3:
        return jsonify({'message': 'Content must be at least 3 characters'}), 400
    
    if len(content) > 5000:
        return jsonify({'message': 'Content too long (max 5000 characters)'}), 400
    
    # Create post
    post = Post(
        user_id=user_id,
        post_type=data.get('post_type', 'GENERAL'),
        content=content,
        images=data.get('images', []),
        city=data.get('city') or user.city,
        state=data.get('state') or user.state,
        latitude=data.get('latitude'),
        longitude=data.get('longitude')
    )
    
    db.session.add(post)
    
    # Track activity
    _track_activity(user_id, 'POST_CREATED', points=10, ref_id=post.id, ref_type='POST')
    
    db.session.commit()
    
    # Sync to Algolia
    try:
        sync_post_to_algolia(post)
    except Exception as e:
        print(f"Failed to sync post to Algolia: {e}")
    
    return jsonify({
        'message': 'Post created successfully',
        'post': post.to_dict(include_author=True, include_stats=True)
    }), 201


@community_bp.route('/posts/<int:post_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_post(post_id):
    """Get a single post with details"""
    post = Post.query.filter_by(id=post_id, is_active=True).first()
    if not post:
        return jsonify({'message': 'Post not found'}), 404
    
    # Track view
    user_id = None
    try:
        user_id = get_jwt_identity()
    except:
        pass
    
    # Create view record (one per user per day)
    if user_id:
        today = datetime.now(timezone.utc).date()
        existing_view = PostView.query.filter(
            PostView.post_id == post_id,
            PostView.user_id == user_id,
            func.date(PostView.viewed_at) == today
        ).first()
        
        if not existing_view:
            view = PostView(post_id=post_id, user_id=user_id)
            db.session.add(view)
            post.views_count += 1
            db.session.commit()
    
    post_data = post.to_dict(include_author=True, include_stats=True)
    
    # Add user-specific data if authenticated
    if user_id:
        liked = PostLike.query.filter_by(post_id=post.id, user_id=user_id).first()
        post_data['user_liked'] = bool(liked)
        post_data['user_reaction'] = liked.reaction_type if liked else None
    
    return jsonify(post_data), 200


@community_bp.route('/posts/<int:post_id>', methods=['PATCH'])
@jwt_required()
def update_post(post_id):
    """Update a post (only by author)"""
    user_id = get_jwt_identity()
    post = Post.query.filter_by(id=post_id, user_id=user_id, is_active=True).first()
    
    if not post:
        return jsonify({'message': 'Post not found or unauthorized'}), 404
    
    data = request.get_json() or {}
    
    # Update allowed fields
    if 'content' in data:
        content = data['content'].strip()
        if len(content) < 3:
            return jsonify({'message': 'Content must be at least 3 characters'}), 400
        if len(content) > 5000:
            return jsonify({'message': 'Content too long'}), 400
        post.content = content
    
    if 'images' in data:
        post.images = data['images']
    
    if 'post_type' in data:
        post.post_type = data['post_type']
    
    post.updated_at = utcnow()
    db.session.commit()
    
    return jsonify({
        'message': 'Post updated successfully',
        'post': post.to_dict(include_author=True, include_stats=True)
    }), 200


@community_bp.route('/posts/<int:post_id>', methods=['DELETE'])
@jwt_required()
def delete_post(post_id):
    """Delete a post (soft delete)"""
    user_id = get_jwt_identity()
    post = Post.query.filter_by(id=post_id, user_id=user_id).first()
    
    if not post:
        return jsonify({'message': 'Post not found or unauthorized'}), 404
    
    # Soft delete
    post.is_active = False
    db.session.commit()
    
    # Remove from Algolia
    try:
        delete_post_from_algolia(post_id)
    except Exception as e:
        print(f"Failed to delete post from Algolia: {e}")
    
    return jsonify({'message': 'Post deleted successfully'}), 200


# ---------------------------------------------------------------------------
# LIKES — Like/Unlike posts
# ---------------------------------------------------------------------------

@community_bp.route('/posts/<int:post_id>/like', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def like_post(post_id):
    """Like or react to a post"""
    user_id = get_jwt_identity()
    post = Post.query.filter_by(id=post_id, is_active=True).first()
    
    if not post:
        return jsonify({'message': 'Post not found'}), 404
    
    data = request.get_json() or {}
    reaction_type = data.get('reaction_type', 'LIKE')
    
    # Check if already liked
    existing = PostLike.query.filter_by(post_id=post_id, user_id=user_id).first()
    
    if existing:
        # Update reaction type
        if existing.reaction_type != reaction_type:
            existing.reaction_type = reaction_type
            db.session.commit()
            return jsonify({'message': 'Reaction updated', 'reaction': reaction_type}), 200
        else:
            return jsonify({'message': 'Already reacted', 'reaction': reaction_type}), 200
    
    # Create new like
    like = PostLike(
        post_id=post_id,
        user_id=user_id,
        reaction_type=reaction_type
    )
    db.session.add(like)
    
    # Update counter
    post.likes_count += 1
    
    # Track activity
    _track_activity(user_id, 'LIKE_GIVEN', points=1, ref_id=post_id, ref_type='POST')
    
    # Notify post author (if not self-like)
    if post.user_id != user_id:
        _send_notification(
            post.user_id,
            'New Reaction',
            f'{User.query.get(user_id).full_name} reacted to your post',
            'COMMUNITY',
            post_id
        )
    
    db.session.commit()
    
    return jsonify({'message': 'Post liked', 'reaction': reaction_type}), 201


@community_bp.route('/posts/<int:post_id>/like', methods=['DELETE'])
@jwt_required()
def unlike_post(post_id):
    """Unlike a post"""
    user_id = get_jwt_identity()
    like = PostLike.query.filter_by(post_id=post_id, user_id=user_id).first()
    
    if not like:
        return jsonify({'message': 'Not liked'}), 404
    
    post = Post.query.get(post_id)
    if post:
        post.likes_count = max(0, post.likes_count - 1)
    
    db.session.delete(like)
    db.session.commit()
    
    return jsonify({'message': 'Post unliked'}), 200


# ---------------------------------------------------------------------------
# COMMENTS — Add, Read, Update, Delete
# ---------------------------------------------------------------------------

@community_bp.route('/posts/<int:post_id>/comments', methods=['GET'])
@limiter.limit("60 per minute")
def get_comments(post_id):
    """Get comments for a post"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)
    
    post = Post.query.filter_by(id=post_id, is_active=True).first()
    if not post:
        return jsonify({'message': 'Post not found'}), 404
    
    # Get top-level comments (no parent)
    query = PostComment.query.filter_by(
        post_id=post_id,
        parent_id=None,
        is_active=True
    ).order_by(PostComment.created_at.desc())
    
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    comments = [c.to_dict(include_author=True, include_replies=True) for c in paginated.items]
    
    return jsonify({
        'comments': comments,
        'total': paginated.total,
        'page': page,
        'per_page': per_page
    }), 200


@community_bp.route('/posts/<int:post_id>/comments', methods=['POST'])
@jwt_required()
@limiter.limit("20 per minute")
def add_comment(post_id):
    """Add a comment to a post"""
    user_id = get_jwt_identity()
    post = Post.query.filter_by(id=post_id, is_active=True).first()
    
    if not post:
        return jsonify({'message': 'Post not found'}), 404
    
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    
    if not content or len(content) < 1:
        return jsonify({'message': 'Comment cannot be empty'}), 400
    
    if len(content) > 1000:
        return jsonify({'message': 'Comment too long (max 1000 characters)'}), 400
    
    parent_id = data.get('parent_id')
    
    # Validate parent comment exists if provided
    if parent_id:
        parent = PostComment.query.filter_by(id=parent_id, post_id=post_id, is_active=True).first()
        if not parent:
            return jsonify({'message': 'Parent comment not found'}), 404
    
    # Create comment
    comment = PostComment(
        post_id=post_id,
        user_id=user_id,
        parent_id=parent_id,
        content=content
    )
    db.session.add(comment)
    
    # Update counter
    post.comments_count += 1
    
    # Track activity
    _track_activity(user_id, 'COMMENT_ADDED', points=5, ref_id=post_id, ref_type='POST')
    
    # Notify post author (if not self-comment)
    if post.user_id != user_id:
        _send_notification(
            post.user_id,
            'New Comment',
            f'{User.query.get(user_id).full_name} commented on your post',
            'COMMUNITY',
            post_id
        )
    
    # Notify parent comment author (if reply)
    if parent_id:
        parent = PostComment.query.get(parent_id)
        if parent and parent.user_id != user_id:
            _send_notification(
                parent.user_id,
                'New Reply',
                f'{User.query.get(user_id).full_name} replied to your comment',
                'COMMUNITY',
                post_id
            )
    
    db.session.commit()
    
    return jsonify({
        'message': 'Comment added',
        'comment': comment.to_dict(include_author=True, include_replies=False)
    }), 201


@community_bp.route('/comments/<int:comment_id>', methods=['PATCH'])
@jwt_required()
def update_comment(comment_id):
    """Update a comment (only by author)"""
    user_id = get_jwt_identity()
    comment = PostComment.query.filter_by(id=comment_id, user_id=user_id, is_active=True).first()
    
    if not comment:
        return jsonify({'message': 'Comment not found or unauthorized'}), 404
    
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    
    if not content or len(content) < 1:
        return jsonify({'message': 'Comment cannot be empty'}), 400
    
    if len(content) > 1000:
        return jsonify({'message': 'Comment too long'}), 400
    
    comment.content = content
    comment.updated_at = utcnow()
    db.session.commit()
    
    return jsonify({
        'message': 'Comment updated',
        'comment': comment.to_dict(include_author=True, include_replies=False)
    }), 200


@community_bp.route('/comments/<int:comment_id>', methods=['DELETE'])
@jwt_required()
def delete_comment(comment_id):
    """Delete a comment (soft delete)"""
    user_id = get_jwt_identity()
    comment = PostComment.query.filter_by(id=comment_id, user_id=user_id).first()
    
    if not comment:
        return jsonify({'message': 'Comment not found or unauthorized'}), 404
    
    # Soft delete
    comment.is_active = False
    
    # Update counter
    post = Post.query.get(comment.post_id)
    if post:
        post.comments_count = max(0, post.comments_count - 1)
    
    db.session.commit()
    
    return jsonify({'message': 'Comment deleted'}), 200


# ---------------------------------------------------------------------------
# FOLLOW — Follow/Unfollow users
# ---------------------------------------------------------------------------

@community_bp.route('/follow/<int:target_user_id>', methods=['POST'])
@jwt_required()
@limiter.limit("30 per minute")
def follow_user(target_user_id):
    """Follow a user"""
    user_id = get_jwt_identity()
    
    if user_id == target_user_id:
        return jsonify({'message': 'Cannot follow yourself'}), 400
    
    target = User.query.get(target_user_id)
    if not target:
        return jsonify({'message': 'User not found'}), 404
    
    # Check if already following
    existing = Follow.query.filter_by(follower_id=user_id, following_id=target_user_id).first()
    if existing:
        return jsonify({'message': 'Already following'}), 200
    
    # Create follow
    follow = Follow(follower_id=user_id, following_id=target_user_id)
    db.session.add(follow)
    
    # Track activity
    _track_activity(user_id, 'FOLLOW', points=2, ref_id=target_user_id, ref_type='USER')
    
    # Notify target user
    _send_notification(
        target_user_id,
        'New Follower',
        f'{User.query.get(user_id).full_name} started following you',
        'COMMUNITY'
    )
    
    db.session.commit()
    
    return jsonify({'message': 'User followed'}), 201


@community_bp.route('/follow/<int:target_user_id>', methods=['DELETE'])
@jwt_required()
def unfollow_user(target_user_id):
    """Unfollow a user"""
    user_id = get_jwt_identity()
    follow = Follow.query.filter_by(follower_id=user_id, following_id=target_user_id).first()
    
    if not follow:
        return jsonify({'message': 'Not following'}), 404
    
    db.session.delete(follow)
    db.session.commit()
    
    return jsonify({'message': 'User unfollowed'}), 200


@community_bp.route('/followers/<int:target_user_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_followers(target_user_id):
    """Get user's followers"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)
    
    query = Follow.query.filter_by(following_id=target_user_id).order_by(Follow.created_at.desc())
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    followers = []
    for follow in paginated.items:
        u = follow.follower
        display_name = u.full_name
        if u.role == 'VENDOR' and u.storefront:
            display_name = u.storefront.store_name or display_name
        followers.append({
            'id': u.id,
            'name': display_name,
            'role': u.role,
            'avatar': u.profile_pic,
            'followed_at': follow.created_at.isoformat() if follow.created_at else None
        })
    
    return jsonify({
        'followers': followers,
        'total': paginated.total,
        'page': page,
        'per_page': per_page
    }), 200


@community_bp.route('/following/<int:target_user_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_following(target_user_id):
    """Get users that a user is following"""
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)
    
    query = Follow.query.filter_by(follower_id=target_user_id).order_by(Follow.created_at.desc())
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    following = []
    for follow in paginated.items:
        u = follow.following
        display_name = u.full_name
        if u.role == 'VENDOR' and u.storefront:
            display_name = u.storefront.store_name or display_name
        following.append({
            'id': u.id,
            'name': display_name,
            'role': u.role,
            'avatar': u.profile_pic,
            'followed_at': follow.created_at.isoformat() if follow.created_at else None
        })
    
    return jsonify({
        'following': following,
        'total': paginated.total,
        'page': page,
        'per_page': per_page
    }), 200


# ---------------------------------------------------------------------------
# SEARCH — Search posts
# ---------------------------------------------------------------------------

@community_bp.route('/search', methods=['GET'])
@limiter.limit("30 per minute")
def search_posts():
    """Search posts by keyword"""
    q = (request.args.get('q') or '').strip()
    page = int(request.args.get('page', 1))
    per_page = min(int(request.args.get('per_page', 20)), 50)
    
    if not q or len(q) < 2:
        return jsonify({'posts': [], 'total': 0}), 200
    
    # Search in content
    query = Post.query.filter(
        Post.is_active == True,
        Post.content.ilike(f'%{q}%')
    ).order_by(Post.created_at.desc())
    
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    
    posts = [p.to_dict(include_author=True, include_stats=True) for p in paginated.items]
    
    return jsonify({
        'posts': posts,
        'total': paginated.total,
        'page': page,
        'per_page': per_page,
        'query': q
    }), 200
