import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from app.extensions import db

def utcnow():
    return datetime.now(timezone.utc)

def get_vendor_auto_release_hours(vendor_id) -> int:
    """
    Get dynamic auto-release window based on vendor trust tier.
    PLATINUM: 24h
    GOLD: 48h
    SILVER: 72h (Standard)
    BRONZE: 96h (Extended)
    """
    from app.models.trust import VendorTrustProfile
    try:
        profile = VendorTrustProfile.query.filter_by(vendor_id=vendor_id).first()
        if not profile:
            return 72
        
        tier = str(profile.trust_tier).upper()
        if tier == 'PLATINUM':
            return 24
        elif tier == 'GOLD':
            return 48
        elif tier == 'SILVER':
            return 72
        elif tier == 'BRONZE':
            return 96
        return 72
    except Exception as e:
        logging.error(f"[TRUST] Error fetching auto-release hours for vendor {vendor_id}: {e}")
        return 72

def get_or_create_trust_profile(vendor_id):
    """
    Safely retrieves or initializes a vendor's trust profile to prevent NoneType errors.
    """
    from app.models.trust import VendorTrustProfile
    try:
        profile = VendorTrustProfile.query.filter_by(vendor_id=vendor_id).first()
        if not profile:
            profile = VendorTrustProfile(
                vendor_id=vendor_id,
                completion_score=Decimal('200.00'),   # Midpoint defaults
                satisfaction_score=Decimal('218.75'), # Bayesian avg (4.5 star equivalent)
                responsiveness_score=Decimal('100.00'),
                compliance_score=Decimal('25.00'),    # Email verified base
                community_score=Decimal('0.00'),
                total_trust_score=500,
                trust_tier='SILVER'
            )
            db.session.add(profile)
            db.session.commit()
        return profile
    except Exception as e:
        logging.error(f"[TRUST] Error in get_or_create_trust_profile for vendor {vendor_id}: {e}")
        db.session.rollback()
        return None

def calculate_completion_score(vendor_id) -> float:
    """Pillar 1: Escrow & POD Completion (Weight: 40% - Max 400 pts)"""
    from app.models.order import Order
    from app.models.escrow import EscrowTransaction
    try:
        # 1. Total orders handled by this vendor
        total_orders = Order.query.filter_by(vendor_id=vendor_id).count()
        if total_orders == 0:
            return 200.00 # default midpoint

        # 2. Completed orders (Escrow released or POD payment confirmed)
        completed_orders = Order.query.filter(
            Order.vendor_id == vendor_id,
            Order.status == 'COMPLETED'
        ).count()

        ratio = completed_orders / total_orders
        base_score = 400.0 * ratio

        # 3. Dispute deductions
        # Open disputes: Escrow transactions currently disputed
        pending_disputes = EscrowTransaction.query.join(Order).filter(
            Order.vendor_id == vendor_id,
            EscrowTransaction.status == 'DISPUTED'
        ).count()

        # Lost disputes: Escrow transactions refunded where a dispute was raised
        lost_disputes = EscrowTransaction.query.join(Order).filter(
            Order.vendor_id == vendor_id,
            EscrowTransaction.status == 'REFUNDED',
            EscrowTransaction.dispute_id.isnot(None)
        ).count()

        dispute_penalty = (pending_disputes * 50.0) + (lost_disputes * 100.0)
        final_score = max(0.0, min(400.0, base_score - dispute_penalty))
        return round(final_score, 2)
    except Exception as e:
        logging.error(f"[TRUST] Error calculating completion score for vendor {vendor_id}: {e}")
        return 200.00

def calculate_satisfaction_score(vendor_id) -> float:
    """Pillar 2: Customer Satisfaction (Weight: 25% - Max 250 pts) using Bayesian Average"""
    from app.models.community import Review
    try:
        # Fetch approved reviews for this vendor
        vendor_reviews = Review.query.filter_by(vendor_id=vendor_id, is_approved=True).all()
        v = len(vendor_reviews)
        
        # Bayesian parameters
        C = 5.0    # Baseline review volume constant
        S = 4.5    # Siiqo baseline global average rating
        
        if v == 0:
            # If no reviews, assume global baseline
            bayesian_avg = S
        else:
            total_rating = sum(r.vendor_rating for r in vendor_reviews)
            m = total_rating / v
            bayesian_avg = ((v * m) + (C * S)) / (v + C)

        # Scale Bayesian rating (1.0 to 5.0) to points (0.0 to 250.0)
        points = 250.0 * ((bayesian_avg - 1.0) / 4.0)
        return round(max(0.0, min(250.0, points)), 2)
    except Exception as e:
        logging.error(f"[TRUST] Error calculating satisfaction score for vendor {vendor_id}: {e}")
        return 218.75

def calculate_responsiveness_score(vendor_id) -> float:
    """Pillar 3: Chat & Negotiation Responsiveness (Weight: 15% - Max 150 pts)"""
    from app.models.negotiation import NegotiationRequest, NegotiationHistory
    try:
        # 1. Median Response Time (Max 90 pts)
        negotiations = NegotiationRequest.query.filter_by(vendor_id=vendor_id).all()
        response_deltas = []

        for neg in negotiations:
            # Sort history chronologically
            history = sorted(neg.history, key=lambda x: x.created_at)
            
            last_buyer_time = None
            for step in history:
                is_buyer = (step.actor_id == neg.buyer_id)
                if is_buyer:
                    last_buyer_time = step.created_at
                elif last_buyer_time and not is_buyer:
                    # Vendor response detected
                    delta = (step.created_at - last_buyer_time).total_seconds()
                    response_deltas.append(delta)
                    last_buyer_time = None # reset to prevent counting double

        if not response_deltas:
            # Default speed: ~4 hours median response time (70 pts)
            speed_score = 70.0
        else:
            response_deltas.sort()
            n = len(response_deltas)
            if n % 2 == 1:
                median_seconds = response_deltas[n // 2]
            else:
                median_seconds = (response_deltas[n // 2 - 1] + response_deltas[n // 2]) / 2.0
            
            median_hours = median_seconds / 3600.0
            
            if median_hours < 2.0:
                speed_score = 90.0
            elif median_hours < 6.0:
                speed_score = 75.0
            elif median_hours < 12.0:
                speed_score = 55.0
            elif median_hours < 24.0:
                speed_score = 35.0
            else:
                speed_score = 10.0

        # 2. Negotiation Conversion Rate (Max 60 pts)
        total_negotiations = NegotiationRequest.query.filter_by(vendor_id=vendor_id).count()
        if total_negotiations == 0:
            conversion_score = 30.0 # default midpoint
        else:
            accepted_negotiations = NegotiationRequest.query.filter_by(
                vendor_id=vendor_id,
                status='ACCEPTED'
            ).count()
            conversion_ratio = accepted_negotiations / total_negotiations
            conversion_score = 60.0 * conversion_ratio

        return round(speed_score + conversion_score, 2)
    except Exception as e:
        logging.error(f"[TRUST] Error calculating responsiveness score for vendor {vendor_id}: {e}")
        return 100.00

def calculate_compliance_score(vendor_id) -> float:
    """Pillar 4: Profile Verification & Compliance (Weight: 15% - Max 150 pts)"""
    from app.models.user import User, Storefront
    from app.models.withdrawal import VendorBankAccount
    try:
        user = db.session.get(User, vendor_id)
        if not user:
            return 0.0

        score = 0.0
        
        # Email verified (25 pts)
        if user.is_verified:
            score += 25.0
            
        # Storefront details
        sf = user.storefront
        if sf:
            # Vetted/Verified by admin (50 pts)
            if sf.is_verified:
                score += 50.0
            # CAC Business Registration number provided (50 pts)
            if sf.cac_reg:
                score += 50.0

        # Linked bank account verified via Paystack (25 pts)
        bank_acc = VendorBankAccount.query.filter_by(vendor_id=vendor_id, is_verified=True).first()
        if bank_acc:
            score += 25.0

        return round(score, 2)
    except Exception as e:
        logging.error(f"[TRUST] Error calculating compliance score for vendor {vendor_id}: {e}")
        return 25.00

def calculate_community_score(vendor_id) -> float:
    """Pillar 5: Community Engagement & Social Standing (Weight: 5% - Max 50 pts)"""
    from app.models.social import Post, PostLike, Follow
    try:
        # 1. Community Posts Published (5 pts each, max 25)
        post_count = Post.query.filter_by(user_id=vendor_id).count()
        post_score = min(25.0, post_count * 5.0)

        # 2. Likes & Followers received (1 pt each, max 25)
        likes_count = PostLike.query.join(Post).filter(Post.user_id == vendor_id).count()
        followers_count = Follow.query.filter_by(following_id=vendor_id).count()
        
        social_score = min(25.0, float(likes_count + followers_count))

        return round(post_score + social_score, 2)
    except Exception as e:
        logging.error(f"[TRUST] Error calculating community score for vendor {vendor_id}: {e}")
        return 0.00

def recalculate_vendor_trust(vendor_id, reason="Recalculation"):
    """
    Triggers recalculation for a vendor, logs score changes, and updates database.
    """
    from app.models.trust import VendorTrustProfile, TrustScoreHistory
    try:
        profile = get_or_create_trust_profile(vendor_id)
        if not profile:
            return None

        # Calculate new sub-scores
        s1 = calculate_completion_score(vendor_id)
        s2 = calculate_satisfaction_score(vendor_id)
        s3 = calculate_responsiveness_score(vendor_id)
        s4 = calculate_compliance_score(vendor_id)
        s5 = calculate_community_score(vendor_id)

        # Sum total
        new_score = int(round(s1 + s2 + s3 + s4 + s5))
        new_score = max(0, min(1000, new_score))

        # Assign tier
        if new_score >= 900:
            new_tier = 'PLATINUM'
        elif new_score >= 700:
            new_tier = 'GOLD'
        elif new_score >= 400:
            new_tier = 'SILVER'
        else:
            new_tier = 'BRONZE'

        old_score = profile.total_trust_score

        # Save updates
        profile.completion_score = Decimal(str(s1))
        profile.satisfaction_score = Decimal(str(s2))
        profile.responsiveness_score = Decimal(str(s3))
        profile.compliance_score = Decimal(str(s4))
        profile.community_score = Decimal(str(s5))
        profile.total_trust_score = new_score
        profile.trust_tier = new_tier
        profile.last_recalculated = utcnow()

        # Log to history if score changed
        if old_score != new_score:
            db.session.add(TrustScoreHistory(
                vendor_id=vendor_id,
                score_before=old_score,
                score_after=new_score,
                change_reason=reason
            ))

        db.session.commit()
        return profile
    except Exception as e:
        logging.error(f"[TRUST] Critical error recalculating trust for vendor {vendor_id}: {e}")
        db.session.rollback()
        return None
