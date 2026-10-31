"""
Dispute cases (Siiqo 2.0, Phase 2 item 2-5).

A case is opened on a paid escrow order by the buyer (or, through the legacy
endpoint, the seller). While it is open the escrow is DISPUTED and every
payout path refuses to pay (see ``app/services/disputes.payout_blocked``).

Statuses:
    open -> awaiting_seller -> awaiting_buyer -> under_review -> resolved

Outcomes (set only by a superadmin, executed by code):
    refund_full | refund_partial | release_to_seller | split

Both tables are new, so ``db.create_all()`` creates them; no ALTER is needed.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class DisputeStatus:
    OPEN = "open"
    AWAITING_SELLER = "awaiting_seller"
    AWAITING_BUYER = "awaiting_buyer"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"

    ACTIVE = (OPEN, AWAITING_SELLER, AWAITING_BUYER, UNDER_REVIEW)
    ALL = ACTIVE + (RESOLVED,)


class DisputeOutcome:
    REFUND_FULL = "refund_full"
    REFUND_PARTIAL = "refund_partial"
    RELEASE_TO_SELLER = "release_to_seller"
    SPLIT = "split"

    ALL = (REFUND_FULL, REFUND_PARTIAL, RELEASE_TO_SELLER, SPLIT)


class DisputeCase(db.Model):
    __tablename__ = "dispute_cases"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(32), unique=True, nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False, index=True)
    escrow_id = db.Column(db.Integer, db.ForeignKey("escrow_transactions.id"), nullable=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Set to str(order_id) while the case is active, NULL once resolved. The
    # unique constraint makes "one open dispute per order" hold in the
    # database too (NULLs never collide in Postgres or SQLite).
    open_key = db.Column(db.String(32), unique=True, nullable=True)

    raised_by = db.Column(db.String(16), nullable=False, default="buyer")   # buyer | seller | admin
    reason = db.Column(db.String(120), nullable=False, default="")
    status = db.Column(db.String(24), nullable=False, default=DisputeStatus.OPEN, index=True)
    # Escrow status before the dispute froze it (restored for a release).
    pre_dispute_escrow_status = db.Column(db.String(50), nullable=True)
    deadline_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    review_started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    review_reason = db.Column(db.String(64), nullable=True)   # both_answered | seller_deadline | ...

    # Reviewer summary (AI, background job). Never shown to the parties.
    summary_status = db.Column(db.String(16), nullable=False, default="none")
    # none | pending | ready | failed | unavailable
    summary = db.Column(db.JSON, nullable=True)
    summary_revision = db.Column(db.Integer, nullable=False, default=0)
    summary_job_id = db.Column(db.Integer, nullable=True)
    summary_error = db.Column(db.String(255), nullable=True)

    # Decision (superadmin) and its deterministic execution (code).
    outcome = db.Column(db.String(24), nullable=True)
    seller_fault = db.Column(db.Boolean, nullable=True)
    decision_note = db.Column(db.Text, nullable=True)
    decided_by_admin_id = db.Column(db.Integer, nullable=True)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    currency = db.Column(db.String(10), nullable=True)
    refund_amount = db.Column(db.Numeric(12, 2), nullable=True)      # to the buyer
    seller_amount = db.Column(db.Numeric(12, 2), nullable=True)      # seller's net share
    fee_kept = db.Column(db.Numeric(12, 2), nullable=True)           # Siiqo fee on the seller's share
    # not_applicable | manual_due | manual_sent
    refund_status = db.Column(db.String(24), nullable=False, default="not_applicable")
    refund_instructions = db.Column(db.Text, nullable=True)
    refund_reference = db.Column(db.String(255), nullable=True)
    refund_marked_by_admin_id = db.Column(db.Integer, nullable=True)
    refund_marked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # not_applicable | released | credited | already_settled | manual_due
    seller_payout_status = db.Column(db.String(24), nullable=False, default="not_applicable")
    seller_payout_note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)

    order = db.relationship("Order")
    evidence = db.relationship("DisputeEvidence", back_populates="case", order_by="DisputeEvidence.id",
                               cascade="all, delete-orphan")


class DisputeEvidence(db.Model):
    """One item of evidence. The AI summary cites items as "E<id>"."""
    __tablename__ = "dispute_evidence"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("dispute_cases.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    party = db.Column(db.String(16), nullable=False)      # buyer | seller | system | admin
    author_user_id = db.Column(db.Integer, nullable=True)
    kind = db.Column(db.String(16), nullable=False)       # text | photo | timeline
    text = db.Column(db.Text, nullable=True)
    file_url = db.Column(db.String(500), nullable=True)
    # When the underlying fact happened (timeline items); defaults to created_at.
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    case = db.relationship("DisputeCase", back_populates="evidence")

    @property
    def cite_id(self) -> str:
        return f"E{self.id}"

    def to_dict(self) -> dict:
        when = self.occurred_at or self.created_at
        return {
            "id": self.id,
            "cite_id": self.cite_id,
            "party": self.party,
            "kind": self.kind,
            "text": self.text,
            "file_url": self.file_url,
            "occurred_at": when.isoformat() if when else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
