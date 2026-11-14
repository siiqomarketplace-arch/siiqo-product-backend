"""
Publish an onboarding session (Siiqo 2.0, Phase 1) - only on the seller's call.

One database transaction creates, from the session's drafts:
  * the Storefront (same fields and defaults as ``POST /api/vendor/onboard``,
    with ``template_options`` from the draft Store Spec), and the user's
    VENDOR role;
  * the bank account: a ``VendorBankAccount`` row plus the storefront's
    ``bank_code`` / ``account_number`` / ``account_name`` (as onboard_vendor);
  * Products from the draft listings that have a title and a price;
  * the first Pay Link (same rules as ``create_payment_link``) from the first
    product, or an open-amount Pay Link when no listing has a price.

The session is claimed with a conditional UPDATE (ready|ready_fallback ->
published) inside that transaction, so a second publish - concurrent or
later - creates nothing and returns the saved result.

After the commit, the Paystack subaccount is attempted exactly as
onboard_vendor does (best effort; publishing never fails because of it).
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.onboarding import OnboardingSession
from app.models.payment_link import PaymentLink
from app.models.product import Category, Product
from app.models.user import Storefront, User, UserRole
from app.models.withdrawal import VendorBankAccount
from app.services.places import place_label, split_place
from app.services.store_spec import template_options_from_spec

log = logging.getLogger(__name__)

SLUG_MAX = 60
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")


def frontend_url() -> str:
    return (os.environ.get("FRONTEND_URL") or "https://siiqo.com").rstrip("/")


def store_url(slug: str) -> str:
    """A store's public address: https://<slug>.siiqo.com. Local hosts keep the
    path form (http://localhost:3000/<slug>), which has no subdomains."""
    base = urlsplit(frontend_url())
    host = (base.hostname or "").removeprefix("www.")
    if not host or base.port or host in ("localhost", "127.0.0.1") or "." not in host:
        return f"{frontend_url()}/{slug}"
    return f"{base.scheme or 'https'}://{slug}.{host}"


def pay_link_url(slug: str) -> str:
    return f"{frontend_url()}/pay/{slug}"


# ── Store names / slugs ─────────────────────────────────────────────────────
def slugify(name: str) -> str:
    """Same rule as onboard_vendor, capped at SLUG_MAX characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:SLUG_MAX].strip("-")


def is_valid_slug(slug: str) -> bool:
    return bool(slug and _SLUG_RE.match(slug))


def _reserved() -> frozenset:
    from app.routes.vendor import RESERVED_STORE_SLUGS
    return RESERVED_STORE_SLUGS


def slug_status(slug: str) -> str | None:
    """None if the slug can be claimed, else "invalid" | "reserved" | "taken"."""
    if not is_valid_slug(slug):
        return "invalid"
    if slug in _reserved():
        return "reserved"
    if Storefront.query.filter_by(store_slug=slug).first() is not None:
        return "taken"
    return None


def suggest_slugs(slug: str, city: str | None = None, limit: int = 3) -> list[str]:
    base = slug[: SLUG_MAX - 6].strip("-") or "my-store"
    candidates = [f"{base}-store", f"{base}-shop", f"{base}-ng", f"shop-{base}", f"{base}-hq"]
    if city:
        c = slugify(city)
        if c:
            candidates.insert(1, f"{base}-{c}"[:SLUG_MAX].strip("-"))
    candidates += [f"{base}-{n}" for n in range(2, 10)]
    out: list[str] = []
    for cand in dict.fromkeys(candidates):
        if slug_status(cand) is None:
            out.append(cand)
        if len(out) >= limit:
            break
    return out


def check_name(name: str) -> dict:
    slug = slugify(name)
    reason = slug_status(slug)
    return {
        "available": reason is None,
        "slug": slug,
        "reason": reason,
        "store_url": store_url(slug) if slug else None,
        "suggestions": [] if reason is None or not slug else suggest_slugs(slug),
        "listing_match": _listing_match(name),
    }


def _listing_match(name: str):
    """Phase 3 (3-5): an unclaimed directory listing with exactly this name, so
    /start can offer "Is this you? Claim it". Never blocks the name check."""
    try:
        from app.services.directory import name_match
        return name_match(name)
    except Exception as e:  # noqa: BLE001
        log.warning("name check: directory lookup skipped (%s)", type(e).__name__)
        return None


# ── Publish ─────────────────────────────────────────────────────────────────
class PublishError(Exception):
    def __init__(self, http_status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = http_status, code, message, extra

    def body(self) -> dict:
        return {"message": self.message, "code": self.code, **self.extra}


def validate_bank(body: dict) -> dict:
    bank_code = str(body.get("bank_code") or "").strip()
    account_number = str(body.get("account_number") or "").strip()
    account_name = str(body.get("account_name") or "").strip()
    bank_name = str(body.get("bank_name") or "").strip()
    errors = {}
    if not re.fullmatch(r"[0-9A-Za-z]{2,10}", bank_code):
        errors["bank_code"] = "Choose your bank."
    if not re.fullmatch(r"\d{10}", account_number):
        errors["account_number"] = "Enter your 10-digit account number."
    if not (2 <= len(account_name) <= 100):
        errors["account_name"] = "Account name is required."
    if len(bank_name) > 100:
        errors["bank_name"] = "Bank name is too long."
    if errors:
        raise PublishError(400, "INVALID_BANK_DETAILS", "Check your bank details.", errors=errors)
    return {"bank_code": bank_code, "account_number": account_number,
            "account_name": account_name, "bank_name": bank_name or "Bank"}


def _confirm_with_bank(bank: dict) -> dict:
    """The saved account name comes from the bank, never from the browser.

    resolved    -> use the bank's name, mark the account verified;
    rejected    -> 400 BANK_NOT_RESOLVED (wrong number or bank);
    unavailable -> Paystack down or not configured: keep the name the seller
                   confirmed on screen, unverified (as /vendor/onboard does).
    """
    from app.services.bank_resolve import resolve_account
    res = resolve_account(bank["bank_code"], bank["account_number"])
    if res.rejected:
        raise PublishError(400, "BANK_NOT_RESOLVED",
                           "We couldn't confirm this account with your bank. "
                           "Check the account number and bank.")
    if res.resolved:
        return {**bank, "account_name": res["account_name"][:100], "verified": True}
    log.warning("onboarding publish: bank resolve unavailable (%s); saving unverified", res["message"])
    return {**bank, "verified": False}


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _category_id(name: str | None) -> int | None:
    if not name:
        return None
    cat = Category.query.filter(Category.name.ilike(name.strip()) | Category.slug.ilike(name.strip())).first()
    return cat.id if cat else None


def _pay_link_slug(title: str) -> str:
    # Same rule as create_payment_link.
    clean_title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = f"{clean_title}-{uuid.uuid4().hex[:6]}"
    if not slug or slug.startswith("-"):
        slug = f"pay-{uuid.uuid4().hex[:10]}"
    return slug[:100]


def _share(store_name: str, s_url: str, link: PaymentLink | None, link_url: str | None) -> dict:
    text = f"{store_name} is now online. See what we sell and order here: {s_url}"
    if link is not None and link.amount is not None:
        text += f"\nPay for {link.title} here: {link_url}"
    return {
        "url": s_url,
        "pay_link_url": link_url,
        "whatsapp_text": text,
        "whatsapp_url": "https://wa.me/?text=" + quote(text),
    }


def publish(session: OnboardingSession, user: User, body: dict) -> tuple[dict, bool]:
    """Returns (result, created). Raises PublishError."""
    if session.status == "published" and session.publish_result:
        return session.publish_result, False
    if session.status not in OnboardingSession.READY_STATUSES or not session.draft_spec:
        raise PublishError(409, "BUILD_NOT_READY", "Your store draft is not ready yet.",
                           status=session.status)
    bank = validate_bank(body or {})
    bank = _confirm_with_bank(bank)
    if not user.is_verified:
        raise PublishError(403, "EMAIL_NOT_VERIFIED", "Please verify your email before publishing your store.")
    if user.storefront is not None:
        raise PublishError(409, "ALREADY_HAS_STORE", "You already have a storefront.",
                           store_slug=user.storefront.store_slug)

    answers = dict(session.answers or {})
    store_name = (answers.get("business_name") or "").strip()
    slug = answers.get("store_slug") or slugify(store_name)
    reason = slug_status(slug)
    if reason is not None:
        raise PublishError(409, "SLUG_UNAVAILABLE", f"The store link '{slug}' is not available.",
                           reason=reason, slug=slug, suggestions=suggest_slugs(slug, answers.get("city")))

    # Claim the session (one transaction with everything below).
    claimed = db.session.execute(
        update(OnboardingSession)
        .where(OnboardingSession.id == session.id,
               OnboardingSession.status.in_(OnboardingSession.READY_STATUSES))
        .values(status="published")
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    if not claimed:
        db.session.rollback()
        db.session.refresh(session)
        if session.status == "published" and session.publish_result:
            return session.publish_result, False
        raise PublishError(409, "BUILD_NOT_READY", "Your store draft is not ready yet.", status=session.status)

    # Mandatory verification (D11): with VERIFICATION_REQUIRED_FOR at
    # new_stores or all_sellers, the store is created unpublished and the
    # seller sees one step. None (the default) changes nothing.
    from app.services import verification_requirement
    requirement = verification_requirement.new_store_requirement(user)
    try:
        result = _create_everything(session, user, answers, store_name, slug, bank)
        if requirement is not None:
            # A new dict (not a mutation) so the stored result is updated too.
            result = {**result, "verification_required": requirement}
            session.publish_result = result
            storefront = db.session.get(Storefront, result["storefront_id"])
            storefront.is_published = False
            verification_requirement.record_new_store_hold(user.id, storefront.id)
        # Marketing code the seller came through, if not credited at signup.
        from app.services.marketing import credit_signup
        credit_signup(user, body)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        log.warning("onboarding publish conflict for session %s: %s", session.id, exc.orig)
        db.session.refresh(session)
        if session.status == "published" and session.publish_result:
            return session.publish_result, False
        raise PublishError(409, "PUBLISH_CONFLICT", "Something changed while publishing. Please try again.")
    except Exception:
        db.session.rollback()
        raise

    _attach_paystack_subaccount(session.published_storefront_id, user.id)
    if requirement is None:
        _send_store_live_email(user, result)
    return result, True


def _send_store_live_email(user: User, result: dict) -> None:
    """Best effort; publishing never fails because of email."""
    if not user.email:
        return
    try:
        from app.utils.email import send_siiqo_email
        send_siiqo_email(
            to_email=user.email,
            subject="Your Siiqo store is live",
            template_name="base_notification",
            first_name=user.first_name or "there",
            title="Your store is live",
            message=(f"Your store is online at {result['store_url']}. "
                     f"Your first Pay Link is ready to share: {result['pay_link_url']}"),
            cta_text="Open my dashboard",
            cta_url=f"{frontend_url()}/vendor/dashboard",
        )
    except Exception as exc:  # noqa: BLE001
        log.info("onboarding publish: store-live email not sent: %s", exc)


def _new_store(user, store_name, slug, bank, *, description=None, city=None, state=None, phone=None,
               template_options=None, logo_url=None) -> Storefront:
    """The Storefront (as onboard_vendor) and its bank account, in the caller's
    transaction. Shared by publish and the Safe Pay request fast path."""
    template_options = template_options or {}
    user.role = UserRole.VENDOR
    storefront = Storefront(
        vendor_id=user.id,
        store_name=store_name[:100],
        store_slug=slug,
        store_description=description,
        city=city,
        state=state,
        country="Nigeria",
        bank_code=bank["bank_code"],
        account_number=bank["account_number"],
        account_name=bank["account_name"],
        phone=phone,
        account_type="INDIVIDUAL",
        theme_color=template_options.get("primary_color", "#0b1b3b"),
        template_options=template_options,
        store_logo=logo_url,
        # As onboard_vendor: the store is live once created; identity review
        # and badges are separate.
        is_published=True,
        is_verified=False,
    )
    db.session.add(storefront)
    db.session.flush()

    # Bank account: CBN code on the row (as add_bank_account); the account
    # name is what the seller confirmed from the live resolve on the website.
    from app.services.escrow.paystack_provider import PAYSTACK_TO_NIBSS_BANKS
    cbn_code = PAYSTACK_TO_NIBSS_BANKS.get(bank["bank_code"], bank["bank_code"])
    bank_row = VendorBankAccount.query.filter_by(vendor_id=user.id, account_number=bank["account_number"]).first()
    if bank_row is None:
        bank_row = VendorBankAccount(
            vendor_id=user.id,
            bank_name=bank["bank_name"],
            bank_code=cbn_code,
            account_number=bank["account_number"],
            account_name=bank["account_name"],
            is_verified=bool(bank.get("verified")),
            is_default=VendorBankAccount.query.filter_by(vendor_id=user.id).count() == 0,
        )
        db.session.add(bank_row)
        db.session.flush()
    return storefront


def _create_everything(session, user, answers, store_name, slug, bank) -> dict:
    template_options = template_options_from_spec(session.draft_spec)
    logo = session.logo()
    place_city, place_state = answers.get("city"), answers.get("state")
    if not place_city and not place_state:
        # The website interview asks one free-text "location" question.
        place_city, place_state = split_place(answers.get("location"))
    storefront = _new_store(
        user, store_name, slug, bank,
        description=answers.get("about") or answers.get("what_you_sell"),
        city=place_city, state=place_state, phone=answers.get("contact_phone"),
        template_options=template_options, logo_url=logo.url if logo else None,
    )

    # Products: listings with a title and a price above zero. Products for
    # digital/service sellers need a file or booking link first, so only
    # physical products are created here (the Pay Link is created either way).
    product_type = answers.get("product_type") or "physical"
    products: list[Product] = []
    seen: set[str] = set()
    priced = []
    for listing in session.draft_listings or []:
        title = (listing.get("title") or "").strip()
        price = _decimal(listing.get("price"))
        if not title or price is None or price <= 0:
            continue
        if title.lower() in seen:
            continue
        seen.add(title.lower())
        priced.append((listing, title, price))
        if product_type != "physical":
            continue
        attrs = {a["name"]: a["value"] for a in (listing.get("attributes") or [])
                 if isinstance(a, dict) and a.get("name") and a.get("value")}
        quantity = listing.get("quantity")
        product = Product(
            storefront_id=storefront.id,
            name=title[:255],
            description=(listing.get("description") or "").strip(),
            price=price,
            currency="NGN",
            stock_quantity=int(quantity) if isinstance(quantity, int) and quantity > 0 else 1,
            category_id=_category_id(listing.get("category")),
            condition=None,
            location=place_label(place_city, place_state),
            product_type="physical",
            is_free=False,
            images=[listing["image_url"]] if listing.get("image_url") else [],
            attributes=attrs or None,
        )
        db.session.add(product)
        products.append(product)
    db.session.flush()

    # First Pay Link (create_payment_link rules: title required, amount > 0
    # or open, PAY_LINK, ACTIVE, slug from the title).
    if priced:
        listing, title, price = priced[0]
        link_title, amount, description = title[:100], price, (listing.get("description") or "").strip()
    else:
        link_title, amount, description = f"Pay {store_name}"[:100], None, ""
    link = PaymentLink(
        vendor_id=user.id,
        link_type="PAY_LINK",
        title=link_title,
        description=description,
        amount=amount,
        currency="NGN",
        buyer_email=None,
        status="ACTIVE",
        slug=_pay_link_slug(link_title),
        # A digital Pay Link with no file would promise an automatic download that
        # never comes, so until the seller adds a file it works as a service
        # (the seller delivers and the buyer is told so).
        product_type="physical" if product_type == "physical" else "service",
        file_url=None,
    )
    db.session.add(link)
    db.session.flush()

    s_url, l_url = store_url(slug), pay_link_url(link.slug)
    result = {
        "store_url": s_url,
        "pay_link_url": l_url,
        "share": _share(store_name, s_url, link, l_url),
        "storefront_id": storefront.id,
        "store_slug": slug,
        "product_ids": [p.id for p in products],
        "payment_link_id": link.id,
        "payment_link_slug": link.slug,
    }
    session.status = "published"
    session.open_user_id = None
    session.step = 7
    session.published_storefront_id = storefront.id
    session.published_at = datetime.now(timezone.utc)
    session.publish_result = result
    return result


def _attach_paystack_subaccount(storefront_id: int | None, vendor_id: int) -> None:
    """Mirror of onboard_vendor's best-effort Paystack subaccount step."""
    storefront = db.session.get(Storefront, storefront_id) if storefront_id else None
    if storefront is None or not (storefront.bank_code and storefront.account_number):
        return
    try:
        from app.services.escrow.paystack_provider import create_paystack_subaccount
        sub_result = create_paystack_subaccount(
            business_name=storefront.store_name,
            bank_code=storefront.bank_code,
            account_number=storefront.account_number,
        )
        if sub_result.get("success"):
            storefront.paystack_subaccount_code = sub_result["subaccount_code"]
            bank_row = VendorBankAccount.query.filter_by(
                vendor_id=vendor_id, account_number=storefront.account_number).first()
            if bank_row is not None and not bank_row.paystack_subaccount_code:
                bank_row.paystack_subaccount_code = sub_result["subaccount_code"]
            db.session.commit()
    except Exception as exc:  # noqa: BLE001 - as onboard_vendor: never fail the publish
        db.session.rollback()
        logging.warning(f"[ONBOARDING] Paystack subaccount creation failed for vendor {vendor_id}: {exc}")
