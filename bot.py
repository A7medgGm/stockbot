import os
import re
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ── إعدادات ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID  = os.environ.get("SHEET_ID")

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

    # تأكد إن الشيتات موجودة
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

# ── مساعد: جيب كل المنتجات ───────────────────────────────
def get_inventory(inv_ws):
    records = inv_ws.get_all_values()
    result = {}
    for row in records[1:]:  # تخطى الهيدر
        if len(row) >= 2 and row[0].strip():
            result[row[0].strip()] = {
                "qty": int(row[1]) if row[1].isdigit() else 0,
                "price": row[2] if len(row) > 2 else ""
            }
    return result

# ── مساعد: دور على منتج بالاسم التقريبي ─────────────────
def find_product(inventory, query):
    query = query.strip().lower()
    for name in inventory:
        if query in name.lower() or name.lower() in query:
            return name
    return None

# ── المعالج الرئيسي للرسائل ──────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    try:
        inv_ws, sales_ws = get_sheets()
        inventory = get_inventory(inv_ws)
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في الاتصال بالشيت: {e}")
        return

    # ── أمر: باقي كم [منتج] ──────────────────────────────
    if re.search(r"باقي كم|كم باقي|كم في المخزن", text, re.IGNORECASE):
        # استخرج اسم المنتج
        query = re.sub(r"باقي كم|كم باقي|كم في المخزن|في المخزن|من", "", text).strip()
        if not query:
            # اعرض كل المخزن
            if not inventory:
                await update.message.reply_text("📦 المخزن فاضي!")
                return
            msg = "📦 *المخزن الحالي:*\n\n"
            for name, data in inventory.items():
                msg += f"• {name}: *{data['qty']}* قطعة\n"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return
        
        product = find_product(inventory, query)
        if product:
            qty = inventory[product]["qty"]
            await update.message.reply_text(f"📦 *{product}*\nالمتبقي: *{qty}* قطعة", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ ما لقيتش منتج بـ '{query}'")
        return

    # ── أمر: عمليات [منتج] ───────────────────────────────
    if text.startswith("عمليات") or text.startswith("سجل"):
        query = re.sub(r"^عمليات|^سجل", "", text).strip()
        all_sales = sales_ws.get_all_values()
        
        matched = []
        for row in all_sales[1:]:
            if len(row) >= 4 and query.lower() in row[2].lower():
                matched.append(row)
        
        if not matched:
            await update.message.reply_text(f"📋 مفيش مبيعات مسجلة لـ '{query}'")
            return
        
        msg = f"📋 *مبيعات {query}:*\n\n"
        for row in matched:
            msg += f"• {row[0]} | {row[1]} | {row[2]} | {row[3]} قطعة\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # ── أمر: إضافة مخزون ─────────────────────────────────
    m = re.search(r"(اضافة|أضف|وصل|استلمت)\s+(\d+)\s+(.+)", text, re.IGNORECASE)
    if m:
        qty_add = int(m.group(2))
        query   = m.group(3).strip()
        product = find_product(inventory, query)
        
        if product:
            # حدّث الكمية
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    new_qty = inventory[product]["qty"] + qty_add
                    inv_ws.update_cell(i + 1, 2, new_qty)
                    await update.message.reply_text(
                        f"✅ تم إضافة *{qty_add}* قطعة لـ *{product}*\nالرصيد الجديد: *{new_qty}*",
                        parse_mode="Markdown"
                    )
                    return
        else:
            # منتج جديد
            inv_ws.append_row([query, qty_add, ""])
            await update.message.reply_text(
                f"✅ تم إضافة منتج جديد *{query}* بكمية *{qty_add}*",
                parse_mode="Markdown"
            )
        return

    # ── أمر: بيع — [اسم] شال/اخد/اشترى [كمية] [منتج] ────
    m = re.search(r"(.+?)\s+(شال|اخد|اشترى|أخد|باع|طلب)\s+(\d+)\s+(.+)", text, re.IGNORECASE)
    if m:
        customer = m.group(1).strip()
        qty_sell = int(m.group(3))
        query    = m.group(4).strip()
        product  = find_product(inventory, query)
        
        if not product:
            await update.message.reply_text(f"❌ ما لقيتش منتج بـ '{query}'")
            return
        
        current_qty = inventory[product]["qty"]
        if current_qty < qty_sell:
            await update.message.reply_text(
                f"⚠️ المخزون مش كافي!\n*{product}*: متبقي *{current_qty}* بس",
                parse_mode="Markdown"
            )
            return
        
        # خصم من المخزون
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                new_qty = current_qty - qty_sell
                inv_ws.update_cell(i + 1, 2, new_qty)
                break
        
        # سجّل البيع
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        sales_ws.append_row([date_str, customer, product, qty_sell])
        
        price = inventory[product]["price"]
        total = ""
        if price:
            try:
                p = float(re.sub(r"[^\d.]", "", price))
                total = f"\nالإجمالي: *{p * qty_sell:.0f} درهم*"
            except:
                pass
        
        await update.message.reply_text(
            f"✅ تم تسجيل البيع\n"
            f"العميل: *{customer}*\n"
            f"المنتج: *{product}*\n"
            f"الكمية: *{qty_sell}*\n"
            f"المتبقي: *{new_qty}*{total}",
            parse_mode="Markdown"
        )
        return

    # ── مساعدة ───────────────────────────────────────────
    help_text = (
        "🤖 *أوامر البوت:*\n\n"
        "📦 *مخزون:*\n"
        "• `باقي كم رام 8` — استعلام كمية\n"
        "• `باقي كم` — كل المخزن\n"
        "• `اضافة 20 رام 8` — إضافة كمية\n\n"
        "🛒 *مبيعات:*\n"
        "• `أحمد شال 2 رام 8` — تسجيل بيع\n\n"
        "📋 *تقارير:*\n"
        "• `عمليات رام 8` — سجل مبيعات منتج\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ── HTTP Server بسيط عشان Render ─────────────────────────
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

# ── تشغيل البوت ──────────────────────────────────────────
def main():
    # شغّل HTTP server في background
    t = threading.Thread(target=run_health_server, daemon=True)
    t.start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("البوت شغال ✅")
    app.run_polling()

if __name__ == "__main__":
    main()