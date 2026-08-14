"""
events.py — Events and Ticketing Routes
Handles: event creation, ticket types, ticket purchases, check-ins, validation
"""
import logging
import re
from datetime import datetime
from decimal import Decimal
from slugify import slugify

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, jwt_required
from sqlalchemy import or_, and_
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.user import User, Storefront, UserRole
from app.models.event import Event, TicketType, TicketPurchase
from app.models.order import Order, OrderItem
from app.utils.upload import save_uploaded_file
from app.utils.email import send_siiqo_email

events_bp = Blueprint('events', __name__)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def _require_vendor_storefront(user_id):
    """Returns (user, storefront) or (None, None) if not a vendor."""
    user = db.session.get(User, int(user_id))
    if not user:
        return None, None
    
    # Check if user is vendor or admin
    if user.role not in [UserRole.VENDOR, UserRole.ADMIN]:
        if user.storefront is not None:
            user.role = UserRole.VENDOR
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            return None, None
    
    if not user.storefront:
        return user, None
    
    return user, user.storefront


def _generate_unique_slug(title, event_id=None):
    """Generate a unique slug for an event"""
    base_slug = slugify(title)
    slug = base_slug
    counter = 1
    
    while True:
        query = Event.query.filter_by(slug=slug)
        if event_id:
            query = query.filter(Event.id != event_id)
        
        if not query.first():
            return slug
        
        slug = f"{base_slug}-{counter}"
        counter += 1


# ---------------------------------------------------------------------------
# PUBLIC EVENT ROUTES
# ---------------------------------------------------------------------------

@events_bp.route('/events', methods=['GET'])
def list_events():
    """
    List all published events with filters
    Query params:
    - city, state, country: location filters
    - event_type: filter by type (concert, workshop, etc.)
    - event_format: in-person, online, hybrid
    - is_free: true/false (events with free tickets)
    - upcoming: true (only future events)
    - page, per_page: pagination
    """
    try:
        # Filters
        city = request.args.get('city')
        state = request.args.get('state')
        country = request.args.get('country')
        event_type = request.args.get('event_type')
        event_format = request.args.get('event_format')
        is_free = request.args.get('is_free', '').lower() == 'true'
        upcoming = request.args.get('upcoming', 'true').lower() == 'true'
        
        # Pagination
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        
        # Base query - only published and active events
        query = Event.query.filter_by(
            is_published=True,
            is_active=True,
            is_deleted=False
        )
        
        # Apply filters
        if upcoming:
            query = query.filter(Event.end_date >= datetime.utcnow())
        
        if city:
            query = query.filter(Event.city.ilike(f'%{city}%'))
        
        if state:
            query = query.filter(Event.state.ilike(f'%{state}%'))
        
        if country:
            query = query.filter(Event.country.ilike(f'%{country}%'))
        
        if event_type:
            query = query.filter(Event.event_type == event_type)
        
        if event_format:
            query = query.filter(Event.event_format == event_format)
        
        # Order by start date
        query = query.order_by(Event.start_date.asc())
        
        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        
        # Filter by free tickets if requested
        events = []
        for event in pagination.items:
            if is_free and not event.has_free_tickets:
                continue
            events.append(event.to_dict())
        
        return jsonify({
            'events': events,
            'page': page,
            'per_page': per_page,
            'total': pagination.total,
            'pages': pagination.pages
        }), 200
        
    except Exception as e:
        logger.error(f"Error listing events: {e}")
        return jsonify({'message': 'Failed to list events'}), 500


@events_bp.route('/events/<slug>', methods=['GET'])
def get_event(slug):
    """Get event details by slug"""
    try:
        event = Event.query.filter_by(
            slug=slug,
            is_published=True,
            is_active=True,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        # Increment view count
        event.view_count = (event.view_count or 0) + 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        
        return jsonify(event.to_dict(include_ticket_types=True)), 200
        
    except Exception as e:
        logger.error(f"Error getting event: {e}")
        return jsonify({'message': 'Failed to get event'}), 500


# ---------------------------------------------------------------------------
# VENDOR EVENT MANAGEMENT ROUTES
# ---------------------------------------------------------------------------

@events_bp.route('/vendor/events', methods=['GET'])
@jwt_required()
def get_vendor_events():
    """Get all events for the authenticated vendor"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        # Get all events for this vendor
        events = Event.query.filter_by(
            vendor_id=user.id,
            is_deleted=False
        ).order_by(Event.created_at.desc()).all()
        
        return jsonify({
            'events': [event.to_dict(include_ticket_types=True) for event in events]
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting vendor events: {e}")
        return jsonify({'message': 'Failed to get events'}), 500


@events_bp.route('/vendor/events', methods=['POST'])
@jwt_required()
def create_event():
    """Create a new event"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        data = request.form if request.form else (request.get_json() or {})
        
        # Support aliases for field names sent by frontend
        title = (data.get('title') or '').strip()
        description = (data.get('description') or '').strip()
        start_date_raw = data.get('start_date') or data.get('start_datetime')
        end_date_raw = data.get('end_date') or data.get('end_datetime')
        event_type = data.get('event_type') or 'in-person'
        
        if not title:
            return jsonify({'message': 'Event title is required'}), 400
        if not description:
            return jsonify({'message': 'Event description is required'}), 400
        if not start_date_raw or not end_date_raw:
            return jsonify({'message': 'Start and end dates are required'}), 400
        
        # Parse dates
        try:
            start_date = datetime.fromisoformat(str(start_date_raw).replace('Z', '+00:00'))
            end_date = datetime.fromisoformat(str(end_date_raw).replace('Z', '+00:00'))
        except ValueError:
            return jsonify({'message': 'Invalid date format. Use ISO 8601'}), 400
        
        # Validate dates
        if end_date <= start_date:
            return jsonify({'message': 'End date must be after start date'}), 400
        
        # Generate unique slug
        slug = _generate_unique_slug(title)
        
        raw_cap = data.get('total_capacity') or data.get('capacity')
        total_capacity = int(raw_cap) if raw_cap and str(raw_cap).isdigit() else None
        
        # Create event
        event = Event(
            storefront_id=storefront.id,
            vendor_id=user.id,
            title=title,
            slug=slug,
            description=description,
            start_date=start_date,
            end_date=end_date,
            timezone=data.get('timezone', 'Africa/Lagos'),
            event_type=event_type,
            event_format=data.get('event_format', 'in-person' if event_type != 'online' else 'online'),
            venue_name=data.get('venue_name'),
            venue_address=data.get('venue_address') or data.get('location'),
            city=data.get('city'),
            state=data.get('state'),
            country=data.get('country', 'Nigeria'),
            latitude=float(data['latitude']) if data.get('latitude') else None,
            longitude=float(data['longitude']) if data.get('longitude') else None,
            meeting_url=data.get('meeting_url') or data.get('online_link'),
            meeting_password=data.get('meeting_password'),
            total_capacity=total_capacity,
            is_active=True,
            is_published=str(data.get('is_published', 'false')).lower() in ('true', '1', 'yes') or data.get('status') == 'published',
            show_on_storefront=str(data.get('show_on_storefront', 'true')).lower() in ('true', '1', 'yes'),
            show_on_marketplace=str(data.get('show_on_marketplace', 'true')).lower() in ('true', '1', 'yes'),
            meta_title=data.get('meta_title') or data.get('seo_title'),
            meta_description=data.get('meta_description') or data.get('seo_description'),
            terms_and_conditions=data.get('terms_and_conditions'),
            contact_email=data.get('contact_email'),
            contact_phone=data.get('contact_phone'),
        )
        
        # Handle cover image upload (File or URL string)
        cover_file = request.files.get('cover_image')
        if cover_file and cover_file.filename:
            try:
                event.cover_image = save_uploaded_file(cover_file, subfolder='events')
            except ValueError as e:
                return jsonify({'message': str(e)}), 400
        elif data.get('cover_image') and isinstance(data['cover_image'], str):
            event.cover_image = data['cover_image']
        
        # Handle multiple images if provided
        if data.get('images'):
            event.images = data['images']
        
        db.session.add(event)
        db.session.commit()
        
        logger.info(f"Event created: {event.id} by vendor {user.id}")
        
        return jsonify({
            'message': 'Event created successfully',
            'event': event.to_dict(include_ticket_types=True)
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating event: {e}")
        return jsonify({'message': 'Failed to create event'}), 500


@events_bp.route('/vendor/events/<int:event_id>', methods=['PUT'])
@jwt_required()
def update_event(event_id):
    """Update an existing event"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        data = request.form if request.form else (request.get_json() or {})
        
        # Support field name aliases from frontend
        new_title = (data.get('title') or '').strip()
        if new_title and new_title != event.title:
            event.title = new_title
            event.slug = _generate_unique_slug(new_title, event_id)
        
        if data.get('description'):
            event.description = data['description']
        
        # Accept start_datetime (frontend) or start_date (backend)
        start_date_raw = data.get('start_datetime') or data.get('start_date')
        end_date_raw = data.get('end_datetime') or data.get('end_date')
        
        if start_date_raw:
            try:
                event.start_date = datetime.fromisoformat(str(start_date_raw).replace('Z', '+00:00'))
            except ValueError:
                return jsonify({'message': 'Invalid start date format'}), 400
        
        if end_date_raw:
            try:
                event.end_date = datetime.fromisoformat(str(end_date_raw).replace('Z', '+00:00'))
            except ValueError:
                return jsonify({'message': 'Invalid end date format'}), 400
        
        # Validate dates only if both are present
        if event.start_date and event.end_date and event.end_date <= event.start_date:
            return jsonify({'message': 'End date must be after start date'}), 400
        
        # Accept field aliases
        if data.get('location') or data.get('venue_address'):
            event.venue_address = data.get('venue_address') or data.get('location')
        
        if data.get('online_link') or data.get('meeting_url'):
            event.meeting_url = data.get('meeting_url') or data.get('online_link')
        
        if data.get('seo_title') or data.get('meta_title'):
            event.meta_title = data.get('meta_title') or data.get('seo_title')
        
        if data.get('seo_description') or data.get('meta_description'):
            event.meta_description = data.get('meta_description') or data.get('seo_description')
        
        # Handle capacity
        raw_cap = data.get('total_capacity') or data.get('capacity')
        if raw_cap and str(raw_cap).isdigit():
            event.total_capacity = int(raw_cap)
        
        # Handle is_published from status or is_published field
        if data.get('status'):
            event.is_published = data['status'] == 'published'
        elif 'is_published' in data:
            event.is_published = str(data['is_published']).lower() in ('true', '1', 'yes')
        
        # Direct updatable string fields
        for field in ['timezone', 'event_type', 'event_format', 'venue_name', 'city',
                      'state', 'country', 'meeting_password', 'terms_and_conditions',
                      'contact_email', 'contact_phone']:
            if data.get(field):
                setattr(event, field, data[field])
        
        # Boolean fields
        for field in ['show_on_storefront', 'show_on_marketplace', 'is_active']:
            if field in data:
                setattr(event, field, str(data[field]).lower() in ('true', '1', 'yes'))
        
        # Float fields
        if data.get('latitude'):
            try:
                event.latitude = float(data['latitude'])
            except (ValueError, TypeError):
                pass
        if data.get('longitude'):
            try:
                event.longitude = float(data['longitude'])
            except (ValueError, TypeError):
                pass
        
        # Handle cover image file upload
        cover_file = request.files.get('cover_image') if request.files else None
        if cover_file and cover_file.filename:
            try:
                event.cover_image = save_uploaded_file(cover_file, subfolder='events')
            except ValueError as e:
                return jsonify({'message': str(e)}), 400
        elif data.get('cover_image') and isinstance(data.get('cover_image'), str):
            event.cover_image = data['cover_image']
        
        db.session.commit()
        
        logger.info(f"Event updated: {event.id} by vendor {user.id}")
        
        return jsonify({
            'message': 'Event updated successfully',
            'event': event.to_dict(include_ticket_types=True)
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating event: {e}")
        return jsonify({'message': 'Failed to update event', 'error': str(e)}), 500


@events_bp.route('/vendor/events/<int:event_id>', methods=['DELETE'])
@jwt_required()
def delete_event(event_id):
    """Soft delete an event"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        # Check if there are any ticket purchases
        has_purchases = TicketPurchase.query.filter_by(event_id=event.id).count() > 0
        
        if has_purchases:
            return jsonify({
                'message': 'Cannot delete event with ticket purchases. Unpublish instead.'
            }), 400
        
        # Soft delete
        event.is_deleted = True
        event.is_published = False
        db.session.commit()
        
        logger.info(f"Event deleted: {event.id} by vendor {user.id}")
        
        return jsonify({'message': 'Event deleted successfully'}), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting event: {e}")
        return jsonify({'message': 'Failed to delete event'}), 500


# ---------------------------------------------------------------------------
# TICKET TYPE MANAGEMENT ROUTES
# ---------------------------------------------------------------------------

@events_bp.route('/vendor/events/<int:event_id>/ticket-types', methods=['POST'])
@jwt_required()
def create_ticket_type(event_id):
    """Create a ticket type for an event"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        data = request.get_json()
        
        # Validate required fields
        if not data.get('name'):
            return jsonify({'message': 'Ticket name is required'}), 400
        
        is_free = data.get('is_free', False)
        price = Decimal(data.get('price', 0))
        
        if not is_free and price <= 0:
            return jsonify({'message': 'Paid tickets must have price > 0'}), 400
        
        # Create ticket type
        ticket_type = TicketType(
            event_id=event.id,
            name=data['name'],
            description=data.get('description'),
            is_free=is_free,
            price=0 if is_free else price,
            quantity_available=data.get('quantity_available'),
            min_per_order=data.get('min_per_order', 1),
            max_per_order=data.get('max_per_order', 10),
            sale_start_date=datetime.fromisoformat(data['sale_start_date'].replace('Z', '+00:00')) if data.get('sale_start_date') else None,
            sale_end_date=datetime.fromisoformat(data['sale_end_date'].replace('Z', '+00:00')) if data.get('sale_end_date') else None,
            is_active=data.get('is_active', True),
            benefits=data.get('benefits', [])
        )
        
        db.session.add(ticket_type)
        db.session.commit()
        
        logger.info(f"Ticket type created: {ticket_type.id} for event {event.id}")
        
        return jsonify({
            'message': 'Ticket type created successfully',
            'ticket_type': ticket_type.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating ticket type: {e}")
        return jsonify({'message': 'Failed to create ticket type'}), 500


@events_bp.route('/vendor/events/<int:event_id>/ticket-types/<int:ticket_type_id>', methods=['PUT'])
@jwt_required()
def update_ticket_type(event_id, ticket_type_id):
    """Update a ticket type"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        ticket_type = TicketType.query.filter_by(
            id=ticket_type_id,
            event_id=event.id
        ).first()
        
        if not ticket_type:
            return jsonify({'message': 'Ticket type not found'}), 404
        
        data = request.get_json()
        
        # Update fields
        updatable_fields = [
            'name', 'description', 'is_free', 'price', 'quantity_available',
            'min_per_order', 'max_per_order', 'is_active', 'benefits'
        ]
        
        for field in updatable_fields:
            if field in data:
                if field == 'price':
                    setattr(ticket_type, field, Decimal(data[field]))
                else:
                    setattr(ticket_type, field, data[field])
        
        if 'sale_start_date' in data and data['sale_start_date']:
            ticket_type.sale_start_date = datetime.fromisoformat(data['sale_start_date'].replace('Z', '+00:00'))
        
        if 'sale_end_date' in data and data['sale_end_date']:
            ticket_type.sale_end_date = datetime.fromisoformat(data['sale_end_date'].replace('Z', '+00:00'))
        
        db.session.commit()
        
        logger.info(f"Ticket type updated: {ticket_type.id}")
        
        return jsonify({
            'message': 'Ticket type updated successfully',
            'ticket_type': ticket_type.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating ticket type: {e}")
        return jsonify({'message': 'Failed to update ticket type'}), 500


@events_bp.route('/vendor/events/<int:event_id>/ticket-types/<int:ticket_type_id>', methods=['DELETE'])
@jwt_required()
def delete_ticket_type(event_id, ticket_type_id):
    """Delete a ticket type (only if no purchases)"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        ticket_type = TicketType.query.filter_by(
            id=ticket_type_id,
            event_id=event.id
        ).first()
        
        if not ticket_type:
            return jsonify({'message': 'Ticket type not found'}), 404
        
        # Check if there are any purchases
        has_purchases = TicketPurchase.query.filter_by(ticket_type_id=ticket_type.id).count() > 0
        
        if has_purchases:
            return jsonify({
                'message': 'Cannot delete ticket type with purchases. Deactivate instead.'
            }), 400
        
        db.session.delete(ticket_type)
        db.session.commit()
        
        logger.info(f"Ticket type deleted: {ticket_type_id}")
        
        return jsonify({'message': 'Ticket type deleted successfully'}), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting ticket type: {e}")
        return jsonify({'message': 'Failed to delete ticket type'}), 500


# ---------------------------------------------------------------------------
# VENDOR EVENT ANALYTICS
# ---------------------------------------------------------------------------

@events_bp.route('/vendor/events/<int:event_id>/analytics', methods=['GET'])
@jwt_required()
def get_event_analytics(event_id):
    """Get analytics for an event"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        # Get ticket purchases statistics
        purchases = TicketPurchase.query.filter_by(event_id=event.id).all()
        
        total_tickets_sold = len(purchases)
        total_revenue = sum(float(p.price_paid) for p in purchases)
        free_tickets_issued = sum(1 for p in purchases if p.price_paid == 0)
        paid_tickets_sold = total_tickets_sold - free_tickets_issued
        
        # Tickets by type
        tickets_by_type = {}
        revenue_by_type = {}
        
        for tt in event.ticket_types:
            type_purchases = [p for p in purchases if p.ticket_type_id == tt.id]
            tickets_by_type[tt.name] = len(type_purchases)
            revenue_by_type[tt.name] = sum(float(p.price_paid) for p in type_purchases)
        
        # Check-in statistics
        checked_in_count = sum(1 for p in purchases if p.is_checked_in)
        
        return jsonify({
            'event': event.to_dict(include_ticket_types=True),
            'analytics': {
                'total_tickets_sold': total_tickets_sold,
                'free_tickets_issued': free_tickets_issued,
                'paid_tickets_sold': paid_tickets_sold,
                'total_revenue': total_revenue,
                'tickets_by_type': tickets_by_type,
                'revenue_by_type': revenue_by_type,
                'checked_in_count': checked_in_count,
                'check_in_rate': (checked_in_count / total_tickets_sold * 100) if total_tickets_sold > 0 else 0,
                'view_count': event.view_count or 0,
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting event analytics: {e}")
        return jsonify({'message': 'Failed to get analytics'}), 500


# ---------------------------------------------------------------------------
# VENDOR TICKET MANAGEMENT
# ---------------------------------------------------------------------------

@events_bp.route('/vendor/events/<int:event_id>/tickets', methods=['GET'])
@jwt_required()
def get_event_tickets(event_id):
    """Get all ticket purchases for an event"""
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        event = Event.query.filter_by(
            id=event_id,
            vendor_id=user.id,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        # Get all ticket purchases
        purchases = TicketPurchase.query.filter_by(
            event_id=event.id
        ).order_by(TicketPurchase.created_at.desc()).all()
        
        return jsonify({
            'tickets': [p.to_dict() for p in purchases]
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting event tickets: {e}")
        return jsonify({'message': 'Failed to get tickets'}), 500



# ---------------------------------------------------------------------------
# TICKET PURCHASING ROUTES (PUBLIC/BUYER)
# ---------------------------------------------------------------------------

@events_bp.route('/events/<slug>/purchase-tickets', methods=['POST'])
@jwt_required()
def purchase_tickets(slug):
    """
    Purchase tickets for an event
    For free tickets: creates ticket immediately
    For paid tickets: creates order and tickets after payment
    """
    try:
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        
        if not user:
            return jsonify({'message': 'User not found'}), 404
        
        event = Event.query.filter_by(
            slug=slug,
            is_published=True,
            is_active=True,
            is_deleted=False
        ).first()
        
        if not event:
            return jsonify({'message': 'Event not found'}), 404
        
        data = request.get_json()
        
        # Validate required fields
        if not data.get('ticket_type_id') or not data.get('quantity'):
            return jsonify({'message': 'ticket_type_id and quantity are required'}), 400
        
        ticket_type = TicketType.query.filter_by(
            id=data['ticket_type_id'],
            event_id=event.id,
            is_active=True
        ).first()
        
        if not ticket_type:
            return jsonify({'message': 'Ticket type not found or not available'}), 404
        
        quantity = int(data['quantity'])
        
        # Validate quantity
        if quantity < ticket_type.min_per_order:
            return jsonify({'message': f'Minimum {ticket_type.min_per_order} tickets required'}), 400
        
        if quantity > ticket_type.max_per_order:
            return jsonify({'message': f'Maximum {ticket_type.max_per_order} tickets allowed'}), 400
        
        # Check if ticket type is on sale
        if not ticket_type.is_on_sale:
            return jsonify({'message': 'Tickets not available for sale at this time'}), 400
        
        # Check if sold out
        if ticket_type.is_sold_out:
            return jsonify({'message': 'This ticket type is sold out'}), 400
        
        # Check if enough tickets available
        if ticket_type.quantity_available is not None:
            if ticket_type.tickets_remaining < quantity:
                return jsonify({
                    'message': f'Only {ticket_type.tickets_remaining} tickets remaining'
                }), 400
        
        # Check event capacity
        if event.total_capacity is not None:
            if event.tickets_remaining < quantity:
                return jsonify({
                    'message': f'Only {event.tickets_remaining} tickets remaining for this event'
                }), 400
        
        # Buyer information
        buyer_name = data.get('buyer_name', user.full_name)
        buyer_email = data.get('buyer_email', user.email)
        buyer_phone = data.get('buyer_phone', user.phone)
        
        if not buyer_name or not buyer_email:
            return jsonify({'message': 'Buyer name and email are required'}), 400
        
        # Calculate total price
        total_price = float(ticket_type.price) * quantity
        
        # FREE TICKETS - Create immediately
        if ticket_type.is_free or total_price == 0:
            tickets = []
            
            for i in range(quantity):
                ticket = TicketPurchase(
                    event_id=event.id,
                    ticket_type_id=ticket_type.id,
                    buyer_id=user.id,
                    buyer_name=buyer_name,
                    buyer_email=buyer_email,
                    buyer_phone=buyer_phone,
                    price_paid=0,
                    quantity=1,
                    status='ACTIVE'
                )
                db.session.add(ticket)
                tickets.append(ticket)
            
            # Update sold counts
            ticket_type.quantity_sold = (ticket_type.quantity_sold or 0) + quantity
            event.tickets_sold = (event.tickets_sold or 0) + quantity
            
            db.session.commit()
            
            # Send ticket confirmation email (non-blocking background thread)
            try:
                event_date_str = event.start_date.strftime('%A, %B %d, %Y') if event.start_date else 'TBA'
                event_time_str = event.start_date.strftime('%I:%M %p') if event.start_date else 'TBA'
                send_siiqo_email(
                    to_email=buyer_email,
                    subject=f"🎟️ Your Free Ticket: {event.title}",
                    template_name="ticket_issued",
                    buyer_name=buyer_name,
                    event_title=event.title,
                    ticket_type_name=ticket_type.name,
                    ticket_code=tickets[0].ticket_code if tickets else '',
                    quantity=quantity,
                    event_date=event_date_str,
                    event_time=event_time_str,
                    event_location=event.venue_address or event.city or '',
                    event_format=event.event_format or 'in-person',
                    total_price='0',
                    is_free=True,
                    tickets_url='https://siiqo.com/buyer/tickets',
                    year=datetime.utcnow().year,
                )
            except Exception as email_err:
                logger.warning(f"Ticket email failed (non-fatal): {email_err}")
            
            logger.info(f"Free tickets issued: {len(tickets)} for event {event.id} to user {user.id}")
            
            return jsonify({
                'message': 'Free tickets issued successfully',
                'tickets': [t.to_dict(include_sensitive=True) for t in tickets],
                'is_free': True
            }), 201
        
        # PAID TICKETS - Create order and return payment info
        else:
            # Create order for payment
            order = Order(
                buyer_id=user.id,
                vendor_id=event.vendor_id,
                total_amount=Decimal(str(total_price)),
                status='PENDING',
                payment_method='ESCROW'
            )
            db.session.add(order)
            db.session.flush()  # Get order ID
            
            # Create order item (we'll use product_id=None for tickets)
            order_item = OrderItem(
                order_id=order.id,
                product_id=None,  # Not a product
                price_at_purchase=ticket_type.price,
                quantity=quantity
            )
            db.session.add(order_item)
            
            # Create ticket purchases in PENDING status
            tickets = []
            for i in range(quantity):
                ticket = TicketPurchase(
                    event_id=event.id,
                    ticket_type_id=ticket_type.id,
                    buyer_id=user.id,
                    order_id=order.id,
                    buyer_name=buyer_name,
                    buyer_email=buyer_email,
                    buyer_phone=buyer_phone,
                    price_paid=ticket_type.price,
                    quantity=1,
                    status='PENDING'  # Will be activated after payment
                )
                db.session.add(ticket)
                tickets.append(ticket)
            
            db.session.commit()
            
            logger.info(f"Paid ticket order created: {order.id} for event {event.id}")
            
            return jsonify({
                'message': 'Order created. Complete payment to receive tickets.',
                'order_id': order.id,
                'total_amount': float(total_price),
                'payment_required': True,
                'is_free': False
            }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error purchasing tickets: {e}")
        return jsonify({'message': 'Failed to purchase tickets'}), 500


@events_bp.route('/user/tickets', methods=['GET'])
@jwt_required()
def get_user_tickets():
    """Get all tickets for the authenticated user"""
    try:
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        
        if not user:
            return jsonify({'message': 'User not found'}), 404
        
        # Get all ticket purchases for this user
        purchases = TicketPurchase.query.filter_by(
            buyer_id=user.id
        ).order_by(TicketPurchase.created_at.desc()).all()
        
        return jsonify({
            'tickets': [p.to_dict(include_sensitive=True) for p in purchases]
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting user tickets: {e}")
        return jsonify({'message': 'Failed to get tickets'}), 500


@events_bp.route('/user/tickets/<ticket_code>', methods=['GET'])
@jwt_required()
def get_ticket_details(ticket_code):
    """Get details of a specific ticket"""
    try:
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        
        if not user:
            return jsonify({'message': 'User not found'}), 404
        
        ticket = TicketPurchase.query.filter_by(
            ticket_code=ticket_code,
            buyer_id=user.id
        ).first()
        
        if not ticket:
            return jsonify({'message': 'Ticket not found'}), 404
        
        return jsonify(ticket.to_dict(include_sensitive=True)), 200
        
    except Exception as e:
        logger.error(f"Error getting ticket details: {e}")
        return jsonify({'message': 'Failed to get ticket'}), 500


# ---------------------------------------------------------------------------
# TICKET VALIDATION AND CHECK-IN ROUTES
# ---------------------------------------------------------------------------

@events_bp.route('/vendor/tickets/<ticket_code>/validate', methods=['GET'])
@jwt_required()
def validate_ticket(ticket_code):
    """
    Validate a ticket code (without checking in)
    Used by vendors to verify ticket authenticity
    """
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        ticket = TicketPurchase.query.filter_by(
            ticket_code=ticket_code.upper()
        ).first()
        
        if not ticket:
            return jsonify({
                'valid': False,
                'message': 'Ticket not found'
            }), 404
        
        # Check if this vendor owns the event
        if ticket.event.vendor_id != user.id:
            return jsonify({
                'valid': False,
                'message': 'Ticket is not for your event'
            }), 403
        
        # Check ticket status
        if ticket.status != 'ACTIVE':
            return jsonify({
                'valid': False,
                'message': f'Ticket status: {ticket.status}',
                'ticket': ticket.to_dict()
            }), 200
        
        if ticket.is_checked_in:
            return jsonify({
                'valid': False,
                'message': 'Ticket already used',
                'checked_in_at': ticket.checked_in_at.isoformat() if ticket.checked_in_at else None,
                'ticket': ticket.to_dict()
            }), 200
        
        # Ticket is valid
        return jsonify({
            'valid': True,
            'message': 'Ticket is valid',
            'ticket': ticket.to_dict(),
            'can_check_in': ticket.can_be_checked_in
        }), 200
        
    except Exception as e:
        logger.error(f"Error validating ticket: {e}")
        return jsonify({'message': 'Failed to validate ticket'}), 500


@events_bp.route('/vendor/tickets/<ticket_code>/check-in', methods=['POST'])
@jwt_required()
def check_in_ticket(ticket_code):
    """
    Check in a ticket at the event
    Marks ticket as used
    """
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        ticket = TicketPurchase.query.filter_by(
            ticket_code=ticket_code.upper()
        ).first()
        
        if not ticket:
            return jsonify({
                'success': False,
                'message': 'Ticket not found'
            }), 404
        
        # Check if this vendor owns the event
        if ticket.event.vendor_id != user.id:
            return jsonify({
                'success': False,
                'message': 'Ticket is not for your event'
            }), 403
        
        # Check if can be checked in
        if not ticket.can_be_checked_in:
            if ticket.is_checked_in:
                return jsonify({
                    'success': False,
                    'message': 'Ticket already checked in',
                    'checked_in_at': ticket.checked_in_at.isoformat() if ticket.checked_in_at else None
                }), 400
            else:
                return jsonify({
                    'success': False,
                    'message': f'Ticket cannot be checked in. Status: {ticket.status}'
                }), 400
        
        # Check in the ticket
        ticket.is_checked_in = True
        ticket.checked_in_at = datetime.utcnow()
        ticket.checked_in_by = user.id
        
        db.session.commit()
        
        logger.info(f"Ticket checked in: {ticket.ticket_code} by vendor {user.id}")
        
        return jsonify({
            'success': True,
            'message': 'Ticket checked in successfully',
            'ticket': ticket.to_dict(),
            'checked_in_at': ticket.checked_in_at.isoformat()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error checking in ticket: {e}")
        return jsonify({'message': 'Failed to check in ticket'}), 500


@events_bp.route('/vendor/tickets/<ticket_code>/undo-check-in', methods=['POST'])
@jwt_required()
def undo_check_in(ticket_code):
    """
    Undo a ticket check-in (in case of mistake)
    """
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403
        
        ticket = TicketPurchase.query.filter_by(
            ticket_code=ticket_code.upper()
        ).first()
        
        if not ticket:
            return jsonify({'message': 'Ticket not found'}), 404
        
        # Check if this vendor owns the event
        if ticket.event.vendor_id != user.id:
            return jsonify({'message': 'Ticket is not for your event'}), 403
        
        if not ticket.is_checked_in:
            return jsonify({'message': 'Ticket is not checked in'}), 400
        
        # Undo check-in
        ticket.is_checked_in = False
        ticket.checked_in_at = None
        ticket.checked_in_by = None
        
        db.session.commit()
        
        logger.info(f"Ticket check-in undone: {ticket.ticket_code} by vendor {user.id}")
        
        return jsonify({
            'message': 'Check-in undone successfully',
            'ticket': ticket.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error undoing check-in: {e}")
        return jsonify({'message': 'Failed to undo check-in'}), 500


# ---------------------------------------------------------------------------
# EVENT SEARCH AND DISCOVERY
# ---------------------------------------------------------------------------

@events_bp.route('/events/search', methods=['GET'])
def search_events():
    """
    Search events by keyword
    Query params:
    - q: search query (searches title, description, venue)
    - page, per_page: pagination
    """
    try:
        query_text = request.args.get('q', '').strip()
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        
        if not query_text:
            return jsonify({'message': 'Search query required'}), 400
        
        # Search in title, description, and venue
        search_filter = or_(
            Event.title.ilike(f'%{query_text}%'),
            Event.description.ilike(f'%{query_text}%'),
            Event.venue_name.ilike(f'%{query_text}%'),
            Event.city.ilike(f'%{query_text}%')
        )
        
        query = Event.query.filter(
            Event.is_published == True,
            Event.is_active == True,
            Event.is_deleted == False,
            Event.end_date >= datetime.utcnow(),
            search_filter
        ).order_by(Event.start_date.asc())
        
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        
        return jsonify({
            'events': [event.to_dict() for event in pagination.items],
            'page': page,
            'per_page': per_page,
            'total': pagination.total,
            'pages': pagination.pages
        }), 200
        
    except Exception as e:
        logger.error(f"Error searching events: {e}")
        return jsonify({'message': 'Failed to search events'}), 500

# ---------------------------------------------------------------------------
# EVENT REVIEWS
# ---------------------------------------------------------------------------

from app.models.event import EventReview  # noqa: E402


@events_bp.route('/events/<slug>/reviews', methods=['GET'])
def get_event_reviews(slug):
    """Get all reviews for an event."""
    try:
        event = Event.query.filter_by(slug=slug, is_deleted=False).first()
        if not event:
            return jsonify({'message': 'Event not found'}), 404

        reviews = EventReview.query.filter_by(event_id=event.id)\
            .order_by(EventReview.created_at.desc()).all()

        total   = len(reviews)
        avg     = round(sum(r.rating for r in reviews) / total, 1) if total else 0

        return jsonify({
            'reviews':        [r.to_dict() for r in reviews],
            'total':          total,
            'average_rating': avg,
        }), 200

    except Exception as e:
        logger.error(f"Error fetching reviews: {e}")
        return jsonify({'message': 'Failed to fetch reviews'}), 500


@events_bp.route('/events/<slug>/reviews', methods=['POST'])
@jwt_required()
def add_event_review(slug):
    """
    Add or update a review for an event.
    Requires the user to have attended (has an ACTIVE or USED ticket).
    """
    try:
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        if not user:
            return jsonify({'message': 'User not found'}), 404

        event = Event.query.filter_by(slug=slug, is_deleted=False).first()
        if not event:
            return jsonify({'message': 'Event not found'}), 404

        data   = request.get_json() or {}
        rating = data.get('rating')
        comment = data.get('comment', '').strip()

        if not rating or not isinstance(rating, int) or not (1 <= rating <= 5):
            return jsonify({'message': 'Rating must be an integer between 1 and 5'}), 400

        # Check the user actually has a ticket
        has_ticket = TicketPurchase.query.filter(
            TicketPurchase.event_id == event.id,
            TicketPurchase.buyer_id == user.id,
            TicketPurchase.status.in_(['ACTIVE', 'USED']),
        ).first()

        if not has_ticket:
            return jsonify({'message': 'You must have a ticket to review this event'}), 403

        # Upsert review
        review = EventReview.query.filter_by(
            event_id=event.id, reviewer_id=user.id
        ).first()

        if review:
            review.rating  = rating
            review.comment = comment
            review.updated_at = datetime.utcnow()
            msg = 'Review updated'
        else:
            review = EventReview(
                event_id=event.id,
                reviewer_id=user.id,
                rating=rating,
                comment=comment,
            )
            db.session.add(review)
            msg = 'Review added'

        db.session.commit()
        return jsonify({'message': msg, 'review': review.to_dict()}), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error adding review: {e}")
        return jsonify({'message': 'Failed to save review'}), 500


@events_bp.route('/events/<slug>/reviews/my', methods=['GET'])
@jwt_required()
def get_my_event_review(slug):
    """Get the current user's review for an event (if any)."""
    try:
        user_id = get_jwt_identity()
        event   = Event.query.filter_by(slug=slug, is_deleted=False).first()
        if not event:
            return jsonify({'message': 'Event not found'}), 404

        review = EventReview.query.filter_by(
            event_id=event.id, reviewer_id=int(user_id)
        ).first()

        return jsonify({'review': review.to_dict() if review else None}), 200

    except Exception as e:
        logger.error(f"Error fetching user review: {e}")
        return jsonify({'message': 'Failed to fetch review'}), 500


# ---------------------------------------------------------------------------
# RECURRING EVENTS
# ---------------------------------------------------------------------------

@events_bp.route('/vendor/events/<int:event_id>/recur', methods=['POST'])
@jwt_required()
def create_recurring_event(event_id):
    """
    Generate recurring copies of an event.
    Body: { "pattern": "weekly"|"biweekly"|"monthly", "occurrences": 4 }
    Creates up to `occurrences` future copies, each shifted by the pattern interval.
    Max 12 occurrences per call to prevent abuse.
    """
    try:
        user_id = get_jwt_identity()
        user, storefront = _require_vendor_storefront(user_id)
        if not user or not storefront:
            return jsonify({'message': 'Vendor access required'}), 403

        source = db.session.get(Event, event_id)
        if not source or source.is_deleted:
            return jsonify({'message': 'Event not found'}), 404
        if source.vendor_id != user.id:
            return jsonify({'message': 'Not your event'}), 403

        data = request.get_json() or {}
        pattern     = data.get('pattern', 'weekly')
        occurrences = min(int(data.get('occurrences', 4)), 12)

        from datetime import timedelta

        DELTAS = {
            'daily':     timedelta(days=1),
            'weekly':    timedelta(weeks=1),
            'biweekly':  timedelta(weeks=2),
            'monthly':   timedelta(days=30),
        }

        delta = DELTAS.get(pattern, timedelta(weeks=1))
        duration = (source.end_date - source.start_date) if (source.start_date and source.end_date) else timedelta(hours=2)

        created_events = []
        current_start = source.start_date + delta

        for i in range(occurrences):
            new_end   = current_start + duration
            new_title = source.title  # same title, new dates
            new_slug  = _generate_unique_slug(f"{source.title} {current_start.strftime('%b-%d')}")

            new_event = Event(
                storefront_id=source.storefront_id,
                vendor_id=source.vendor_id,
                title=new_title,
                slug=new_slug,
                description=source.description,
                cover_image=source.cover_image,
                images=source.images,
                start_date=current_start,
                end_date=new_end,
                timezone=source.timezone,
                event_type=source.event_type,
                event_format=source.event_format,
                venue_name=source.venue_name,
                venue_address=source.venue_address,
                city=source.city,
                state=source.state,
                country=source.country,
                latitude=source.latitude,
                longitude=source.longitude,
                meeting_url=source.meeting_url,
                meeting_password=source.meeting_password,
                total_capacity=source.total_capacity,
                is_active=True,
                is_published=source.is_published,
                meta_title=source.meta_title,
                meta_description=source.meta_description,
                contact_email=source.contact_email,
                contact_phone=source.contact_phone,
            )
            db.session.add(new_event)
            db.session.flush()  # get new_event.id

            # Copy ticket types
            for tt in source.ticket_types:
                new_tt = TicketType(
                    event_id=new_event.id,
                    name=tt.name,
                    description=tt.description,
                    price=tt.price,
                    quantity_available=tt.quantity_available,
                    quantity_sold=0,
                    is_free=tt.is_free,
                    benefits=tt.benefits,
                )
                db.session.add(new_tt)

            created_events.append(new_event)
            current_start += delta

        db.session.commit()
        logger.info(f"Created {len(created_events)} recurring events from event {event_id}")

        return jsonify({
            'message': f'Created {len(created_events)} recurring events',
            'events': [e.to_dict() for e in created_events],
        }), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating recurring events: {e}")
        return jsonify({'message': 'Failed to create recurring events'}), 500
