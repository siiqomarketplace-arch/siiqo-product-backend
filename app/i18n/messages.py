"""
Short user-facing messages (in-app notifications and bot text), per locale.

English is the source and the fallback. Other locales are added as new
dictionaries here ONLY with human-reviewed text (docs/TRANSLATIONS.md); never
machine-translate money, fee, refund, verification or legal wording.
Placeholders use str.format names ({store_name}) and must match English.
"""

MESSAGES = {
    "en": {
        # Address verification (V5)
        "address.verified.title": "Address verified",
        "address.verified.body": (
            "Your business address check passed. Your Trust Passport now shows "
            "\"Address verified\". It does not change your Trust Score."
        ),
        "address.not_verified.title": "Address not verified",
        "address.not_verified.body": (
            "We couldn't confirm your business address. Check the details and try "
            "again from the Trust tab, or contact support@siiqo.com."
        ),
        "address.unable.title": "Address check couldn't be completed",
        "address.unable.body": (
            "The verification agent couldn't complete your address check. You can "
            "try again from the Trust tab."
        ),
    },
    # "pcm": {...}, "yo": {...}, "ig": {...}, "ha": {...}  (human-reviewed only)
}
