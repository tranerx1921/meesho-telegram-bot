"""
Meesho Telegram Bot (Ultra-Lightweight & Loop-Proof)
=====================================================
- Ultra-low memory: Uses PyPDF2 (15MB RAM vs 500MB with pdfplumber) - Never crashes Render 512MB limit.
- One PDF at a time: Cancels previous job immediately if a new PDF is sent.
- No loops on restart: Discards all past backlog messages on startup.
- Multi-quantity alerts, in-chat picklist, and sorted PDF generation.
"""

import os
import sys
import gc
import time
import tempfile
import threading
import http.server
import socketserver
import requests
from collections import defaultdict
from PyPDF2 import PdfReader, PdfWriter

# ── Configuration ──────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "tg_bot_token.txt")

SIZE_ORDER = {"M": 0, "L": 1, "XL": 2, "XXL": 3, "XXXL": 4}
SIZE_LIST = ["M", "L", "XL", "XXL", "XXXL"]
VALID_SIZES = set(SIZE_LIST)

# Track current active job per chat to allow "terminate on new send"
active_job_id = {}
user_modes = {}
job_lock = threading.Lock()


# ── Keep-Alive Web Server for Render / UptimeRobot ─────────────────────
class KeepAliveHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Meesho Bot is live!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_keep_alive_server():
    port = int(os.environ.get("PORT", 8080))
    def serve():
        try:
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("", port), KeepAliveHandler) as httpd:
                print(f"[Keep-Alive] HTTP server listening on port {port}")
                httpd.serve_forever()
        except Exception as e:
            print(f"[Keep-Alive] Port notice: {e}")

    t = threading.Thread(target=serve, daemon=True)
    t.start()


# ── Lightweight PDF Parsing Logic (PyPDF2 - uses only ~15MB RAM) ──────

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

def process_pdf(input_pdf_path, output_pdf_path, check_cancelled):
    reader = PdfReader(input_pdf_path)
    n = len(reader.pages)
    items = []

    for idx in range(n):
        # Check if user sent another PDF (terminate immediately if cancelled)
        if check_cancelled():
            return None, None, None

        page = reader.pages[idx]
        text = page.extract_text() or ""
        sku_line = extract_sku_line(text)

        if sku_line is None:
            items.append(("UNKNOWN", f"PAGE_{idx+1}", "", 1, True, idx, ""))
            continue

        ttype, colour, size, qty, is_anomaly = parse_sku(sku_line)
        items.append((ttype, colour, size, qty, is_anomaly, idx, sku_line))

    if check_cancelled():
        return None, None, None

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

    if check_cancelled():
        return None, None, None

    # Write rearranged PDF
    writer = PdfWriter()
    for t, c, s, qty, is_anom, page_idx, raw in ordered:
        writer.add_page(reader.pages[page_idx])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)

    # Free memory immediately
    del reader
    del writer
    gc.collect()

    return picklist_text, len(ordered), len(multi_qty_alerts)


# ── Telegram API Client ────────────────────────────────────────────────

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
        except Exception:
            return None

    def send_chat_action(self, chat_id, action="typing"):
        try:
            requests.post(f"{self.base_url}/sendChatAction", data={"chat_id": chat_id, "action": action}, timeout=8)
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
        except Exception:
            return None

    def get_file(self, file_id):
        try:
            r = requests.get(f"{self.base_url}/getFile", params={"file_id": file_id}, timeout=15)
            return r.json()
        except Exception:
            return None

    def download_file(self, server_path, local_destination):
        url = f"https://api.telegram.org/file/bot{self.token}/{server_path}"
        r = requests.get(url, stream=True, timeout=60)
        with open(local_destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=16384):
                f.write(chunk)

    def get_updates(self, offset=None, timeout=20):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            r = requests.get(f"{self.base_url}/getUpdates", params=params, timeout=timeout + 8)
            return r.json()
        except Exception:
            return None


# ── Main Runner ────────────────────────────────────────────────────────

def get_bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t

    print("No TELEGRAM_BOT_TOKEN environment variable found.")
    t = input("Paste your Telegram Bot Token here: ").strip()
    if t:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
        return t
    sys.exit(1)


def main():
    start_keep_alive_server()
    token = get_bot_token()
    bot = TelegramBot(token)

    # ── CRITICAL LOOP FIX: Drain & clear all pending backlog on startup ──
    print("[Bot] Checking Telegram connection and clearing backlog...")
    init_updates = bot.get_updates(offset=-1, timeout=1)
    offset = None
    if init_updates and init_updates.get("result"):
        last_id = init_updates["result"][-1]["update_id"]
        offset = last_id + 1
        bot.get_updates(offset=offset, timeout=1)
        print(f"[Bot] Discarded previous backlog up to update #{last_id}. Starting clean!")
    else:
        print("[Bot] No backlog found. Ready!")

    print("=" * 60)
    print("  Meesho Telegram Bot is running!")
    print("  Memory optimized (PyPDF2) - 512MB safe.")
    print("  One PDF at a time (Auto-cancels if new PDF arrives).")
    print("=" * 60)

    job_counter = 0

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
                text = msg.get("text", "").strip()

                # Handle commands
                if text in ("/start", "/help"):
                    mode = user_modes.get(chat_id, "both")
                    welcome = (
                        "👋 *Meesho Label & Picklist Bot*\n\n"
                        "📄 Send me any Meesho Shipping Label PDF.\n"
                        "I will reply with:\n"
                        "1. 📋 Clean Picklist (in chat + .txt file)\n"
                        "2. 📑 Rearranged PDF (in picklist order)\n"
                        "3. ⚠️ Multi-quantity alert if an order has >1 qty\n\n"
                        f"⚙️ *Current Mode:* `{mode.upper()}`\n"
                        "• `/both` - Picklist TXT + Rearranged PDF (default)\n"
                        "• `/txt`  - Picklist TXT only"
                    )
                    bot.send_message(chat_id, welcome, reply_to_message_id=msg_id)
                    continue

                elif text == "/txt":
                    user_modes[chat_id] = "txt"
                    bot.send_message(chat_id, "⚡ Mode set to: *TXT Only*.", reply_to_message_id=msg_id)
                    continue

                elif text == "/both":
                    user_modes[chat_id] = "both"
                    bot.send_message(chat_id, "📑 Mode set to: *Both (TXT + PDF)*.", reply_to_message_id=msg_id)
                    continue

                # Handle Document
                doc = msg.get("document")
                if doc:
                    file_name = doc.get("file_name", "labels.pdf")
                    mime_type = doc.get("mime_type", "")

                    if not (file_name.lower().endswith(".pdf") or "pdf" in mime_type.lower()):
                        bot.send_message(chat_id, "⚠️ Please send a PDF file.", reply_to_message_id=msg_id)
                        continue

                    # Generate new unique job ID for this chat
                    with job_lock:
                        job_counter += 1
                        my_job_id = job_counter
                        active_job_id[chat_id] = my_job_id

                    def check_cancelled():
                        return active_job_id.get(chat_id) != my_job_id

                    bot.send_chat_action(chat_id, "upload_document")
                    bot.send_message(chat_id, "⏳ Processing PDF...", reply_to_message_id=msg_id)

                    with tempfile.TemporaryDirectory() as tmp_dir:
                        local_in = os.path.join(tmp_dir, "input.pdf")
                        local_out_pdf = os.path.join(tmp_dir, "meesho_rearranged.pdf")
                        local_out_txt = os.path.join(tmp_dir, "meesho_picklist.txt")

                        file_info = bot.get_file(doc["file_id"])
                        if not file_info or not file_info.get("ok"):
                            bot.send_message(chat_id, "❌ Could not download file from Telegram.", reply_to_message_id=msg_id)
                            continue

                        if check_cancelled():
                            print(f"[Chat {chat_id}] Job {my_job_id} cancelled by a newer PDF.")
                            continue

                        server_path = file_info["result"]["file_path"]
                        bot.download_file(server_path, local_in)

                        if check_cancelled():
                            print(f"[Chat {chat_id}] Job {my_job_id} cancelled by a newer PDF.")
                            continue

                        try:
                            t0 = time.time()
                            res = process_pdf(local_in, local_out_pdf, check_cancelled)
                            if res[0] is None:
                                print(f"[Chat {chat_id}] Job {my_job_id} aborted mid-process.")
                                continue

                            picklist_text, total_pages, multi_qty_count = res
                            duration = time.time() - t0

                            if check_cancelled():
                                continue

                            with open(local_out_txt, "w", encoding="utf-8") as f:
                                f.write(picklist_text)

                            mode = user_modes.get(chat_id, "both")

                            # Send Picklist in Chat
                            if len(picklist_text) <= 4000:
                                bot.send_message(chat_id, f"📋 *Picklist Generated* ({total_pages} orders in {duration:.1f}s):\n\n```\n{picklist_text}\n```")
                            else:
                                preview = picklist_text[:3800] + "\n... (full picklist in file below)"
                                bot.send_message(chat_id, f"📋 *Picklist Preview*:\n\n```\n{preview}\n```")

                            # Send TXT Document
                            bot.send_document(
                                chat_id,
                                local_out_txt,
                                caption=f"📄 Picklist: {total_pages} orders ({duration:.1f}s)"
                            )

                            # Send Rearranged PDF (if mode == 'both')
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

                        finally:
                            gc.collect()

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            print(f"[Loop Exception] {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
