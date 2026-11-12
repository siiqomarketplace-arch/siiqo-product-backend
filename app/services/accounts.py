"""
Account deletion by anonymisation (Phase 0B).

Deleting an account scrubs the person's details but keeps the business
record: orders, escrow, ledger, reviews, subscriptions and trust evidence
stay, so buyers keep their order history and the trust record can't be
erased by deleting an account.
"""
import secrets
from datetime import datetime, timezone

from app.extensions import db

OPEN_ESCROW_STATUSES = ("IN_ESCROW", "SHIPPED", "DELIVERED", "DISPUTED")

# An anonymised user's email is deleted-<id>@deleted.siiqo.invalid.
DELETED_EMAIL_PREFIX = "deleted-"
DELETED_EMAIL_DOMAIN = "@deleted.siiqo.invalid"
DELETED_EMAIL_LIKE = f"{DELETED_EMAIL_PREFIX}%{DELETED_EMAIL_DOMAIN}"


def is_deleted_email(email):
    email = email or ""
    return email.startswith(DELETED_EMAIL_PREFIX) and email.endswith(DELETED_EMAIL_DOMAIN)


def is_deleted_user(user):
    return is_deleted_email(getattr(user, "email", None))


class AccountDeletionBlocked(Exception):
    pass


def open_orders_count(user_id):
    from app.models.escrow import EscrowTransaction
    from app.models.order import Order
    return (EscrowTransaction.query.join(Order, EscrowTransaction.order_id == Order.id)
            .filter((Order.buyer_id == user_id) | (Order.vendor_id == user_id),
                    EscrowTransaction.status.in_(OPEN_ESCROW_STATUSES))
            .count())


def _remove_bank_accounts(uid):
    """Payout history points at the bank account it was paid to
    (withdrawals.bank_account_id, NOT NULL). An account with payout history
    is kept as a scrubbed row so that history stays valid; the rest are
    deleted. Deleting a referenced row fails on Postgres."""
    from app.models.withdrawal import VendorBankAccount, Withdrawal
    for account in VendorBankAccount.query.filter_by(vendor_id=uid).all():
        has_history = db.session.query(Withdrawal.id).filter_by(bank_account_id=account.id).first()
        if has_history is None:
            db.session.delete(account)
            continue
        digits = "".join(ch for ch in (account.account_number or "") if ch.isdigit())
        account.account_number = f"******{digits[-4:]}" if len(digits) >= 4 else "deleted"
        account.account_name = "Deleted user"
        # Gateway handles for this person's account; bank name and code stay.
        account.recipient_code = None
        account.paystack_subaccount_code = None
        account.flw_subaccount_id = None
        account.is_verified = False
        account.is_default = False


def anonymise_user(user, *, force=False, commit=True):
    """Scrub personal data and disable the login. Refuses while orders are
    still open (in escrow, shipped, delivered or disputed) unless `force`.

    With commit=False nothing is committed, so the caller can commit this
    together with its own rows (the admin route's audit log)."""
    from app.models.communication import Message, Notification
    from app.models.order import Order
    from app.models.withdrawal import VendorCryptoWallet

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
        if sf.closed_at is None:
            sf.closed_at = datetime.now(timezone.utc)
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

    # Nothing references crypto wallets, notifications or messages, so those
    # rows can go. Bank accounts are referenced by withdrawals.
    _remove_bank_accounts(uid)
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
    if commit:
        db.session.commit()
    else:
        db.session.flush()
