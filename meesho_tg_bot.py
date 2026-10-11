"""
Meesho Telegram Bot
===================
Run this script on your PC (or free cloud server) to process Meesho PDFs via Telegram.
Send any Meesho label PDF to your bot from mobile or desktop, and it will reply with:
  1. Picklist formatted directly in chat
  2. meesho_picklist.txt
  3. meesho_rearranged.pdf (Packslip pages sorted in picklist order)

Zero extra packages needed - works with Python's requests, pdfplumber, and PyPDF2.
"""

import os
import sys
import time
import tempfile
import threading
import http.server
import socketserver
import requests
from collections import defaultdict
import pdfplumber
from PyPDF2 import PdfReader, PdfWriter

# ── Keep-Alive Web Server (for Render / UptimeRobot / Koyeb) ───────────
class KeepAliveHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Meesho Telegram Bot is live and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy HTTP ping logs in console
        pass

def start_keep_alive_server():
    port = int(os.environ.get("PORT", 8080))
    def serve():
        try:
            # Allow address reuse to prevent 'address already in use' errors
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("", port), KeepAliveHandler) as httpd:
                print(f"[Keep-Alive] HTTP server listening on port {port} (ready for UptimeRobot / Render)")
                httpd.serve_forever()
        except Exception as e:
            print(f"[Keep-Alive Warn] Could not bind port {port}: {e}")

    t = threading.Thread(target=serve, daemon=True)
    t.start()

# ── Configuration ──────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "tg_bot_token.txt")

SIZE_ORDER = {"M": 0, "L": 1, "XL": 2, "XXL": 3, "XXXL": 4}
SIZE_LIST = ["M", "L", "XL", "XXL", "XXXL"]
VALID_SIZES = set(SIZE_LIST)

# Store per-user mode ('both' or 'txt')
user_modes = {}


# ── PDF Parsing Logic ──────────────────────────────────────────────────

def normalize_size(raw):
    return raw.strip().upper()

def normalize_colour(raw):
    return raw.strip().title()

def extract_sku_line(page_text):
    lines = page_text.split("\n")
    for i, line in enumerate(lines):
        if "SKU" in line and "Size" in line and "Qty" in line:
            if i + 1 < len(lines):
                return lines[i + 1].strip()
    return None

def parse_sku(sku_line):
    parts = sku_line.split()
    if len(parts) < 4:
        return (sku_line, "", "", 1, True)

    size_idx = None
    for idx in range(len(parts) - 3, 0, -1):
        if parts[idx].upper() in VALID_SIZES:
            size_idx = idx
            break

    if size_idx is None or size_idx < 2:
        if size_idx == 1:
            return (sku_line, "", "", 1, True)
        return (sku_line, "", "", 1, True)

    pre_size = parts[:size_idx]
    size = normalize_size(parts[size_idx])
    ttype = pre_size[0].upper()
    colour_parts = pre_size[1:]

    if not colour_parts:
        return (sku_line, "", "", 1, True)

    colour = normalize_colour(" ".join(colour_parts))

    if not ttype.isalpha():
        return (sku_line, "", "", 1, True)

    qty = 1
    if size_idx + 1 < len(parts) and parts[size_idx + 1].isdigit():
        qty = int(parts[size_idx + 1])

    return (ttype, colour, size, qty, False)

def process_pdf(input_pdf_path, output_pdf_path):
    items = []
    plumber = pdfplumber.open(input_pdf_path)
    reader = PdfReader(input_pdf_path)
    n = len(plumber.pages)

    for idx in range(n):
        text = plumber.pages[idx].extract_text() or ""
        sku_line = extract_sku_line(text)
        if sku_line is None:
            items.append(("UNKNOWN", f"PAGE_{idx+1}", "", 1, True, idx, ""))
            continue

        ttype, colour, size, qty, is_anomaly = parse_sku(sku_line)
        items.append((ttype, colour, size, qty, is_anomaly, idx, sku_line))

    plumber.close()

    known = [it for it in items if not it[4]]
    anomalies = [it for it in items if it[4]]

    def sort_key(item):
        t, c, s, qty, *_ = item
        return (t.lower(), SIZE_ORDER.get(s, 99), c.lower())

    known.sort(key=sort_key)
    anomalies.sort(key=lambda x: x[6].lower() if x[6] else x[1].lower())

    picklist_data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for t, c, s, qty, *_ in known:
        picklist_data[t][s][c].append(qty)

    sorted_types = sorted(picklist_data.keys(), key=str.lower)
    lines = []

    for ttype in sorted_types:
        sizes = picklist_data[ttype]
        type_total = sum(sum(qtys) for s_dict in sizes.values() for qtys in s_dict.values())
        type_labels = sum(len(qtys) for s_dict in sizes.values() for qtys in s_dict.values())

        label_str = f" ({type_labels} labels)" if type_total != type_labels else ""
        lines.append(f"[ {ttype} ] - {type_total} pcs{label_str}")
        lines.append("")

        for size in SIZE_LIST:
            colours_dict = sizes.get(size, {})
            if not colours_dict:
                continue

            lines.append(f"   {size}")

            sorted_colours = sorted(colours_dict.items(), key=lambda x: x[0].lower())
            for colour, qtys in sorted_colours:
                count = sum(qtys)
                multi_qtys = [q for q in qtys if q > 1]
                alert_text = ""
                if multi_qtys:
                    multi_counts = defaultdict(int)
                    for q in multi_qtys:
                        multi_counts[q] += 1
                    details = ", ".join(
                        f"{cnt} label with qty {q}" if cnt == 1 else f"{cnt} labels with qty {q}"
                        for q, cnt in sorted(multi_counts.items())
                    )
                    alert_text = f"  (ALERT: {details})"
                lines.append(f"      {colour:<18}{count}{alert_text}")

        lines.append("")
        lines.append("- - - - - - - - - - - - - - -")
        lines.append("")

    total_known_pcs = sum(sum(qtys) for t_dict in picklist_data.values() for s_dict in t_dict.values() for qtys in s_dict.values())
    total_known_labels = len(known)

    if total_known_pcs != total_known_labels:
        lines.append(f"Total : {total_known_labels} labels ({total_known_pcs} pcs)")
    else:
        lines.append(f"Total : {total_known_labels}")

    if anomalies:
        lines.append(f"Anomalies : {len(anomalies)}")
        for a in anomalies:
            desc = a[6] if a[6] else a[1]
            lines.append(f"   {desc}")

    total_all_labels = len(items)
    total_all_pcs = total_known_pcs + sum(it[3] for it in anomalies)
    if total_all_pcs != total_all_labels:
        lines.append(f"Grand Total : {total_all_labels} labels ({total_all_pcs} pcs)")
    else:
        lines.append(f"Grand Total : {total_all_labels}")

    ordered = known + anomalies
    multi_qty_alerts = []
    for new_page_idx, it in enumerate(ordered):
        t, c, s, qty, is_anom, orig_idx, raw = it
        if qty > 1:
            multi_qty_alerts.append(f"   Page {new_page_idx + 1}: {t} {c} {s} -> Qty {qty}")

    if multi_qty_alerts:
        lines.append("")
        lines.append("! ! ! MULTI-QTY ALERTS ! ! !")
        lines.extend(multi_qty_alerts)

    picklist_text = "\n".join(lines)

    # Generate rearranged PDF
    writer = PdfWriter()
    for t, c, s, qty, is_anom, page_idx, raw in ordered:
        writer.add_page(reader.pages[page_idx])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)

    return picklist_text, len(ordered), len(multi_qty_alerts)


# ── Telegram API Helpers ───────────────────────────────────────────────

class TelegramBot:
    def __init__(self, token):
        self.token = token.strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, chat_id, text, reply_to_message_id=None):
        url = f"{self.base_url}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        try:
            r = requests.post(url, data=data, timeout=15)
            return r.json()
        except Exception as e:
            print(f"[Err] send_message: {e}")
            return None

    def send_chat_action(self, chat_id, action="typing"):
        url = f"{self.base_url}/sendChatAction"
        try:
            requests.post(url, data={"chat_id": chat_id, "action": action}, timeout=10)
        except Exception:
            pass

    def send_document(self, chat_id, file_path, caption="", reply_to_message_id=None):
        url = f"{self.base_url}/sendDocument"
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id

        try:
            filename = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                files = {"document": (filename, f)}
                r = requests.post(url, data=data, files=files, timeout=60)
            return r.json()
        except Exception as e:
            print(f"[Err] send_document: {e}")
            return None

    def get_file(self, file_id):
        url = f"{self.base_url}/getFile"
        r = requests.get(url, params={"file_id": file_id}, timeout=15)
        return r.json()

    def download_file(self, file_path_on_server, local_destination):
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path_on_server}"
        r = requests.get(url, stream=True, timeout=60)
        with open(local_destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    def get_updates(self, offset=None, timeout=25):
        url = f"{self.base_url}/getUpdates"
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(url, params=params, timeout=timeout + 10)
            return r.json()
        except Exception as e:
            print(f"[Warn] get_updates timeout/error: {e}")
            return None


# ── Main Bot Runner ────────────────────────────────────────────────────

def get_bot_token():
    # 1. Environment variable
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()

    # 2. Local token file
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t

    # 3. Prompt user
    print("=" * 60)
    print("  MEESHO TELEGRAM BOT SETUP")
    print("=" * 60)
    print("To use this bot, you need a free Bot Token from Telegram.")
    print("1. Open Telegram and search for @BotFather")
    print("2. Send /newbot and choose a name and username")
    print("3. Copy the HTTP API token given by BotFather\n")
    t = input("Paste your Telegram Bot Token here: ").strip()

    if t:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
        print(f"Token saved to {TOKEN_FILE}!")
        return t
    else:
        print("No token provided. Exiting.")
        sys.exit(1)


def main():
    # Start background web server for Render / Koyeb / UptimeRobot
    start_keep_alive_server()

    token = get_bot_token()
    bot = TelegramBot(token)

    # Test token
    test = bot.get_updates(timeout=1)
    if test is None or not test.get("ok"):
        print("[Error] Invalid bot token or cannot connect to Telegram API.")
        print(f"Details: {test}")
        if os.path.exists(TOKEN_FILE):
            print(f"Tip: Delete {TOKEN_FILE} to re-enter your token.")
        input("Press Enter to exit...")
        return

    print("=" * 60)
    print("  Meesho Telegram Bot is running!")
    print("  Send any Meesho label PDF to your bot on Telegram.")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    offset = None

    while True:
        try:
            updates = bot.get_updates(offset=offset)
            if not updates or not updates.get("ok"):
                time.sleep(2)
                continue

            for item in updates.get("result", []):
                offset = item["update_id"] + 1

                msg = item.get("message")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                msg_id = msg["message_id"]

                # ── Handle Commands ──
                text = msg.get("text", "").strip()
                if text == "/start" or text == "/help":
                    mode = user_modes.get(chat_id, "both")
                    welcome = (
                        "👋 *Welcome to Meesho Label & Picklist Bot!*\n\n"
                        "📄 *How to use:*\n"
                        "Simply send me any Meesho Shipping Label PDF.\n\n"
                        "I will instantly send back:\n"
                        "1. 📋 Clean Picklist (text in chat + .txt file)\n"
                        "2. 📑 Rearranged PDF (ordered by type, size & colour)\n"
                        "3. ⚠️ Multi-quantity alert if an order has >1 qty\n\n"
                        f"⚙️ *Current Mode:* `{mode.upper()}`\n"
                        "• `/both` - Receive Picklist TXT + Rearranged PDF (default)\n"
                        "• `/txt`  - Receive Picklist TXT only (fastest)"
                    )
                    bot.send_message(chat_id, welcome, reply_to_message_id=msg_id)
                    continue

                elif text == "/txt":
                    user_modes[chat_id] = "txt"
                    bot.send_message(chat_id, "⚡ Mode set to: *TXT Only*. PDF reordering will be skipped.", reply_to_message_id=msg_id)
                    continue

                elif text == "/both":
                    user_modes[chat_id] = "both"
                    bot.send_message(chat_id, "📑 Mode set to: *Both (TXT + Rearranged PDF)*.", reply_to_message_id=msg_id)
                    continue

                # ── Handle PDF Document ──
                doc = msg.get("document")
                if doc:
                    file_name = doc.get("file_name", "labels.pdf")
                    mime_type = doc.get("mime_type", "")

                    if not (file_name.lower().endswith(".pdf") or "pdf" in mime_type.lower()):
                        bot.send_message(chat_id, "⚠️ Please send a PDF file.", reply_to_message_id=msg_id)
                        continue

                    mode = user_modes.get(chat_id, "both")
                    bot.send_chat_action(chat_id, "upload_document")
                    status_msg = bot.send_message(chat_id, "⏳ Downloading & processing PDF...", reply_to_message_id=msg_id)

                    # Temporary files
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        local_in = os.path.join(tmp_dir, "input.pdf")
                        local_out_pdf = os.path.join(tmp_dir, "meesho_rearranged.pdf")
                        local_out_txt = os.path.join(tmp_dir, "meesho_picklist.txt")

                        # 1. Download file
                        file_info = bot.get_file(doc["file_id"])
                        if not file_info.get("ok"):
                            bot.send_message(chat_id, "❌ Failed to download file from Telegram.", reply_to_message_id=msg_id)
                            continue

                        server_path = file_info["result"]["file_path"]
                        bot.download_file(server_path, local_in)

                        # 2. Process PDF
                        try:
                            t0 = time.time()
                            picklist_text, total_pages, multi_qty_count = process_pdf(local_in, local_out_pdf)
                            duration = time.time() - t0

                            with open(local_out_txt, "w", encoding="utf-8") as f:
                                f.write(picklist_text)

                            # 3. Send Picklist as Chat Message (formatted)
                            # Telegram message limit is 4096 chars
                            if len(picklist_text) <= 4000:
                                bot.send_message(chat_id, f"📋 *Picklist Generated* ({total_pages} orders in {duration:.1f}s):\n\n```\n{picklist_text}\n```")
                            else:
                                preview = picklist_text[:3800] + "\n... (truncated, see full txt file below)"
                                bot.send_message(chat_id, f"📋 *Picklist Preview*:\n\n```\n{preview}\n```")

                            # 4. Send picklist.txt document
                            bot.send_document(
                                chat_id,
                                local_out_txt,
                                caption=f"📄 Picklist: {total_pages} orders ({duration:.1f}s)"
                            )

                            # 5. Send rearranged PDF (if mode == 'both')
                            if mode == "both":
                                alert_note = f" | ⚠️ {multi_qty_count} multi-qty alert(s)" if multi_qty_count > 0 else ""
                                bot.send_document(
                                    chat_id,
                                    local_out_pdf,
                                    caption=f"📑 Rearranged PDF: {total_pages} pages sorted{alert_note}"
                                )

                        except Exception as e:
                            import traceback
                            traceback.print_exc()
                            bot.send_message(chat_id, f"❌ Error processing PDF: {e}", reply_to_message_id=msg_id)

                elif text:
                    bot.send_message(
                        chat_id,
                        "💡 Send me a Meesho PDF document to get the picklist and sorted PDF!\nType /help for options.",
                        reply_to_message_id=msg_id
                    )

        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            break
        except Exception as e:
            print(f"[Loop Exception] {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
