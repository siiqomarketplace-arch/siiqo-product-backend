"""
One-off: replace the old default store text that made claims Siiqo can't
stand behind ("100% genuine", "verified warranty", "escrow buyer protection",
"guaranteed" ...) with the claim-free defaults new stores now get.

Only a field whose saved value is still EXACTLY the old default is changed,
so anything a seller wrote or edited is left alone. Dry run by default:

    from app.tasks.store_default_copy_cleanup import run
    run()              # report only: which stores and fields would change
    run(apply=True)    # make the changes
"""
from app.extensions import db

# field -> {old default text: new default text}, from _build_default_template_options
# in app/routes/vendor.py before and after the change (30 Oct 2026).
REPLACEMENTS = {
    "hero_heading": {
        "Next-Gen Tech & Authentic Gadgets": "Gadgets, Devices & Accessories",
        "Pure Radiance & Organic Beauty": "Beauty, Skincare & Hair Care",
        "Fresh & Delicious Meals Delivered Fast": "Delicious Meals & Treats",
        "Quality Products & Trusted Service": "Products for Every Day",
    },
    "hero_subtext": {
        "Discover premium apparel, footwear and accessories crafted for timeless elegance.":
            "Browse our clothing, footwear and accessories.",
        "Top-tier electronics, smart devices, and accessories with verified warranty and fast delivery.":
            "Browse our electronics, smart devices and accessories.",
        "Nourish your skin and hair with clean, organic, and dermatologist-tested beauty essentials.":
            "Browse our skincare, hair care and beauty essentials.",
        "Savour authentic flavours, freshly prepared dishes, and pantry essentials right to your doorstep.":
            "Browse our dishes, snacks and pantry essentials.",
        "Explore our full catalog of authentic products backed by Siiqo escrow buyer protection.":
            "Browse our full catalogue and order online.",
    },
    "hero_cta": {
        "Order Fresh Now": "Order Now",
    },
    "about_title": {
        "Why Buy From Us": "About Our Store",
        "Our Beauty Promise": "About Our Store",
        "Freshness & Taste Guaranteed": "About Our Kitchen",
    },
    "about_description": {
        "We deliver authentic, stylish, and high-quality fashion pieces designed to elevate your everyday wardrobe.":
            "Stylish fashion pieces for your everyday wardrobe. Send us a message if you have any questions.",
        "We specialize in 100% genuine electronics, guaranteed warranty, and seamless customer support.":
            "We sell electronics, devices and accessories. Send us a message if you have any questions.",
        "Specially formulated products for all skin and hair textures with authentic, skin-safe ingredients.":
            "Beauty products for skin and hair. Send us a message if you would like help choosing.",
        "We use the highest quality fresh ingredients to bring you hygienic and unforgettable tastes every day.":
            "Good food made with care. Send us a message to place an order.",
        "Your trusted vendor for authentic goods, prompt dispatch, and dedicated customer care.":
            "We are happy to help. Send us a message if you have any questions.",
    },
    "store_tagline": {
        "Glow With Natural Confidence": "Glow With Confidence",
        "Taste the Fresh Difference": "Made With Care",
        "Quality You Can Trust": "Shop With Us",
    },
}


def changes_for(template_options):
    """[(field, old, new)] for fields still holding an old default."""
    out = []
    if not isinstance(template_options, dict):
        return out
    for field, mapping in REPLACEMENTS.items():
        value = template_options.get(field)
        if isinstance(value, str) and value in mapping:
            out.append((field, value, mapping[value]))
    return out


def run(apply=False):
    from app.models.user import Storefront

    report = {"applied": False, "stores_checked": 0, "stores_affected": 0, "changes": []}
    for sf in Storefront.query.order_by(Storefront.id.asc()).yield_per(500):
        report["stores_checked"] += 1
        found = changes_for(sf.template_options)
        if not found:
            continue
        report["stores_affected"] += 1
        for field, old, new in found:
            report["changes"].append({"storefront_id": sf.id, "store_slug": sf.store_slug,
                                      "field": field, "old": old, "new": new})
        if apply:
            updated = dict(sf.template_options)
            for field, _old, new in found:
                updated[field] = new
            sf.template_options = updated  # new object so the JSON change is saved

    if apply:
        db.session.commit()
        report["applied"] = True
    else:
        db.session.rollback()
    return report
