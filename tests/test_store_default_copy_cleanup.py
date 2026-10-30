"""0A-7: new stores get claim-free default text; existing stores change only where
the text is still exactly the old default."""
from app.extensions import db
from app.routes.vendor import _build_default_template_options
from app.tasks import store_default_copy_cleanup as cleanup

BANNED = ("genuine", "guarantee", "warranty", "escrow", "verified", "authentic", "trusted", "100%")


def test_new_store_defaults_make_no_claims():
    for category in ("fashion", "phone", "skincare", "food", "anything else"):
        text = " ".join(str(v) for v in _build_default_template_options(category).values()).lower()
        assert not [w for w in BANNED if w in text], category


def test_dry_run_reports_and_changes_nothing(make_vendor):
    _, store = make_vendor()
    store.template_options = {"hero_subtext": "Explore our full catalog of authentic products backed by "
                                              "Siiqo escrow buyer protection.", "hero_cta": "Browse Catalog"}
    db.session.commit()
    report = cleanup.run()
    assert report["applied"] is False and report["stores_affected"] == 1
    assert report["changes"] == [{"storefront_id": store.id, "store_slug": store.store_slug,
                                  "field": "hero_subtext",
                                  "old": "Explore our full catalog of authentic products backed by "
                                         "Siiqo escrow buyer protection.",
                                  "new": "Browse our full catalogue and order online."}]
    db.session.refresh(store)
    assert "escrow" in store.template_options["hero_subtext"]


def test_apply_replaces_only_untouched_defaults(make_vendor):
    _, untouched = make_vendor()
    untouched.template_options = {"about_title": "Why Buy From Us", "store_tagline": "Quality You Can Trust",
                                  "primaryColor": "#000000"}
    _, edited = make_vendor()
    edited.template_options = {"about_title": "Why Buy From Us!", "store_tagline": "My own words"}
    db.session.commit()

    report = cleanup.run(apply=True)
    assert report["applied"] is True and report["stores_affected"] == 1
    db.session.refresh(untouched)
    db.session.refresh(edited)
    assert untouched.template_options == {"about_title": "About Our Store", "store_tagline": "Shop With Us",
                                          "primaryColor": "#000000"}
    assert edited.template_options == {"about_title": "Why Buy From Us!", "store_tagline": "My own words"}
    assert cleanup.run()["stores_affected"] == 0  # second run finds nothing
