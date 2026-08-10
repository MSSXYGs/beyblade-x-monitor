import requests
from bs4 import BeautifulSoup
import os
import re
import sqlite3
import logging
from datetime import datetime
from telegram import Bot

# ========== 環境變數 ==========
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

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
    c.execute('''CREATE TABLE IF NOT EXISTS ig_posts
                 (post_id TEXT PRIMARY KEY,
                  caption TEXT,
                  post_url TEXT,
                  source TEXT,
                  posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
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

def check_ig_post_seen(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT post_id FROM ig_posts WHERE post_id=?", (post_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def save_ig_post(post_id, caption, post_url, source):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO ig_posts (post_id, caption, post_url, source)
                 VALUES (?,?,?,?)''',
              (post_id, caption, post_url, source))
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
def send_telegram_message(msg_list):
    if not msg_list or not bot:
        return
    batch = []
    current_len = 0
    for msg in msg_list:
        msg_len = len(msg) + 5
        if current_len + msg_len > 3500:
            _send_batch(batch)
            batch = [msg]
            current_len = msg_len
        else:
            batch.append(msg)
            current_len += msg_len
    if batch:
        _send_batch(batch)

def _send_batch(batch):
    full_text = "\n\n".join(batch)
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=full_text, disable_web_page_preview=True)
        logger.info(f"已發送 {len(batch)} 則通知")
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")

def send_error_alert(source_name, error_msg):
    if not bot:
        return
    msg = f"⚠️ 監察暫時失效 - {source_name}\n需要更新 selector / 處理錯誤\n\n錯誤資訊：{str(error_msg)[:200]}"
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        logger.error(f"錯誤提醒發送失敗: {e}")

# ========== HTTP 工具 ==========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def safe_get(url, timeout=15, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            if i == retries - 1:
                raise e
            time.sleep(2 * (i + 1))
    return None

# ========== 工具函式 ==========
def extract_product_code(name, url):
    bx_match = re.search(r'BX[- ]?\d{2,3}', name, re.IGNORECASE)
    if bx_match:
        return bx_match.group(0).upper().replace(' ', '-')
    code_match = re.search(r'\b([A-Z]{2,4}[- ]?\d{2,4}[A-Z]?)\b', name)
    if code_match:
        return code_match.group(1).upper().replace(' ', '-')
    slug = url.rstrip('/').split('/')[-1]
    return slug[:30]

def format_notification(product):
    return f"""🔥 新貨通知 - Beyblade X

【產品】{product['name']} ({product['code']})

【狀態】{product['status']}

【價錢】{product['price']}

【限購】{product['limit']}

【直接購買】{product['url']}"""

def parse_hobbyland_product(item, source_page):
    try:
        title_elem = item.select_one(".woocommerce-loop-product__title a, .product-title a, h2 a, h3 a")
        if not title_elem:
            title_elem = item.find('a', href=True)
        if not title_elem:
            return None
        
        product_name = title_elem.get_text(strip=True)
        product_url = title_elem['href']
        if not product_url.startswith('http'):
            product_url = "https://www.hobbylandeshop.com" + product_url
        
        product_code = extract_product_code(product_name, product_url)
        
        price_elem = item.select_one(".price, .woocommerce-Price-amount")
        price = price_elem.get_text(strip=True) if price_elem else "未標價"
        
        add_cart = item.select_one(".add_to_cart_button, .single_add_to_cart_button, [data-product_id]")
        pre_order = item.select_one(".pre-order-btn, .preorder, .onbackorder, .pre-order")
        out_of_stock = item.select_one(".out-of-stock, .sold-out, .stock.out-of-stock")
        
        stock_status = ""
        if out_of_stock:
            stock_status = "售罄"
        elif add_cart and not out_of_stock:
            stock_status = "現貨"
        elif pre_order:
            stock_status = "預訂（待公布到貨日期）"
        else:
            if price_elem and not add_cart:
                stock_status = "售罄"
            else:
                return None
        
        limit_buy = "無"
        limit_elem = item.select_one(".stock-limit, .limit-quantity, .max-quantity")
        if limit_elem:
            limit_text = limit_elem.get_text(strip=True)
            if limit_text:
                limit_buy = limit_text
        
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
]

def scrape_hobbyland_shop():
    notice_list = []
    seen_codes = set()
    
    for page_name, url in HOBBYLAND_URLS:
        try:
            logger.info(f"爬取 Hobbyland {page_name}")
            r = safe_get(url)
            soup = BeautifulSoup(r.text, 'lxml')
            
            items = soup.select(".product, .product-item, .woocommerce li.product")
            if not items:
                items = soup.select("[class*='product'] [class*='item']")
            
            logger.info(f"{page_name} 找到 {len(items)} 個產品")
            
            for item in items:
                product = parse_hobbyland_product(item, page_name)
                if not product:
                    continue
                if product['code'] in seen_codes:
                    continue
                seen_codes.add(product['code'])
                
                if product['status'] == "售罄":
                    old = get_old_product(product['code'])
                    if old and old[2] != "售罄":
                        save_product(product['code'], product['name'], product['status'], 
                                    product['price'], product['limit'], product['url'], product['source'])
                    continue
                
                old = get_old_product(product['code'])
                if not old:
                    msg = format_notification(product)
                    notice_list.append(msg)
                    save_product(**product)
                else:
                    old_status = old[2]
                    if old_status == "售罄" and product['status'] in ["現貨", "預訂（待公布到貨日期）"]:
                        msg = format_notification(product)
                        notice_list.append(msg)
                        save_product(**product)
                    elif old_status != product['status']:
                        save_product(**product)
                        
        except Exception as e:
            logger.error(f"Hobbyland {page_name} 爬取失敗: {e}")
            send_error_alert(f"Hobbyland {page_name}", e)
    
    logger.info(f"Hobbyland 網店新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 2. Hobbyland Instagram ==========
def scrape_hobbyland_ig():
    notice_list = []
    try:
        logger.info("爬取 Hobbyland Instagram @hobbylandhk")
        url = "https://www.picuki.com/profile/hobbylandhk"
        r = safe_get(url, timeout=20)
        soup = BeautifulSoup(r.text, 'lxml')
        
        posts = soup.select(".box-photo, .post-item, [class*='post']")
        posts = posts[:20]
        logger.info(f"Hobbyland IG 找到 {len(posts)} 則貼文")
        
        keywords = ["現貨", "到貨", "入荷", "補貨", "Beyblade X", "爆旋陀螺X", "BX-"]
        
        for post in posts:
            try:
                link_elem = post.find('a', href=True)
                if not link_elem:
                    continue
                post_href = link_elem['href']
                post_id_match = re.search(r'/p/([^/]+)/', post_href)
                if not post_id_match:
                    continue
                post_id = post_id_match.group(1)
                
                if check_ig_post_seen(post_id):
                    continue
                
                caption = ""
                img = post.find('img')
                if img and img.get('alt'):
                    caption = img['alt']
                elif img and img.get('title'):
                    caption = img['title']
                
                is_related = any(k.lower() in caption.lower() for k in keywords)
                if not is_related:
                    save_ig_post(post_id, caption, post_href, "hobbylandhk")
                    continue
                
                code_match = re.search(r'BX[- ]?\d{2,3}', caption, re.IGNORECASE)
                product_code = code_match.group(0).upper().replace(' ', '-') if code_match else f"IG-{post_id}"
                
                if get_old_product(product_code):
                    save_ig_post(post_id, caption, post_href, "hobbylandhk")
                    continue
                
                product = {
                    'code': product_code,
                    'name': caption[:80] + ("..." if len(caption) > 80 else ""),
                    'status': "現貨（門市到貨）",
                    'price': "請查詢門市",
                    'limit': "請查詢門市",
                    'url': f"https://www.instagram.com/p/{post_id}/",
                    'source': "IG-hobbylandhk"
                }
                
                msg = format_notification(product)
                notice_list.append(msg)
                save_product(**product)
                save_ig_post(post_id, caption, post_href, "hobbylandhk")
                
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"Hobbyland IG 爬取失敗: {e}")
        send_error_alert("Hobbyland Instagram", e)
    
    logger.info(f"Hobbyland IG 新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 3. Hobbyland Facebook ==========
def scrape_hobbyland_fb():
    notice_list = []
    try:
        logger.info("爬取 Hobbyland Facebook")
        url = "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FHobbylandHK&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId"
        r = safe_get(url, timeout=20)
        soup = BeautifulSoup(r.text, 'lxml')
        
        posts = soup.select("[role='article'], ._5pcb, .userContentWrapper")
        logger.info(f"Hobbyland FB 找到 {len(posts)} 則貼文")
        
        keywords = ["現貨", "到貨", "入荷", "補貨", "Beyblade X", "爆旋陀螺", "BX-"]
        
        for idx, post in enumerate(posts[:10]):
            try:
                content_elem = post.select_one(".userContent, [data-testid='post_message']")
                content = content_elem.get_text(strip=True) if content_elem else ""
                
                post_id = f"hobbyland-fb-{idx}-{hash(content[:50])}"
                
                if check_fb_post_seen(post_id):
                    continue
                
                is_related = any(k.lower() in content.lower() for k in keywords)
                if not is_related:
                    save_fb_post(post_id, content, "", "HobbylandHK")
                    continue
                
                code_match = re.search(r'BX[- ]?\d{2,3}', content, re.IGNORECASE)
                product_code = code_match.group(0).upper().replace(' ', '-') if code_match else f"FB-{post_id}"
                
                if get_old_product(product_code):
                    save_fb_post(post_id, content, "", "HobbylandHK")
                    continue
                
                product = {
                    'code': product_code,
                    'name': content[:80] + ("..." if len(content) > 80 else ""),
                    'status': "現貨（門市到貨）",
                    'price': "請查詢門市",
                    'limit': "請查詢門市",
                    'url': "https://www.facebook.com/HobbylandHK",
                    'source': "FB-HobbylandHK"
                }
                
                msg = format_notification(product)
                notice_list.append(msg)
                save_product(**product)
                save_fb_post(post_id, content, "", "HobbylandHK")
                
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"Hobbyland FB 爬取失敗: {e}")
        send_error_alert("Hobbyland Facebook", e)
    
    logger.info(f"Hobbyland FB 新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 4. Toysrus Instagram ==========
def scrape_toysrus_ig():
    notice_list = []
    try:
        logger.info("爬取 Toysrus Instagram @toysrus_hk")
        url = "https://www.picuki.com/profile/toysrus_hk"
        r = safe_get(url, timeout=20)
        soup = BeautifulSoup(r.text, 'lxml')
        
        posts = soup.select(".box-photo, .post-item, [class*='post']")
        posts = posts[:20]
        logger.info(f"Toysrus IG 找到 {len(posts)} 則貼文")
        
        keywords = ["Beyblade X", "爆旋陀螺X", "BX-", "陀螺"]
        
        for post in posts:
            try:
                link_elem = post.find('a', href=True)
                if not link_elem:
                    continue
                post_href = link_elem['href']
                post_id_match = re.search(r'/p/([^/]+)/', post_href)
                if not post_id_match:
                    continue
                post_id = post_id_match.group(1)
                
                if check_ig_post_seen(post_id):
                    continue
                
                caption = ""
                img = post.find('img')
                if img and img.get('alt'):
                    caption = img['alt']
                
                is_related = any(k.lower() in caption.lower() for k in keywords)
                if not is_related:
                    save_ig_post(post_id, caption, post_href, "toysrus_hk")
                    continue
                
                code_match = re.search(r'BX[- ]?\d{2,3}', caption, re.IGNORECASE)
                product_code = code_match.group(0).upper().replace(' ', '-') if code_match else f"IG-TR-{post_id}"
                
                if get_old_product(product_code):
                    save_ig_post(post_id, caption, post_href, "toysrus_hk")
                    continue
                
                product = {
                    'code': product_code,
                    'name': caption[:80] + ("..." if len(caption) > 80 else ""),
                    'status': "請查詢門市/網店",
                    'price': "請查詢門市",
                    'limit': "請查詢門市",
                    'url': f"https://www.instagram.com/p/{post_id}/",
                    'source': "IG-toysrus_hk"
                }
                
                msg = format_notification(product)
                notice_list.append(msg)
                save_product(**product)
                save_ig_post(post_id, caption, post_href, "toysrus_hk")
                
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"Toysrus IG 爬取失敗: {e}")
        send_error_alert("Toysrus Instagram", e)
    
    logger.info(f"Toysrus IG 新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 5. Toysrus Facebook ==========
def scrape_toysrus_fb():
    notice_list = []
    try:
        logger.info("爬取 Toysrus Facebook")
        url = "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FToysRUsHongKong&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId"
        r = safe_get(url, timeout=20)
        soup = BeautifulSoup(r.text, 'lxml')
        
        posts = soup.select("[role='article'], ._5pcb, .userContentWrapper")
        logger.info(f"Toysrus FB 找到 {len(posts)} 則貼文")
        
        keywords = ["Beyblade X", "爆旋陀螺", "BX-", "陀螺", "現貨"]
        
        for idx, post in enumerate(posts[:10]):
            try:
                content_elem = post.select_one(".userContent, [data-testid='post_message']")
                content = content_elem.get_text(strip=True) if content_elem else ""
                
                post_id = f"toysrus-fb-{idx}-{hash(content[:50])}"
                
                if check_fb_post_seen(post_id):
                    continue
                
                is_related = any(k.lower() in content.lower() for k in keywords)
                if not is_related:
                    save_fb_post(post_id, content, "", "ToysRUsHongKong")
                    continue
                
                code_match = re.search(r'BX[- ]?\d{2,3}', content, re.IGNORECASE)
                product_code = code_match.group(0).upper().replace(' ', '-') if code_match else f"FB-TR-{post_id}"
                
                if get_old_product(product_code):
                    save_fb_post(post_id, content, "", "ToysRUsHongKong")
                    continue
                
                product = {
                    'code': product_code,
                    'name': content[:80] + ("..." if len(content) > 80 else ""),
                    'status': "請查詢門市/網店",
                    'price': "請查詢門市",
                    'limit': "請查詢門市",
                    'url': "https://www.facebook.com/ToysRUsHongKong",
                    'source': "FB-ToysRUsHongKong"
                }
                
                msg = format_notification(product)
                notice_list.append(msg)
                save_product(**product)
                save_fb_post(post_id, content, "", "ToysRUsHongKong")
                
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"Toysrus FB 爬取失敗: {e}")
        send_error_alert("Toysrus Facebook", e)
    
    logger.info(f"Toysrus FB 新增 {len(notice_list)} 項通知")
    return notice_list

# ========== 主流程 ==========
def run_all_check():
    import time
    logger.info("=" * 50)
    logger.info(f"開始執行庫存檢查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)
    
    all_notice = []
    
    try:
        all_notice += scrape_hobbyland_shop()
    except Exception as e:
        logger.error(f"Hobbyland 網店整體失敗: {e}")
    
    time.sleep(2)
    
    try:
        all_notice += scrape_hobbyland_ig()
    except Exception as e:
        logger.error(f"Hobbyland IG 整體失敗: {e}")
    
    time.sleep(2)
    
    try:
        all_notice += scrape_hobbyland_fb()
    except Exception as e:
        logger.error(f"Hobbyland FB 整體失敗: {e}")
    
    time.sleep(2)
    
    try:
        all_notice += scrape_toysrus_ig()
    except Exception as e:
        logger.error(f"Toysrus IG 整體失敗: {e}")
    
    time.sleep(2)
    
    try:
        all_notice += scrape_toysrus_fb()
    except Exception as e:
        logger.error(f"Toysrus FB 整體失敗: {e}")
    
    if all_notice:
        send_telegram_message(all_notice)
    else:
        logger.info("本輪無新貨通知")
    
    logger.info(f"本輪檢查完成，共 {len(all_notice)} 項新通知")
    logger.info("=" * 50)

if __name__ == "__main__":
    import time
    init_db()
    run_all_check()
