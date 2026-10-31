"""AI Trust Report on /check (Phase 3, 3-2): rules decide the verdict and the
Safe Pay advice; the AI only words the narrative, a code critic checks it, the
template is the fallback, and narratives are cached by evidence hash.
No network: the AI uses a fake client."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models.ai import AIUsage
from app.models.dispute import DisputeCase
from app.models.payment_link import PaymentLink
from app.models.telemetry import TrustEvidence
from app.models.trust import VendorTrustProfile
from app.models.trust_report import TrustReportNarrative
from app.services import trust_report as svc
from app.services.passport import settings_for


# ── evidence profiles (pure rules) ─────────────────────────────────────────

def _ev(**over):
    ev = {
        "vendor_id": 1, "store_slug": "ada-fabrics", "store_name": "Ada Fabrics",
        "verified": True, "business_registered": False,
        "tier": "GOLD", "score": 82, "score_max": 100,
        "completed_orders": 31, "completed_orders_public": True,
        "member_since": date(2025, 3, 2), "member_since_public": True,
        "tenure_days": 600,
        "disputes": {"opened": 0, "seller_fault": 0, "open_now": 0},
        "reports": None, "sells": "physical",
    }
    disputes = over.pop("disputes", None)
    ev.update(over)
    if disputes:
        ev["disputes"] = {**ev["disputes"], **disputes}
    return ev


@pytest.mark.parametrize("profile,expected", [
    (_ev(), "established"),
    (_ev(verified=False), "established"),                      # verification is shown, not scored
    (_ev(completed_orders=0, score=None, tier="NEW", tenure_days=3), "new"),
    (_ev(completed_orders=0, score=None, tier="NEW", tenure_days=900), "new"),
    (_ev(completed_orders=3, score=None, tier="NEW"), "early"),
    (_ev(completed_orders=12, score=None, tier="NEW"), "building"),
    (_ev(completed_orders=12, tier="SILVER", score=60), "good"),
    (_ev(tenure_days=60), "good"),                             # many orders but under 6 months
    (_ev(tier="BRONZE", score=40), "mixed"),
    (_ev(disputes={"opened": 2, "seller_fault": 1}, completed_orders=31), "mixed"),   # 3.2 per 100
    (_ev(disputes={"opened": 1, "seller_fault": 1}, completed_orders=100), "established"),  # 1 per 100
    (_ev(disputes={"opened": 2, "open_now": 2}), "mixed"),
    (_ev(disputes={"opened": 1, "open_now": 1}), "good"),     # one open dispute blocks "established"
    (_ev(completed_orders=0, score=None, tier="NEW", disputes={"opened": 1, "seller_fault": 1}), "mixed"),
])
def test_verdict_rules(profile, expected):
    assert svc.verdict_code(profile) == expected


def test_verdict_wording_is_neutral():
    labels = [label for label, _ in svc.VERDICTS.values()] + list(svc.MIXED_LABELS.values())
    texts = labels + list(svc.SAFE_PAY_ADVICE.values()) + list(svc.SAFE_PAY_CAUTION.values())
    for text in texts:
        assert not svc.NEVER_SAY_RE.search(svc._SAFE_PAY_RE.sub("", text)), text
    assert svc.VERDICTS["established"][0] == "Established seller with a good record"
    assert svc.VERDICTS["new"][0] == "New seller — no history yet"
    assert svc.MIXED_LABELS["physical"] == "Mixed record — use Safe Pay"


def test_safe_pay_physical_holds_money():
    body = svc.verdict(_ev(sells="physical"))
    sp = body["safe_pay"]
    assert sp["applies"] == "physical" and sp["holds_money"] is True and sp["use_siiqo"] is True
    assert "holds your money until you confirm delivery" in sp["advice"]


def test_safe_pay_digital_is_not_held():
    body = svc.verdict(_ev(sells="not_held"))
    sp = body["safe_pay"]
    assert sp["applies"] == "not_held" and sp["holds_money"] is False
    assert "does not hold these payments" in sp["advice"]
    assert "holds your money" not in sp["advice"]


def test_safe_pay_mixed_catalogue_says_physical_only():
    sp = svc.verdict(_ev(sells="mixed"))["safe_pay"]
    assert sp["holds_money"] is True
    assert "physical orders only" in sp["advice"] and "paid to the seller when you pay" in sp["advice"]


def test_mixed_record_label_follows_what_safe_pay_can_hold():
    bad = {"tier": "BRONZE", "score": 40}
    assert svc.verdict(_ev(sells="physical", **bad))["verdict"]["label"] == "Mixed record — use Safe Pay"
    digital = svc.verdict(_ev(sells="not_held", **bad))
    assert "use Safe Pay" not in digital["verdict"]["label"]
    assert digital["safe_pay"]["emphasis"] is True
    assert digital["safe_pay"]["advice"].endswith("Pay only an amount you are ready to pay up front.")


def test_hidden_facts_never_shown_or_sent_to_ai():
    ev = _ev(completed_orders_public=False, member_since_public=False)
    evidence = svc.ai_evidence(ev)
    assert "completed_orders" not in evidence and "on_siiqo_since" not in evidence
    body = svc.verdict(ev)
    text = json.dumps(body)
    assert "31" not in text and "2025" not in text
    assert body["verdict"]["code"] == "established"   # the rules still use the facts


def test_facts_line_and_template_pass_the_critic():
    ev = _ev(disputes={"opened": 1, "seller_fault": 0})
    assert svc.facts_line(ev) == ("Verified · 31 completed orders · 0 seller-fault disputes · "
                                  "On Siiqo since March 2025 · Trust tier GOLD (82/100)")
    for e in (ev, _ev(verified=False, score=None, tier="NEW", completed_orders=0),
              _ev(tier="BRONZE", score=40, disputes={"seller_fault": 3, "open_now": 1})):
        assert svc.critique(svc.template_text(e), svc.ai_evidence(e)) == []


# ── critic ─────────────────────────────────────────────────────────────────

GOOD_TEXT = ("Ada Fabrics has completed 31 orders on Siiqo since March 2025 and holds the GOLD tier "
             "with a score of 82 out of 100. Its identity is verified and no disputes were decided against it.")


def test_critic_accepts_a_faithful_report():
    assert svc.critique(GOOD_TEXT, svc.ai_evidence(_ev())) == []


@pytest.mark.parametrize("text,problem", [
    ("Ada Fabrics has completed 45 orders on Siiqo.", "number '45'"),
    ("Ada Fabrics has completed about forty orders.", "number word"),
    ("Ada Fabrics has been on Siiqo since June 2025.", "month 'June'"),
    ("Ada Fabrics holds the PLATINUM tier.", "tier"),
    ("Ada Fabrics is a legit seller with 31 orders.", "uses the word"),
    ("Ada Fabrics is safe to buy from.", "uses the word"),
    ("Buying from Ada Fabrics is guaranteed.", "uses the word"),
    ("Ada Fabrics is not a scam.", "uses the word"),
    ("Ada Fabrics has an 80% success rate.", "number '80'"),
])
def test_critic_rejects(text, problem):
    problems = svc.critique(text, svc.ai_evidence(_ev()))
    assert any(problem in p for p in problems), problems


def test_critic_allows_the_product_name_safe_pay_only():
    ev = svc.ai_evidence(_ev())
    assert svc.critique("Ada Fabrics has 31 completed orders. Buyers can use Safe Pay.", ev) == []
    assert svc.critique("Ada Fabrics has 31 completed orders. Paying is safe.", ev) != []


def test_critic_checks_verification_and_dispute_claims():
    unverified = svc.ai_evidence(_ev(verified=False, disputes={"seller_fault": 2, "opened": 2}))
    assert any("verified" in p for p in svc.critique("Ada Fabrics is verified by Siiqo.", unverified))
    assert svc.critique("Ada Fabrics is not verified by Siiqo.", unverified) == []
    assert any("no disputes" in p for p in svc.critique("Ada Fabrics has had no disputes.", unverified))
    verified = svc.ai_evidence(_ev())
    assert any("not verified" in p for p in svc.critique("Ada Fabrics is not verified.", verified))


def test_critic_ignores_numbers_and_tiers_in_the_store_name():
    ev = svc.ai_evidence(_ev(store_name="Gold 24 Mart", tier="SILVER", score=60))
    assert svc.critique("Gold 24 Mart holds the SILVER tier with 31 completed orders.", ev) == []


# ── narrative: fake AI client, cache, retry, fallback ──────────────────────

def _resp(obj):
    usage = SimpleNamespace(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, iterations=None)
    return SimpleNamespace(id="msg_test", model="claude-opus-5-5", stop_reason="end_turn", stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))], usage=usage)


class FakeClient:
    def __init__(self, *reports):
        self.reports = list(reports)
        self.calls = 0
        endpoint = SimpleNamespace(create=self._create)
        self.messages = endpoint
        self.beta = SimpleNamespace(messages=endpoint)

    def _create(self, **kwargs):
        self.calls += 1
        self.last_request = kwargs
        return _resp({"report": self.reports[min(self.calls, len(self.reports)) - 1]})


def _completed(make_order, vendor):
    order = make_order(vendor)
    order.status = "COMPLETED"
    db.session.commit()
    return order


@pytest.fixture
def seller(make_vendor, make_order, make_product):
    vendor, store = make_vendor()
    store.store_slug = "ada-fabrics"
    store.store_name = "Ada Fabrics"
    store.is_pro_verified = True
    vendor.created_at = datetime(2025, 3, 2, tzinfo=timezone.utc)
    db.session.commit()
    make_product(store, product_type="physical")
    for _ in range(3):
        _completed(make_order, vendor)
    return vendor, store


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "1")
    for key in ("AI_ENABLED_ACTIONS", "AI_DAILY_COST_CAP_USD"):
        monkeypatch.delenv(key, raising=False)

    def install(*reports):
        fake = FakeClient(*reports)
        monkeypatch.setattr(svc, "get_ai_client", lambda: fake)
        return fake
    return install


def _good(store_name="Ada Fabrics"):
    return f"{store_name} has completed 3 orders on Siiqo since March 2025. Its identity is verified."


def test_report_endpoint_returns_verdict_without_ai(client, seller, monkeypatch):
    monkeypatch.setattr(svc, "get_ai_client", lambda: pytest.fail("verdict must not call the AI"))
    res = client.get("/api/check/report/ada-fabrics")
    assert res.status_code == 200
    body = res.get_json()
    assert body["verdict"]["code"] == "early"
    assert body["safe_pay"]["applies"] == "physical"
    assert body["facts_line"].startswith("Verified · 3 completed orders")
    assert body["passport_url"] == "/id/ada-fabrics"
    assert AIUsage.query.count() == 0
    text = res.get_data(as_text=True)
    assert seller[0].email not in text


def test_report_404_for_unknown_or_hidden_seller(client, seller):
    assert client.get("/api/check/report/nobody-here").status_code == 404
    assert client.get("/api/check/report/nobody-here/narrative").status_code == 404
    settings_for(seller[0].id, create=True).is_public = False
    db.session.commit()
    assert client.get("/api/check/report/ada-fabrics").status_code == 404


def test_report_digital_seller_advice(client, make_vendor):
    vendor, store = make_vendor()
    db.session.add(PaymentLink(vendor_id=vendor.id, title="Logo design", slug="pl-logo1", product_type="service"))
    db.session.commit()
    body = client.get(f"/api/check/report/{store.store_slug}").get_json()
    assert body["verdict"]["code"] == "new"
    assert body["safe_pay"]["applies"] == "not_held" and body["safe_pay"]["holds_money"] is False


def test_report_counts_disputes_from_evidence_and_cases(client, seller, make_order):
    vendor, store = seller
    o1, o2, o3 = (make_order(vendor) for _ in range(3))
    db.session.add(TrustEvidence(business_id=vendor.id, evidence_type="dispute_lost", source="escrow",
                                 source_id=str(o1.id), provenance="siiqo_transaction_verified"))
    db.session.add(DisputeCase(reference="DSP-T1", order_id=o2.id, vendor_id=vendor.id, status="resolved",
                               seller_fault=True))
    db.session.add(DisputeCase(reference="DSP-T2", order_id=o3.id, vendor_id=vendor.id, status="open",
                               open_key=str(o3.id)))
    db.session.commit()
    assert svc.dispute_counts(vendor.id) == {"opened": 3, "seller_fault": 2, "open_now": 1}
    body = client.get("/api/check/report/ada-fabrics").get_json()
    assert body["verdict"]["code"] == "mixed"
    assert {"code": "seller_fault_disputes", "text": "2 disputes decided against the seller"} in body["reasons"]


def test_ai_off_returns_template_and_stores_nothing(client, seller, monkeypatch):
    monkeypatch.delenv("AI_ENABLED", raising=False)
    body = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert body["source"] == "template" and body["ai"] == "off"
    assert body["narrative"].startswith("Ada Fabrics: Verified · 3 completed orders")
    assert TrustReportNarrative.query.count() == 0 and AIUsage.query.count() == 0


def test_narrative_ai_then_cache_hit(client, seller, ai_on):
    fake = ai_on(_good())
    first = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert first["source"] == "ai" and first["cached"] is False and first["narrative"] == _good()
    assert fake.calls == 1
    # The AI sees public business facts only.
    sent = fake.last_request["messages"][0]["content"]
    assert seller[0].email not in sent and "completed_orders" in sent
    second = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert second["cached"] is True and second["narrative"] == _good() and fake.calls == 1
    assert TrustReportNarrative.query.count() == 1


def test_narrative_regenerates_when_evidence_changes(client, seller, ai_on, make_order):
    fake = ai_on(_good(), "Ada Fabrics has completed 4 orders on Siiqo since March 2025.")
    first = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    _completed(make_order, seller[0])
    second = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert second["evidence_hash"] != first["evidence_hash"]
    assert second["cached"] is False and "4 orders" in second["narrative"] and fake.calls == 2
    assert TrustReportNarrative.query.count() == 2


def test_critic_rejects_then_retry_succeeds(client, seller, ai_on):
    fake = ai_on("Ada Fabrics has completed 300 orders.", _good())
    body = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert body["source"] == "ai" and body["narrative"] == _good() and fake.calls == 2
    assert TrustReportNarrative.query.one().attempts == 2


def test_critic_rejects_twice_then_template(client, seller, ai_on):
    fake = ai_on("Ada Fabrics is a safe seller.", "Ada Fabrics is guaranteed legit with 99 orders.")
    body = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert body["source"] == "template" and fake.calls == 2
    assert body["narrative"] == svc.template_text(svc.gather(seller[1]))
    row = TrustReportNarrative.query.one()
    assert row.source == "template"
    # A fresh template row is reused: no new AI calls until it is TEMPLATE_RETRY_HOURS old.
    again = client.get("/api/check/report/ada-fabrics/narrative").get_json()
    assert again["cached"] is True and fake.calls == 2
    row.updated_at = datetime.now(timezone.utc) - timedelta(hours=svc.TEMPLATE_RETRY_HOURS + 1)
    db.session.commit()
    client.get("/api/check/report/ada-fabrics/narrative")
    assert fake.calls == 4


def test_narrative_rate_limit_per_ip(client, make_vendor, ai_on, monkeypatch):
    from app.extensions import limiter
    monkeypatch.setenv("TRUST_REPORT_NARRATIVES_PER_DAY", "2")
    slugs = []
    for _ in range(3):
        _, store = make_vendor()
        slugs.append(store.store_slug)
    fake = ai_on("This seller has no completed orders on Siiqo yet.")
    limiter.enabled = True
    try:
        limiter.reset()
        bodies = [client.get(f"/api/check/report/{s}/narrative").get_json() for s in slugs]
        assert [b["source"] for b in bodies] == ["ai", "ai", "template"]
        assert bodies[2]["rate_limited"] is True and fake.calls == 2
        # Cached narratives never count against the limit.
        again = client.get(f"/api/check/report/{slugs[0]}/narrative").get_json()
        assert again["cached"] is True and "rate_limited" not in again
        # Verdicts stay available.
        assert client.get(f"/api/check/report/{slugs[2]}").status_code == 200
    finally:
        limiter.reset()
        limiter.enabled = False


def test_profile_tier_and_score_feed_the_verdict(client, seller, make_order):
    vendor, _ = seller
    for _ in range(20):
        _completed(make_order, vendor)
    db.session.add(VendorTrustProfile(vendor_id=vendor.id, total_trust_score=820, trust_tier="GOLD"))
    db.session.commit()
    body = client.get("/api/check/report/ada-fabrics").get_json()
    assert body["verdict"] == {"code": "established", "label": "Established seller with a good record",
                               "tone": "positive"}
    assert {"code": "tier", "text": "Trust tier GOLD (820/1000)"} in body["reasons"]
