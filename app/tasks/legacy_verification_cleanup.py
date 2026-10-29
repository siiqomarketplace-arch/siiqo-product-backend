"""
One-off cleanup of legacy verification data (no longer collected: verification
now runs through a provider and stores only a keyed hash of the NIN).

Never runs on its own. From a `flask shell` on the server:

    from app.tasks.legacy_verification_cleanup import run
    run()              # dry run: counts only
    run(apply=True)    # clears stored NINs and document links in the database

It does not delete files from S3; it returns the list of stored document URLs
so the owner can remove those files deliberately.
"""
from app.extensions import db


def run(apply=False):
    from app.models.user import Storefront, User

    users = User.query.filter(User.nin.isnot(None), User.nin != "").all()
    stores = Storefront.query.filter(
        (Storefront.nin_document_url.isnot(None)) | (Storefront.cac_document_url.isnot(None))).all()
    document_urls = sorted({u for sf in stores for u in (sf.nin_document_url, sf.cac_document_url) if u})

    report = {
        "users_with_stored_nin": len(users),
        "stores_with_document_links": len(stores),
        "document_urls": document_urls,
        "applied": False,
    }
    if apply:
        for u in users:
            u.nin = None
        for sf in stores:
            sf.nin_document_url = None
            sf.cac_document_url = None
        db.session.commit()
        report["applied"] = True
    return report
