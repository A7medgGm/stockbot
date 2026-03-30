import os
import re
import json
import logging
import threading
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ── إعدادات ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID  = os.environ.get("SHEET_ID")
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── اتصال بـ Google Sheets ────────────────────────────────
def get_sheets():
    try:
        creds_json = os.environ.get("GOOGLE_CREDS_JSON")
        if not creds_json:
            logger.error("❌ GOOGLE_CREDS_JSON is missing!")
            return None, None
            
        creds_dict = json.loads(creds_json, strict=False)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)

        titles = [ws.title for ws in spreadsheet.worksheets()]
        if "Inventory" not in titles:
            ws = spreadsheet.add_worksheet(title="Inventory", rows=100, cols=3)
            ws.append_row(["المنتج", "الكمية", "السعر"])
        if "Sales" not in titles:
            ws = spreadsheet.add_worksheet(title="Sales", rows=1000, cols=4)
            ws.append_row(["التاريخ", "العميل", "المنتج", "الكمية"])

        inv   = spreadsheet.worksheet("Inventory")
        sales = spreadsheet.worksheet("Sales")
        return inv, sales
    except Exception as e:
        logger.error(f"❌ Error in get_sheets: {e}")
        return None, None

# ── إرسال رسالة ──────────────────────────────────────────
def send_message(chat_id, text):
    try:
        requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        logger.error(f"❌ Error sending message: {e}")

# ── جيب كل المنتجات ──────────────────────────────────────
def get_inventory(inv_ws):
    try:
        records = inv_ws.get_all_values()
        result = {}
        for row in records[1:]:
            if len(row) >= 2 and row[0].strip():
                try:
                    qty = int(row[1])
                except:
                    qty = 0
                result[row[0].strip()] = {
                    "qty": qty,
                    "price": row[2] if len(row) > 2 else ""
                }
        return result
    except:
        return {}

# ── دور على منتج ─────────────────────────────────────────
def find_product(inventory, query):
    query = query.strip().lower()
    for name in inventory:
        if query in name.lower() or name.lower() in query:
            return name
    return None

# ── معالجة الرسائل ───────────────────────────────────────
def handle_message(chat_id, text):
    text = text.strip()
    logger.info(f"📥 Received: {text}")

    # --- اختبار فوري للاتصال ---
    # لو شفت الرسالة دي على تليجرام يبقى البوت شغال والتوكن صح
    if text.lower() == "تست" or text == "test":
        send_message(chat_id, "✅ البوت شغال ومستني أوامرك!")
        return

    # محاولة الاتصال بجوجل شيت
    inv_ws, sales_ws = get_sheets()
    if not inv_ws:
        send_message(chat_id, "❌ مشكلة في الوصول لجوجل شيت. اتأكد من الصلاحيات والـ JSON.")
        return
        
    inventory = get_inventory(inv_ws)

    # 1. استعلام: باقي كم
    if re.search(r"باقي كم|كم باقي|كم في المخزن", text):
        query = re.sub(r"باقي كم|كم باقي|كم في المخزن|في المخزن|من", "", text).strip()
        if not query:
            if not inventory:
                send_message(chat_id, "المخزن فاضي!")
                return
            msg = "📊 *المخزن الحالي:*\n\n"
            for name, data in inventory.items():
                msg += f"- {name}: `{data['qty']}` قطعة\n"
            send_message(chat_id, msg)
            return
        
        product = find_product(inventory, query)
        if product:
            send_message(chat_id, f"📦 *{product}*\nالمتبقي: `{inventory[product]['qty']}` قطعة")
        else:
            send_message(chat_id, f"🔍 مش لاقي منتج بالاسم ده: '{query}'")
        return

    # 2. إضافة مخزون (اضافة 5 بطارية)
    m = re.search(r"(اضافة|أضف|وصل|استلمت)\s+(\d+)\s+(.+)", text)
    if m:
        qty_add = int(m.group(2))
        query   = m.group(3).strip()
        product = find_product(inventory, query)
        if product:
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    new_qty = inventory[product]["qty"] + qty_add
                    inv_ws.update_cell(i + 1, 2, new_qty)
                    send_message(chat_id, f"✅ تم إضافة {qty_add} لـ {product}\nالرصيد الحالي: `{new_qty}`")
                    return
        else:
            inv_ws.append_row([query, qty_add, ""])
            send_message(chat_id, f"🆕 منتج جديد: *{query}* بكمية `{qty_add}`")
        return

    # 3. تسجيل بيع (احمد شال 2 بطارية)
    m = re.search(r"(.+?)\s+(شال|اخد|اشترى|أخد|باع|طلب)\s+(\d+)\s+(.+)", text)
    if m:
        customer = m.group(1).strip()
        qty_sell = int(m.group(3))
        query    = m.group(4).strip()
        product  = find_product(inventory, query)
        
        if not product:
            send_message(chat_id, f"❓ المنتج '{query}' مش موجود في المخزن.")
            return
        
        current_qty = inventory[product]["qty"]
        if current_qty < qty_sell:
            send_message(chat_id, f"⚠️ الكمية مش كفاية! المتاح `{current_qty}` بس.")
            return
            
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                new_qty = current_qty - qty_sell
                inv_ws.update_cell(i + 1, 2, new_qty)
                break
                
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sales_ws.append_row([date_str, customer, product, qty_sell])
        send_message(chat_id, f"💰 *عملية بيع*\n👤 العميل: {customer}\n📦 المنتج: {product}\n📉 الكمية: {qty_sell}\n✅ المتبقي: `{new_qty}`")
        return

    # رسالة المساعدة
    send_message(chat_id, "🤖 *أوامر البوت:*\n- باقي كم (اسم المنتج)\n- اضافة 10 (اسم المنتج)\n- محمد شال 2 (اسم المنتج)")

# ── HTTP Server (ضروري لـ Render) ─────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is online")
    def log_message(self, format, *args): pass

def run_health_server():
    # ريندر بيبعت البورت في متغير PORT
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"🚀 Health Server on port {port}")
    server.serve_forever()

# ── Polling (سحب الرسائل) ─────────────────────────────────
def run_bot():
    offset = None
    logger.info("📡 Bot Polling started...")
    while True:
        try:
            # استخدام timeout قصير (10 ثواني) لضمان عدم تعليق السيرفر
            params = {"timeout": 10, "offset": offset}
            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=15)
            
            if resp.status_code == 200:
                updates = resp.json().get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "")
                    if chat_id and text:
                        handle_message(chat_id, text)
            else:
                logger.error(f"Telegram API Error: {resp.status_code}")
                time.sleep(5)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(10)

# ── التشغيل ────────────────────────────────────────────────
if __name__ == "__main__":
    # تشغيل سيرفر الـ Health في Thread منفصل
    threading.Thread(target=run_health_server, daemon=True).start()
    # تشغيل البوت
    run_bot()
