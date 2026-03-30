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
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    creds_dict = json.loads(creds_json)
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

# ── إرسال رسالة ──────────────────────────────────────────
def send_message(chat_id, text):
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    })

# ── جيب كل المنتجات ──────────────────────────────────────
def get_inventory(inv_ws):
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

    try:
        inv_ws, sales_ws = get_sheets()
        inventory = get_inventory(inv_ws)
    except Exception as e:
        send_message(chat_id, f"خطأ في الاتصال بالشيت: {e}")
        return

    # باقي كم
    if re.search(r"باقي كم|كم باقي|كم في المخزن", text):
        query = re.sub(r"باقي كم|كم باقي|كم في المخزن|في المخزن|من", "", text).strip()
        if not query:
            if not inventory:
                send_message(chat_id, "المخزن فاضي!")
                return
            msg = "المخزن الحالي:\n\n"
            for name, data in inventory.items():
                msg += f"- {name}: {data['qty']} قطعة\n"
            send_message(chat_id, msg)
            return
        product = find_product(inventory, query)
        if product:
            send_message(chat_id, f"{product}\nالمتبقي: {inventory[product]['qty']} قطعة")
        else:
            send_message(chat_id, f"ما لقيتش منتج بـ '{query}'")
        return

    # عمليات
    if text.startswith("عمليات") or text.startswith("سجل"):
        query = re.sub(r"^عمليات|^سجل", "", text).strip()
        all_sales = sales_ws.get_all_values()
        matched = [row for row in all_sales[1:] if len(row) >= 4 and query.lower() in row[2].lower()]
        if not matched:
            send_message(chat_id, f"مفيش مبيعات مسجلة لـ '{query}'")
            return
        msg = f"مبيعات {query}:\n\n"
        for row in matched:
            msg += f"- {row[0]} | {row[1]} | {row[2]} | {row[3]} قطعة\n"
        send_message(chat_id, msg)
        return

    # إضافة مخزون
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
                    send_message(chat_id, f"تم اضافة {qty_add} لـ {product}\nالرصيد: {new_qty}")
                    return
        else:
            inv_ws.append_row([query, qty_add, ""])
            send_message(chat_id, f"منتج جديد {query} بكمية {qty_add}")
        return

    # بيع
    m = re.search(r"(.+?)\s+(شال|اخد|اشترى|أخد|باع|طلب)\s+(\d+)\s+(.+)", text)
    if m:
        customer = m.group(1).strip()
        qty_sell = int(m.group(3))
        query    = m.group(4).strip()
        product  = find_product(inventory, query)
        if not product:
            send_message(chat_id, f"ما لقيتش منتج بـ '{query}'")
            return
        current_qty = inventory[product]["qty"]
        if current_qty < qty_sell:
            send_message(chat_id, f"المخزون مش كافي!\n{product}: متبقي {current_qty} بس")
            return
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                new_qty = current_qty - qty_sell
                inv_ws.update_cell(i + 1, 2, new_qty)
                break
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sales_ws.append_row([date_str, customer, product, qty_sell])
        send_message(chat_id,
            f"تم تسجيل البيع\n"
            f"العميل: {customer}\n"
            f"المنتج: {product}\n"
            f"الكمية: {qty_sell}\n"
            f"المتبقي: {new_qty}"
        )
        return

    # مساعدة
    send_message(chat_id,
        "اوامر البوت:\n\n"
        "باقي كم رام 8\n"
        "باقي كم — كل المخزن\n"
        "اضافة 20 رام 8\n"
        "احمد شال 2 رام 8\n"
        "عمليات رام 8\n"
    )

# ── HTTP Server ───────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ── Polling ───────────────────────────────────────────────
def run_bot():
    offset = None
    logger.info("البوت شغال")
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=35)
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")
                if chat_id and text:
                    handle_message(chat_id, text)
        except Exception as e:
            logger.error(f"خطأ: {e}")
            time.sleep(5)

# ── تشغيل ─────────────────────────────────────────────────
if __name__ == "__main__":
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    run_bot()
