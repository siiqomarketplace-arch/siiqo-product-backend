"""
Account deletion by anonymisation (Phase 0B).

Deleting an account scrubs the person's details but keeps the business
record: orders, escrow, ledger, reviews, subscriptions and trust evidence
stay, so buyers keep their order history and the trust record can't be
erased by deleting an account.
"""
import secrets

from app.extensions import db

OPEN_ESCROW_STATUSES = ("IN_ESCROW", "SHIPPED", "DELIVERED", "DISPUTED")


class AccountDeletionBlocked(Exception):
    pass


def open_orders_count(user_id):
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    return (EscrowTransaction.query.join(Order, EscrowTransaction.order_id == Order.id)
            .filter((Order.buyer_id == user_id) | (Order.vendor_id == user_id),
                    EscrowTransaction.status.in_(OPEN_ESCROW_STATUSES))
            .count())


def anonymise_user(user, *, force=False):
    """Scrub personal data and disable the login. Refuses while orders are
    still open (in escrow, shipped, delivered or disputed) unless `force`."""
    from app.models.communication import Message, Notification
    from app.models.order import Order
    from app.models.withdrawal import VendorBankAccount, VendorCryptoWallet

    if not force and open_orders_count(user.id):
        raise AccountDeletionBlocked("Finish or cancel your open orders before deleting your account.")

    uid = user.id
    user.email = f"deleted-{uid}@deleted.siiqo.invalid"
    user.set_password(secrets.token_urlsafe(32))  # unusable: nobody knows it
    user.first_name, user.last_name = "Deleted", "User"
    user.phone = None
    user.profile_pic = None
    user.nin = None
    user.telegram_id = None
    user.telegram_notification_prefs = {}
    user.city = user.state = None
    user.reset_otp = None
    user.otp_expiry = None
    user.is_subscribed_to_broadcasts = False
    user.is_active = False

    sf = user.storefront
    if sf is not None:
        # The store URL stays reserved so old links never lead to someone else's store.
        sf.is_published = False
        sf.store_name = "Closed store"
        sf.store_description = None
        sf.store_logo = sf.banner_url = None
        sf.address = sf.city = sf.state = None
        sf.phone = sf.website = None
        sf.social_links = {}
        sf.working_hours = None
        sf.bank_code = sf.account_number = sf.account_name = None
        sf.custom_domain = None
        sf.cac_reg = None
        sf.nin_document_url = sf.cac_document_url = None
        sf.meta_pixel_id = sf.tiktok_pixel_id = sf.ga4_measurement_id = None

    VendorBankAccount.query.filter_by(vendor_id=uid).delete(synchronize_session=False)
    VendorCryptoWallet.query.filter_by(vendor_id=uid).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=uid).delete(synchronize_session=False)
    Message.query.filter((Message.sender_id == uid) | (Message.receiver_id == uid)).delete(
        synchronize_session=False)

    # Orders stay; the buyer's contact details on them go.
    for order in Order.query.filter_by(buyer_id=uid).all():
        order.buyer_name = "Deleted user"
        order.buyer_email = None
        order.buyer_phone = None
        order.delivery_name = order.delivery_phone = None
        order.delivery_address = order.delivery_city = order.delivery_state = None
    db.session.commit()
