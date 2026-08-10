import requests
from bs4 import BeautifulSoup
import os
import re
import sqlite3
import logging
import time
import asyncio
from datetime import datetime
from telegram import Bot

# ========== 環境變數 ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ========== 日誌 ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== 資料庫 ==========
DB_PATH = "beyblade_stock.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS products
                 (product_code TEXT PRIMARY KEY,
                  product_name TEXT,
                  status TEXT,
                  price TEXT,
                  limit_buy TEXT,
                  url TEXT,
                  source TEXT,
                  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fb_posts
                 (post_id TEXT PRIMARY KEY,
                  content TEXT,
                  post_url TEXT,
                  source TEXT,
                  posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_old_product(product_code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE product_code=?", (product_code,))
    res = c.fetchone()
    conn.close()
    return res

def save_product(product_code, product_name, status, price, limit_buy, url, source):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''REPLACE INTO products 
                 (product_code, product_name, status, price, limit_buy, url, source, last_update)
                 VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)''',
              (product_code, product_name, status, price, limit_buy, url, source))
    conn.commit()
    conn.close()

def check_fb_post_seen(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT post_id FROM fb_posts WHERE post_id=?", (post_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def save_fb_post(post_id, content, post_url, source):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO fb_posts (post_id, content, post_url, source)
                 VALUES (?,?,?,?)''',
              (post_id, content, post_url, source))
    conn.commit()
    conn.close()

# ========== Telegram 通知 ==========
async def _send_batch_async(batch):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    full_text = "\n\n".join(batch)
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=full_text, disable_web_page_preview=True)
        logger.info(f"已發送 {len(batch)} 則通知")
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")

def send_telegram_message(msg_list):
    if not msg_list or not TELEGRAM_BOT_TOKEN:
        return
    batch = []
    current_len = 0
    for msg in msg_list:
        msg_len = len(msg) + 5
        if current_len + msg_len > 3500:
            asyncio.run(_send_batch_async(batch))
            batch = [msg]
            current_len = msg_len
        else:
            batch.append(msg)
            current_len += msg_len
    if batch:
        asyncio.run(_send_batch_async(batch))

async def _send_error_async(source_name, error_msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    msg = f"⚠️ 監察暫時失效 - {source_name}\n\n錯誤資訊：{str(error_msg)[:250]}"
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        logger.error(f"錯誤提醒發送失敗: {e}")

def send_error_alert(source_name, error_msg):
    if not TELEGRAM_BOT_TOKEN:
        return
    asyncio.run(_send_error_async(source_name, error_msg))

# ========== HTTP 工具（更穩定） ==========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def safe_get(url, timeout=25, retries=4):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            wait = 4 * (i + 1)
            logger.warning(f"請求失敗 ({i+1}/{retries})，{wait}秒後重試: {e}")
            if i == retries - 1:
                raise e
            time.sleep(wait)
    return None

# ========== 工具函式 ==========
def extract_product_code(name, url=""):
    for pattern in [r'UX[- ]?\d{2,3}[A-Z]?', r'CX[- ]?\d{2,3}[A-Z]?', r'BX[- ]?\d{2,3}[A-Z]?']:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            return match.group(0).upper().replace(' ', '-')
    code_match = re.search(r'\b([A-Z]{2,4}[- ]?\d{2,4}[A-Z]?)\b', name)
    if code_match:
        return code_match.group(1).upper().replace(' ', '-')
    if url:
        slug = url.rstrip('/').split('/')[-1]
        return slug[:40]
    return "UNKNOWN"

def format_notification(product):
    return f"""🔥 新貨通知 - Beyblade X

【產品】{product['name']} ({product['code']})

【狀態】{product['status']}

【價錢】{product['price']}

【限購】{product['limit']}

【來源】{product['source']}

【直接購買】{product['url']}"""

def parse_hobbyland_product(item, source_page):
    try:
        title_elem = None
        for sel in [
            ".woocommerce-loop-product__title a",
            ".product-title a",
            "h2 a", "h3 a",
            ".woocommerce-LoopProduct-link",
            "a.woocommerce-LoopProduct-link",
            ".product-name a",
            "a[href*='product']"
        ]:
            title_elem = item.select_one(sel)
            if title_elem:
                break
        
        if not title_elem:
            title_elem = item.find('a', href=True)
        if not title_elem:
            return None
        
        product_name = title_elem.get_text(strip=True)
        if not product_name or len(product_name) < 3:
            return None
            
        product_url = title_elem.get('href', '')
        if not product_url:
            return None
        if not product_url.startswith('http'):
            product_url = "https://www.hobbylandeshop.com" + product_url
        
        product_code = extract_product_code(product_name, product_url)
        
        price = "未標價"
        for sel in [".price", ".woocommerce-Price-amount", ".amount", ".price-wrapper"]:
            price_elem = item.select_one(sel)
            if price_elem:
                price = price_elem.get_text(strip=True)
                break
        
        stock_status = "未知"
        text_lower = item.get_text(" ", strip=True).lower()
        
        if any(x in text_lower for x in ["售罄", "out of stock", "sold out", "缺貨"]):
            stock_status = "售罄"
        elif any(x in text_lower for x in ["預訂", "pre-order", "preorder", "到貨", "backorder"]):
            stock_status = "預訂"
        elif item.select_one(".add_to_cart_button, .single_add_to_cart_button, [data-product_id], .button.product_type_simple, .ajax_add_to_cart"):
            stock_status = "現貨"
        elif price != "未標價":
            stock_status = "現貨"
        
        limit_buy = "無"
        
        return {
            'code': product_code,
            'name': product_name,
            'status': stock_status,
            'price': price,
            'limit': limit_buy,
            'url': product_url,
            'source': f"Hobbyland-{source_page}"
        }
    except Exception as e:
        logger.debug(f"解析產品失敗: {e}")
        return None

# ========== 1. Hobbyland 網店 ==========
HOBBYLAND_URLS = [
    ("分類頁", "https://www.hobbylandeshop.com/product-category/takaratomy/beyblade%E9%99%80%E8%9E%BA"),
    ("搜尋頁", "https://www.hobbylandeshop.com/product-search/Beyblade%20x"),
    ("標籤頁", "https://www.hobbylandeshop.com/product-tag/BEYBLADEX"),
    ("主頁搜尋", "https://www.hobbylandeshop.com/?s=Beyblade+X&post_type=product"),
    ("爆旋搜尋", "https://www.hobbylandeshop.com/?s=%E7%88%86%E6%97%8B%E9%99%80%E8%9E%BA&post_type=product"),
]

def scrape_hobbyland_shop():
    notice_list = []
    seen_codes = set()
    
    for page_name, url in HOBBYLAND_URLS:
        try:
            logger.info(f"爬取 Hobbyland {page_name}")
            time.sleep(2.5)
            r = safe_get(url)
            
            logger.info(f"{page_name} 回應長度: {len(r.text)} 字元 | 狀態碼: {r.status_code}")
            
            soup = BeautifulSoup(r.text, 'html.parser')
            
            items = []
            for sel in [
                "li.product",
                ".products .product",
                ".woocommerce ul.products li.product",
                ".product-item",
                "[class*='product']",
                ".type-product"
            ]:
                items = soup.select(sel)
                if items:
                    logger.info(f"{page_name} 用 selector '{sel}' 找到 {len(items)} 個元素")
                    break
            
            if not items:
                items = soup.select("a[href*='product']")
                logger.info(f"{page_name} 用連結方式找到 {len(items)} 個元素")
            
            for item in items:
                product = parse_hobbyland_product(item, page_name)
                if not product:
                    continue
                if product['code'] in seen_codes:
                    continue
                if "beyblade" not in product['name'].lower() and "爆旋" not in product['name'] and not re.search(r'(UX|CX|BX)[- ]?\d', product['name'], re.I):
                    continue
                    
                seen_codes.add(product['code'])
                
                if product['status'] == "售罄":
                    old = get_old_product(product['code'])
                    if old and old[2] != "售罄":
                        save_product(**product)
                    continue
                
                old = get_old_product(product['code'])
                if not old:
                    msg = format_notification(product)
                    notice_list.append(msg)
                    save_product(**product)
                    logger.info(f"✅ 發現新產品: {product['code']} | {product['name'][:40]}")
                else:
                    old_status = old[2]
                    if old_status == "售罄" and product['status'] in ["現貨", "預訂"]:
                        msg = format_notification(product)
                        notice_list.append(msg)
                        save_product(**product)
                        logger.info(f"🔄 產品補貨: {product['code']}")
                    elif old_status != product['status']:
                        save_product(**product)
                        
        except Exception as e:
            logger.error(f"Hobbyland {page_name} 爬取失敗: {e}")
            send_error_alert(f"Hobbyland {page_name}", e)
    
    logger.info(f"Hobbyland 網店本輪新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 2. Hobbyland Facebook ==========
def scrape_hobbyland_fb():
    notice_list = []
    try:
        logger.info("爬取 Hobbyland Facebook")
        urls = [
            "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FHobbylandHK&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId",
            "https://www.facebook.com/HobbylandHK"
        ]
        
        for url in urls:
            try:
                r = safe_get(url, timeout=20)
                soup = BeautifulSoup(r.text, 'html.parser')
                
                posts = soup.select("[role='article'], ._5pcb, .userContentWrapper, .post, div[data-ad-preview='message']")
                logger.info(f"Facebook 找到 {len(posts)} 則潛在貼文")
                
                keywords = ["現貨", "到貨", "入荷", "補貨", "Beyblade", "爆旋", "BX-", "UX-", "CX-", "陀螺"]
                
                for idx, post in enumerate(posts[:15]):
                    try:
                        content = post.get_text(" ", strip=True)
                        if len(content) < 10:
                            continue
                            
                        post_id = f"fb-{hash(content[:80])}"
                        
                        if check_fb_post_seen(post_id):
                            continue
                        
                        is_related = any(k.lower() in content.lower() for k in keywords)
                        if not is_related:
                            save_fb_post(post_id, content[:200], "", "HobbylandHK")
                            continue
                        
                        code_match = re.search(r'(UX|CX|BX)[- ]?\d{2,3}', content, re.IGNORECASE)
                        product_code = code_match.group(0).upper().replace(' ', '-') if code_match else f"FB-{post_id}"
                        
                        if get_old_product(product_code):
                            save_fb_post(post_id, content[:200], "", "HobbylandHK")
                            continue
                        
                        product = {
                            'code': product_code,
                            'name': content[:70] + ("..." if len(content) > 70 else ""),
                            'status': "現貨（門市/網店）",
                            'price': "請查詢",
                            'limit': "請查詢",
                            'url': "https://www.facebook.com/HobbylandHK",
                            'source': "FB-HobbylandHK"
                        }
                        
                        msg = format_notification(product)
                        notice_list.append(msg)
                        save_product(**product)
                        save_fb_post(post_id, content[:200], "", "HobbylandHK")
                        logger.info(f"✅ FB 發現相關貼文: {product_code}")
                        
                    except Exception:
                        continue
                        
                if posts:
                    break
                    
            except Exception as e:
                logger.warning(f"Facebook URL 失敗: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Hobbyland FB 爬取失敗: {e}")
        send_error_alert("Hobbyland Facebook", e)
    
    logger.info(f"Hobbyland FB 本輪新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 主流程 ==========
def run_all_check():
    logger.info("=" * 55)
    logger.info(f"開始執行庫存檢查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)
    
    all_notice = []
    
    try:
        all_notice += scrape_hobbyland_shop()
    except Exception as e:
        logger.error(f"Hobbyland 網店整體失敗: {e}")
    
    time.sleep(3)
    
    try:
        all_notice += scrape_hobbyland_fb()
    except Exception as e:
        logger.error(f"Hobbyland FB 整體失敗: {e}")
    
    if all_notice:
        send_telegram_message(all_notice)
        logger.info(f"已發送 {len(all_notice)} 則新貨通知")
    else:
        logger.info("本輪無新貨通知")
    
    logger.info(f"本輪檢查完成，共 {len(all_notice)} 項新通知")
    logger.info("=" * 55)

if __name__ == "__main__":
    init_db()
    run_all_check()
