-- ============================================================================
-- Migration: Enhance Events & Tickets, Multi-session, Dynamic Forms & Guest Orders
-- ============================================================================

-- 1. Events Table Enhancements
ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_name VARCHAR(255);
ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_bio TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_avatar VARCHAR(500);
ALTER TABLE events ADD COLUMN IF NOT EXISTS organizer_socials JSON DEFAULT '{}';
ALTER TABLE events ADD COLUMN IF NOT EXISTS agenda JSON DEFAULT '[]';
ALTER TABLE events ADD COLUMN IF NOT EXISTS faqs JSON DEFAULT '[]';
ALTER TABLE events ADD COLUMN IF NOT EXISTS schedules JSON DEFAULT '[]';
ALTER TABLE events ADD COLUMN IF NOT EXISTS custom_fields JSON DEFAULT '[]';
ALTER TABLE events ADD COLUMN IF NOT EXISTS cta_button_text VARCHAR(100) DEFAULT 'Get Tickets';

-- 2. Ticket Purchases Table Enhancements
ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS selected_schedule_id VARCHAR(100);
ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS selected_schedule_title VARCHAR(255);
ALTER TABLE ticket_purchases ADD COLUMN IF NOT EXISTS custom_responses JSON DEFAULT '{}';
ALTER TABLE ticket_purchases ALTER COLUMN buyer_id DROP NOT NULL;

-- 3. Orders Table Enhancements for Guest Checkout
ALTER TABLE orders ALTER COLUMN buyer_id DROP NOT NULL;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_email VARCHAR(255);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_name VARCHAR(255);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_guest BOOLEAN DEFAULT FALSE;
