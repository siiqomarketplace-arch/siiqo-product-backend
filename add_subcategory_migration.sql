-- Add subcategory column to articles table for grants filtering
-- Run this SQL on your production database

-- Check if column exists first
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 
        FROM information_schema.columns 
        WHERE table_name='articles' AND column_name='subcategory'
    ) THEN
        -- Add the column
        ALTER TABLE articles ADD COLUMN subcategory VARCHAR(100);
        RAISE NOTICE 'Column subcategory added successfully';
    ELSE
        RAISE NOTICE 'Column subcategory already exists';
    END IF;
END $$;

-- Verify the column was added
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns 
WHERE table_name='articles' 
ORDER BY ordinal_position;
