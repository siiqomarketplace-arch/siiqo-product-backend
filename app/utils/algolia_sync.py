import logging
logger = logging.getLogger(__name__)
import os
from algoliasearch.search_client import SearchClient

def get_algolia_client():
    app_id = os.environ.get("ALGOLIA_APP_ID")
    write_key = os.environ.get("ALGOLIA_WRITE_KEY")
    if app_id and write_key:
        try:
            return SearchClient.create(app_id, write_key)
        except Exception as e:
            logger.info(f"Error initializing Algolia client: {e}")
    return None

def sync_product_to_algolia(product):
    client = get_algolia_client()
    if not client:
        return
    try:
        index = client.init_index("siiqo_products")
        # Build product data
        images = product.images if isinstance(product.images, list) else []
        main_image = images[0] if images else None
        
        record = {
            "objectID": str(product.id),
            "name": product.name,
            "description": product.description,
            "price": float(product.price) if product.price else 0,
            "category_id": product.category_id,
            "storefront_id": product.storefront_id,
            "images": images,
            "main_image": main_image,
            "stock_quantity": product.stock_quantity,
            "is_active": product.is_active,
            "created_at": product.created_at.isoformat() if product.created_at else None
        }
        index.save_object(record)
    except Exception as e:
        logger.info(f"Algolia Sync Error (Product): {e}")

def delete_product_from_algolia(product_id):
    client = get_algolia_client()
    if not client:
        return
    try:
        index = client.init_index("siiqo_products")
        index.delete_object(str(product_id))
    except Exception as e:
        logger.info(f"Algolia Delete Error (Product): {e}")

def sync_post_to_algolia(post):
    client = get_algolia_client()
    if not client:
        return
    try:
        index = client.init_index("siiqo_community_posts")
        
        author_name = "Community Member"
        try:
            if getattr(post, "author", None):
                author_name = getattr(post.author, "business_name", None) or getattr(post.author, "first_name", None) or "Community Member"
        except Exception:
            pass
        
        record = {
            "objectID": str(post.id),
            "content": post.content,
            "post_type": post.post_type,
            "city": post.city,
            "state": post.state,
            "author_name": author_name,
            "created_at": post.created_at.isoformat() if post.created_at else None
        }
        index.save_object(record)
    except Exception as e:
        logger.info(f"Algolia Sync Error (Post): {e}")

def delete_post_from_algolia(post_id):
    client = get_algolia_client()
    if not client:
        return
    try:
        index = client.init_index("siiqo_community_posts")
        index.delete_object(str(post_id))
    except Exception as e:
        logger.info(f"Algolia Delete Error (Post): {e}")
