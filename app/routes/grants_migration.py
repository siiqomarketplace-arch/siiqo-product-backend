"""
Grants Migration - ORM-based grant initialization
Temporary endpoint for initializing grants table with seed data.
"""
import logging
from flask import Blueprint, jsonify
from app.extensions import db
from app.models.grant import Grant
from app.middleware.security import limiter

grants_migration_bp = Blueprint('grants_migration', __name__)


@grants_migration_bp.route('/debug-grants', methods=['GET'])
@limiter.limit("10 per minute")
def debug_grants():
    """
    Debug endpoint to check grants visibility
    """
    try:
        from app.models.grant import Grant
        from sqlalchemy import text
        
        # Method 1: ORM query
        orm_count = Grant.query.count()
        orm_published = Grant.query.filter_by(is_published=True).count()
        
        # Method 2: Raw SQL
        raw_result = db.session.execute(text("SELECT COUNT(*) FROM grants"))
        raw_count = raw_result.scalar()
        
        # Method 3: Get all grants
        all_grants = Grant.query.all()
        grants_list = [{'id': g.id, 'name': g.name, 'is_published': g.is_published} for g in all_grants]
        
        return jsonify({
            'orm_total_count': orm_count,
            'orm_published_count': orm_published,
            'raw_sql_count': raw_count,
            'grants_sample': grants_list[:5],
            'session_id': id(db.session),
            'message': 'Debug info - grants visibility check'
        }), 200
        
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500


@grants_migration_bp.route('/init-grants-orm', methods=['POST'])
@limiter.limit("5 per hour")
def init_grants_orm():
    """
    TEMPORARY PUBLIC ENDPOINT - NO AUTH REQUIRED
    Uses ORM to create grants - fixes transaction isolation issue
    REMOVE THIS ENDPOINT AFTER SUCCESSFUL MIGRATION!
    """
    try:
        # Check if table already has data
        count = Grant.query.count()
        if count > 0:
            return jsonify({
                'message': 'Grants table already has data',
                'grants_count': count,
                'status': 'already_initialized'
            }), 200
        
        logging.info("[ORM MIGRATION] Creating grants using ORM...")
        
        # Create grants using ORM - this ensures proper transaction handling
        grants_data = [
            {
                'slug': 'tony-elumelu-foundation-entrepreneurship-programme-2026',
                'name': 'Tony Elumelu Foundation Entrepreneurship Programme 2026',
                'amount': '$5,000 grant + training',
                'category': ['Startups', 'Small Business', 'Youth'],
                'country': 'Africa',
                'eligibility': 'African entrepreneurs aged 18-35 with a business idea or startup less than 3 years old. All sectors considered.',
                'description': '''# Tony Elumelu Foundation Entrepreneurship Programme

The Tony Elumelu Foundation (TEF) Entrepreneurship Programme is Africa's largest entrepreneurship programme, empowering African entrepreneurs with funding, training, and mentorship.

## Programme Benefits
- **$5,000 seed capital** (non-refundable)
- 12-week online business training
- Access to TEF network of 15,000+ alumni
- Mentorship from experienced entrepreneurs
- Opportunities for additional funding

## Key Features
- Open to all 54 African countries
- All business sectors accepted
- Focus on scalable, innovative businesses
- Alumni benefits and networking opportunities

## Application Requirements
- Detailed business plan
- Proof of age (18-35)
- African citizenship or residence
- Business less than 3 years old''',
                'application_tips': 'Focus on the social impact of your business. Show clear evidence of how your idea solves a real problem. Be specific about how you will use the $5,000. Previous winners had detailed financial projections.',
                'deadline': '2026-03-31',
                'status': 'open',
                'official_url': 'https://www.tonyelumelufoundation.org/apply',
                'featured': True,
                'meta_title': 'Tony Elumelu $5,000 Grant 2026 - Apply for TEF Entrepreneurship Programme',
                'meta_description': 'Apply for the Tony Elumelu Foundation $5,000 grant. Get seed funding, training & mentorship for your African startup. Deadline: March 31, 2026.'
            },
            {
                'slug': 'boi-youth-entrepreneurship-support-programme',
                'name': 'Bank of Industry Youth Entrepreneurship Support (YES) Programme',
                'amount': '₦100,000 - ₦5,000,000',
                'category': ['Youth', 'Startups', 'Small Business'],
                'country': 'Nigeria',
                'eligibility': 'Nigerian youths aged 18-35 with innovative business ideas. Priority given to agriculture, tech, creative industries, and manufacturing.',
                'description': '''# BOI YES Programme

The Bank of Industry (BOI) Youth Entrepreneurship Support (YES) Programme provides affordable financing to young Nigerian entrepreneurs.

## Loan Details
- Amount: ₦100,000 to ₦5,000,000
- Interest Rate: 9% per annum
- Tenor: Up to 5 years
- Moratorium: Up to 12 months

## Eligible Sectors
- Agriculture and Agribusiness
- Information Technology
- Creative Industries
- Manufacturing
- Services

## Requirements
- Valid means of identification
- Business plan
- Proof of business registration
- Bank statements (6 months)
- Collateral (for amounts above ₦1M)''',
                'application_tips': 'Prepare a solid business plan with clear projections. Show evidence of market research. Have all documents ready before applying. Consider joining BOI\'s free business training.',
                'deadline': 'Rolling',
                'status': 'open',
                'official_url': 'https://www.boi.ng/youth-entrepreneurship/',
                'featured': True,
                'meta_title': 'BOI Youth Grant 2026 - Up to ₦5M for Nigerian Youth Entrepreneurs',
                'meta_description': 'Apply for Bank of Industry YES Programme. Get up to ₦5 million at 9% interest for your business. For Nigerian youth aged 18-35.'
            },
            {
                'slug': 'smedan-women-business-fund-2026',
                'name': 'SMEDAN Women Business Fund 2026',
                'amount': '₦50,000 - ₦500,000',
                'category': ['Women', 'Small Business'],
                'country': 'Nigeria',
                'eligibility': 'Nigerian women entrepreneurs with registered businesses. Must be actively trading for at least 6 months. All sectors considered.',
                'description': '''# SMEDAN Women Business Fund

The Small and Medium Enterprises Development Agency of Nigeria (SMEDAN) provides grants to support women-owned businesses across Nigeria.

## Grant Features
- Non-refundable grant
- No interest, no collateral
- Business development support
- Access to markets and networks

## Application Requirements
- Business registration certificate
- Valid means of ID
- Business bank account
- Proof of business operations
- Tax Identification Number (TIN)

## Selection Criteria
- Business viability
- Job creation potential
- Innovation
- Social impact''',
                'application_tips': 'Emphasize how your business creates jobs for other women. Show clear financial records. Demonstrate community impact. Join SMEDAN training before applying.',
                'deadline': '2026-09-30',
                'status': 'upcoming',
                'official_url': 'https://www.smedan.gov.ng/women-fund',
                'featured': False,
                'meta_title': 'SMEDAN Women Business Grant 2026 - Up to ₦500,000 for Nigerian Women',
                'meta_description': 'Free business grant for Nigerian women entrepreneurs. Get ₦50,000 to ₦500,000 from SMEDAN. No repayment required. Apply by September 2026.'
            }
        ]
        
        created_grants = []
        for grant_info in grants_data:
            grant = Grant(**grant_info)
            db.session.add(grant)
            created_grants.append(grant.name)
        
        # Commit all grants at once
        db.session.commit()
        
        # Verify after commit - use new query to ensure we see committed data
        total_count = Grant.query.count()
        open_count = Grant.query.filter_by(status='open').count()
        featured_count = Grant.query.filter_by(featured=True).count()
        
        logging.info(f"[ORM MIGRATION SUCCESS] Created {total_count} grants: {created_grants}")
        
        return jsonify({
            'message': 'Grants created successfully using ORM',
            'method': 'SQLAlchemy ORM',
            'grants_created': total_count,
            'open_grants': open_count,
            'featured_grants': featured_count,
            'grant_names': created_grants,
            'warning': '⚠️ REMOVE THIS PUBLIC ENDPOINT IMMEDIATELY AFTER USE!'
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"[ORM MIGRATION ERROR] {str(e)}")
        import traceback
        return jsonify({
            'message': 'Migration failed',
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500
