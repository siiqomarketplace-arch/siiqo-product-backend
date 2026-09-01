from app.extensions import db
from datetime import datetime
import secrets
import string

def generate_ticket_code(length=12):
    """Generate a unique ticket code (e.g., EVT-ABC123XYZ456)"""
    chars = string.ascii_uppercase + string.digits
    code = ''.join(secrets.choice(chars) for _ in range(length))
    return f"EVT-{code[:6]}-{code[6:]}"

class Event(db.Model):
    """Events created by vendors - concerts, workshops, webinars, meetups, etc."""
    __tablename__ = 'events'
    
    id = db.Column(db.Integer, primary_key=True)
    storefront_id = db.Column(db.Integer, db.ForeignKey('storefronts.id'), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Basic event information
    title = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(300), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    cover_image = db.Column(db.String(500), nullable=True)
    images = db.Column(db.JSON, default=list)  # Additional event images
    
    # Event timing
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime, nullable=False)
    timezone = db.Column(db.String(50), default='Africa/Lagos')
    
    # Event type and format
    event_type = db.Column(db.String(50), nullable=False)  # concert, workshop, webinar, conference, meetup, etc.
    event_format = db.Column(db.String(20), default='in-person')  # in-person, online, hybrid
    
    # Location (for in-person and hybrid events)
    venue_name = db.Column(db.String(255), nullable=True)
    venue_address = db.Column(db.String(500), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    country = db.Column(db.String(100), default='Nigeria')
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    
    # Online event details (for online and hybrid events)
    meeting_url = db.Column(db.String(500), nullable=True)  # Zoom, Google Meet, etc.
    meeting_password = db.Column(db.String(100), nullable=True)
    
    # Capacity and status
    total_capacity = db.Column(db.Integer, nullable=True)  # NULL = unlimited
    tickets_sold = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    is_published = db.Column(db.Boolean, default=False)
    is_deleted = db.Column(db.Boolean, default=False)
    show_on_storefront = db.Column(db.Boolean, default=True)
    show_on_marketplace = db.Column(db.Boolean, default=True)
    
    # Analytics
    view_count = db.Column(db.Integer, default=0)
    
    # SEO
    meta_title = db.Column(db.String(255), nullable=True)
    meta_description = db.Column(db.Text, nullable=True)
    
    # Organizer details
    organizer_name = db.Column(db.String(255), nullable=True)
    organizer_bio = db.Column(db.Text, nullable=True)
    organizer_avatar = db.Column(db.String(500), nullable=True)
    organizer_socials = db.Column(db.JSON, default=dict)
    
    # Event highlights, agenda and FAQs
    agenda = db.Column(db.JSON, default=list)
    faqs = db.Column(db.JSON, default=list)
    
    # Multi-location / Multi-session schedules
    schedules = db.Column(db.JSON, default=list)
    
    # Custom registration form fields & CTA
    custom_fields = db.Column(db.JSON, default=list)
    cta_button_text = db.Column(db.String(100), default='Get Tickets')

    # Additional details
    terms_and_conditions = db.Column(db.Text, nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    contact_phone = db.Column(db.String(20), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    storefront = db.relationship('Storefront', backref='events')
    vendor = db.relationship('User', foreign_keys=[vendor_id])
    ticket_types = db.relationship('TicketType', back_populates='event', cascade="all, delete-orphan")
    ticket_purchases = db.relationship('TicketPurchase', back_populates='event', cascade="all, delete-orphan")
    
    @property
    def is_sold_out(self):
        """Check if event is sold out"""
        if self.total_capacity is None:
            return False
        return self.tickets_sold >= self.total_capacity
    
    @property
    def tickets_remaining(self):
        """Get number of tickets remaining"""
        if self.total_capacity is None:
            return None  # Unlimited
        return max(0, self.total_capacity - self.tickets_sold)
    
    @property
    def has_free_tickets(self):
        """Check if event has any free ticket types"""
        return any(tt.is_free for tt in self.ticket_types if tt.is_active)
    
    @property
    def has_paid_tickets(self):
        """Check if event has any paid ticket types"""
        return any(not tt.is_free for tt in self.ticket_types if tt.is_active)
    
    @property
    def min_ticket_price(self):
        """Get minimum ticket price (for paid tickets)"""
        paid_tickets = [tt.price for tt in self.ticket_types if tt.is_active and not tt.is_free]
        return min(paid_tickets) if paid_tickets else 0
    
    def to_dict(self, include_ticket_types=True):
        """Convert event to dictionary"""
        _start = self.start_date.isoformat() if self.start_date else None
        _end = self.end_date.isoformat() if self.end_date else None
        _status = 'published' if self.is_published else 'draft'
        data = {
            'id': self.id,
            'storefront_id': self.storefront_id,
            'vendor_id': self.vendor_id,
            'title': self.title,
            'slug': self.slug,
            'description': self.description,
            'cover_image': self.cover_image,
            'images': self.images or [],
            # Both naming conventions for frontend compatibility
            'start_date': _start,
            'end_date': _end,
            'start_datetime': _start,
            'end_datetime': _end,
            'timezone': self.timezone,
            'event_type': self.event_type,
            'event_format': self.event_format or 'in-person',
            'venue_name': self.venue_name,
            'venue_address': self.venue_address,
            'location': self.venue_address,       # frontend alias
            'city': self.city,
            'state': self.state,
            'country': self.country,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'meeting_url': self.meeting_url,
            'online_link': self.meeting_url,       # frontend alias
            'meeting_password': self.meeting_password,
            'total_capacity': self.total_capacity,
            'capacity': self.total_capacity,        # frontend alias
            'tickets_sold': self.tickets_sold or 0,
            'tickets_remaining': self.tickets_remaining,
            'is_sold_out': self.is_sold_out,
            'is_active': self.is_active,
            'is_published': self.is_published,
            'status': _status,                     # frontend uses status string
            'show_on_storefront': self.show_on_storefront if self.show_on_storefront is not None else True,
            'show_on_marketplace': self.show_on_marketplace if self.show_on_marketplace is not None else True,
            'view_count': self.view_count or 0,
            'revenue': 0,                          # placeholder; computed in analytics endpoint
            'meta_title': self.meta_title,
            'seo_title': self.meta_title,           # frontend alias
            'meta_description': self.meta_description,
            'seo_description': self.meta_description,  # frontend alias
            'organizer_name': self.organizer_name,
            'organizer_bio': self.organizer_bio,
            'organizer_avatar': self.organizer_avatar,
            'organizer_socials': self.organizer_socials or {},
            'agenda': self.agenda or [],
            'faqs': self.faqs or [],
            'schedules': self.schedules or [],
            'custom_fields': self.custom_fields or [],
            'cta_button_text': self.cta_button_text or 'Get Tickets',
            'contact_email': self.contact_email,
            'contact_phone': self.contact_phone,
            'has_free_tickets': self.has_free_tickets,
            'has_paid_tickets': self.has_paid_tickets,
            'min_ticket_price': float(self.min_ticket_price) if self.min_ticket_price else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        
        if include_ticket_types:
            data['ticket_types'] = [tt.to_dict() for tt in self.ticket_types if tt.is_active]
        
        return data


class TicketType(db.Model):
    """Different ticket types for an event (VIP, Regular, Early Bird, etc.)"""
    __tablename__ = 'ticket_types'
    
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)
    
    name = db.Column(db.String(100), nullable=False)  # e.g., "VIP", "Regular", "Early Bird"
    description = db.Column(db.Text, nullable=True)
    
    # Pricing
    is_free = db.Column(db.Boolean, default=False)
    price = db.Column(db.Numeric(10, 2), default=0.00)
    
    # Availability
    quantity_available = db.Column(db.Integer, nullable=True)  # NULL = unlimited
    quantity_sold = db.Column(db.Integer, default=0)
    min_per_order = db.Column(db.Integer, default=1)
    max_per_order = db.Column(db.Integer, default=10)
    
    # Sales period
    sale_start_date = db.Column(db.DateTime, nullable=True)  # NULL = available immediately
    sale_end_date = db.Column(db.DateTime, nullable=True)  # NULL = until event starts
    
    # Status
    is_active = db.Column(db.Boolean, default=True)
    
    # Benefits/perks (stored as JSON array)
    benefits = db.Column(db.JSON, default=list)  # e.g., ["Front row seat", "Meet & greet", "Free drink"]
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    event = db.relationship('Event', back_populates='ticket_types')
    purchases = db.relationship('TicketPurchase', back_populates='ticket_type', cascade="all, delete-orphan")
    
    @property
    def is_sold_out(self):
        """Check if this ticket type is sold out"""
        if self.quantity_available is None:
            return False
        return self.quantity_sold >= self.quantity_available
    
    @property
    def tickets_remaining(self):
        """Get number of tickets remaining for this type"""
        if self.quantity_available is None:
            return None  # Unlimited
        return max(0, self.quantity_available - self.quantity_sold)
    
    @property
    def is_on_sale(self):
        """Check if this ticket type is currently on sale"""
        now = datetime.utcnow()
        
        # Check if sales have started
        if self.sale_start_date and now < self.sale_start_date:
            return False
        
        # Check if sales have ended
        if self.sale_end_date and now > self.sale_end_date:
            return False
        
        return True
    
    def to_dict(self):
        """Convert ticket type to dictionary"""
        return {
            'id': self.id,
            'event_id': self.event_id,
            'name': self.name,
            'description': self.description,
            'is_free': self.is_free,
            'price': float(self.price) if self.price else 0.00,
            'quantity_available': self.quantity_available,
            'quantity_sold': self.quantity_sold,
            'tickets_remaining': self.tickets_remaining,
            'is_sold_out': self.is_sold_out,
            'min_per_order': self.min_per_order,
            'max_per_order': self.max_per_order,
            'sale_start_date': self.sale_start_date.isoformat() if self.sale_start_date else None,
            'sale_end_date': self.sale_end_date.isoformat() if self.sale_end_date else None,
            'is_on_sale': self.is_on_sale,
            'is_active': self.is_active,
            'benefits': self.benefits or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class TicketPurchase(db.Model):
    """Individual ticket purchase record with unique ticket code"""
    __tablename__ = 'ticket_purchases'
    
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)
    ticket_type_id = db.Column(db.Integer, db.ForeignKey('ticket_types.id'), nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Optional for guest buyers
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)  # NULL for free tickets
    
    # Unique ticket identification
    ticket_code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    qr_code_url = db.Column(db.String(500), nullable=True)  # URL to QR code image
    
    # Buyer information (captured at purchase)
    buyer_name = db.Column(db.String(255), nullable=False)
    buyer_email = db.Column(db.String(255), nullable=False)
    buyer_phone = db.Column(db.String(20), nullable=True)
    
    # Schedule / Location selected
    selected_schedule_id = db.Column(db.String(100), nullable=True)
    selected_schedule_title = db.Column(db.String(255), nullable=True)
    
    # Custom registration responses
    custom_responses = db.Column(db.JSON, default=dict)
    
    # Purchase details
    price_paid = db.Column(db.Numeric(10, 2), default=0.00)
    quantity = db.Column(db.Integer, default=1)  # Usually 1, but can be multiple
    
    # Ticket status
    status = db.Column(db.String(20), default='ACTIVE')  # ACTIVE, USED, CANCELLED, REFUNDED
    is_checked_in = db.Column(db.Boolean, default=False)
    checked_in_at = db.Column(db.DateTime, nullable=True)
    checked_in_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Staff/vendor who checked in
    
    # Transfer tracking (if tickets can be transferred)
    original_buyer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    transferred_at = db.Column(db.DateTime, nullable=True)
    
    # PDF ticket URL (generated after purchase)
    pdf_ticket_url = db.Column(db.String(500), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    event = db.relationship('Event', back_populates='ticket_purchases')
    ticket_type = db.relationship('TicketType', back_populates='purchases')
    buyer = db.relationship('User', foreign_keys=[buyer_id], backref='ticket_purchases')
    order = db.relationship('Order', backref='ticket_purchases')
    checked_in_by_user = db.relationship('User', foreign_keys=[checked_in_by])
    original_buyer = db.relationship('User', foreign_keys=[original_buyer_id])
    
    def __init__(self, **kwargs):
        """Generate unique ticket code on creation"""
        super(TicketPurchase, self).__init__(**kwargs)
        if not self.ticket_code:
            self.ticket_code = self.generate_unique_code()
    
    @staticmethod
    def generate_unique_code():
        """Generate a unique ticket code"""
        while True:
            code = generate_ticket_code()
            existing = TicketPurchase.query.filter_by(ticket_code=code).first()
            if not existing:
                return code
    
    @property
    def is_valid(self):
        """Check if ticket is valid for use"""
        return self.status == 'ACTIVE' and not self.is_checked_in
    
    @property
    def can_be_checked_in(self):
        """Check if ticket can be checked in"""
        if self.status != 'ACTIVE':
            return False
        if self.is_checked_in:
            return False
        
        return True

    def to_dict(self, include_sensitive=False):
        """Convert ticket purchase to dictionary"""
        _price = float(self.price_paid) if self.price_paid else 0.00
        _unit = _price / self.quantity if (self.quantity and self.quantity > 0) else _price
        _status_lower = (self.status or 'active').lower()
        data = {
            'id': self.id,
            'event_id': self.event_id,
            'ticket_type_id': self.ticket_type_id,
            'buyer_id': self.buyer_id,
            'order_id': self.order_id,
            'ticket_code': self.ticket_code,
            'qr_code_url': self.qr_code_url,
            'buyer_name': self.buyer_name,
            'buyer_email': self.buyer_email,
            'buyer_phone': self.buyer_phone,
            'selected_schedule_id': self.selected_schedule_id,
            'selected_schedule_title': self.selected_schedule_title,
            'custom_responses': self.custom_responses or {},
            'price_paid': _price,
            'total_price': _price,  # frontend alias
            'unit_price': _unit,    # frontend alias
            'quantity': self.quantity or 1,
            'status': _status_lower,  # frontend uses lowercase 'active'
            'status_raw': self.status,
            'is_checked_in': self.is_checked_in,
            'checked_in_at': self.checked_in_at.isoformat() if self.checked_in_at else None,
            'is_valid': self.is_valid,
            'can_be_checked_in': self.can_be_checked_in,
            'pdf_ticket_url': self.pdf_ticket_url,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        
        # Include event and ticket type details for convenience
        if self.event:
            data['event'] = {
                'id': self.event.id,
                'title': self.event.title,
                'slug': self.event.slug,
                'start_date': self.event.start_date.isoformat() if self.event.start_date else None,
                'end_date': self.event.end_date.isoformat() if self.event.end_date else None,
                'start_datetime': self.event.start_date.isoformat() if self.event.start_date else None,
                'end_datetime': self.event.end_date.isoformat() if self.event.end_date else None,
                'venue_name': self.event.venue_name,
                'venue_address': self.event.venue_address,
                'location': self.event.venue_address,
                'city': self.event.city,
                'event_format': self.event.event_format,
                'cover_image': self.event.cover_image,
            }
            
            # Include meeting URL only for ticket holders
            if include_sensitive and self.event.meeting_url:
                data['event']['meeting_url'] = self.event.meeting_url
                data['event']['meeting_password'] = self.event.meeting_password
        
        if self.ticket_type:
            data['ticket_type'] = {
                'id': self.ticket_type.id,
                'name': self.ticket_type.name,
                'description': self.ticket_type.description,
                'benefits': self.ticket_type.benefits or [],
            }
        
        return data


class EventReview(db.Model):
    """Reviews and ratings left by attendees after an event."""
    __tablename__ = 'event_reviews'

    id         = db.Column(db.Integer, primary_key=True)
    event_id   = db.Column(db.Integer, db.ForeignKey('events.id', ondelete='CASCADE'), nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    rating     = db.Column(db.Integer, nullable=False)   # 1-5
    comment    = db.Column(db.Text, nullable=True)

    # Ensure one review per user per event
    __table_args__ = (
        db.UniqueConstraint('event_id', 'reviewer_id', name='uq_event_review_user'),
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    event    = db.relationship('Event', backref=db.backref('reviews', lazy='dynamic'))
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])

    def to_dict(self):
        reviewer_name = ''
        if self.reviewer:
            reviewer_name = (
                self.reviewer.business_name
                or f"{self.reviewer.first_name or ''} {self.reviewer.last_name or ''}".strip()
                or self.reviewer.email.split('@')[0]
            )
        return {
            'id':            self.id,
            'event_id':      self.event_id,
            'reviewer_id':   self.reviewer_id,
            'reviewer_name': reviewer_name,
            'rating':        self.rating,
            'comment':       self.comment,
            'created_at':    self.created_at.isoformat() if self.created_at else None,
        }
