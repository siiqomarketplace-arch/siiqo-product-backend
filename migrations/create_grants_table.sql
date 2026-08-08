-- Migration: Create grants table
-- Date: 2026-08-08
-- Description: Creates the grants table for storing funding opportunities

-- Create grants table
CREATE TABLE IF NOT EXISTS grants (
    id SERIAL PRIMARY KEY,
    
    -- Basic Information
    slug VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    amount VARCHAR(100) NOT NULL,
    
    -- Categorization
    category VARCHAR(50)[] NOT NULL,  -- PostgreSQL array for multiple categories
    country VARCHAR(100) NOT NULL,
    
    -- Grant Details
    eligibility TEXT NOT NULL,
    description TEXT NOT NULL,
    application_tips TEXT,
    
    -- Dates and Status
    deadline VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'upcoming',
    last_verified TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- URLs and Media
    official_url VARCHAR(500) NOT NULL,
    cover_image VARCHAR(255),
    
    -- Display Options
    featured BOOLEAN DEFAULT FALSE,
    is_published BOOLEAN DEFAULT TRUE,
    
    -- SEO Fields
    meta_title VARCHAR(255),
    meta_description VARCHAR(500),
    
    -- Admin tracking
    admin_author_id INTEGER REFERENCES admin_users(id) ON DELETE SET NULL,
    
    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_grants_slug ON grants(slug);
CREATE INDEX IF NOT EXISTS idx_grants_status ON grants(status);
CREATE INDEX IF NOT EXISTS idx_grants_featured ON grants(featured);
CREATE INDEX IF NOT EXISTS idx_grants_country ON grants(country);
CREATE INDEX IF NOT EXISTS idx_grants_created_at ON grants(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_grants_category ON grants USING GIN(category);  -- GIN index for array queries

-- Add constraint for valid status values
ALTER TABLE grants ADD CONSTRAINT chk_grants_status 
    CHECK (status IN ('open', 'upcoming', 'closed'));

-- Create trigger to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_grants_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_grants_updated_at
    BEFORE UPDATE ON grants
    FOR EACH ROW
    EXECUTE FUNCTION update_grants_updated_at();

-- Insert sample grant data for testing
INSERT INTO grants (
    slug,
    name,
    amount,
    category,
    country,
    eligibility,
    description,
    application_tips,
    deadline,
    status,
    official_url,
    featured,
    meta_title,
    meta_description
) VALUES 
(
    'tony-elumelu-foundation-entrepreneurship-programme-2026',
    'Tony Elumelu Foundation Entrepreneurship Programme 2026',
    '$5,000 USD (₦7,500,000)',
    ARRAY['Startups', 'Small Business', 'Youth'],
    'Africa',
    'African entrepreneurs aged 18-35 with a business idea or existing business less than 3 years old. All sectors welcome.',
    '# About the Programme

The Tony Elumelu Foundation (TEF) Entrepreneurship Programme is Africa''s largest entrepreneurship programme. Since 2015, the Foundation has empowered over 18,000 African entrepreneurs with $100 million in funding.

## What You Get
- $5,000 seed capital (non-refundable)
- 12-week business management training
- Mentorship from industry experts
- Access to TEF alumni network
- Business tools and resources

## Selection Process
1. Online application (January - March)
2. Shortlisting based on business viability
3. Final selection announcement (June)
4. Training programme (July - September)
5. Seed capital disbursement (October)',
    'Focus on the problem you''re solving, not just the product. Show clear financial projections. Highlight your commitment and passion. Video applications stand out.',
    '2026-03-31',
    'open',
    'https://www.tonyelumelufoundation.org/apply',
    TRUE,
    'Tony Elumelu Grant 2026 - $5,000 for African Entrepreneurs | Apply Now',
    'Get $5,000 seed funding, training, and mentorship from Tony Elumelu Foundation. Open to all African entrepreneurs aged 18-35. Deadline: March 31, 2026.'
),
(
    'bank-of-industry-youth-entrepreneurship-support-programme',
    'Bank of Industry Youth Entrepreneurship Support (YES) Programme',
    '₦100,000 - ₦5,000,000',
    ARRAY['Youth', 'Startups', 'Small Business'],
    'Nigeria',
    'Nigerian youths aged 18-35 with innovative business ideas. Priority given to agriculture, tech, creative industries, and manufacturing.',
    '# BOI YES Programme

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
- Collateral (for amounts above ₦1M)',
    'Prepare a solid business plan with clear projections. Show evidence of market research. Have all documents ready before applying. Consider joining BOI''s free business training.',
    'Rolling',
    'open',
    'https://www.boi.ng/youth-entrepreneurship/',
    TRUE,
    'BOI Youth Grant 2026 - Up to ₦5M for Nigerian Youth Entrepreneurs',
    'Apply for Bank of Industry YES Programme. Get up to ₦5 million at 9% interest for your business. For Nigerian youth aged 18-35.'
),
(
    'smedan-women-business-fund-2026',
    'SMEDAN Women Business Fund 2026',
    '₦50,000 - ₦500,000',
    ARRAY['Women', 'Small Business'],
    'Nigeria',
    'Nigerian women entrepreneurs with registered businesses. Must be actively trading for at least 6 months. All sectors considered.',
    '# SMEDAN Women Business Fund

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
- Social impact',
    'Emphasize how your business creates jobs for other women. Show clear financial records. Demonstrate community impact. Join SMEDAN training before applying.',
    '2026-09-30',
    'upcoming',
    'https://www.smedan.gov.ng/women-fund',
    FALSE,
    'SMEDAN Women Business Grant 2026 - Up to ₦500,000 for Nigerian Women',
    'Free business grant for Nigerian women entrepreneurs. Get ₦50,000 to ₦500,000 from SMEDAN. No repayment required. Apply by September 2026.'
);

-- Verification query
SELECT 
    id,
    name,
    amount,
    status,
    country,
    category,
    featured,
    created_at
FROM grants
ORDER BY created_at DESC;
