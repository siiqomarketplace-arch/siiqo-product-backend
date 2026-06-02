import os
import re
from datetime import datetime
from app import create_app, db
from app.models.community import Article
from app.models.admin import AdminUser

app = create_app()

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

articles_data = [
    {
        "title": "Siiqo Launches Nigeria's First Live Commerce OS for Small Businesses",
        "category": "News",
        "cover_image": "https://siiqo.com/images/blog/news_img.png",
        "excerpt": "Discover how Siiqo is redefining the digital landscape for Nigerian SMEs in 2025 with an all-in-one business operating system.",
        "content": """<h2>The Dawn of a New Era for Nigerian SMEs</h2>
<p>For years, Nigerian small and medium enterprises (SMEs) have struggled with fragmented tools. You use WhatsApp to chat with customers, Excel to track inventory, and cash or bank transfers to receive payments. Today, <a href='https://siiqo.com' target='_blank'>Siiqo</a> is changing the narrative by launching Nigeria's first Live Commerce OS.</p>
<h3>What is a Live Commerce OS?</h3>
<p>Unlike basic storefront builders, a Business Operating System integrates your entire workflow. With Siiqo, you get an automated storefront, real-time inventory management, built-in invoicing, and customer retention tools—all from a single dashboard. Whether you're selling from a stall in Balogun market or running an Instagram boutique in Abuja, Siiqo provides the digital infrastructure to scale seamlessly.</p>
<h3>The Impact on the Nigerian Market</h3>
<p>As we move deeper into 2025, digital transformation is no longer optional. Siiqo empowers you to look professional, build instant trust, and get paid faster. Say goodbye to scattered DMs and manual bookkeeping. Welcome to the future of Nigerian commerce.</p>"""
    },
    {
        "title": "Escrow Payments Are Coming to Nigerian Street Markets — Here's What That Means",
        "category": "News",
        "cover_image": "https://siiqo.com/images/blog/news_img.png",
        "excerpt": "Trust is the biggest currency in Nigerian commerce. Learn how escrow payments are poised to revolutionize buying and selling in local markets.",
        "content": """<h2>Bridging the Trust Gap in Nigerian Commerce</h2>
<p>"What I ordered vs. what I got." It's the phrase that haunts online shoppers and honest vendors alike in Nigeria. A lack of trust is arguably the biggest bottleneck to scaling local commerce. Enter: Escrow Payments.</p>
<h3>How Escrow Works for Small Businesses</h3>
<p>Escrow services hold a buyer's funds securely until they confirm the product received matches the description. For vendors, this eliminates "pay on delivery" risks where buyers mysteriously vanish. For buyers, it eliminates the fear of being scammed by ghost pages. By integrating escrow-like security features, platforms like <a href='https://siiqo.com' target='_blank'>Siiqo</a> are creating a trustless environment where strangers can transact with 100% confidence.</p>
<h3>The Future of Street Markets</h3>
<p>Imagine buying Ankara fabric from Aba or shoes from Trade Fair market without ever leaving Lagos, knowing your money is safe until the goods arrive. This trust mechanism will unlock billions of Naira in interstate and hyperlocal commerce.</p>"""
    },
    {
        "title": "Why Nigerian SMEs Are Losing Money Without a Business Operating System",
        "category": "Businesses",
        "cover_image": "https://siiqo.com/images/blog/business_img.png",
        "excerpt": "Juggling multiple apps is costing you sales. Find out why adopting a business OS is the ultimate game-changer for your bottom line.",
        "content": """<h2>The Hidden Cost of Disorganization</h2>
<p>If you're a Nigerian vendor, your typical day probably looks like this: answering DMs on Instagram, sending pictures on WhatsApp, writing down orders in a notebook, and manually checking your bank app for transfers. While this hustle is commendable, it's costing you money.</p>
<h3>The Speed of Sale</h3>
<p>When a customer is ready to buy, every minute counts. If you take an hour to reply with an account number or product availability, that customer is gone. A Business Operating System like <a href='https://siiqo.com' target='_blank'>Siiqo</a> automates this. Your customers can browse your live catalog, see real-time availability, and pay instantly without waiting for your reply.</p>
<h3>Stop Leaking Revenue</h3>
<p>Lost receipts, untracked inventory, and forgotten follow-ups are revenue leaks. By consolidating your operations into a single OS, you regain control over your business health. You look incredibly professional, which justifies premium pricing, and you reclaim hours of your day to focus on growth, not just survival.</p>"""
    },
    {
        "title": "How to Run Your Nigerian Business Like a Corporation (Without the Budget)",
        "category": "Businesses",
        "cover_image": "https://siiqo.com/images/blog/business_img.png",
        "excerpt": "You don't need a million-Naira budget to build corporate structure. Learn how to leverage free digital tools to professionalize your SME.",
        "content": """<h2>Perception is Reality</h2>
<p>In the competitive Nigerian market, the way your business is perceived often dictates the type of customers you attract. A business that looks like a corporation commands respect, trust, and premium pricing. But how do you achieve this as a solo entrepreneur?</p>
<h3>1. Professional Invoicing</h3>
<p>Stop sending your personal account number via WhatsApp text. Send a branded, professional invoice. <a href='https://siiqo.com' target='_blank'>Siiqo</a> provides free, stunning invoice generators that make you look like a top-tier agency or retail brand. When a client sees a properly formatted invoice, they negotiate less.</p>
<h3>2. Dedicated Storefronts</h3>
<p>Instead of "check my pinned post for prices," send a dedicated link to your branded storefront. It creates an immersive, distraction-free shopping environment.</p>
<h3>3. Automated Receipts</h3>
<p>Follow up every purchase with an instant digital receipt. It provides a paper trail and reassures the customer. Corporate structure isn't about having an HR department; it's about having organized, predictable, and professional systems in place.</p>"""
    },
    {
        "title": "10 Things Every Nigerian Vendor Should Set Up Before Selling Online",
        "category": "Vendor Tips",
        "cover_image": "https://siiqo.com/images/blog/vendor_img.png",
        "excerpt": "Don't launch your online business blind. Follow this 10-point checklist to ensure your Nigerian SME is ready for massive sales.",
        "content": """<h2>Preparation Precedes Profit</h2>
<p>Selling online in Nigeria is incredibly lucrative, but the competition is fierce. Before you make your first post or run your first ad, make sure your foundation is solid.</p>
<h3>The Vendor Checklist</h3>
<ol>
<li><strong>A Branded Link-in-Bio:</strong> Don't just link to WhatsApp. Use a smart storefront like <a href='https://siiqo.com' target='_blank'>Siiqo</a> to display all your products professionally.</li>
<li><strong>High-Quality Imagery:</strong> Lighting is everything. Ensure your product photos are bright and clear.</li>
<li><strong>Clear Policies:</strong> State your delivery times, return policies, and location clearly to avoid time-wasting questions.</li>
<li><strong>Automated Invoicing:</strong> Have a system ready to instantly generate professional invoices.</li>
<li><strong>Inventory Tracking:</strong> Never sell an item you don't actually have in stock. A business OS handles this automatically.</li>
</ol>
<p>By treating your online hustle as a serious business from Day 1, you instantly elevate yourself above 90% of your competitors. Trust is built through professionalism, and professionalism requires the right tools.</p>"""
    },
    {
        "title": "Why Your WhatsApp Catalog Is Costing You Sales (And What to Use Instead)",
        "category": "Vendor Tips",
        "cover_image": "https://siiqo.com/images/blog/vendor_img.png",
        "excerpt": "WhatsApp is great for chatting, but terrible for scaling a business. Discover the superior alternatives for Nigerian vendors.",
        "content": """<h2>The Limits of Chat Commerce</h2>
<p>Almost every Nigerian vendor starts on WhatsApp. It's free, familiar, and direct. However, as your business grows, your WhatsApp catalog becomes a massive bottleneck.</p>
<h3>Why WhatsApp Fails at Scale</h3>
<p>First, customers hate scrolling through endless images. Second, the search functionality is poor. Third, and most importantly, it requires manual intervention for every single sale. If you are asleep, you cannot sell. If you are busy, the customer waits. In 2025, modern buyers want instant gratification.</p>
<h3>The Smart Storefront Alternative</h3>
<p>Migrating your customers to a dedicated, automated storefront via <a href='https://siiqo.com' target='_blank'>Siiqo</a> solves this. Siiqo provides a beautiful, searchable, and professional web interface for your products. Customers can browse, add to cart, and check out without you lifting a finger. You wake up to sales notifications, not "is this available?" messages. WhatsApp is for customer support; your storefront is for sales.</p>"""
    },
    {
        "title": "The Future of Buying and Selling in Nigerian Cities Is Hyperlocal",
        "category": "Local Commerce",
        "cover_image": "https://siiqo.com/images/blog/local_img.png",
        "excerpt": "Discover why the next big boom in Nigerian e-commerce isn't cross-country shipping, but ultra-fast neighborhood delivery networks.",
        "content": """<h2>Hyperlocal is the New Global</h2>
<p>E-commerce in Nigeria has traditionally been viewed through a broad lens: shipping goods from Lagos to Kano, or Abuja to Port Harcourt. But the logistics are brutal, expensive, and slow. The real revolution happening right now is hyperlocal commerce.</p>
<h3>Winning the Neighborhood</h3>
<p>Consumers want their goods today, not next week. Vendors who dominate their immediate local government areas (LGAs) are seeing explosive growth. By targeting customers within a 5-10km radius, delivery fees drop drastically, and same-day delivery becomes a reality.</p>
<h3>How Tech Enables Hyperlocal</h3>
<p>Platforms like <a href='https://siiqo.com' target='_blank'>Siiqo</a> allow vendors to tag their exact locations and offer localized pickup or instant dispatch options. When you optimize your business for your immediate community, you build intense local loyalty. The future belongs to the vendor who can get a hot meal, a fresh dress, or a gadget to a customer in under two hours.</p>"""
    },
    {
        "title": "What LGA-Level Commerce Data Tells Us About Nigerian Buyer Behaviour",
        "category": "Local Commerce",
        "cover_image": "https://siiqo.com/images/blog/local_img.png",
        "excerpt": "A deep dive into local government area data reveals surprising truths about how, when, and why Nigerians spend their money.",
        "content": """<h2>Data is the New Oil</h2>
<p>If you treat the entire Nigerian market as one homogenous block, you are marketing blind. Buyer behavior in Ikeja is vastly different from buyer behavior in Garki. Analyzing Local Government Area (LGA) level data gives vendors an incredible edge.</p>
<h3>Key Behavioral Insights</h3>
<p>Recent data indicates that suburban LGAs see a spike in bulk household purchases on Friday evenings, while business districts see high transaction volumes for fast food and small electronics during weekday lunch hours. Furthermore, trust in prepaying for goods is significantly higher in tight-knit, defined residential communities.</p>
<h3>Adapting Your Strategy</h3>
<p>Using a Business OS like <a href='https://siiqo.com' target='_blank'>Siiqo</a>, you can track where your highest-paying customers reside. If 60% of your sales come from a specific LGA, you can run hyper-targeted Facebook or Instagram ads restricted only to that area. Stop broadcasting; start narrowcasting.</p>"""
    },
    {
        "title": "Why Nigerian Customers Don't Come Back — And How to Fix It",
        "category": "Customer Retention",
        "cover_image": "https://siiqo.com/images/blog/retention_img.png",
        "excerpt": "Acquiring a new customer costs 5x more than retaining an old one. Learn the exact reasons why your Nigerian buyers aren't returning.",
        "content": """<h2>The One-Time Buyer Trap</h2>
<p>Many Nigerian businesses suffer from a leaky bucket syndrome. They spend heavily on influencer ads and sponsored posts, get a rush of sales, and then... crickets. Why do Nigerian customers buy once and disappear?</p>
<h3>The Customer Experience Deficit</h3>
<p>The primary reason is friction. If your ordering process requires five back-and-forth WhatsApp messages, begging for account details, and unclear delivery timelines, the customer will actively seek an easier alternative next time. Poor packaging and zero post-sale communication seal the deal.</p>
<h3>Building a Retention Machine</h3>
<p>Fixing this is simple: streamline the buying experience. Use <a href='https://siiqo.com' target='_blank'>Siiqo</a> to provide a seamless, 3-click checkout process. Once the sale is done, the system can automatically send a beautiful digital receipt and a thank-you note. When buying from you feels like buying from a premium global brand, they won't just come back—they'll bring their friends.</p>"""
    },
    {
        "title": "The Simple Follow-Up System That Keeps Buyers Returning Every Month",
        "category": "Customer Retention",
        "cover_image": "https://siiqo.com/images/blog/retention_img.png",
        "excerpt": "Don't let your customers forget you. Implement this automated follow-up strategy to skyrocket your repeat purchases.",
        "content": """<h2>The Power of \"Checking In\"</h2>
<p>In the crowded Nigerian digital space, out of sight is out of mind. If you don't proactively remind your customers that you exist, they will buy from the next vendor who pops up on their feed.</p>
<h3>The 3-Step Follow-Up Rule</h3>
<ol>
<li><strong>Day 1: The Thank You.</strong> Send an automated digital receipt and a personalized thank-you message immediately after purchase.</li>
<li><strong>Day 7: The Check-In.</strong> Ask how they are enjoying the product. Do not try to sell anything here. This builds pure goodwill and trust.</li>
<li><strong>Day 30: The Restock/Upsell.</strong> Offer a special discount code for their next purchase or suggest a complementary item.</li>
</ol>
<p>Managing this manually is impossible at scale. That's why smart vendors use a comprehensive CRM and Business OS like <a href='https://siiqo.com' target='_blank'>Siiqo</a>. By keeping your customer data organized, follow-ups become systematic, leading to predictable, recurring monthly revenue.</p>"""
    },
    {
        "title": "AI Storefronts Are Here — What Nigerian Vendors Need to Know",
        "category": "Trending",
        "cover_image": "https://siiqo.com/images/blog/trending_img.png",
        "excerpt": "Artificial Intelligence is no longer a buzzword; it's actively helping Nigerian SMEs sell more. Here's how you can leverage AI in 2025.",
        "content": """<h2>The AI Commerce Revolution</h2>
<p>If you think AI is just for writing essays or generating art, you are missing out on the biggest commercial shift of the decade. AI is now powering storefronts, making them smarter, faster, and highly personalized for every single visitor.</p>
<h3>What Makes a Storefront \"Smart\"?</h3>
<p>An AI-powered storefront can dynamically recommend products based on a user's browsing history. It can write highly persuasive product descriptions instantly. It can automatically categorize your inventory and optimize your images for fast loading on Nigerian mobile networks.</p>
<h3>How to Get Involved</h3>
<p>You don't need to be a software engineer to use AI. Platforms like <a href='https://siiqo.com' target='_blank'>Siiqo</a> are integrating these intelligent features directly into their Business Operating Systems. By adopting these tools early, your business will operate with the efficiency of a 10-person team, allowing you to dominate your niche while your competitors are still manually typing out product descriptions.</p>"""
    },
    {
        "title": "Gen Z Nigerians Are Changing How SMEs Must Sell — Are You Ready?",
        "category": "Trending",
        "cover_image": "https://siiqo.com/images/blog/trending_img.png",
        "excerpt": "The new generation of buyers demands aesthetics, speed, and transparency. Is your Nigerian business model outdated?",
        "content": """<h2>The New Consumer Class</h2>
<p>Generation Z is rapidly becoming the dominant consumer block in Nigeria. But they don't shop like Millennials or Boomers. They have zero tolerance for slow websites, ugly designs, or tedious buying processes. If your business isn't visually appealing and instant, you are invisible to them.</p>
<h3>Aesthetics and Speed</h3>
<p>Gen Z buyers make purchasing decisions in seconds. They value clean, dark-mode designs, smooth animations, and ultra-fast loading speeds. They also demand transparency—prices must be visible immediately. "DM for price" is the fastest way to lose a Gen Z customer.</p>
<h3>Adapting Your Business</h3>
<p>To capture this market, you must upgrade your digital presence. Using a modern, beautifully designed platform like <a href='https://siiqo.com' target='_blank'>Siiqo</a> ensures your storefront aligns with Gen Z aesthetic expectations. Provide instant checkouts, crystal-clear pricing, and flawless mobile experiences, and you will win their loyalty for life.</p>"""
    },
    {
        "title": "How Nigerian SMEs Can Rank on Google Without a Big Budget",
        "category": "Digital Marketing",
        "cover_image": "https://siiqo.com/images/blog/marketing_img.png",
        "excerpt": "SEO isn't just for big corporations. Learn the highly effective, free strategies Nigerian small businesses can use to dominate search results.",
        "content": """<h2>The Power of Organic Traffic</h2>
<p>Running Instagram and Facebook ads is getting more expensive every day in Nigeria. The smart alternative? Search Engine Optimization (SEO). When a customer searches for "best organic skincare in Lagos," you want your business to be the first thing they see—and it doesn't have to cost you a Naira.</p>
<h3>Mastering Niche Keywords</h3>
<p>Don't try to rank for generic terms like "shoes." Instead, target long-tail, localized keywords like "buy leather office shoes in Ikeja." These searches have lower competition but incredibly high purchase intent.</p>
<h3>Optimizing Your Storefront</h3>
<p>Search engines favor fast, secure, and mobile-friendly websites. If you use a premium platform like <a href='https://siiqo.com' target='_blank'>Siiqo</a>, the technical SEO (like fast loading speeds, proper header tags, and SSL security) is already handled for you. Focus on writing detailed, human-sounding product descriptions that include the exact phrases your Nigerian customers are typing into Google.</p>"""
    },
    {
        "title": "Why Your Google Business Profile Is the Most Underrated Tool for Nigerian Vendors",
        "category": "Digital Marketing",
        "cover_image": "https://siiqo.com/images/blog/marketing_img.png",
        "excerpt": "Claiming your spot on Google Maps is the ultimate hack for hyper-local dominance. Discover how to leverage it for maximum sales.",
        "content": """<h2>The Map to Success</h2>
<p>If you run a physical shop or offer local delivery in Nigeria, a Google Business Profile (GBP) is your most powerful free marketing asset. Yet, thousands of Nigerian SMEs leave it unclaimed or poorly optimized.</p>
<h3>Why It Works</h3>
<p>When someone searches for a service "near me," Google instantly displays the top 3 verified businesses in that area. This "Local Pack" gets massive click-through rates. Customers can see your reviews, your opening hours, and a direct link to your store.</p>
<h3>Connecting It All</h3>
<p>The perfect setup is having a fully optimized GBP that links directly to a professional <a href='https://siiqo.com' target='_blank'>Siiqo</a> storefront. Encourage every happy customer to leave a Google review. Over time, these reviews will push your business to the top of the search results, providing a steady stream of free, highly targeted daily traffic to your Siiqo store.</p>"""
    }
]

def seed_database():
    print("Starting database seeding...")
    # Find the superadmin to attribute posts to
    admin = AdminUser.query.filter_by(role='superadmin').first()
    admin_id = admin.id if admin else None

    for item in articles_data:
        slug = slugify(item['title'])
        # Handle duplicate slugs gracefully
        original_slug = slug
        import uuid
        while Article.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{str(uuid.uuid4())[:6]}"
            
        print(f"Creating article: {item['title']}")
        article = Article(
            admin_author_id=admin_id,
            title=item['title'],
            slug=slug,
            category=item['category'],
            content=item['content'],
            excerpt=item['excerpt'],
            cover_image=item['cover_image'],
            is_published=True,
            meta_title=item['title'][:60],
            meta_description=item['excerpt'][:160]
        )
        db.session.add(article)
    
    try:
        db.session.commit()
        print("Successfully seeded 14 blog articles.")
    except Exception as e:
        db.session.rollback()
        print(f"Failed to seed articles: {e}")

if __name__ == '__main__':
    with app.app_context():
        seed_database()
