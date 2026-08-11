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
                  url TEXT,
                  source TEXT,
                  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fb_posts
                 (post_id TEXT PRIMARY KEY,
                  content TEXT,
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

def save_product(product_code, product_name, status, price, url, source):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''REPLACE INTO products 
                 (product_code, product_name, status, price, url, source, last_update)
                 VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)''',
              (product_code, product_name, status, price, url, source))
    conn.commit()
    conn.close()

def check_fb_seen(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT post_id FROM fb_posts WHERE post_id=?", (post_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def save_fb(post_id, content, source):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR IGNORE INTO fb_posts (post_id, content, source) VALUES (?,?,?)''',
              (post_id, content[:300], source))
    conn.commit()
    conn.close()

# ========== Telegram ==========
async def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=False)
        logger.info("Telegram 已發送")
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")

def notify(text):
    if text:
        asyncio.run(send_telegram(text))

# ========== HTTP ==========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def safe_get(url, timeout=20, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            if i == retries - 1:
                raise e
            time.sleep(3 * (i + 1))
    return None

# ========== 工具 ==========
def extract_code(name):
    for p in [r'UX[- ]?\d{2,3}[A-Z]?', r'CX[- ]?\d{2,3}[A-Z]?', r'BX[- ]?\d{2,3}[A-Z]?']:
        m = re.search(p, name, re.I)
        if m:
            return m.group(0).upper().replace(' ', '-')
    return None

def format_list(store_name, products):
    if not products:
        return None
    lines = [f"{store_name}", f"有貨 {len(products)} 件：", ""]
    for p in products:
        lines.append(p['name'])
        lines.append(p['url'])
        lines.append(f"HK${p['price']}")
        if p.get('sku'):
            lines.append(f"SKU: {p['sku']}")
        lines.append(f"Status: {p['status']}")
        lines.append("")
    return "\n".join(lines).strip()

# ========== 1. Hobbyland 網站 ==========
def scrape_hobbyland_web():
    notice = []
    urls = [
        "https://www.hobbylandeshop.com/product-category/takaratomy/beyblade%E9%99%80%E8%9E%BA",
        "https://www.hobbylandeshop.com/?s=Beyblade+X&post_type=product",
        "https://www.hobbylandeshop.com/?s=%E7%88%86%E6%97%8B%E9%99%80%E8%9E%BA&post_type=product",
    ]
    seen = set()
    
    for url in urls:
        try:
            logger.info(f"爬取 Hobbyland 網站...")
            r = safe_get(url)
            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.select("li.product, .product, [class*='product']")
            logger.info(f"Hobbyland 網站找到 {len(items)} 個元素 (回應 {len(r.text)} 字元)")
            
            for item in items:
                try:
                    a = item.select_one("a[href*='product'], h2 a, h3 a, .woocommerce-loop-product__title a")
                    if not a:
                        continue
                    name = a.get_text(strip=True)
                    if not name or len(name) < 4:
                        continue
                    if "beyblade" not in name.lower() and "爆旋" not in name and not extract_code(name):
                        continue
                    
                    href = a.get('href', '')
                    if not href.startswith('http'):
                        href = "https://www.hobbylandeshop.com" + href
                    
                    code = extract_code(name) or href.rstrip('/').split('/')[-1][:30]
                    if code in seen:
                        continue
                    seen.add(code)
                    
                    price_el = item.select_one(".price, .woocommerce-Price-amount, .amount")
                    price = price_el.get_text(strip=True).replace('HK$', '').replace('$', '').strip() if price_el else "未標價"
                    
                    text = item.get_text(" ", strip=True).lower()
                    if any(x in text for x in ["售罄", "out of stock", "sold out"]):
                        continue
                    status = "Pre-order" if any(x in text for x in ["預訂", "pre-order", "preorder"]) else "In stock"
                    
                    old = get_old_product(code)
                    if not old or old[2] != status:
                        notice.append({
                            'name': name,
                            'url': href,
                            'price': price,
                            'sku': code,
                            'status': status
                        })
                        save_product(code, name, status, price, href, "Hobbyland")
                        logger.info(f"✅ Hobbyland 新貨: {code}")
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Hobbyland 網站失敗: {e}")
    return notice

# ========== 2. Facebook 通用 ==========
def scrape_facebook(page_name, plugin_url, page_url, source_name, keywords):
    notice = []
    try:
        logger.info(f"爬取 {page_name} Facebook")
        r = safe_get(plugin_url, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')
        
        posts = soup.select("[role='article'], ._5pcb, .userContentWrapper, .post")
        logger.info(f"{page_name} FB 找到 {len(posts)} 則潛在貼文")
        
        for idx, post in enumerate(posts[:12]):
            try:
                content = post.get_text(" ", strip=True)
                if len(content) < 15:
                    continue
                
                post_id = f"{source_name}-{hash(content[:100])}"
                if check_fb_seen(post_id):
                    continue
                
                if not any(k.lower() in content.lower() for k in keywords):
                    save_fb(post_id, content, source_name)
                    continue
                
                code = extract_code(content) or f"FB-{post_id[-8:]}"
                
                if get_old_product(f"{source_name}-{code}"):
                    save_fb(post_id, content, source_name)
                    continue
                
                product = {
                    'name': content[:80] + ("..." if len(content) > 80 else ""),
                    'url': page_url,
                    'price': "請查詢",
                    'sku': code,
                    'status': "In stock / 到貨通知"
                }
                notice.append(product)
                save_product(f"{source_name}-{code}", product['name'], product['status'], product['price'], page_url, source_name)
                save_fb(post_id, content, source_name)
                logger.info(f"✅ {page_name} FB 新相關貼文: {code}")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"{page_name} Facebook 失敗: {e}")
    return notice

def scrape_hobbyland_fb():
    return scrape_facebook(
        "Hobbyland",
        "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FHobbylandHK&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId",
        "https://www.facebook.com/HobbylandHK",
        "FB-Hobbyland",
        ["現貨", "到貨", "入荷", "補貨", "Beyblade", "爆旋", "BX-", "UX-", "CX-", "陀螺"]
    )

def scrape_toysrus_fb():
    return scrape_facebook(
        "ToysRUs",
        "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2FToysRUsHongKong&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId",
        "https://www.facebook.com/ToysRUsHongKong",
        "FB-ToysRUs",
        ["現貨", "到貨", "入荷", "補貨", "Beyblade", "爆旋", "BX-", "UX-", "CX-", "陀螺"]
    )

def scrape_mani_fb():
    return scrape_facebook(
        "萬信",
        "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2Fmanilimited&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId",
        "https://www.facebook.com/manilimited",
        "FB-萬信",
        ["現貨", "到貨", "入荷", "補貨", "Beyblade", "爆旋", "BX-", "UX-", "CX-", "陀螺", "TRU"]
    )

def scrape_sogo_fb():
    return scrape_facebook(
        "SOGO",
        "https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2Fsogohongkong&tabs=timeline&width=500&height=800&small_header=false&adapt_container_width=true&hide_cover=false&show_facepile=true&appId",
        "https://www.facebook.com/sogohongkong",
        "FB-SOGO",
        ["現貨", "到貨", "入荷", "補貨", "Beyblade", "爆旋", "BX-", "UX-", "CX-", "陀螺", "啟德"]
    )

# ========== 主流程 ==========
def run_once():
    logger.info("=" * 55)
    logger.info(f"開始檢查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)
    
    all_msgs = []
    
    # 1. Hobbyland 網站
    try:
        items = scrape_hobbyland_web()
        msg = format_list("Hobbyland 網站", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"Hobbyland 網站失敗: {e}")
    
    time.sleep(2)
    
    # 2. Hobbyland Facebook
    try:
        items = scrape_hobbyland_fb()
        msg = format_list("Hobbyland Facebook", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"Hobbyland FB 失敗: {e}")
    
    time.sleep(2)
    
    # 3. ToysRUs Facebook
    try:
        items = scrape_toysrus_fb()
        msg = format_list("Toys\"R\"Us Facebook", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"ToysRUs FB 失敗: {e}")
    
    time.sleep(2)
    
    # 4. 萬信 Facebook
    try:
        items = scrape_mani_fb()
        msg = format_list("萬信 Facebook", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"萬信 FB 失敗: {e}")
    
    time.sleep(2)
    
    # 5. SOGO Facebook
    try:
        items = scrape_sogo_fb()
        msg = format_list("SOGO Facebook", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"SOGO FB 失敗: {e}")
    
    if all_msgs:
        full = "\n\n".join(all_msgs)
        notify(full)
        logger.info(f"已發送通知，共 {len(all_msgs)} 個來源有新貨")
    else:
        logger.info("本輪無新貨通知")
    
    logger.info("本輪完成")
    logger.info("=" * 55)

if __name__ == "__main__":
    init_db()
    logger.info("Beyblade X 庫存監察啟動（每 5 分鐘）")
    logger.info("範圍：Hobbyland網站+FB / ToysRUs FB / 萬信 FB / SOGO FB")
    
    while True:
        try:
            run_once()
        except Exception as e:
            logger.error(f"主循環錯誤: {e}")
        logger.info("等待 5 分鐘...")
        time.sleep(300)
