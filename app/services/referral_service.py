import logging
from app.extensions import db
from app.models.user import User
from app.models.order import Order
from app.models.partnerships import Referral
from app.models.communication import Notification

def check_and_reward_referral_on_order_complete(order):
    """
    Checks if the buyer of this order was referred, and if this is their first completed order.
    If so, awards 1,000 Siiqo points to the referrer and marks the referral as QUALIFIED.
    """
    try:
        buyer_id = order.buyer_id
        # Find if this buyer was referred
        referral = Referral.query.filter_by(referred_id=buyer_id).first()
        if not referral:
            return
            
        # Check if the referral is already qualified
        if referral.status == 'QUALIFIED':
            return
            
        # Count completed orders of this buyer in the database
        completed_count = Order.query.filter_by(buyer_id=buyer_id, status='COMPLETED').count()
        
        # If completed_count is <= 1, it means this is the first completed order (including this one).
        if completed_count <= 1:
            referrer = db.session.get(User, referral.referrer_id)
            if referrer:
                # Award 1,000 Siiqo points
                reward_points = 1000.00
                referral.status = 'QUALIFIED'
                referral.reward_earned = float(referral.reward_earned or 0) + reward_points
                referrer.points_balance = float(referrer.points_balance or 0) + reward_points
                
                # Add notification for the referrer
                referred_name = order.buyer.first_name if (order.buyer and order.buyer.first_name) else "A user you referred"
                db.session.add(Notification(
                    user_id=referrer.id,
                    title="1,000 Referral Points Earned!",
                    message=f"Congratulations! You have earned 1,000 Siiqo points because {referred_name} completed their first transaction.",
                    type="SYSTEM"
                ))
                
                logging.info(f"[REFERRAL] Referrer ID {referrer.id} awarded {reward_points} points for first transaction of Buyer ID {buyer_id}")
    except Exception as e:
        logging.error(f"[REFERRAL ERROR] Failed to process referral points for Order #{order.id}: {e}")
