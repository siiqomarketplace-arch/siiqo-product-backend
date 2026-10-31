"""
AI seller onboarding (Siiqo 2.0, Phase 1).

An ``OnboardingSession`` holds one seller's answers, photos and drafts while
they go through /start. Nothing here is public: the store, bank account,
products and first Pay Link are created only when the seller publishes.

Statuses: interview -> building -> ready | ready_fallback -> published.
A user has at most one open (not yet published) session. That rule is held
by ``open_user_id``: it equals ``user_id`` while the session is open and is
NULL afterwards, and it is UNIQUE (NULLs never collide on SQLite or Postgres).
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if value else None


STAGE_KEYS = ('copy', 'colours', 'products', 'assemble')


def empty_stages():
    return [{'key': key, 'status': 'pending'} for key in STAGE_KEYS]


class OnboardingSession(db.Model):
    __tablename__ = 'onboarding_sessions'

    STATUSES = ('interview', 'building', 'ready', 'ready_fallback', 'published')
    OPEN_STATUSES = ('interview', 'building', 'ready', 'ready_fallback')
    READY_STATUSES = ('ready', 'ready_fallback')

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    open_user_id = db.Column(db.Integer, nullable=True, unique=True)

    status = db.Column(db.String(20), nullable=False, default='interview', index=True)
    step = db.Column(db.Integer, nullable=False, default=1)

    answers = db.Column(db.JSON, nullable=False, default=dict)
    draft_spec = db.Column(db.JSON, nullable=True)
    draft_listings = db.Column(db.JSON, nullable=True)
    stages = db.Column(db.JSON, nullable=False, default=empty_stages)
    build_meta = db.Column(db.JSON, nullable=True)   # sources, fallback reasons, critic notes

    job_id = db.Column(db.Integer, nullable=True)     # latest build job (jobs.id)

    published_storefront_id = db.Column(db.Integer, db.ForeignKey('storefronts.id'), nullable=True)
    publish_result = db.Column(db.JSON, nullable=True)  # returned again on a repeat publish
    published_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    photos = db.relationship(
        'OnboardingPhoto', backref='session', lazy='select',
        order_by='OnboardingPhoto.id', cascade='all, delete-orphan',
    )

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def product_photos(self):
        return [p for p in self.photos if p.kind == 'product']

    def logo(self):
        return next((p for p in self.photos if p.kind == 'logo'), None)

    def to_dict(self, job=None):
        from app.services.store_spec import template_options_from_spec

        preview = None
        if self.draft_spec:
            try:
                preview = template_options_from_spec(self.draft_spec)
            except Exception:  # noqa: BLE001 - a bad draft must not break polling
                preview = None
        return {
            'id': self.id,
            'status': self.status,
            'step': self.step,
            'answers': self.answers or {},
            'photos': [p.to_dict() for p in self.photos],
            'stages': self.stages or empty_stages(),
            'draft_spec': self.draft_spec,
            'draft_listings': self.draft_listings or [],
            'preview_template_options': preview,
            'build': self.build_meta or None,
            'job': job.to_dict() if job is not None else ({'id': self.job_id} if self.job_id else None),
            'published_storefront_id': self.published_storefront_id,
            'publish_result': self.publish_result,
            'published_at': _iso(self.published_at),
            'created_at': _iso(self.created_at),
            'updated_at': _iso(self.updated_at),
        }


class OnboardingPhoto(db.Model):
    __tablename__ = 'onboarding_photos'

    KINDS = ('product', 'logo')

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('onboarding_sessions.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False, default='product')
    url = db.Column(db.String(500), nullable=False)
    # Optional words the seller typed with the photo (used as-is, never invented).
    caption = db.Column(db.String(200), nullable=True)
    price = db.Column(db.Numeric(12, 2), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'url': self.url,
            'kind': self.kind,
            'caption': self.caption,
            'price': float(self.price) if self.price is not None else None,
            'created_at': _iso(self.created_at),
        }
