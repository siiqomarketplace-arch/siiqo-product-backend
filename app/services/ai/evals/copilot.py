"""
Seller copilot scenario evals (Phase 4, 8b): about 50 questions with known
answers from a fixed fixture (two sellers; every case asks as seller A).

Each case in ``scenarios/seller_copilot.v1.json`` has:
  question        what the seller asks ($A.O4 style refs become real ids)
  script          offline only: the tool calls a scripted fake model makes,
                  one list per model turn, e.g. [[["list_orders", {"status": "UNPAID"}]]]
  template        offline only: the fake's final answer, filled from the tool
                  results: {tool.path|filter}; filters: naira, yesno, len;
                  a path segment "key=value" picks the first list item matching
  actions         offline only: the fake's suggested actions (refs allowed)
  expect:
    outcome         answered | fallback | step_cap (default answered)
    contains        substrings the answer must have (case-insensitive)
    contains_any    at least one of these
    not_contains    must not appear
    tools_any       at least one of these tools must be called
    action_types    offline: exactly these button types; live: no other types
  offline_only    true for cases a real model cannot be scripted into (step cap)

Every case also checks: no seller B data in the answer or tool results, and
the database (orders, Pay Links, messages, offers) is unchanged.

Offline (free, in tests): ``tests/test_copilot_scenarios.py`` runs every case
with the scripted fake model. That tests the loop, the tools and the action
validation against the known answers, not the model's wording.

Live (paid; real model, real tokens). Uses a throwaway SQLite database for the
fixture, so it never writes to a real database:

    AI_ENABLED=true ANTHROPIC_API_KEY=... .venv/Scripts/python.exe -m app.services.ai.evals.copilot
    # optional: AI_COPILOT_MODEL=claude-sonnet-5-5, or case ids as arguments

It prints one line per case and exits non-zero if any case fails.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

SCENARIOS = Path(__file__).resolve().parent / "scenarios" / "seller_copilot.v1.json"

# Seller B's data must never appear in seller A's answers or tool results.
B_MARKERS = ("Bola", "777000", "777,000", "Gadget")


# ── Scripted fake model ────────────────────────────────────────────────────
def _usage():
    return SimpleNamespace(input_tokens=400, output_tokens=60, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, iterations=None)


class ScriptedModel:
    """Anthropic-compatible fake. ``script`` is a list of steps:
    ("tools", [(name, input), ...]) or ("final", obj | callable(results) -> obj)."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self._ids = 0
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))

    def _beta_create(self, **kwargs):
        kwargs.pop("betas", None)
        kwargs.pop("extra_body", None)
        return self._create(**kwargs)

    @staticmethod
    def results(messages):
        names, out = {}, []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    names[block["id"]] = block["name"]
                elif block.get("type") == "tool_result":
                    try:
                        parsed = json.loads(block["content"])
                    except (TypeError, ValueError):
                        parsed = {"error": block.get("content"), "is_error": block.get("is_error")}
                    out.append((names.get(block["tool_use_id"]), parsed))
        return out

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError("the model was called more often than scripted")
        kind, payload = self.script.pop(0)
        if kind == "tools":
            blocks = [SimpleNamespace(type="tool_use", id=f"toolu_{self._ids + i}", name=n, input=inp)
                      for i, (n, inp) in enumerate(payload)]
            self._ids += len(payload)
            return SimpleNamespace(id="msg_tools", model="claude-haiku-5-5", stop_reason="tool_use",
                                   stop_details=None, usage=_usage(), content=blocks)
        obj = payload(self.results(kwargs["messages"])) if callable(payload) else payload
        return SimpleNamespace(id="msg_final", model="claude-haiku-5-5", stop_reason="end_turn",
                               stop_details=None, usage=_usage(),
                               content=[SimpleNamespace(type="text", text=json.dumps(obj))])

    @property
    def calls(self):
        return len(self.requests)


def final_answer(text, actions=None, answered=True) -> dict:
    return {"answer": text, "answered": answered, "actions": [
        {"type": a.get("type"), "order_id": a.get("order_id"), "product_id": a.get("product_id"),
         "offer_id": a.get("offer_id"), "page": a.get("page")} for a in (actions or [])]}


# ── Fixture ────────────────────────────────────────────────────────────────
def build_fixture(now: datetime | None = None) -> dict:
    """Two sellers with known data. Returns refs: "$A.O1" -> id, plus the
    seller objects under "A" and "B" and "now"."""
    from app.extensions import db
    from app.models.assistant import ConciergeConversation, ConciergeOffer
    from app.models.dispute import DisputeCase
    from app.models.escrow import EscrowStatus, EscrowTransaction
    from app.models.order import Order, OrderItem
    from app.models.payment_link import PaymentLink
    from app.models.product import Product
    from app.models.trust import TrustScoreShadow
    from app.models.user import Storefront, User, UserRole
    from app.models.withdrawal import VendorBankAccount, Withdrawal
    from app.services import trust_v1

    now = now or datetime.now(timezone.utc)
    naive = now.replace(tzinfo=None)
    refs: dict = {"now": now}

    def user(email, role=UserRole.VENDOR, first="Eval"):
        u = User(email=email, role=role, first_name=first, last_name="Seller")
        u.set_password("eval-fixture-not-a-password")
        db.session.add(u)
        db.session.flush()
        return u

    def seller(tag, name, slug, views):
        u = user(f"eval-{tag.lower()}@example.com", first=name.split()[0])
        s = Storefront(vendor_id=u.id, store_name=name, store_slug=slug, is_published=True,
                       view_count=views)
        db.session.add(s)
        db.session.flush()
        db.session.add(VendorBankAccount(vendor_id=u.id, bank_name="Eval Bank", bank_code="000",
                                         account_number="0000000000", account_name=name))
        db.session.flush()
        refs[tag] = u
        return u, s

    buyer = user("eval-buyer@example.com", role=UserRole.BUYER, first="Buyer")
    a, a_store = seller("A", "Ada Fabrics", "eval-ada-fabrics", 345)
    b, b_store = seller("B", "Bola Gadgets", "eval-bola-gadgets", 999)

    def product(tag, store, name, price, stock, views=0, ptype="physical", active=True, **kw):
        p = Product(storefront_id=store.id, name=name, description=f"{name}.", price=price, currency="NGN",
                    stock_quantity=stock, product_type=ptype, is_active=active, view_count=views, **kw)
        db.session.add(p)
        db.session.flush()
        refs[tag] = p.id
        return p

    pa = {
        "P1": product("$A.P1", a_store, "Ankara fabric 6 yards", 15000, 12, 40, is_negotiable=True, floor_price=13000),
        "P2": product("$A.P2", a_store, "Lace fabric 5 yards", 45000, 2, 25),
        "P3": product("$A.P3", a_store, "Aso oke set", 80000, 0, 60),
        "P4": product("$A.P4", a_store, "Silk scarf", 7500, 30, 5),
        "P5": product("$A.P5", a_store, "Tailoring service", 20000, 0, 10, ptype="service"),
        "P6": product("$A.P6", a_store, "Old cotton", 3000, 4, 1, active=False),
    }
    pb = product("$B.P1", b_store, "Bola phone", 777000, 9, 500)

    links = {}
    for tag, vendor, title, amount, status, age in (
        ("$A.L1", a, "Ankara bundle", 30000, "ACTIVE", 30),
        ("$A.L2", a, "Old promo", 10000, "EXPIRED", 60),
        ("$A.L3", a, "Lace deposit", 20000, "ACTIVE", 5),
        ("$B.L1", b, "Bola promo", 777000, "ACTIVE", 3),
    ):
        link = PaymentLink(vendor_id=vendor.id, link_type="PAY_LINK", title=title, amount=amount,
                           currency="NGN", status=status, slug=f"eval-{tag[1:].lower().replace('.', '-')}",
                           product_type="physical", created_at=naive - timedelta(days=age))
        db.session.add(link)
        db.session.flush()
        refs[tag] = link.id
        links[tag] = link

    def order(tag, vendor, status, total, days, prod, with_buyer=True, escrow=None, link=None):
        o = Order(vendor_id=vendor.id, buyer_id=buyer.id if with_buyer else None,
                  buyer_email="eval-buyer@example.com" if with_buyer else "guest@example.com",
                  buyer_name="Eval Buyer", buyer_phone="08030000000", total_amount=total, status=status,
                  payment_method="ESCROW", currency="NGN", created_at=naive - timedelta(days=days),
                  payment_link_id=links[link].id if link else None,
                  delivery_address="1 Private Road", delivery_phone="08030000001")
        if status not in ("PENDING", "CANCELLED"):
            o.paid_at = now - timedelta(days=days)
        db.session.add(o)
        db.session.flush()
        db.session.add(OrderItem(order_id=o.id, product_id=prod.id, price_at_purchase=total, quantity=1))
        if escrow:
            fee = (Decimal(total) * Decimal("0.03")).quantize(Decimal("0.01"))
            db.session.add(EscrowTransaction(
                order_id=o.id, status=escrow, amount=total, fee_amount=fee, currency="NGN",
                released_at=now if escrow == EscrowStatus.RELEASED else None))
        db.session.flush()
        refs[tag] = o.id
        return o

    E = EscrowStatus
    order("$A.O1", a, "PENDING", 15000, 1, pa["P1"])
    order("$A.O2", a, "PENDING", 45000, 2, pa["P2"], with_buyer=False)
    order("$A.O3", a, "PENDING", 7500, 10, pa["P4"])
    order("$A.O4", a, "PAID", 30000, 1, pa["P1"], escrow=E.IN_ESCROW, link="$A.L1")
    order("$A.O5", a, "PROCESSING", 80000, 3, pa["P3"], escrow=E.IN_ESCROW)
    o6 = order("$A.O6", a, "SHIPPED", 15000, 5, pa["P1"], escrow=E.SHIPPED)
    order("$A.O7", a, "DELIVERED", 20000, 8, pa["P5"], escrow=E.DELIVERED)
    order("$A.O8", a, "COMPLETED", 45000, 20, pa["P2"], escrow=E.RELEASED)
    order("$A.O9", a, "CANCELLED", 7500, 4, pa["P4"])
    o10 = order("$A.O10", a, "COMPLETED", 15000, 40, pa["P1"])
    for i in range(1, 6):
        order(f"$B.O{i}", b, "PENDING", 777000, 1, pb)
    order("$B.O6", b, "PAID", 777000, 1, pb, escrow=E.IN_ESCROW)
    bo7 = order("$B.O7", b, "SHIPPED", 777000, 2, pb, escrow=E.SHIPPED)

    for tag, vendor, number, amount, status, days in (
        ("$A.W1", a, "WD-EVALA1", 50000, "COMPLETED", 10),
        ("$A.W2", a, "WD-EVALA2", 20000, "PENDING", 1),
        ("$B.W1", b, "WD-EVALB1", 777000, "PENDING", 1),
    ):
        bank = VendorBankAccount.query.filter_by(vendor_id=vendor.id).first()
        w = Withdrawal(vendor_id=vendor.id, bank_account_id=bank.id, withdrawal_number=number,
                       amount=amount, fee_amount=50, net_amount=amount - 50, currency="NGN", status=status,
                       requested_at=now - timedelta(days=days),
                       completed_at=(now - timedelta(days=days - 1)) if status == "COMPLETED" else None)
        db.session.add(w)

    for ref, o, vendor, status, reason, outcome, days in (
        ("DSP-EVAL-A1", o6, a, "awaiting_seller", "Item not received", None, 4),
        ("DSP-EVAL-A2", o10, a, "resolved", "Wrong colour", "release_to_seller", 30),
        ("DSP-EVAL-B1", bo7, b, "awaiting_seller", "Bola phone broken", None, 1),
    ):
        db.session.add(DisputeCase(reference=ref, order_id=o.id, vendor_id=vendor.id, buyer_id=buyer.id,
                                   raised_by="buyer", reason=reason, status=status, outcome=outcome,
                                   created_at=now - timedelta(days=days)))

    for tag, vendor, store, prod, agreed in (("$A.OFFER", a, a_store, pa["P1"], 14000),
                                             ("$B.OFFER", b, b_store, pb, 700000)):
        conv = ConciergeConversation(storefront_id=store.id, session_hash=f"eval-{tag}".ljust(64, "0")[:64])
        db.session.add(conv)
        db.session.flush()
        offer = ConciergeOffer(storefront_id=store.id, vendor_id=vendor.id, conversation_id=conv.id,
                               product_id=prod.id, quantity=1, currency="NGN", list_price=prod.price,
                               offered_price=agreed, agreed_price=agreed, rule_decision="accept",
                               status="pending_seller")
        db.session.add(offer)
        db.session.flush()
        refs[tag] = offer.id

    db.session.add(TrustScoreShadow(
        vendor_id=a.id, score=72, tier="GOLD", version=trust_v1.VERSION, computed_on=now.date(),
        components={"components": {"outcomes": 0.9, "on_time": 0.8, "repeat_buyers": 0.4,
                                   "tenure": 0.5, "consistency": 0.6},
                    "reasons": ["Most orders completed well"], "minimum_history": True}))
    db.session.add(TrustScoreShadow(
        vendor_id=b.id, score=11, tier="BRONZE", version=trust_v1.VERSION, computed_on=now.date(),
        components={"components": {}, "reasons": ["Bola reasons"]}))
    db.session.commit()
    return refs


def snapshot() -> dict:
    """What a read-only copilot must never change."""
    from app.models.assistant import ConciergeOffer
    from app.models.communication import Message
    from app.models.order import Order
    from app.models.payment_link import PaymentLink
    from app.models.product import Product

    return {
        "orders": sorted((o.id, o.status) for o in Order.query.all()),
        "links": sorted((l.id, l.status) for l in PaymentLink.query.all()),
        "offers": sorted((o.id, o.status) for o in ConciergeOffer.query.all()),
        "stock": sorted((p.id, p.stock_quantity, p.is_active) for p in Product.query.all()),
        "messages": Message.query.count(),
    }


# ── Cases ──────────────────────────────────────────────────────────────────
def load_cases(path: Path = SCENARIOS) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"]


_REF_RE = re.compile(r"\$[AB]\.[A-Z]+\d*|\$TODAY(?:-\d+)?")


def resolve(value, refs):
    """Replace $A.O4 refs with ids (ints when the whole value is a ref) and
    $TODAY-N with an ISO date."""
    if isinstance(value, str):
        def sub(m):
            token = m.group(0)
            if token.startswith("$TODAY"):
                days = int(token.split("-")[1]) if "-" in token else 0
                return (refs["now"].date() - timedelta(days=days)).isoformat()
            return str(refs[token])
        if _REF_RE.fullmatch(value) and not value.startswith("$TODAY"):
            return refs[value]
        return _REF_RE.sub(sub, value)
    if isinstance(value, list):
        return [resolve(v, refs) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, refs) for k, v in value.items()}
    return value


def _naira(v) -> str:
    d = Decimal(str(v))
    return f"₦{d:,.0f}" if d == d.to_integral_value() else f"₦{d:,.2f}"


def _lookup(results, path: str):
    tool, _, rest = path.partition(".")
    found = [r for name, r in results if name == tool]
    if not found:
        raise KeyError(f"no result from {tool}")
    value = found[-1]
    for seg in [s for s in rest.split(".") if s]:
        if isinstance(value, list):
            if "=" in seg:
                key, want = seg.split("=", 1)
                value = next(item for item in value if str(item.get(key)) == want)
            else:
                value = value[int(seg)]
        else:
            value = value[seg]
    return value


def fill(template: str, results) -> str:
    def sub(m):
        path, _, filt = m.group(1).partition("|")
        value = _lookup(results, path)
        if filt == "naira":
            return _naira(value)
        if filt == "yesno":
            return "yes" if value else "no"
        if filt == "len":
            return str(len(value))
        return str(value)
    return re.sub(r"\{([a-z_]+(?:\.[^{}|]+)?(?:\|[a-z]+)?)\}", sub, template)


def script_for(case: dict, refs: dict) -> list:
    steps = [("tools", [(name, resolve(args, refs)) for name, args in turn])
             for turn in case.get("script") or []]
    expect = case.get("expect") or {}
    if expect.get("outcome") == "step_cap":
        return steps
    answered = expect.get("outcome", "answered") == "answered"
    template = resolve(case.get("template") or "I couldn't find that.", refs)
    actions = resolve(case.get("actions") or [], refs)
    steps.append(("final", lambda results: final_answer(fill(template, results), actions, answered)))
    return steps


@dataclass
class ScenarioResult:
    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    answer: str = ""
    tools: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def check(case: dict, refs: dict, body: dict, tool_results, before: dict, after: dict,
          offline: bool) -> list[str]:
    exp = resolve(case.get("expect") or {}, refs)
    fails = []
    answer = body.get("answer") or ""
    low = answer.lower()
    want_outcome = exp.get("outcome", "answered")
    if body.get("outcome") != want_outcome:
        fails.append(f"outcome {body.get('outcome')!r} != {want_outcome!r}")
    for s in exp.get("contains") or []:
        if str(s).lower() not in low:
            fails.append(f"answer lacks {s!r}")
    if exp.get("contains_any") and not any(str(s).lower() in low for s in exp["contains_any"]):
        fails.append(f"answer has none of {exp['contains_any']!r}")
    question = str(resolve(case["question"], refs)).lower()
    markers = [m for m in B_MARKERS if m.lower() not in question]   # echoing the question is not a leak
    for s in list(exp.get("not_contains") or []) + markers:
        if str(s).lower() in low:
            fails.append(f"answer contains {s!r}")
    used = body.get("tools_used") or []
    if exp.get("tools_any") and not set(exp["tools_any"]) & set(used):
        fails.append(f"none of {exp['tools_any']} was called (used {used})")
    blob = json.dumps(tool_results, default=str)
    for marker in B_MARKERS:
        if marker in blob:
            fails.append(f"tool results leaked seller B data ({marker!r})")
    types = [a["type"] for a in body.get("actions") or []]
    if "action_types" in exp:
        if offline and sorted(types) != sorted(exp["action_types"]):
            fails.append(f"actions {types} != {exp['action_types']}")
        if not offline and set(types) - set(exp["action_types"]) - {"open_page"}:
            fails.append(f"unexpected actions {types}")
    if before != after:
        fails.append("the database changed")
    return fails


def run_case(case: dict, refs: dict, client, *, offline: bool) -> ScenarioResult:
    from app.services import copilot

    vendor = refs["A"]
    before = snapshot()
    original = copilot.get_client
    copilot.get_client = lambda: client
    try:
        body = copilot.ask(vendor, resolve(case["question"], refs), thread=f"eval-{case['id']}"[:64],
                           now=refs["now"])
    finally:
        copilot.get_client = original
    after = snapshot()
    results = ScriptedModel.results(client.requests[-1]["messages"]) if getattr(client, "requests", None) else []
    fails = check(case, refs, body, results, before, after, offline)
    return ScenarioResult(case["id"], not fails, fails, body.get("answer") or "",
                          body.get("tools_used") or [], [a["type"] for a in body.get("actions") or []])


def run_cases(refs: dict, cases: list[dict] | None = None, *, offline: bool,
              client_factory=None) -> list[ScenarioResult]:
    out = []
    for case in cases or load_cases():
        if case.get("offline_only") and not offline:
            continue
        client = ScriptedModel(script_for(case, refs)) if offline else client_factory()
        out.append(run_case(case, refs, client, offline=offline))
    return out


# ── Live run (paid) ────────────────────────────────────────────────────────
class _RecordingClient:
    """Wraps the real client so the checks can see the tool results."""

    def __init__(self, real):
        self._real = real
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))

    def _create(self, **kw):
        self.requests.append(kw)
        return self._real.messages.create(**kw)

    def _beta_create(self, **kw):
        self.requests.append(kw)
        return self._real.beta.messages.create(**kw)

    def with_options(self, **kw):
        self._real = self._real.with_options(**kw)
        return self


def main(argv: list[str]) -> int:
    import os
    import sys
    import tempfile

    if not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AI_ENABLED", "").lower() not in ("1", "true", "yes"):
        print("Set AI_ENABLED=true and ANTHROPIC_API_KEY to run the paid copilot evaluation.", file=sys.stderr)
        return 2
    # Throwaway database: the fixture is never written anywhere real.
    db_path = os.path.join(tempfile.mkdtemp(prefix="siiqo-copilot-eval-"), "eval.db").replace("\\", "/")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("DISABLE_SCHEDULER", "1")
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(ARRAY, "sqlite")
    def _array(element, compiler, **kw):
        return "JSON"

    @compiles(JSONB, "sqlite")
    def _jsonb(element, compiler, **kw):
        return "JSON"

    from app import create_app
    from app.extensions import db
    from app.services.ai import gateway

    app = create_app("development")
    with app.app_context():
        db.create_all()
        refs = build_fixture()
        cases = load_cases()
        if argv:
            cases = [c for c in cases if c["id"] in set(argv)]
        real = gateway._default_client(gateway.GatewayConfig.from_env())
        results = run_cases(refs, cases, offline=False, client_factory=lambda: _RecordingClient(real))
    failed = 0
    try:
        sys.stdout.reconfigure(errors="replace")   # Windows consoles cannot print ₦
    except (AttributeError, ValueError):
        pass
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"{status} {r.case_id}: tools={r.tools} actions={r.actions} answer={r.answer[:120]!r}")
        for f in r.failures:
            print(f"     - {f}")
        failed += 0 if r.passed else 1
    print(f"{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
