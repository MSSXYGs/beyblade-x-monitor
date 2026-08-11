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

# ========== Telegram ==========
async def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, disable_web_page_preview=False, parse_mode=None)
        logger.info("Telegram 已發送")
    except Exception as e:
        logger.error(f"Telegram 發送失敗: {e}")

def notify(text):
    if text:
        asyncio.run(send_telegram(text))

# ========== HTTP ==========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-HK,zh-TW;q=0.9,zh;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
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

# ========== 1. Hobbyland ==========
def scrape_hobbyland():
    notice = []
    urls = [
        "https://www.hobbylandeshop.com/product-category/takaratomy/beyblade%E9%99%80%E8%9E%BA",
        "https://www.hobbylandeshop.com/?s=Beyblade+X&post_type=product",
        "https://www.hobbylandeshop.com/?s=%E7%88%86%E6%97%8B%E9%99%80%E8%9E%BA&post_type=product",
    ]
    seen = set()
    
    for url in urls:
        try:
            logger.info(f"爬取 Hobbyland: {url[:60]}...")
            r = safe_get(url)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            items = soup.select("li.product, .product, [class*='product']")
            logger.info(f"Hobbyland 找到 {len(items)} 個元素 (回應 {len(r.text)} 字元)")
            
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
                        status = "Out of stock"
                    elif any(x in text for x in ["預訂", "pre-order", "preorder"]):
                        status = "Pre-order"
                    else:
                        status = "In stock"
                    
                    if status == "Out of stock":
                        continue
                    
                    old = get_old_product(code)
                    if not old or old[2] != status:
                        product = {
                            'name': name,
                            'url': href,
                            'price': price,
                            'sku': code,
                            'status': status
                        }
                        notice.append(product)
                        save_product(code, name, status, price, href, "Hobbyland")
                        logger.info(f"✅ Hobbyland 新貨: {code} | {name[:30]}")
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Hobbyland 失敗: {e}")
    
    return notice

# ========== 2. Toys R Us HK ==========
def scrape_toysrus():
    notice = []
    urls = [
        "https://www.toysrus.com.hk/zh-hk/search?q=Beyblade+X",
        "https://www.toysrus.com.hk/zh-hk/search?q=%E7%88%86%E6%97%8B%E9%99%80%E8%9E%BA",
        "https://www.toysrus.com.hk/zh-hk/beyblade/",
    ]
    seen = set()
    
    for url in urls:
        try:
            logger.info(f"爬取 ToysRUs: {url[:50]}...")
            r = safe_get(url)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            items = soup.select(".product, .product-tile, [class*='product'], .product-item")
            logger.info(f"ToysRUs 找到 {len(items)} 個元素 (回應 {len(r.text)} 字元)")
            
            for item in items:
                try:
                    a = item.select_one("a[href*='.html'], a.product-link, h2 a, h3 a, .name a")
                    if not a:
                        continue
                    name = a.get_text(strip=True) or item.select_one(".product-name, .name")
                    if hasattr(name, 'get_text'):
                        name = name.get_text(strip=True)
                    if not name or len(name) < 4:
                        continue
                    if "beyblade" not in name.lower() and "爆旋" not in name and not extract_code(name):
                        continue
                    
                    href = a.get('href', '')
                    if not href.startswith('http'):
                        href = "https://www.toysrus.com.hk" + href
                    
                    code = extract_code(name)
                    if not code:
                        m = re.search(r'(\d{5,})', href)
                        code = m.group(1) if m else href.rstrip('/').split('/')[-1][:20]
                    if code in seen:
                        continue
                    seen.add(code)
                    
                    price_el = item.select_one(".price, .sales, .value, [class*='price']")
                    price = price_el.get_text(strip=True).replace('HK$', '').replace('$', '').strip() if price_el else "未標價"
                    
                    text = item.get_text(" ", strip=True).lower()
                    if any(x in text for x in ["暫時缺貨", "out of stock", "sold out", "unavailable"]):
                        status = "Out of stock"
                    elif any(x in text for x in ["預訂", "pre-order", "preorder", "預購"]):
                        status = "Pre-order"
                    else:
                        status = "In stock"
                    
                    if status == "Out of stock":
                        continue
                    
                    old = get_old_product(f"TRU-{code}")
                    if not old or old[2] != status:
                        product = {
                            'name': name,
                            'url': href,
                            'price': price,
                            'sku': code,
                            'status': status
                        }
                        notice.append(product)
                        save_product(f"TRU-{code}", name, status, price, href, "ToysRUs")
                        logger.info(f"✅ ToysRUs 新貨: {code} | {name[:30]}")
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"ToysRUs 失敗: {e}")
    
    return notice

# ========== 3. 萬信 / T Club ==========
def scrape_tclub():
    notice = []
    urls = [
        "https://www.tclub.com.hk/search?q=Beyblade",
        "https://www.tclub.com.hk/search?q=%E7%88%86%E6%97%8B%E9%99%80%E8%9E%BA",
        "https://www.tclub.com.hk/collections/beyblade-x",
    ]
    seen = set()
    
    for url in urls:
        try:
            logger.info(f"爬取 T Club (萬信): {url[:50]}...")
            r = safe_get(url)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            items = soup.select(".product, .product-card, .grid-product, [class*='product']")
            logger.info(f"T Club 找到 {len(items)} 個元素 (回應 {len(r.text)} 字元)")
            
            for item in items:
                try:
                    a = item.select_one("a[href*='/products/'], a.product-link, h2 a, h3 a, .product-title a")
                    if not a:
                        continue
                    name = a.get_text(strip=True)
                    if not name or len(name) < 4:
                        continue
                    if "beyblade" not in name.lower() and "爆旋" not in name and not extract_code(name):
                        continue
                    
                    href = a.get('href', '')
                    if not href.startswith('http'):
                        href = "https://www.tclub.com.hk" + href
                    
                    code = extract_code(name) or href.rstrip('/').split('/')[-1][:30]
                    if code in seen:
                        continue
                    seen.add(code)
                    
                    price_el = item.select_one(".price, .product-price, [class*='price']")
                    price = price_el.get_text(strip=True).replace('HK$', '').replace('$', '').strip() if price_el else "未標價"
                    
                    text = item.get_text(" ", strip=True).lower()
                    if any(x in text for x in ["售罄", "out of stock", "sold out"]):
                        status = "Out of stock"
                    elif any(x in text for x in ["預訂", "pre-order", "preorder", "預購"]):
                        status = "Pre-order"
                    else:
                        status = "In stock"
                    
                    if status == "Out of stock":
                        continue
                    
                    old = get_old_product(f"TCLUB-{code}")
                    if not old or old[2] != status:
                        product = {
                            'name': name,
                            'url': href,
                            'price': price,
                            'sku': code,
                            'status': status
                        }
                        notice.append(product)
                        save_product(f"TCLUB-{code}", name, status, price, href, "TClub")
                        logger.info(f"✅ T Club 新貨: {code} | {name[:30]}")
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"T Club 失敗: {e}")
    
    return notice

# ========== 主流程 ==========
def run_once():
    logger.info("=" * 55)
    logger.info(f"開始檢查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)
    
    all_msgs = []
    
    try:
        items = scrape_hobbyland()
        msg = format_list("Hobbyland", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"Hobbyland 整體失敗: {e}")
    
    time.sleep(2)
    
    try:
        items = scrape_toysrus()
        msg = format_list("Toys\"R\"Us HK", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"ToysRUs 整體失敗: {e}")
    
    time.sleep(2)
    
    try:
        items = scrape_tclub()
        msg = format_list("萬信 (T Club)", items)
        if msg:
            all_msgs.append(msg)
    except Exception as e:
        logger.error(f"T Club 整體失敗: {e}")
    
    if all_msgs:
        full = "\n\n".join(all_msgs)
        notify(full)
        logger.info(f"已發送通知，共 {len(all_msgs)} 個店舖有新貨")
    else:
        logger.info("本輪無新貨通知")
    
    logger.info("本輪完成")
    logger.info("=" * 55)

if __name__ == "__main__":
    init_db()
    logger.info("Beyblade X 庫存監察啟動（每 5 分鐘一次）")
    logger.info("監察範圍：Hobbyland / ToysRUs HK / 萬信T Club")
    
    while True:
        try:
            run_once()
        except Exception as e:
            logger.error(f"主循環錯誤: {e}")
        logger.info("等待 5 分鐘...")
        time.sleep(300)
