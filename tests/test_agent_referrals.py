"""C3: an approved agent's personal referral code (/join?ref=XXXX) credits
their agent code, the same as a scan; other people's referral codes keep the
old behaviour only; unknown codes never block signup. C6: the agent is told
once when a seller joins with their code and once per rewarded sale."""
import pytest

import app.routes.auth as auth_routes
from app.extensions import db
from app.models.communication import Notification
from app.models.marketing import SellerAcquisition
from app.models.partnerships import Referral
from app.models.user import User
from app.services import marketing as svc
from tests.test_marketing_system import _acq, _pay, agent, legacy_reward_rules  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from app.middleware import bot_protection
    bot_protection.registration_attempts.clear()
    monkeypatch.setattr(auth_routes, "send_siiqo_email", lambda *a, **k: None)


def _register(client, email, **extra):
    body = {"email": email, "password": "a-long-password-1", "first_name": "Ngozi", **extra}
    return client.post("/api/auth/register", json=body)


def _with_referral_code(user):
    user.generate_referral_code()
    db.session.commit()
    return user


def _joined_notes(agent_user):
    return Notification.query.filter_by(user_id=agent_user.id, title="New seller with your code").all()


def test_agents_personal_referral_code_credits_their_agent_code(client, agent):
    agent_user, _, agent_code = agent()
    _with_referral_code(agent_user)
    # /join?ref=XXXX: the website sends it as referral_code and as ref_code.
    res = _register(client, "via-join@example.com", referral_code=agent_user.referral_code,
                    ref_code=agent_user.referral_code.lower())
    assert res.status_code == 201, res.get_json()
    seller = User.query.filter_by(email="via-join@example.com").first()
    acq = _acq(seller)
    assert acq.code_id == agent_code.id and acq.agent_user_id == agent_user.id
    # The old referral row is still written (old behaviour kept).
    assert Referral.query.filter_by(referred_id=seller.id).count() == 1


def test_referral_code_field_alone_credits_agent(make_user, agent):
    agent_user, _, agent_code = agent()
    _with_referral_code(agent_user)
    seller = make_user()
    assert svc.credit_signup(seller, {"referral_code": agent_user.referral_code}) is not None
    db.session.commit()
    assert _acq(seller).agent_user_id == agent_user.id


def test_non_agent_referral_code_is_old_behaviour_only(client, make_user):
    friend = make_user()
    friend.generate_referral_code()
    db.session.commit()
    res = _register(client, "friend-invite@example.com", referral_code=friend.referral_code,
                    ref_code=friend.referral_code)
    assert res.status_code == 201
    seller = User.query.filter_by(email="friend-invite@example.com").first()
    assert _acq(seller) is None
    assert Referral.query.filter_by(referred_id=seller.id, referrer_id=friend.id).count() == 1


def test_unknown_code_never_blocks_signup(client):
    res = _register(client, "unknown-code@example.com", ref_code="NOPE42", referral_code="ZZZZ9999")
    assert res.status_code == 201
    seller = User.query.filter_by(email="unknown-code@example.com").first()
    assert seller is not None and _acq(seller) is None


def test_unapproved_agent_referral_code_does_not_credit(make_user, agent):
    agent_user, profile, _ = agent()
    profile.status = "PAUSED"
    _with_referral_code(agent_user)
    seller = make_user()
    assert svc.credit_signup(seller, {"referral_code": agent_user.referral_code}) is None


def test_agent_told_once_when_seller_joins(client, agent, make_user):
    agent_user, _, agent_code = agent()
    assert _register(client, "joined@example.com", ref_code=agent_code.code).status_code == 201
    seller = User.query.filter_by(email="joined@example.com").first()
    svc.credit_signup(seller, {"ref_code": agent_code.code})  # second touch: no new credit, no new notice
    db.session.commit()
    notes = _joined_notes(agent_user)
    assert len(notes) == 1
    assert notes[0].message == ("🎉 Ngozi just joined with your code. "
                                "Help them add 5 products and send their first Pay Link.")


def test_agent_told_for_each_rewarded_sale(make_user, make_vendor, make_order, agent):
    agent_user, _, agent_code = agent()
    vendor, store = make_vendor()
    svc.credit_signup(vendor, {"ref_code": agent_code.code})
    db.session.commit()
    assert _joined_notes(agent_user)[0].message.startswith(f"🎉 {store.store_name} just joined")
    for _ in range(2):
        _pay(make_order(vendor, buyer=make_user()))
    svc.recompute_for(vendor.id)  # idempotent: no extra notices
    db.session.commit()
    notes = (Notification.query.filter_by(user_id=agent_user.id, title="Sale reward earned")
             .order_by(Notification.id).all())
    assert [n.message for n in notes] == [
        f"₦200 earned: first-sale bonus from {store.store_name}.",
        f"₦200 earned: sale 2 of 5 from {store.store_name}.",
    ]
    assert SellerAcquisition.query.filter_by(seller_user_id=vendor.id).count() == 1
