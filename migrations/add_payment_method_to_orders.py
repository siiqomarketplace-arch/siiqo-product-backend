"""
Database migration: Add payment_method column to orders table

Run this migration:
1. cd "Siiqo backend"
2. flask db migrate -m "Add payment_method to orders"
3. flask db upgrade

Or run this SQL directly:
ALTER TABLE orders ADD COLUMN payment_method VARCHAR(20) DEFAULT 'ESCROW' NOT NULL;
"""

# SQL to run manually if needed:
SQL_MIGRATION = """
-- Add payment_method column to orders table
ALTER TABLE orders ADD COLUMN payment_method VARCHAR(20) DEFAULT 'ESCROW' NOT NULL;

-- Update existing orders to have ESCROW as payment method
UPDATE orders SET payment_method = 'ESCROW' WHERE payment_method IS NULL;
"""

print("Migration SQL:")
print(SQL_MIGRATION)
