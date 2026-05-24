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
            
        # Try to find a price
        price = "0.00"
        import re
        price_tags = soup.find_all(string=lambda text: text and ("$" in text or "£" in text or "€" in text))
        if price_tags:
            for t in price_tags:
                match = re.search(r'[\$£€]?\s*(\d+(?:,\d{3})*(?:\.\d{2})?)', t)
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
