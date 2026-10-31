"""
Store revisions (Siiqo 2.0, Phase 1 items 1-6 and 1-8).

A ``StoreRevision`` is a copy of a storefront's ``template_options`` taken
just before the store editor, "Refresh my store with AI" or a revert changes
it, so the seller can undo. Rows are owner-only and never public.

Sources: ``editor`` (saved from the store editor), ``ai_refresh`` (the seller
tapped "Use this" on an AI refresh proposal), ``revert`` (the state replaced
by a revert, so a revert can itself be undone).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class StoreRevision(db.Model):
    __tablename__ = 'store_revisions'

    SOURCES = ('editor', 'ai_refresh', 'revert')

    id = db.Column(db.Integer, primary_key=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey('storefronts.id'), nullable=False, index=True)
    template_options = db.Column(db.JSON, nullable=False, default=dict)
    source = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    def to_dict(self, include_options: bool = True) -> dict:
        tpl = self.template_options or {}
        out = {
            "id": self.id,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "layout_style": tpl.get("layout_style"),
            "sections_control": tpl.get("sections_control"),
        }
        if include_options:
            out["template_options"] = tpl
        return out
