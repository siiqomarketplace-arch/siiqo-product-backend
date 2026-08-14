-- ============================================================================
-- EVENTS AND TICKETING SYSTEM MIGRATION
-- ============================================================================
-- Creates tables for event management and ticket sales
-- Supports: free/paid tickets, online/in-person events, QR codes, check-ins
-- ============================================================================

-- Create events table
CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    storefront_id INTEGER NOT NULL REFERENCES storefronts(id) ON DELETE CASCADE,
    vendor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Basic information
    title VARCHAR(255) NOT NULL,
    slug VARCHAR(300) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    cover_image VARCHAR(500),
    images JSON DEFAULT '[]',
    
    -- Event timing
    start_date TIMESTAMP NOT NULL,
    end_date TIMESTAMP NOT NULL,
    timezone VARCHAR(50) DEFAULT 'Africa/Lagos',
    
    -- Event type and format
    event_type VARCHAR(50) NOT NULL,
    event_format VARCHAR(20) DEFAULT 'in-person',
    
    -- Location (for in-person and hybrid events)
    venue_name VARCHAR(255),
    venue_address VARCHAR(500),
    city VARCHAR(100),
    state VARCHAR(100),
    country VARCHAR(100) DEFAULT 'Nigeria',
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    
    -- Online event details
    meeting_url VARCHAR(500),
    meeting_password VARCHAR(100),
    
    -- Capacity and status
    total_capacity INTEGER,
    tickets_sold INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    is_published BOOLEAN DEFAULT FALSE,
    is_deleted BOOLEAN DEFAULT FALSE,
    show_on_storefront BOOLEAN DEFAULT TRUE,
    show_on_marketplace BOOLEAN DEFAULT TRUE,
    
    -- Analytics
    view_count INTEGER DEFAULT 0,
    
    -- SEO
    meta_title VARCHAR(255),
    meta_description TEXT,
    
    -- Additional details
    terms_and_conditions TEXT,
    contact_email VARCHAR(255),
    contact_phone VARCHAR(20),
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for events
CREATE INDEX IF NOT EXISTS idx_events_storefront ON events(storefront_id);
CREATE INDEX IF NOT EXISTS idx_events_vendor ON events(vendor_id);
CREATE INDEX IF NOT EXISTS idx_events_slug ON events(slug);
CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date);
CREATE INDEX IF NOT EXISTS idx_events_published ON events(is_published, is_active, is_deleted);
CREATE INDEX IF NOT EXISTS idx_events_location ON events(city, state, country);


-- Create ticket_types table
CREATE TABLE IF NOT EXISTS ticket_types (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    
    name VARCHAR(100) NOT NULL,
    description TEXT,
    
    -- Pricing
    is_free BOOLEAN DEFAULT FALSE,
    price NUMERIC(10, 2) DEFAULT 0.00,
    
    -- Availability
    quantity_available INTEGER,
    quantity_sold INTEGER DEFAULT 0,
    min_per_order INTEGER DEFAULT 1,
    max_per_order INTEGER DEFAULT 10,
    
    -- Sales period
    sale_start_date TIMESTAMP,
    sale_end_date TIMESTAMP,
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    
    -- Benefits/perks
    benefits JSON DEFAULT '[]',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for ticket_types
CREATE INDEX IF NOT EXISTS idx_ticket_types_event ON ticket_types(event_id);
CREATE INDEX IF NOT EXISTS idx_ticket_types_active ON ticket_types(is_active);


-- Create ticket_purchases table
CREATE TABLE IF NOT EXISTS ticket_purchases (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    ticket_type_id INTEGER NOT NULL REFERENCES ticket_types(id) ON DELETE CASCADE,
    buyer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    
    -- Unique ticket identification
    ticket_code VARCHAR(50) UNIQUE NOT NULL,
    qr_code_url VARCHAR(500),
    
    -- Buyer information
    buyer_name VARCHAR(255) NOT NULL,
    buyer_email VARCHAR(255) NOT NULL,
    buyer_phone VARCHAR(20),
    
    -- Purchase details
    price_paid NUMERIC(10, 2) DEFAULT 0.00,
    quantity INTEGER DEFAULT 1,
    
    -- Ticket status
    status VARCHAR(20) DEFAULT 'ACTIVE',
    is_checked_in BOOLEAN DEFAULT FALSE,
    checked_in_at TIMESTAMP,
    checked_in_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    
    -- Transfer tracking
    original_buyer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    transferred_at TIMESTAMP,
    
    -- PDF ticket URL
    pdf_ticket_url VARCHAR(500),
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for ticket_purchases
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_event ON ticket_purchases(event_id);
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_ticket_type ON ticket_purchases(ticket_type_id);
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_buyer ON ticket_purchases(buyer_id);
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_order ON ticket_purchases(order_id);
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_code ON ticket_purchases(ticket_code);
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_status ON ticket_purchases(status);
CREATE INDEX IF NOT EXISTS idx_ticket_purchases_email ON ticket_purchases(buyer_email);


-- Add is_free column to products table (for free digital products/services)
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_free BOOLEAN DEFAULT FALSE;

-- Ensure events table visibility columns exist
ALTER TABLE events ADD COLUMN IF NOT EXISTS show_on_storefront BOOLEAN DEFAULT TRUE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS show_on_marketplace BOOLEAN DEFAULT TRUE;

-- Create index for free products
CREATE INDEX IF NOT EXISTS idx_products_is_free ON products(is_free);

-- Add comment
COMMENT ON COLUMN products.is_free IS 'Whether this digital product or service is free (no payment required)';


-- Create trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to events table
DROP TRIGGER IF EXISTS update_events_updated_at ON events;
CREATE TRIGGER update_events_updated_at
    BEFORE UPDATE ON events
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Apply trigger to ticket_types table
DROP TRIGGER IF EXISTS update_ticket_types_updated_at ON ticket_types;
CREATE TRIGGER update_ticket_types_updated_at
    BEFORE UPDATE ON ticket_types
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Apply trigger to ticket_purchases table
DROP TRIGGER IF EXISTS update_ticket_purchases_updated_at ON ticket_purchases;
CREATE TRIGGER update_ticket_purchases_updated_at
    BEFORE UPDATE ON ticket_purchases
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- ============================================================================
-- VERIFICATION QUERIES
-- ============================================================================

-- Check if tables were created successfully
DO $$ 
BEGIN
    RAISE NOTICE '✓ Events table exists: %', (SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'events'));
    RAISE NOTICE '✓ Ticket Types table exists: %', (SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'ticket_types'));
    RAISE NOTICE '✓ Ticket Purchases table exists: %', (SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'ticket_purchases'));
    RAISE NOTICE '✓ Products.is_free column exists: %', (SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = 'products' AND column_name = 'is_free'));
END $$;
