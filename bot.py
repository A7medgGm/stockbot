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
LOW_STOCK_THRESHOLD = 5  # تنبيه لما المخزون ينخفض

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
        ws = spreadsheet.add_worksheet(title="Sales", rows=1000, cols=5)
        ws.append_row(["ID", "التاريخ", "العميل", "المنتج", "الكمية"])
    if "Expenses" not in titles:
        ws = spreadsheet.add_worksheet(title="Expenses", rows=1000, cols=3)
        ws.append_row(["التاريخ", "البيان", "المبلغ"])

    inv      = spreadsheet.worksheet("Inventory")
    sales    = spreadsheet.worksheet("Sales")
    expenses = spreadsheet.worksheet("Expenses")
    return inv, sales, expenses

# ── إرسال رسالة ──────────────────────────────────────────
def send(chat_id, text):
    requests.post(f"{API_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

# ── جيب المخزون ──────────────────────────────────────────
def get_inventory(inv_ws):
    records = inv_ws.get_all_values()
    result = {}
    for row in records[1:]:
        if len(row) >= 2 and row[0].strip():
            try:
                qty = int(row[1])
            except:
                qty = 0
            try:
                price = float(re.sub(r"[^\d.]", "", row[2])) if len(row) > 2 and row[2] else 0
            except:
                price = 0
            result[row[0].strip()] = {"qty": qty, "price": price}
    return result

# ── دور على منتج ─────────────────────────────────────────
def find_product(inventory, query):
    query = query.strip().lower()
    for name in inventory:
        if query in name.lower() or name.lower() in query:
            return name
    return None

# ── جيب ID آخر عملية بيع ─────────────────────────────────
def get_next_sale_id(sales_ws):
    all_rows = sales_ws.get_all_values()
    if len(all_rows) <= 1:
        return 1
    try:
        last_id = int(all_rows[-1][0])
        return last_id + 1
    except:
        return len(all_rows)

# ── تنبيه مخزون منخفض ────────────────────────────────────
def check_low_stock(chat_id, inventory):
    low = [f"- {name}: {data['qty']} قطعة" for name, data in inventory.items() if data['qty'] <= LOW_STOCK_THRESHOLD]
    if low:
        send(chat_id, "تحذير: المخزون منخفض!\n\n" + "\n".join(low))

# ── دليل الأوامر ──────────────────────────────────────────
HELP_TEXT = """دليل الاوامر:

المخزون:
- باقي كم رام 8
- باقي كم (كل المخزن)
- اضافة 20 رام 8
- تعديل سعر رام 8 الى 130
- تعديل اسم رام 8 الى رام 8GB
- حذف منتج رام 8

المبيعات:
- احمد شال 2 رام 8
- الغاء بيع 5 (رقم العملية)
- مرتجع احمد 1 رام 8

المصاريف:
- مصروف ايجار 500
- مصروف كهرباء 200

التقارير:
- عمليات رام 8
- تقرير اليوم
- تقرير الاسبوع
- تقرير الشهر
- ارباح اليوم
- اكثر منتج
- اكثر عميل

مساعدة — لعرض هذا الدليل"""

# ── معالجة الرسائل ───────────────────────────────────────
def handle_message(chat_id, text):
    text = text.strip()

    try:
        inv_ws, sales_ws, exp_ws = get_sheets()
        inventory = get_inventory(inv_ws)
    except Exception as e:
        send(chat_id, f"خطا في الاتصال بالشيت: {e}")
        return

    # مساعدة
    if text in ["مساعدة", "help", "هيلب", "الاوامر"]:
        send(chat_id, HELP_TEXT)
        return

    # ── باقي كم ──────────────────────────────────────────
    if re.search(r"باقي كم|كم باقي|كم في المخزن", text):
        query = re.sub(r"باقي كم|كم باقي|كم في المخزن|في المخزن|من", "", text).strip()
        if not query:
            if not inventory:
                send(chat_id, "المخزن فاضي!")
                return
            msg = "المخزن الحالي:\n\n"
            for name, data in inventory.items():
                price_str = f" | {data['price']} درهم" if data['price'] else ""
                msg += f"- {name}: {data['qty']} قطعة{price_str}\n"
            send(chat_id, msg)
            return
        product = find_product(inventory, query)
        if product:
            d = inventory[product]
            price_str = f"\nالسعر: {d['price']} درهم" if d['price'] else ""
            send(chat_id, f"{product}\nالمتبقي: {d['qty']} قطعة{price_str}")
        else:
            send(chat_id, f"ما لقيتش منتج بـ '{query}'")
        return

    # ── تعديل سعر ────────────────────────────────────────
    m = re.search(r"تعديل سعر (.+?) (?:الى|إلى|ب) ?([\d.]+)", text)
    if m:
        query = m.group(1).strip()
        new_price = m.group(2)
        product = find_product(inventory, query)
        if product:
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    inv_ws.update_cell(i + 1, 3, new_price)
                    send(chat_id, f"تم تعديل سعر {product} الى {new_price} درهم")
                    return
        else:
            send(chat_id, f"ما لقيتش منتج بـ '{query}'")
        return

    # ── تعديل اسم ────────────────────────────────────────
    m = re.search(r"تعديل اسم (.+?) (?:الى|إلى) (.+)", text)
    if m:
        query = m.group(1).strip()
        new_name = m.group(2).strip()
        product = find_product(inventory, query)
        if product:
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    inv_ws.update_cell(i + 1, 1, new_name)
                    send(chat_id, f"تم تغيير الاسم من {product} الى {new_name}")
                    return
        else:
            send(chat_id, f"ما لقيتش منتج بـ '{query}'")
        return

    # ── حذف منتج ─────────────────────────────────────────
    m = re.search(r"حذف منتج (.+)", text)
    if m:
        query = m.group(1).strip()
        product = find_product(inventory, query)
        if product:
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    inv_ws.delete_rows(i + 1)
                    send(chat_id, f"تم حذف {product}")
                    return
        else:
            send(chat_id, f"ما لقيتش منتج بـ '{query}'")
        return

    # ── إضافة مخزون ──────────────────────────────────────
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
                    send(chat_id, f"تم اضافة {qty_add} لـ {product}\nالرصيد: {new_qty}")
                    return
        else:
            inv_ws.append_row([query, qty_add, ""])
            send(chat_id, f"منتج جديد: {query} | الكمية: {qty_add}")
        return

    # ── بيع ──────────────────────────────────────────────
    m = re.search(r"(.+?)\s+(شال|اخد|اشترى|أخد|باع|طلب)\s+(\d+)\s+(.+)", text)
    if m:
        customer = m.group(1).strip()
        qty_sell = int(m.group(3))
        query    = m.group(4).strip()
        product  = find_product(inventory, query)
        if not product:
            send(chat_id, f"ما لقيتش منتج بـ '{query}'")
            return
        current_qty = inventory[product]["qty"]
        if current_qty < qty_sell:
            send(chat_id, f"المخزون مش كافي!\n{product}: متبقي {current_qty} بس")
            return
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                new_qty = current_qty - qty_sell
                inv_ws.update_cell(i + 1, 2, new_qty)
                break
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sale_id  = get_next_sale_id(sales_ws)
        sales_ws.append_row([sale_id, date_str, customer, product, qty_sell])
        price    = inventory[product]["price"]
        total    = price * qty_sell if price else 0
        total_str = f"\nالاجمالي: {total:.0f} درهم" if total else ""
        send(chat_id,
            f"تم تسجيل البيع (رقم {sale_id})\n"
            f"العميل: {customer}\n"
            f"المنتج: {product}\n"
            f"الكمية: {qty_sell}{total_str}\n"
            f"المتبقي: {new_qty}"
        )
        check_low_stock(chat_id, get_inventory(inv_ws))
        return

    # ── إلغاء بيع ────────────────────────────────────────
    m = re.search(r"الغاء بيع (\d+)", text)
    if m:
        sale_id = int(m.group(1))
        all_rows = sales_ws.get_all_values()
        for i, row in enumerate(all_rows[1:], start=2):
            if len(row) >= 5 and str(row[0]) == str(sale_id):
                product  = row[3]
                qty_back = int(row[4])
                prod = find_product(inventory, product)
                if prod:
                    all_inv = inv_ws.get_all_values()
                    for j, r in enumerate(all_inv):
                        if r[0].strip() == prod:
                            new_qty = inventory[prod]["qty"] + qty_back
                            inv_ws.update_cell(j + 1, 2, new_qty)
                            break
                sales_ws.delete_rows(i)
                send(chat_id, f"تم الغاء عملية البيع رقم {sale_id}\nتم ارجاع {qty_back} قطعة من {product}")
                return
        send(chat_id, f"ما لقيتش عملية بيع رقم {sale_id}")
        return

    # ── مرتجع ────────────────────────────────────────────
    m = re.search(r"مرتجع (.+?)\s+(\d+)\s+(.+)", text)
    if m:
        customer  = m.group(1).strip()
        qty_back  = int(m.group(2))
        query     = m.group(3).strip()
        product   = find_product(inventory, query)
        if not product:
            send(chat_id, f"ما لقيتش منتج بـ '{query}'")
            return
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                new_qty = inventory[product]["qty"] + qty_back
                inv_ws.update_cell(i + 1, 2, new_qty)
                break
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sales_ws.append_row(["مرتجع", date_str, customer, product, f"-{qty_back}"])
        send(chat_id, f"تم تسجيل المرتجع\nالعميل: {customer}\nالمنتج: {product}\nالكمية: {qty_back}\nالمخزون الجديد: {new_qty}")
        return

    # ── مصروف ────────────────────────────────────────────
    m = re.search(r"مصروف (.+?)\s+([\d.]+)", text)
    if m:
        desc   = m.group(1).strip()
        amount = m.group(2)
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        exp_ws.append_row([date_str, desc, amount])
        send(chat_id, f"تم تسجيل المصروف\n{desc}: {amount} درهم")
        return

    # ── تقارير ───────────────────────────────────────────
    if re.search(r"تقرير|ارباح|اكثر منتج|اكثر عميل|عمليات", text):

        # عمليات منتج معين
        if text.startswith("عمليات"):
            query = re.sub(r"^عمليات", "", text).strip()
            all_sales = sales_ws.get_all_values()
            matched = [row for row in all_sales[1:] if len(row) >= 5 and query.lower() in row[3].lower()]
            if not matched:
                send(chat_id, f"مفيش مبيعات لـ '{query}'")
                return
            msg = f"مبيعات {query}:\n\n"
            for row in matched:
                msg += f"- {row[1]} | {row[2]} | {row[4]} قطعة\n"
            send(chat_id, msg)
            return

        # تحديد الفترة
        today = datetime.now().strftime("%Y-%m-%d")
        if "اليوم" in text:
            period = today
            period_label = "اليوم"
        elif "الاسبوع" in text:
            period = datetime.now().strftime("%Y-%m-")
            period_label = "هذا الاسبوع"
        elif "الشهر" in text:
            period = datetime.now().strftime("%Y-%m")
            period_label = "هذا الشهر"
        else:
            period = ""
            period_label = "الكل"

        all_sales = sales_ws.get_all_values()
        filtered  = [row for row in all_sales[1:] if len(row) >= 5 and (not period or row[1].startswith(period)) and not str(row[4]).startswith("-")]

        # أرباح
        if "ارباح" in text:
            total_revenue = 0
            for row in filtered:
                prod = find_product(inventory, row[3])
                if prod and inventory[prod]["price"]:
                    total_revenue += inventory[prod]["price"] * int(row[4])
            all_exp = exp_ws.get_all_values()
            total_exp = sum(float(row[2]) for row in all_exp[1:] if len(row) >= 3 and (not period or row[0].startswith(period)) and row[2])
            profit = total_revenue - total_exp
            send(chat_id,
                f"الارباح ({period_label}):\n\n"
                f"المبيعات: {total_revenue:.0f} درهم\n"
                f"المصاريف: {total_exp:.0f} درهم\n"
                f"صافي الربح: {profit:.0f} درهم"
            )
            return

        # أكثر منتج
        if "اكثر منتج" in text:
            counts = {}
            for row in filtered:
                p = row[3]
                counts[p] = counts.get(p, 0) + int(row[4])
            if not counts:
                send(chat_id, "مفيش مبيعات")
                return
            sorted_p = sorted(counts.items(), key=lambda x: x[1], reverse=True)
            msg = f"اكثر المنتجات مبيعا ({period_label}):\n\n"
            for p, qty in sorted_p[:5]:
                msg += f"- {p}: {qty} قطعة\n"
            send(chat_id, msg)
            return

        # أكثر عميل
        if "اكثر عميل" in text:
            counts = {}
            for row in filtered:
                c = row[2]
                counts[c] = counts.get(c, 0) + 1
            if not counts:
                send(chat_id, "مفيش مبيعات")
                return
            sorted_c = sorted(counts.items(), key=lambda x: x[1], reverse=True)
            msg = f"اكثر العملاء شراء ({period_label}):\n\n"
            for c, cnt in sorted_c[:5]:
                msg += f"- {c}: {cnt} عملية\n"
            send(chat_id, msg)
            return

        # تقرير عام
        total_sold = sum(int(row[4]) for row in filtered if row[4].lstrip('-').isdigit())
        total_revenue = 0
        for row in filtered:
            prod = find_product(inventory, row[3])
            if prod and inventory[prod]["price"]:
                total_revenue += inventory[prod]["price"] * int(row[4])
        send(chat_id,
            f"تقرير {period_label}:\n\n"
            f"عدد العمليات: {len(filtered)}\n"
            f"اجمالي القطع المباعة: {total_sold}\n"
            f"اجمالي المبيعات: {total_revenue:.0f} درهم"
        )
        return

    # رسالة غير معروفة
    send(chat_id, "ما فهمتش الامر. ابعت 'مساعدة' لعرض الاوامر")

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
            logger.error(f"خطا: {e}")
            time.sleep(5)

if __name__ == "__main__":
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()
    run_bot()
