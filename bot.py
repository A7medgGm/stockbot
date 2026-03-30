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
LOW_STOCK  = 5

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# حالة المحادثات النشطة
sessions = {}

# ── Google Sheets ─────────────────────────────────────────
def get_sheets():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDS_JSON"))
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sp = client.open_by_key(SHEET_ID)
    titles = [ws.title for ws in sp.worksheets()]

    if "Inventory" not in titles:
        ws = sp.add_worksheet("Inventory", 100, 3)
        ws.append_row(["المنتج", "الكمية", "السعر"])
    if "Sales" not in titles:
        ws = sp.add_worksheet("Sales", 1000, 5)
        ws.append_row(["ID", "التاريخ", "العميل", "المنتج", "الكمية"])
    if "Expenses" not in titles:
        ws = sp.add_worksheet("Expenses", 1000, 3)
        ws.append_row(["التاريخ", "البيان", "المبلغ"])

    return sp.worksheet("Inventory"), sp.worksheet("Sales"), sp.worksheet("Expenses")

def get_inventory(inv_ws):
    rows = inv_ws.get_all_values()
    result = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip():
            try: qty = int(row[1])
            except: qty = 0
            try: price = float(re.sub(r"[^\d.]", "", row[2])) if len(row) > 2 and row[2] else 0
            except: price = 0
            result[row[0].strip()] = {"qty": qty, "price": price}
    return result

def get_next_id(ws):
    rows = ws.get_all_values()
    if len(rows) <= 1: return 1
    try: return int(rows[-1][0]) + 1
    except: return len(rows)

# ── Telegram API ──────────────────────────────────────────
def send(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    requests.post(f"{API_URL}/sendMessage", json=data)

def answer_callback(callback_id):
    requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": callback_id})

def find_product(inventory, query):
    query = query.strip().lower()
    for name in inventory:
        if query in name.lower() or name.lower() in query:
            return name
    return None

def check_low_stock(chat_id, inventory):
    low = [f"- {n}: {d['qty']} قطعة" for n, d in inventory.items() if d['qty'] <= LOW_STOCK]
    if low:
        send(chat_id, "تحذير: مخزون منخفض!\n\n" + "\n".join(low))

# ── القوائم الرئيسية ──────────────────────────────────────
def main_menu(chat_id):
    keyboard = [
        [{"text": "🧾 فاتورة جديدة", "callback_data": "new_invoice"}],
        [{"text": "📦 المخزون", "callback_data": "menu_inventory"},
         {"text": "📊 التقارير", "callback_data": "menu_reports"}],
        [{"text": "💸 مصروف", "callback_data": "menu_expense"},
         {"text": "🔧 إدارة المنتجات", "callback_data": "menu_manage"}],
        [{"text": "📋 دليل الأوامر", "callback_data": "help"}]
    ]
    send(chat_id, "القائمة الرئيسية:", keyboard)

def inventory_menu(chat_id):
    keyboard = [
        [{"text": "📦 عرض المخزون", "callback_data": "show_inventory"}],
        [{"text": "➕ إضافة كمية", "callback_data": "add_stock"},
         {"text": "🆕 منتج جديد", "callback_data": "new_product"}],
        [{"text": "🔙 رجوع", "callback_data": "main_menu"}]
    ]
    send(chat_id, "إدارة المخزون:", keyboard)

def reports_menu(chat_id):
    keyboard = [
        [{"text": "📅 تقرير اليوم", "callback_data": "report_today"},
         {"text": "📅 تقرير الأسبوع", "callback_data": "report_week"}],
        [{"text": "📅 تقرير الشهر", "callback_data": "report_month"}],
        [{"text": "💰 أرباح اليوم", "callback_data": "profit_today"},
         {"text": "💰 أرباح الشهر", "callback_data": "profit_month"}],
        [{"text": "🏆 أكثر منتج", "callback_data": "top_product"},
         {"text": "👤 أكثر عميل", "callback_data": "top_customer"}],
        [{"text": "🔙 رجوع", "callback_data": "main_menu"}]
    ]
    send(chat_id, "التقارير:", keyboard)

def manage_menu(chat_id):
    keyboard = [
        [{"text": "✏️ تعديل سعر", "callback_data": "edit_price"},
         {"text": "✏️ تعديل اسم", "callback_data": "edit_name"}],
        [{"text": "🗑️ حذف منتج", "callback_data": "delete_product"}],
        [{"text": "❌ إلغاء بيع", "callback_data": "cancel_sale"},
         {"text": "↩️ مرتجع", "callback_data": "return_sale"}],
        [{"text": "🔙 رجوع", "callback_data": "main_menu"}]
    ]
    send(chat_id, "إدارة المنتجات:", keyboard)

def products_keyboard(inventory, callback_prefix="inv"):
    buttons = []
    items = list(inventory.keys())
    for i in range(0, len(items), 2):
        row = [{"text": items[i], "callback_data": f"{callback_prefix}:{items[i]}"}]
        if i + 1 < len(items):
            row.append({"text": items[i+1], "callback_data": f"{callback_prefix}:{items[i+1]}"})
        buttons.append(row)
    buttons.append([{"text": "🔙 رجوع", "callback_data": "main_menu"}])
    return buttons

# ── معالجة الأزرار ────────────────────────────────────────
def handle_callback(chat_id, data, callback_id):
    answer_callback(callback_id)

    try:
        inv_ws, sales_ws, exp_ws = get_sheets()
        inventory = get_inventory(inv_ws)
    except Exception as e:
        send(chat_id, f"خطأ: {e}")
        return

    # القوائم
    if data == "main_menu":
        sessions.pop(chat_id, None)
        main_menu(chat_id)
        return
    if data == "menu_inventory":
        inventory_menu(chat_id)
        return
    if data == "menu_reports":
        reports_menu(chat_id)
        return
    if data == "menu_manage":
        manage_menu(chat_id)
        return
    if data == "help":
        send(chat_id, HELP_TEXT)
        return

    # عرض المخزون
    if data == "show_inventory":
        if not inventory:
            send(chat_id, "المخزن فاضي!")
            return
        msg = "المخزن الحالي:\n\n"
        for name, d in inventory.items():
            price_str = f" | {d['price']:.0f} درهم" if d['price'] else ""
            msg += f"- {name}: {d['qty']} قطعة{price_str}\n"
        send(chat_id, msg, [[{"text": "🔙 رجوع", "callback_data": "menu_inventory"}]])
        return

    # ── فاتورة جديدة ─────────────────────────────────────
    if data == "new_invoice":
        sessions[chat_id] = {"step": "invoice_customer"}
        send(chat_id, "اكتب اسم العميل:")
        return

    if data.startswith("invoice_add:"):
        product = data.split(":", 1)[1]
        sessions[chat_id]["current_product"] = product
        sessions[chat_id]["step"] = "invoice_qty"
        send(chat_id, f"كم قطعة من {product}؟")
        return

    if data == "invoice_done":
        sess = sessions.get(chat_id, {})
        items = sess.get("items", [])
        if not items:
            send(chat_id, "مفيش منتجات في الفاتورة!")
            main_menu(chat_id)
            return
        customer = sess.get("customer", "")
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"الفاتورة - {customer}\n{'─'*20}\n"
        total = 0
        for item in items:
            price = inventory.get(item["product"], {}).get("price", 0)
            subtotal = price * item["qty"]
            total += subtotal
            msg += f"- {item['product']} × {item['qty']}"
            if subtotal: msg += f" = {subtotal:.0f} درهم"
            msg += "\n"
            # خصم من المخزون
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == item["product"]:
                    new_qty = inventory[item["product"]]["qty"] - item["qty"]
                    inv_ws.update_cell(i + 1, 2, new_qty)
                    break
            # تسجيل في Sales
            sale_id = get_next_id(sales_ws)
            sales_ws.append_row([sale_id, date_str, customer, item["product"], item["qty"]])
        if total:
            msg += f"{'─'*20}\nالإجمالي: {total:.0f} درهم"
        send(chat_id, msg)
        sessions.pop(chat_id, None)
        check_low_stock(chat_id, get_inventory(inv_ws))
        main_menu(chat_id)
        return

    if data == "invoice_add_more":
        sess = sessions.get(chat_id, {})
        sess["step"] = "invoice_product"
        keyboard = products_keyboard(inventory, "invoice_add")
        send(chat_id, "اختار منتج:", keyboard)
        return

    # ── إضافة كمية ───────────────────────────────────────
    if data == "add_stock":
        sessions[chat_id] = {"step": "add_stock_product"}
        keyboard = products_keyboard(inventory, "addstock")
        send(chat_id, "اختار المنتج:", keyboard)
        return

    if data.startswith("addstock:"):
        product = data.split(":", 1)[1]
        sessions[chat_id] = {"step": "add_stock_qty", "product": product}
        send(chat_id, f"كم قطعة تضيف لـ {product}؟")
        return

    # ── منتج جديد ─────────────────────────────────────────
    if data == "new_product":
        sessions[chat_id] = {"step": "new_product_name"}
        send(chat_id, "اكتب اسم المنتج الجديد:")
        return

    # ── تعديل سعر ─────────────────────────────────────────
    if data == "edit_price":
        sessions[chat_id] = {"step": "edit_price_product"}
        keyboard = products_keyboard(inventory, "editprice")
        send(chat_id, "اختار المنتج:", keyboard)
        return

    if data.startswith("editprice:"):
        product = data.split(":", 1)[1]
        sessions[chat_id] = {"step": "edit_price_value", "product": product}
        send(chat_id, f"اكتب السعر الجديد لـ {product}:")
        return

    # ── تعديل اسم ─────────────────────────────────────────
    if data == "edit_name":
        sessions[chat_id] = {"step": "edit_name_product"}
        keyboard = products_keyboard(inventory, "editname")
        send(chat_id, "اختار المنتج:", keyboard)
        return

    if data.startswith("editname:"):
        product = data.split(":", 1)[1]
        sessions[chat_id] = {"step": "edit_name_value", "product": product}
        send(chat_id, f"اكتب الاسم الجديد لـ {product}:")
        return

    # ── حذف منتج ─────────────────────────────────────────
    if data == "delete_product":
        sessions[chat_id] = {"step": "delete_product"}
        keyboard = products_keyboard(inventory, "deleteprod")
        send(chat_id, "اختار المنتج للحذف:", keyboard)
        return

    if data.startswith("deleteprod:"):
        product = data.split(":", 1)[1]
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                inv_ws.delete_rows(i + 1)
                break
        sessions.pop(chat_id, None)
        send(chat_id, f"تم حذف {product}")
        main_menu(chat_id)
        return

    # ── مصروف ─────────────────────────────────────────────
    if data == "menu_expense":
        sessions[chat_id] = {"step": "expense_desc"}
        send(chat_id, "اكتب وصف المصروف:")
        return

    # ── إلغاء بيع ─────────────────────────────────────────
    if data == "cancel_sale":
        sessions[chat_id] = {"step": "cancel_sale_id"}
        send(chat_id, "اكتب رقم عملية البيع للإلغاء:")
        return

    # ── مرتجع ─────────────────────────────────────────────
    if data == "return_sale":
        sessions[chat_id] = {"step": "return_customer"}
        send(chat_id, "اكتب اسم العميل:")
        return

    if data.startswith("return_prod:"):
        product = data.split(":", 1)[1]
        sessions[chat_id]["product"] = product
        sessions[chat_id]["step"] = "return_qty"
        send(chat_id, f"كم قطعة مرتجع من {product}؟")
        return

    # ── التقارير ──────────────────────────────────────────
    if data.startswith("report_") or data.startswith("profit_") or data in ["top_product", "top_customer"]:
        handle_report(chat_id, data, inventory, sales_ws, exp_ws)
        return

def handle_report(chat_id, data, inventory, sales_ws, exp_ws):
    today = datetime.now().strftime("%Y-%m-%d")
    month = datetime.now().strftime("%Y-%m")

    if "today" in data: period, label = today, "اليوم"
    elif "week" in data: period, label = today[:8], "هذا الأسبوع"
    elif "month" in data: period, label = month, "هذا الشهر"
    else: period, label = "", "الكل"

    all_sales = sales_ws.get_all_values()
    filtered = [r for r in all_sales[1:] if len(r) >= 5 and (not period or r[1].startswith(period)) and not str(r[4]).startswith("-")]

    if "profit" in data:
        rev = sum(inventory.get(find_product(inventory, r[3]) or "", {}).get("price", 0) * int(r[4]) for r in filtered if r[4].isdigit())
        all_exp = exp_ws.get_all_values()
        exp_total = sum(float(r[2]) for r in all_exp[1:] if len(r) >= 3 and (not period or r[0].startswith(period)) and r[2])
        send(chat_id, f"الأرباح ({label}):\n\nالمبيعات: {rev:.0f} درهم\nالمصاريف: {exp_total:.0f} درهم\nصافي الربح: {rev - exp_total:.0f} درهم",
             [[{"text": "🔙 رجوع", "callback_data": "menu_reports"}]])
        return

    if data == "top_product":
        counts = {}
        for r in filtered:
            counts[r[3]] = counts.get(r[3], 0) + int(r[4])
        if not counts:
            send(chat_id, "مفيش مبيعات", [[{"text": "🔙 رجوع", "callback_data": "menu_reports"}]])
            return
        msg = "أكثر المنتجات مبيعاً:\n\n"
        for p, q in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]:
            msg += f"- {p}: {q} قطعة\n"
        send(chat_id, msg, [[{"text": "🔙 رجوع", "callback_data": "menu_reports"}]])
        return

    if data == "top_customer":
        counts = {}
        for r in filtered:
            counts[r[2]] = counts.get(r[2], 0) + 1
        if not counts:
            send(chat_id, "مفيش مبيعات", [[{"text": "🔙 رجوع", "callback_data": "menu_reports"}]])
            return
        msg = "أكثر العملاء شراءً:\n\n"
        for c, n in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]:
            msg += f"- {c}: {n} عملية\n"
        send(chat_id, msg, [[{"text": "🔙 رجوع", "callback_data": "menu_reports"}]])
        return

    total = sum(int(r[4]) for r in filtered if r[4].isdigit())
    rev = sum(inventory.get(find_product(inventory, r[3]) or "", {}).get("price", 0) * int(r[4]) for r in filtered if r[4].isdigit())
    send(chat_id,
        f"تقرير {label}:\n\nعدد العمليات: {len(filtered)}\nإجمالي القطع: {total}\nإجمالي المبيعات: {rev:.0f} درهم",
        [[{"text": "🔙 رجوع", "callback_data": "menu_reports"}]])

# ── معالجة الرسائل النصية ─────────────────────────────────
def handle_message(chat_id, text):
    text = text.strip()
    sess = sessions.get(chat_id, {})
    step = sess.get("step", "")

    try:
        inv_ws, sales_ws, exp_ws = get_sheets()
        inventory = get_inventory(inv_ws)
    except Exception as e:
        send(chat_id, f"خطأ: {e}")
        return

    # ── خطوات الفاتورة ────────────────────────────────────
    if step == "invoice_customer":
        sessions[chat_id] = {"step": "invoice_product", "customer": text, "items": []}
        keyboard = products_keyboard(inventory, "invoice_add")
        send(chat_id, f"اختار المنتج للعميل {text}:", keyboard)
        return

    if step == "invoice_qty":
        try:
            qty = int(text)
            product = sess["current_product"]
            if inventory.get(product, {}).get("qty", 0) < qty:
                send(chat_id, f"المخزون مش كافي! متبقي {inventory[product]['qty']} بس")
                return
            sess["items"].append({"product": product, "qty": qty})
            sess["step"] = "invoice_product"
            keyboard = [
                [{"text": "✅ إنهاء الفاتورة", "callback_data": "invoice_done"}],
                [{"text": "➕ إضافة منتج آخر", "callback_data": "invoice_add_more"}]
            ]
            items_text = "\n".join([f"- {i['product']} × {i['qty']}" for i in sess["items"]])
            send(chat_id, f"المنتجات حتى الآن:\n{items_text}", keyboard)
        except:
            send(chat_id, "اكتب رقم صحيح للكمية")
        return

    # ── خطوات إضافة كمية ─────────────────────────────────
    if step == "add_stock_qty":
        try:
            qty = int(text)
            product = sess["product"]
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    new_qty = inventory[product]["qty"] + qty
                    inv_ws.update_cell(i + 1, 2, new_qty)
                    break
            sessions.pop(chat_id, None)
            send(chat_id, f"تم إضافة {qty} لـ {product}\nالرصيد: {new_qty}")
            main_menu(chat_id)
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    # ── منتج جديد ─────────────────────────────────────────
    if step == "new_product_name":
        sess["product"] = text
        sess["step"] = "new_product_qty"
        send(chat_id, f"كم الكمية الابتدائية لـ {text}؟")
        return

    if step == "new_product_qty":
        try:
            qty = int(text)
            sess["qty"] = qty
            sess["step"] = "new_product_price"
            send(chat_id, f"كم سعر {sess['product']}؟ (اكتب 0 لو مش عارف)")
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    if step == "new_product_price":
        try:
            price = float(text)
            inv_ws.append_row([sess["product"], sess["qty"], price if price else ""])
            sessions.pop(chat_id, None)
            send(chat_id, f"تم إضافة {sess['product']}\nالكمية: {sess['qty']}\nالسعر: {price} درهم")
            main_menu(chat_id)
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    # ── تعديل سعر ─────────────────────────────────────────
    if step == "edit_price_value":
        try:
            price = float(text)
            product = sess["product"]
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    inv_ws.update_cell(i + 1, 3, price)
                    break
            sessions.pop(chat_id, None)
            send(chat_id, f"تم تعديل سعر {product} إلى {price} درهم")
            main_menu(chat_id)
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    # ── تعديل اسم ─────────────────────────────────────────
    if step == "edit_name_value":
        product = sess["product"]
        all_rows = inv_ws.get_all_values()
        for i, row in enumerate(all_rows):
            if row[0].strip() == product:
                inv_ws.update_cell(i + 1, 1, text)
                break
        sessions.pop(chat_id, None)
        send(chat_id, f"تم تغيير الاسم من {product} إلى {text}")
        main_menu(chat_id)
        return

    # ── مصروف ─────────────────────────────────────────────
    if step == "expense_desc":
        sess["desc"] = text
        sess["step"] = "expense_amount"
        send(chat_id, "كم المبلغ؟")
        return

    if step == "expense_amount":
        try:
            amount = float(text)
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            exp_ws.append_row([date_str, sess["desc"], amount])
            sessions.pop(chat_id, None)
            send(chat_id, f"تم تسجيل المصروف\n{sess['desc']}: {amount} درهم")
            main_menu(chat_id)
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    # ── إلغاء بيع ─────────────────────────────────────────
    if step == "cancel_sale_id":
        try:
            sale_id = int(text)
            all_rows = sales_ws.get_all_values()
            for i, row in enumerate(all_rows[1:], start=2):
                if len(row) >= 5 and str(row[0]) == str(sale_id):
                    product = row[3]
                    qty_back = int(row[4])
                    prod = find_product(inventory, product)
                    if prod:
                        all_inv = inv_ws.get_all_values()
                        for j, r in enumerate(all_inv):
                            if r[0].strip() == prod:
                                inv_ws.update_cell(j + 1, 2, inventory[prod]["qty"] + qty_back)
                                break
                    sales_ws.delete_rows(i)
                    sessions.pop(chat_id, None)
                    send(chat_id, f"تم إلغاء عملية {sale_id}\nتم إرجاع {qty_back} قطعة من {product}")
                    main_menu(chat_id)
                    return
            send(chat_id, f"ما لقيتش عملية رقم {sale_id}")
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    # ── مرتجع ─────────────────────────────────────────────
    if step == "return_customer":
        sess["customer"] = text
        sess["step"] = "return_product"
        keyboard = products_keyboard(inventory, "return_prod")
        send(chat_id, "اختار المنتج المرتجع:", keyboard)
        return

    if step == "return_qty":
        try:
            qty = int(text)
            product = sess["product"]
            customer = sess["customer"]
            all_rows = inv_ws.get_all_values()
            for i, row in enumerate(all_rows):
                if row[0].strip() == product:
                    new_qty = inventory[product]["qty"] + qty
                    inv_ws.update_cell(i + 1, 2, new_qty)
                    break
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            sales_ws.append_row(["مرتجع", date_str, customer, product, f"-{qty}"])
            sessions.pop(chat_id, None)
            send(chat_id, f"تم تسجيل المرتجع\nالعميل: {customer}\nالمنتج: {product}\nالكمية: {qty}\nالمخزون الجديد: {new_qty}")
            main_menu(chat_id)
        except:
            send(chat_id, "اكتب رقم صحيح")
        return

    # ── أوامر نصية سريعة ─────────────────────────────────
    if text in ["مساعدة", "help", "الاوامر"]:
        send(chat_id, HELP_TEXT)
        return

    if text in ["القائمة", "قائمة", "menu", "ابدأ", "/start"]:
        sessions.pop(chat_id, None)
        main_menu(chat_id)
        return

    # باقي كم (نصي سريع)
    if re.search(r"باقي كم|كم باقي", text):
        query = re.sub(r"باقي كم|كم باقي|من", "", text).strip()
        if not query:
            msg = "المخزن الحالي:\n\n"
            for name, d in inventory.items():
                msg += f"- {name}: {d['qty']} قطعة\n"
            send(chat_id, msg)
        else:
            product = find_product(inventory, query)
            if product:
                send(chat_id, f"{product}: {inventory[product]['qty']} قطعة")
            else:
                send(chat_id, f"ما لقيتش '{query}'")
        return

    # رسالة غير معروفة
    sessions.pop(chat_id, None)
    main_menu(chat_id)

# ── دليل الأوامر ──────────────────────────────────────────
HELP_TEXT = """دليل الأوامر السريعة:

- القائمة / menu — القائمة الرئيسية
- باقي كم رام 8 — استعلام مخزون
- باقي كم — كل المخزن
- مساعدة — هذا الدليل

كل العمليات التانية عن طريق الأزرار"""

# ── HTTP Server ───────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, *args): pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ── Polling ───────────────────────────────────────────────
def run_bot():
    offset = None
    logger.info("البوت شغال")
    while True:
        try:
            params = {"timeout": 30}
            if offset: params["offset"] = offset
            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=35)
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    cq = update["callback_query"]
                    handle_callback(cq["message"]["chat"]["id"], cq["data"], cq["id"])
                elif "message" in update:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg.get("text", "")
                    if text:
                        handle_message(chat_id, text)
        except Exception as e:
            logger.error(f"خطأ: {e}")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    run_bot()
