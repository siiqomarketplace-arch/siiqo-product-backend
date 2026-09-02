#!/usr/bin/env python3
"""
SEO Article Consolidation - 301 Redirect Migration
===================================================
Approved by: Product Owner
Date: 2026-09-02

MIGRATION PLAN:
  Redirect 1 (slug already 404 -- insert redirect record only):
    everything-to-know-about-nigeria-s-social-commerce-boom
    -> nigeria-s-social-commerce-boom-2026-what-smes-need-to-know (ID 62)

  Redirect 2 (ID 77, published -> unpublish + redirect):
    breaking-nigeria-opens-5-million-grant-portal-for-young-founders-as-niya-cascador-programme-goes-live
    -> niya-x-cascador-founders-programme-how-to-apply (ID 88)

  Redirect 3 (ID 73, published -> unpublish + redirect):
    niya-cascador-founders-programme-how-to-apply-for-the-fg-s-5m-youth-funding-2026
    -> niya-x-cascador-founders-programme-how-to-apply (ID 88)

  Redirect 4 (slug already 404 -- insert redirect record only):
    the-complete-2025-2026-guide-to-registering-your-business-in-nigeria-with-cac-and-why-your-cac-certificate-is-worthless-without-this
    -> cac-registration-2026-full-cost-timeline-steps (ID 60)

  Redirect 5 (ID 87, published -> unpublish + redirect):
    startup-south-pledges-30m-for-startup-challenge-at-offchart-nxt
    -> how-to-apply-for-the-offchart-nxt-startup-challenge-2026 (ID 89)

SAFETY GUARANTEES:
  - No article records are hard-deleted (only is_published set to False)
  - Full transactional rollback on any error
  - Pre-flight checks run before any writes
  - Idempotent: safe to re-run
"""

import os
import sys
import datetime
import traceback

backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# Load .env
env_path = os.path.join(backend_dir, '.env')
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    os.environ.setdefault(k, v)

from sqlalchemy import create_engine, text

# Database connection
db_url = os.environ.get('DATABASE_URL', '')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

if not db_url or db_url == 'postgresql://user:password@host:5432/siiqo_db':
    print('[FATAL] DATABASE_URL is a placeholder. Set the real connection string in .env')
    sys.exit(1)

print('=' * 80)
print('SEO ARTICLE CONSOLIDATION MIGRATION')
print('=' * 80)
print('Timestamp: ' + datetime.datetime.utcnow().isoformat() + 'Z')
print('Database: ' + db_url[:40] + '...')

engine = create_engine(db_url, pool_pre_ping=True)

# Migration map: (old_slug, destination_article_id, destination_slug, notes)
MIGRATIONS = [
    (
        'everything-to-know-about-nigeria-s-social-commerce-boom',
        62,
        'nigeria-s-social-commerce-boom-2026-what-smes-need-to-know',
        'Old article was already 404 on live site -- redirect record only'
    ),
    (
        'breaking-nigeria-opens-5-million-grant-portal-for-young-founders-as-niya-cascador-programme-goes-live',
        88,
        'niya-x-cascador-founders-programme-how-to-apply',
        'Article ID 77 -- unpublish + redirect'
    ),
    (
        'niya-cascador-founders-programme-how-to-apply-for-the-fg-s-5m-youth-funding-2026',
        88,
        'niya-x-cascador-founders-programme-how-to-apply',
        'Article ID 73 -- unpublish + redirect'
    ),
    (
        'the-complete-2025-2026-guide-to-registering-your-business-in-nigeria-with-cac-and-why-your-cac-certificate-is-worthless-without-this',
        60,
        'cac-registration-2026-full-cost-timeline-steps',
        'Old article was already 404 on live site -- redirect record only'
    ),
    (
        'startup-south-pledges-30m-for-startup-challenge-at-offchart-nxt',
        89,
        'how-to-apply-for-the-offchart-nxt-startup-challenge-2026',
        'Article ID 87 -- unpublish + redirect'
    ),
]


def run_migration():
    # PHASE 1: PRE-FLIGHT CHECKS (read-only, uses autocommit connection)
    print('\n' + '=' * 80)
    print('PHASE 1: PRE-FLIGHT CHECKS')
    print('=' * 80)
    with engine.connect() as conn:

        # 1. Confirm all destination articles exist and are published
        print('\n[CHECK] Destination articles...')
        destination_ids = [60, 62, 88, 89]
        for art_id in destination_ids:
            row = conn.execute(
                text('SELECT id, title, slug, is_published FROM articles WHERE id = :id'),
                {'id': art_id}
            ).fetchone()
            if not row:
                print('  [FATAL] Destination article ID ' + str(art_id) + ' NOT FOUND in database!')
                return False
            if not row[3]:
                print('  [FATAL] Destination article ID ' + str(art_id) + ' (' + row[2] + ') is NOT published!')
                return False
            print('  [OK] ID ' + str(row[0]) + ' | slug: ' + row[2] + ' | title: ' + str(row[1])[:50])

        # 2. Check for existing redirect records
        print('\n[CHECK] Existing redirect records...')
        all_old_slugs = [m[0] for m in MIGRATIONS]
        for s in all_old_slugs:
            existing = conn.execute(
                text('SELECT id, article_id FROM article_slug_redirects WHERE old_slug = :s'),
                {'s': s}
            ).fetchone()
            if existing:
                dest_art = conn.execute(
                    text('SELECT slug FROM articles WHERE id = :id'),
                    {'id': existing[1]}
                ).fetchone()
                dest_slug_val = dest_art[0] if dest_art else '?'
                print('  [NOTE] Redirect already exists: ' + s[:60] + ' -> Article ' + str(existing[1]) + ' (' + dest_slug_val + ')')
            else:
                print('  [OK] No existing redirect for: ' + s[:60] + '...')

        # 3. Check for chain risk
        print('\n[CHECK] Redirect chain risk...')
        for old_slug, dest_id, dest_slug, notes in MIGRATIONS:
            old_art = conn.execute(
                text('SELECT id FROM articles WHERE slug = :s'),
                {'s': old_slug}
            ).fetchone()
            if old_art:
                child_reds = conn.execute(
                    text('SELECT id, old_slug FROM article_slug_redirects WHERE article_id = :aid'),
                    {'aid': old_art[0]}
                ).fetchall()
                if child_reds:
                    print('  [WARNING] Article ' + str(old_art[0]) + ' is already target for ' + str(len(child_reds)) + ' redirect(s) -- will re-point to dest ID ' + str(dest_id))
        print('  [OK] Chain risk check complete.')

    # PHASE 2: EXECUTE MIGRATION (separate transactional connection)
    print('\n' + '=' * 80)
    print('PHASE 2: EXECUTING MIGRATION (inside transaction)')
    print('=' * 80)

    try:
        with engine.begin() as conn:
            now = datetime.datetime.utcnow()

            now = datetime.datetime.utcnow()

            for old_slug, dest_id, dest_slug, notes in MIGRATIONS:
                print('\n--- Processing: ' + old_slug[:70])
                print('    Notes: ' + notes)

                # Step A: Check if old slug exists as an article record
                old_art = conn.execute(
                    text('SELECT id, title, slug, is_published FROM articles WHERE slug = :s'),
                    {'s': old_slug}
                ).fetchone()

                if old_art:
                    art_id = old_art[0]
                    print('    Found old article ID ' + str(art_id) + ' | published: ' + str(old_art[3]) + ' | title: ' + str(old_art[1])[:50])

                    # Step B: Unpublish if currently published
                    if old_art[3]:
                        conn.execute(
                            text('UPDATE articles SET is_published = false, updated_at = :now WHERE id = :id'),
                            {'now': now, 'id': art_id}
                        )
                        print('    [DONE] Set article ID ' + str(art_id) + ' is_published = false (archived)')
                    else:
                        print('    [SKIP] Article ID ' + str(art_id) + ' was already unpublished')

                    # Step C: Re-point any existing child redirects to destination
                    child_reds = conn.execute(
                        text('SELECT id, old_slug FROM article_slug_redirects WHERE article_id = :aid'),
                        {'aid': art_id}
                    ).fetchall()
                    for cr in child_reds:
                        conn.execute(
                            text('UPDATE article_slug_redirects SET article_id = :dest, updated_at = :now WHERE id = :rid'),
                            {'dest': dest_id, 'now': now, 'rid': cr[0]}
                        )
                        print('    [DONE] Re-pointed child redirect "' + str(cr[1])[:50] + '" -> dest ID ' + str(dest_id))
                else:
                    print('    [NOTE] Old slug not found as article record -- redirect record only')

                # Step D: Insert redirect record (idempotent)
                existing_redirect = conn.execute(
                    text('SELECT id FROM article_slug_redirects WHERE old_slug = :s'),
                    {'s': old_slug}
                ).fetchone()

                if existing_redirect:
                    print('    [SKIP] Redirect record already exists (ID ' + str(existing_redirect[0]) + ')')
                else:
                    conn.execute(
                        text('INSERT INTO article_slug_redirects (article_id, old_slug, created_at, updated_at) VALUES (:article_id, :old_slug, :now, :now)'),
                        {'article_id': dest_id, 'old_slug': old_slug, 'now': now}
                    )
                    print('    [DONE] Inserted redirect: "' + old_slug[:60] + '" -> article ID ' + str(dest_id) + ' (' + dest_slug + ')')

        print('\n[SUCCESS] All migration steps committed successfully.')
        return True

    except Exception as e:
        print('\n[ERROR] Migration failed -- ROLLED BACK. No changes applied.')
        print('Error: ' + str(e))
        traceback.print_exc()
        return False


def run_verification():
    """Verify all 5 redirects are correctly registered."""
    print('\n' + '=' * 80)
    print('PHASE 3: VERIFICATION')
    print('=' * 80)

    all_passed = True

    with engine.connect() as conn:
        for old_slug, dest_id, dest_slug, notes in MIGRATIONS:
            print('\n[VERIFY] ' + old_slug[:70])

            # Check redirect record exists
            redir = conn.execute(
                text('SELECT id, article_id FROM article_slug_redirects WHERE old_slug = :s'),
                {'s': old_slug}
            ).fetchone()

            if not redir:
                print('  [FAIL] No redirect record found!')
                all_passed = False
                continue

            if redir[1] != dest_id:
                print('  [FAIL] Redirect points to article ' + str(redir[1]) + ', expected ' + str(dest_id) + '!')
                all_passed = False
                continue

            # Check destination article is published
            dest_art = conn.execute(
                text('SELECT id, slug, is_published FROM articles WHERE id = :id'),
                {'id': dest_id}
            ).fetchone()

            if not dest_art or not dest_art[2]:
                print('  [FAIL] Destination article ' + str(dest_id) + ' is not published!')
                all_passed = False
                continue

            # If old article exists, verify unpublished
            old_art = conn.execute(
                text('SELECT id, is_published FROM articles WHERE slug = :s'),
                {'s': old_slug}
            ).fetchone()

            if old_art and old_art[1]:
                print('  [FAIL] Old article ID ' + str(old_art[0]) + ' is still published!')
                all_passed = False
                continue

            print('  [PASS] redirect_id=' + str(redir[0]) + ' | "' + old_slug[:50] + '" -> ID ' + str(dest_id) + ' (' + dest_slug + ')')
            if old_art:
                print('         Old article ID ' + str(old_art[0]) + ' is_published=False (archived)')
            else:
                print('         Old article absent from DB -- OK')

    if all_passed:
        print('\n' + '=' * 80)
        print('ALL 5 REDIRECT MIGRATIONS VERIFIED SUCCESSFULLY')
        print('=' * 80)
    else:
        print('\n[WARNING] Some verifications failed -- review output above.')

    return all_passed


if __name__ == '__main__':
    success = run_migration()
    if success:
        run_verification()
    else:
        print('\nMigration did not complete. No changes were applied.')
        sys.exit(1)
