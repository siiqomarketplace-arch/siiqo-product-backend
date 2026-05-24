import os
import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def scrape_product_url(url):
    proxy_url = os.environ.get("BRIGHT_DATA_PROXY")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    
    proxies = None
    if proxy_url:
        proxies = {
            "http": proxy_url,
            "https": proxy_url
        }
        
    try:
        response = requests.get(url, headers=headers, proxies=proxies, verify=False, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Extract basic OpenGraph or standard meta tags
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
            
        # Try to find a price — supports $, £, €, ₦, ₵, ¥, ₹, KES, NGN etc.
        price = "0"
        import re
        # Look for og:price:amount first (Shopify, WooCommerce etc.)
        og_price = soup.find("meta", property="og:price:amount") or soup.find("meta", attrs={"property": "product:price:amount"})
        if og_price and og_price.get("content"):
            price = og_price["content"].replace(",", "").strip()
        else:
            # Scan visible text for any currency followed by a number
            price_tags = soup.find_all(string=lambda text: text and re.search(r'[\$£€₦₵¥₹]', text))
            if price_tags:
                for t in price_tags:
                    match = re.search(r'[\$£€₦₵¥₹]\s*([\d,]+(?:\.\d{1,2})?)', str(t))
                    if match:
                        price = match.group(1).replace(',', '')
                        break
            # Also try price with currency code like NGN 12,000
            if price == "0":
                price_tags2 = soup.find_all(string=lambda text: text and re.search(r'NGN|USD|GBP|EUR', str(text)))
                for t in price_tags2:
                    match = re.search(r'(?:NGN|USD|GBP|EUR)\s*([\d,]+(?:\.\d{1,2})?)', str(t))
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
        print(f"Bright Data Scraping Error: {e}")
        return {
            "success": False,
            "message": str(e)
        }

def analyze_storefront_url(url):
    proxy_url = os.environ.get("BRIGHT_DATA_PROXY")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    
    proxies = None
    if proxy_url:
        proxies = {
            "http": proxy_url,
            "https": proxy_url
        }
        
    try:
        response = requests.get(url, headers=headers, proxies=proxies, verify=False, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
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
                    import os
                    os.remove(tmp_path)
            except Exception as ce:
                print(f"Color extraction error: {ce}")
                
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
        print(f"Bright Data Scraping Error: {e}")
        return {
            "success": False,
            "message": str(e)
        }
