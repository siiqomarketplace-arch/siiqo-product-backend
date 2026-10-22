import logging
logger = logging.getLogger(__name__)
import os
import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _fetch_page_content(url):
    proxy_val = os.environ.get("BRIGHT_DATA_PROXY")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    
    if proxy_val:
        proxy_val = proxy_val.strip()
        # Check if it's a proxy URL format (contains @ sign for auth)
        if "@" in proxy_val and ("brd.superproxy.io" in proxy_val or proxy_val.startswith("http")):
            # Extract username and password from proxy URL
            # Format: http://username:password@host:port
            try:
                import re
                match = re.match(r'https?://([^:]+):([^@]+)@([^:]+):(\d+)', proxy_val)
                if match:
                    username, password, host, port = match.groups()
                    # Use proxy with authentication
                    proxies = {
                        "http": f"http://{username}:{password}@{host}:{port}",
                        "https": f"http://{username}:{password}@{host}:{port}"
                    }
                    response = requests.get(url, headers=headers, proxies=proxies, verify=False, timeout=30)
                    response.raise_for_status()
                    return response.text
            except Exception as e:
                logger.info(f"Proxy connection error: {e}")
                # Fall back to direct request
                pass
        
        # Try as API key (for Direct API access)
        if not proxy_val.startswith("http"):
            try:
                api_headers = {
                    "Authorization": f"Bearer {proxy_val}",
                    "Content-Type": "application/json"
                }
                api_body = {
                    "zone": "web_unlocker1",
                    "url": url,
                    "format": "raw"
                }
                response = requests.post("https://api.brightdata.com/request", headers=api_headers, json=api_body, timeout=30)
                response.raise_for_status()
                return response.text
            except Exception as e:
                logger.info(f"API access error: {e}")
                # Fall back to direct request
                pass
    
    # Fallback: direct request without proxy
    response = requests.get(url, headers=headers, verify=False, timeout=30)
    response.raise_for_status()
    return response.text

def scrape_product_url(url):
    try:
        html_text = _fetch_page_content(url)
        soup = BeautifulSoup(html_text, "html.parser")
        import re
        
        # Detect which site we're scraping
        is_amazon = "amazon." in url.lower()
        is_jumia = "jumia." in url.lower()
        is_konga = "konga.com" in url.lower()
        is_ebay = "ebay." in url.lower()
        is_aliexpress = "aliexpress." in url.lower()
        is_shopify = "myshopify.com" in url.lower() or soup.find("meta", attrs={"name": "shopify-checkout-api-token"})
        
        # ==================== EXTRACT NAME ====================
        name = ""
        # Try OpenGraph title first (works for most sites)
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            name = og_title["content"]
        # Try Twitter card title
        if not name:
            twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
            if twitter_title and twitter_title.get("content"):
                name = twitter_title["content"]
        
        # Site-specific title extraction
        if not name and is_amazon:
            product_title = soup.find("span", {"id": "productTitle"})
            if product_title:
                name = product_title.get_text(strip=True)
        if not name and is_jumia:
            h1_title = soup.find("h1", class_=re.compile(".*-title.*|.*heading.*", re.I))
            if h1_title:
                name = h1_title.get_text(strip=True)
        if not name and is_konga:
            product_name = soup.find("h1", class_=re.compile(".*product.*name.*|.*title.*", re.I))
            if product_name:
                name = product_name.get_text(strip=True)
        if not name and is_ebay:
            ebay_title = soup.find("h1", class_="x-item-title__mainTitle")
            if ebay_title:
                name = ebay_title.get_text(strip=True)
        
        # Generic fallbacks
        if not name:
            h1 = soup.find("h1")
            if h1:
                name = h1.get_text(strip=True)
        if not name:
            title_tag = soup.find("title")
            if title_tag:
                name = title_tag.text.strip()
                # Clean up site suffixes
                name = re.sub(r'\s*[:\|]\s*(Amazon|Jumia|Konga|eBay|AliExpress|Shopify).*$', '', name, flags=re.IGNORECASE)
                
        # ==================== EXTRACT DESCRIPTION ====================
        description = ""
        # Try OpenGraph description
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            description = og_desc["content"]
        # Try Twitter description
        if not description:
            twitter_desc = soup.find("meta", attrs={"name": "twitter:description"})
            if twitter_desc and twitter_desc.get("content"):
                description = twitter_desc["content"]
        # Try standard meta description
        if not description:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                description = meta_desc["content"]
        
        # Site-specific description extraction
        if not description and is_amazon:
            feature_bullets = soup.find("div", {"id": "feature-bullets"})
            if feature_bullets:
                bullets = feature_bullets.find_all("li")
                if bullets:
                    description = " ".join([li.get_text(strip=True) for li in bullets[:5]])
            if not description:
                prod_desc = soup.find("div", {"id": "productDescription"})
                if prod_desc:
                    description = prod_desc.get_text(strip=True)[:500]
        
        if not description and is_jumia:
            desc_div = soup.find("div", class_=re.compile(".*description.*|.*details.*", re.I))
            if desc_div:
                description = desc_div.get_text(strip=True)[:500]
        
        if not description and is_konga:
            desc_content = soup.find("div", class_=re.compile(".*product.*description.*|.*details.*", re.I))
            if desc_content:
                description = desc_content.get_text(strip=True)[:500]
        
        # Generic: look for any div/section with "description" in class/id
        if not description:
            desc_elem = soup.find(["div", "section"], class_=re.compile("description|details|about", re.I))
            if not desc_elem:
                desc_elem = soup.find(["div", "section"], id=re.compile("description|details|about", re.I))
            if desc_elem:
                description = desc_elem.get_text(strip=True)[:500]
                
        # ==================== EXTRACT IMAGE ====================
        image_url = ""
        # Try OpenGraph image (best for sharing)
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            image_url = og_image["content"]
        # Try Twitter image
        if not image_url:
            twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
            if twitter_image and twitter_image.get("content"):
                image_url = twitter_image["content"]
        
        # Site-specific image extraction
        if not image_url and is_amazon:
            main_image = soup.find("img", {"id": "landingImage"}) or soup.find("img", {"data-a-image-name": "landingImage"})
            if main_image and main_image.get("src"):
                image_url = main_image["src"]
            if not image_url:
                img_block = soup.find("div", {"id": "imgTagWrapperId"})
                if img_block:
                    img = img_block.find("img")
                    if img and img.get("src"):
                        image_url = img["src"]
        
        if not image_url and is_jumia:
            jumia_img = soup.find("img", class_=re.compile(".*main.*image.*|.*product.*image.*", re.I))
            if jumia_img and jumia_img.get("src"):
                image_url = jumia_img["src"]
        
        if not image_url and is_konga:
            konga_img = soup.find("img", class_=re.compile(".*product.*image.*|.*main.*photo.*", re.I))
            if konga_img and konga_img.get("src"):
                image_url = konga_img["src"]
        
        # Generic: find first product/main image
        if not image_url:
            img = soup.find("img", class_=re.compile("product|main|primary|featured", re.I))
            if img and img.get("src"):
                image_url = img["src"]
            
        # ==================== EXTRACT PRICE ====================
        price = "0"
        # Try og:price:amount first (Shopify, WooCommerce)
        og_price = soup.find("meta", property="og:price:amount") or soup.find("meta", attrs={"property": "product:price:amount"})
        if og_price and og_price.get("content"):
            price = og_price["content"].replace(",", "").strip()
        
        # Site-specific price extraction
        if price == "0" and is_amazon:
            price_span = soup.find("span", {"class": "a-price-whole"})
            if price_span:
                price_text = price_span.get_text(strip=True).replace(",", "").replace(".", "")
                fraction_span = soup.find("span", {"class": "a-price-fraction"})
                if fraction_span:
                    price_text += "." + fraction_span.get_text(strip=True)
                try:
                    price = str(float(price_text))
                except:
                    pass
            if price == "0":
                price_elem = soup.find("span", {"id": "priceblock_ourprice"}) or soup.find("span", {"id": "priceblock_dealprice"})
                if price_elem:
                    price_text = price_elem.get_text(strip=True)
                    match = re.search(r'([\d,]+\.?\d*)', price_text)
                    if match:
                        price = match.group(1).replace(',', '')
        
        if price == "0" and (is_jumia or is_konga):
            price_elem = soup.find(class_=re.compile(".*price.*", re.I))
            if price_elem:
                price_text = price_elem.get_text(strip=True)
                match = re.search(r'([\d,]+(?:\.\d{1,2})?)', price_text)
                if match:
                    price = match.group(1).replace(',', '')
        
        # Generic: search for currency symbols
        if price == "0":
            price_tags = soup.find_all(string=lambda text: text and re.search(r'[\$£€₦₵¥₹]', text))
            if price_tags:
                for t in price_tags:
                    match = re.search(r'[\$£€₦₵¥₹]\s*([\d,]+(?:\.\d{1,2})?)', str(t))
                    if match:
                        price = match.group(1).replace(',', '')
                        break
        # Try currency code patterns (NGN 12,000)
        if price == "0":
            price_tags2 = soup.find_all(string=lambda text: text and re.search(r'\b(?:NGN|USD|GBP|EUR)\b', str(text), re.IGNORECASE))
            for t in price_tags2:
                match = re.search(r'(?:NGN|USD|GBP|EUR)\s*([\d,]+(?:\.\d{1,2})?)', str(t), re.IGNORECASE)
                if match:
                    price = match.group(1).replace(',', '')
                    break
                    
        return {
            "success": True,
            "data": {
                "name": name,
                "description": description,
                "price": price,
                "image_url": image_url,
                "sku": "SCRAPED-ITEM"
            }
        }
    except Exception as e:
        logger.info(f"Bright Data Scraping Error: {e}")
        return {
            "success": False,
            "message": str(e)
        }

def analyze_storefront_url(url):
    try:
        html_text = _fetch_page_content(url)
        soup = BeautifulSoup(html_text, "html.parser")
        
        name = ""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            name = og_title["content"]
        else:
            title_tag = soup.find("title")
            if title_tag:
                name = title_tag.text.strip()
                
        description = ""
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            description = og_desc["content"]
        else:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                description = meta_desc["content"]
                
        image_url = ""
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            image_url = og_image["content"]
            
        colors = []
        if image_url:
            try:
                import tempfile
                from colorthief import ColorThief
                img_resp = requests.get(image_url, timeout=15)
                if img_resp.status_code == 200:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                        tmp.write(img_resp.content)
                        tmp_path = tmp.name
                    color_thief = ColorThief(tmp_path)
                    palette = color_thief.get_palette(color_count=4)
                    colors = ['#{:02x}{:02x}{:02x}'.format(r, g, b) for r, g, b in palette]
                    os.remove(tmp_path)
            except Exception as ce:
                logger.info(f"Color extraction error: {ce}")
                
        return {
            "success": True,
            "data": {
                "name": name,
                "description": description,
                "image_url": image_url,
                "colors": colors
            }
        }
    except Exception as e:
        logger.info(f"Bright Data Scraping Error: {e}")
        return {
            "success": False,
            "message": str(e)
        }

