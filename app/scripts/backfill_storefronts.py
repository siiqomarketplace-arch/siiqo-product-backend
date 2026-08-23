"""
backfill_storefronts.py
Updates existing published storefronts so is_verified=True, making them live immediately.
Also cleans any existing community posts containing raw HTML tags.
"""
import os
import sys
import re
import html

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import create_app
from app.extensions import db
from app.models.user import Storefront
from app.models.social import Post

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</(p|div|li|h[1-6]|tr)>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def run_backfill():
    app = create_app()
    with app.app_context():
        # 1. Backfill storefronts
        unverified_stores = Storefront.query.filter_by(is_published=True, is_verified=False).all()
        print(f"Found {len(unverified_stores)} published storefronts pending verification.")
        for sf in unverified_stores:
            sf.is_verified = True
            print(f"  -> Store '{sf.store_name}' ({sf.store_slug}) marked live & verified.")

        # 2. Clean any existing community posts with HTML tags
        posts = Post.query.all()
        cleaned_count = 0
        for p in posts:
            if p.content and ('<' in p.content and '>' in p.content):
                cleaned = clean_text(p.content)
                if cleaned != p.content:
                    p.content = cleaned
                    cleaned_count += 1

        db.session.commit()
        print(f"Backfill complete! {len(unverified_stores)} stores updated to live. {cleaned_count} community posts sanitized.")

if __name__ == '__main__':
    run_backfill()
