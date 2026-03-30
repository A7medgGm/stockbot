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

# ── الإعدادات ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID  = os.environ.get("SHEET_ID")
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── الاتصال بـ Google Sheets ────────────────────────────────
def get_sheets():
    try:
        creds_json = os.environ.get("GOOGLE_CREDS_JSON")
        creds_dict = json.loads(creds_json, strict=False)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        return spreadsheet.worksheet("Inventory"), spreadsheet.worksheet("Sales"), spreadsheet.worksheet("Expenses")
    except Exception as e:
        logger.error(f"❌ Sheets Error: {e}")
        return None, None, None

def send_message(chat_id, text):
    requests.post(f"{API_URL}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

# ── المعالجة الاحترافية ──────────────────────────────────────
def handle_message(chat_id, text):
    text = text.strip()
    inv_ws, sales_ws, exp_ws = get_sheets()
    if not inv_ws: return

    # 1. البيع (يدعم السعر الخاص)
    m_sale = re.search(r"(.+?)\s+(شال|باع|اخد|طلب)\s+(\d+)\s+(.+?)(?:\s+(\d+))?$", text)
    if m_sale:
        customer, qty, p_query, custom_p = m_sale.group(1), int(m_sale.group(3)), m_sale.group(4).strip(), m_sale.group(5)
        data = inv_ws.get_all_values()
        for i, row in enumerate(data[1:], 2):
            if p_query.lower() in row[0].lower():
                curr_qty = int(row[1])
                sell_p = float(custom_p) if custom_p else (float(row[2]) if row[2] else 0)
                cost_p = float(row[3]) if row[3] else 0
                profit = (sell_p - cost_p) * qty
                inv_ws.update_cell(i, 2, curr_qty - qty)
                sales_ws.append_row([datetime.now().strftime("%Y-%m-%d"), customer, row[0], qty, sell_p * qty, profit])
                send_message(chat_id, f"💰 *تم البيع*\n📦 {row[0]}\n💵 الإجمالي: {sell_p * qty}\n✨ الربح: {profit}\n📉 المتبقي: {curr_qty - qty}")
                return

    # 2. المرتجع (يرجع البضاعة ويخصم الأرباح)
    m_ret = re.search(r"مرتجع\s+(\d+)\s+(.+?)\s+(.+)", text)
    if m_ret:
        qty_ret, p_query, customer = int(m_ret.group(1)), m_ret.group(2).strip(), m_ret.group(3).strip()
        data = inv_ws.get_all_values()
        for i, row in enumerate(data[1:], 2):
            if p_query.lower() in row[0].lower():
                # إرجاع للمخزن
                inv_ws.update_cell(i, 2, int(row[1]) + qty_ret)
                # تسجيل في المبيعات بقيمة سالبة لخصمها من التقرير
                sell_p = float(row[2]) if row[2] else 0
                cost_p = float(row[3]) if row[3] else 0
                loss_to_deduct = (sell_p - cost_p) * qty_ret
                sales_ws.append_row([datetime.now().strftime("%Y-%m-%d"), f"مرتجع: {customer}", row[0], -qty_ret, -(sell_p * qty_ret), -loss_to_deduct])
                send_message(chat_id, f"↩️ *تم تسجيل مرتجع*\n📦 {row[0]}\n📥 دخل المخزن: {qty_ret}\n💸 خصم مبيعات: {sell_p * qty_ret}")
                return

    # 3. تعديل السعر الرسمي
    m_mod = re.search(r"تعديل\s+(.+?)\s+(\d+)(?:\s+(\d+))?$", text)
    if m_mod:
        p_query, s_p, c_p = m_mod.group(1).strip(), m_mod.group(2), m_mod.group(3)
        data = inv_ws.get_all_values()
        for i, row in enumerate(data[1:], 2):
            if p_query.lower() in row[0].lower():
                inv_ws.update_cell(i, 3, s_p)
                if c_p: inv_ws.update_cell(i, 4, c_p)
                send_message(chat_id, f"✅ تم تعديل أسعار *{row[0]}*")
                return

    # 4. إضافة كمية
    m_add = re.search(r"اضافة\s+(\d+)\s+(.+)$", text)
    if m_add:
        qty, p_query = int(m_add.group(1)), m_add.group(2).strip()
        data = inv_ws.get_all_values()
        for i, row in enumerate(data[1:], 2):
            if p_query.lower() in row[0].lower():
                inv_ws.update_cell(i, 2, int(row[1]) + qty)
                send_message(chat_id, f"✅ زاد المخزن {qty} لـ {row[0]}")
                return

    # 5. التقارير والجرد والمصاريف
    if text == "تقرير":
        today = datetime.now().strftime("%Y-%m-%d")
        s_data, e_data = sales_ws.get_all_values(), exp_ws.get_all_values()
        t_s = sum(float(r[4]) for r in s_data[1:] if r[0] == today)
        t_p = sum(float(r[5]) for r in s_data[1:] if r[0] == today)
        t_e = sum(float(r[2]) for r in e_data[1:] if r[0] == today)
        send_message(chat_id, f"📊 *تقرير اليوم*\n💰 مبيعات: {t_s}\n💸 مصاريف: {t_e}\n📈 صافي ربح: {t_p - t_e}")
    
    if "باقي كم" in text:
        data = inv_ws.get_all_values()
        msg = "📦 *المخزن:*\n"
        for r in data[1:]: msg += f"- {r[0]}: `{r[1]}` (سعر: {r[2]})\n"
        send_message(chat_id, msg)

# ── نظام التشغيل ──────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Live")
def run_bot():
    offset = None
    while True:
        try:
            resp = requests.get(f"{API_URL}/getUpdates", params={"timeout": 10, "offset": offset}, timeout=15)
            if resp.status_code == 200:
                for up in resp.json().get("result", []):
                    offset = up["update_id"] + 1
                    msg = up.get("message", {})
                    if msg.get("text"): handle_message(msg["chat"]["id"], msg["text"])
        except: time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), HealthHandler).serve_forever(), daemon=True).start()
    run_bot()
