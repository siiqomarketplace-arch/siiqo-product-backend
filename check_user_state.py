import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from app import create_app
from app.models.user import User
from app.models.order import Order

app = create_app()
with app.app_context():
    email = "stillwalker689@gmail.com"
    user = User.query.filter_by(email=email).first()
    if not user:
        print("NOT FOUND: " + email)
    else:
        print("USER STATE:")
        print("  ID         : " + str(user.id))
        print("  Email      : " + str(user.email))
        print("  Name       : " + str(user.first_name) + " " + str(user.last_name))
        print("  Role       : " + str(user.role))
        print("  Verified   : " + str(user.is_verified))
        print("  Active     : " + str(user.is_active))
        print("  OTP        : " + str(user.reset_otp))
        print("  Referral   : " + str(user.referral_code))
        print("  Storefront : " + (user.storefront.store_name if user.storefront else "None"))
        orders = Order.query.filter((Order.buyer_id==user.id)|(Order.vendor_id==user.id)).count()
        print("  Orders     : " + str(orders))
        print()
        if user.is_verified:
            print("READY TO LOGIN: YES")
        else:
            print("READY TO LOGIN: NO - email not verified")
