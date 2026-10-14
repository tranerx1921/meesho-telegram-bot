"""
Meesho Telegram Bot (Ultra-Fast pypdfium2, Zero-Loop, Picklist Adjustment)
============================================================================
- Features:
  1. Sends Meesho PDF -> Generates Picklist + Rearranged PDF in seconds.
  2. /adjust Command:
     - Copy the picklist, change the numbers for stock you have at home, send it to the bot.
     - Bot calculates deducted pieces and MOVES THEIR LABELS TO THE VERY END (TAIL) OF THE PDF!
     - Sends updated factory picklist + adjusted PDF so you can grab the tail labels easily.
  3. Ultra-Low RAM: Google's C++ PDFium engine (pypdfium2, ~2MB RAM).
  4. Mathematical Zero-Loop Guarantee with immediate Telegram confirmation.
  5. Built-in Keep-Alive server for UptimeRobot (24/7 on Render).
"""

import os
import sys
import gc
import re
import json
import time
import shutil
import tempfile
import threading
import http.server
import socketserver
import requests
from collections import defaultdict
import pypdfium2

# ── Configuration ──────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "tg_bot_token.txt")

SIZE_ORDER = {"M": 0, "L": 1, "XL": 2, "XXL": 3, "XXXL": 4}
SIZE_LIST = ["M", "L", "XL", "XXL", "XXXL"]
VALID_SIZES = set(SIZE_LIST)

processed_update_ids = set()
active_job_id = {}
user_modes = {}
user_last_batch = {}      # chat_id -> {"pdf_path", "items", "known", "anomalies"}
user_waiting_adjust = set() # chat_ids waiting to send edited picklist
job_counter = 0
processing_lock = threading.Lock()


# ── Keep-Alive Web Server (for UptimeRobot / Render Health Check) ───────

class KeepAliveHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Meesho Telegram Bot is awake and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_keep_alive_server(port):
    def serve():
        socketserver.TCPServer.allow_reuse_address = True
        try:
            with socketserver.TCPServer(("", port), KeepAliveHandler) as httpd:
                print(f"[Keep-Alive] HTTP server listening on port {port} (UptimeRobot ready)")
                httpd.serve_forever()
        except Exception as e:
            print(f"[Keep-Alive] Port notice: {e}")

    t = threading.Thread(target=serve, daemon=True)
    t.start()


# ── Fast & Ultra-Low RAM PDF Engine (pypdfium2: ~2MB RAM) ─────────────

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

def parse_edited_picklist(text):
    """
    Parses edited picklist text from user.
    100% space-proof: handles any amount of spaces or hyphens between colour and quantity.
    """
    current_type = None
    current_size = None
    target_counts = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("-") or line.startswith("="):
            continue

        # Skip summary, alert, and total lines
        lower_line = line.lower()
        if any(lower_line.startswith(prefix) for prefix in (
            "total", "grand total", "factory total", "anomalies", "at-home", "🏠", "🏭", "(pages", "!", "*"
        )):
            continue

        # Header like [ P ] or [P] or [ TSHIRT ]
        m_type = re.match(r"^\[\s*([A-Za-z0-9_-]+)\s*\]", line)
        if m_type:
            current_type = m_type.group(1).upper()
            current_size = None
            continue

        # Size like M, L, XL, XXL, XXXL
        parts = line.split()
        if len(parts) == 1 and parts[0].upper() in VALID_SIZES:
            current_size = parts[0].upper()
            continue

        # Inline format: e.g. 'P XL Maroon - 2' or 'P XL Maroon 2'
        m_inline = re.match(r"^([A-Za-z0-9_-]+)\s+(M|L|XL|XXL|XXXL|2XL|3XL)\s+([A-Za-z\s]+?)[\s\-:]+(\d+)\s*$", line, re.I)
        if m_inline:
            t = m_inline.group(1).upper()
            s = m_inline.group(2).upper()
            c = re.sub(r"\s+", " ", m_inline.group(3)).strip().lower()
            q = int(m_inline.group(4))
            target_counts[(t, s, c)] = q
            continue

        # Standard block format: e.g. 'Maroon - 2' or 'Maroon-2' or 'Maroon 2'
        clean = re.sub(r"\(ALERT:.*?\)", "", line, flags=re.I).strip()
        m_item = re.match(r"^([A-Za-z\s]+?)[\s\-:]+(\d+)\s*$", clean)

        if m_item and current_type and current_size:
            c = re.sub(r"\s+", " ", m_item.group(1)).strip().lower()
            q = int(m_item.group(2))
            target_counts[(current_type, current_size, c)] = q

    return target_counts

def build_picklist_text(items, anomalies=None, tail_items=None):
    """Generates clean formatted picklist text with compact 'Colour - Count' spacing."""
    picklist_data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for t, c, s, qty, *_ in items:
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
                lines.append(f"      {colour} - {count}{alert_text}")

        lines.append("")
        lines.append("- - - - - - - - - - - - - - -")
        lines.append("")

    total_pcs = sum(sum(qtys) for t_dict in picklist_data.values() for s_dict in t_dict.values() for qtys in s_dict.values())
    total_labels = len(items)

    if total_pcs != total_labels:
        lines.append(f"Total : {total_labels} labels ({total_pcs} pcs)")
    else:
        lines.append(f"Total : {total_labels}")

    if anomalies:
        lines.append(f"Anomalies : {len(anomalies)}")
        for a in anomalies:
            desc = a[6] if a[6] else a[1]
            lines.append(f"   {desc}")

    # Add At-Home Stock info at the bottom if tail items exist
    if tail_items:
        start_tail = len(items) + (len(anomalies) if anomalies else 0) + 1
        end_tail = len(items) + (len(anomalies) if anomalies else 0) + len(tail_items)
        lines.append("")
        lines.append("=" * 40)
        lines.append(f"🏠 AT-HOME / TAIL LABELS: {len(tail_items)} pcs")
        lines.append(f"(Pages {start_tail} to {end_tail} at the VERY END of PDF)")
        lines.append("=" * 40)

        tail_summary = defaultdict(int)
        for t, c, s, q, *_ in tail_items:
            tail_summary[f"{t} {c} {s}"] += q
        for desc, cnt in sorted(tail_summary.items()):
            lines.append(f"   • {desc:<20} {cnt} pcs")

    return "\n".join(lines)


def process_initial_pdf(input_pdf_path, output_pdf_path, check_cancelled):
    pdf = pypdfium2.PdfDocument(input_pdf_path)
    n = len(pdf)
    items = []

    for idx in range(n):
        if check_cancelled():
            pdf.close()
            return None, None, None, None, None

        page = pdf[idx]
        tp = page.get_textpage()
        text = tp.get_text_range()
        tp.close()
        page.close()

        sku_line = extract_sku_line(text)
        if sku_line is None:
            items.append(("UNKNOWN", f"PAGE_{idx+1}", "", 1, True, idx, ""))
            continue

        ttype, colour, size, qty, is_anomaly = parse_sku(sku_line)
        items.append((ttype, colour, size, qty, is_anomaly, idx, sku_line))

    if check_cancelled():
        pdf.close()
        return None, None, None, None, None

    known = [it for it in items if not it[4]]
    anomalies = [it for it in items if it[4]]

    def sort_key(item):
        t, c, s, qty, *_ = item
        return (t.lower(), SIZE_ORDER.get(s, 99), c.lower())

    known.sort(key=sort_key)
    anomalies.sort(key=lambda x: x[6].lower() if x[6] else x[1].lower())

    ordered = known + anomalies
    picklist_text = build_picklist_text(known, anomalies)

    multi_qty_alerts = []
    for new_page_idx, it in enumerate(ordered):
        t, c, s, qty, is_anom, orig_idx, raw = it
        if qty > 1:
            multi_qty_alerts.append(f"   Page {new_page_idx + 1}: {t} {c} {s} -> Qty {qty}")

    if multi_qty_alerts:
        picklist_text += "\n\n! ! ! MULTI-QTY ALERTS ! ! !\n" + "\n".join(multi_qty_alerts)

    if check_cancelled():
        pdf.close()
        return None, None, None, None, None

    ordered_indices = [it[5] for it in ordered]
    new_pdf = pypdfium2.PdfDocument.new()
    new_pdf.import_pages(pdf, ordered_indices)
    new_pdf.save(output_pdf_path)
    new_pdf.close()
    pdf.close()

    del pdf
    del new_pdf
    gc.collect()

    return picklist_text, len(ordered), len(multi_qty_alerts), known, anomalies


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
            return requests.post(url, data=data, timeout=15).json()
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
                return requests.post(url, data=data, files=files, timeout=90).json()
        except Exception:
            return None

    def get_file(self, file_id):
        try:
            return requests.get(f"{self.base_url}/getFile", params={"file_id": file_id}, timeout=15).json()
        except Exception:
            return None

    def download_file(self, server_path, local_destination):
        url = f"https://api.telegram.org/file/bot{self.token}/{server_path}"
        r = requests.get(url, stream=True, timeout=90)
        with open(local_destination, "wb") as f:
            for chunk in r.iter_content(chunk_size=16384):
                f.write(chunk)

    def delete_webhook(self, drop_pending=True):
        url = f"{self.base_url}/deleteWebhook"
        try:
            r = requests.post(url, data={"drop_pending_updates": drop_pending}, timeout=15)
            print(f"[Telegram] Cleared webhook & purged backlog -> {r.text}")
            return r.json()
        except Exception as e:
            print(f"[Telegram] deleteWebhook notice: {e}")
            return None

    def get_updates(self, offset=None, timeout=20):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            return requests.get(f"{self.base_url}/getUpdates", params=params, timeout=timeout + 8).json()
        except Exception:
            return None


# ── Picklist Adjustment Handler (At-Home Stock to Tail) ────────────────

def handle_adjustment(bot, chat_id, msg_id, edited_text):
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous PDF found in memory!\nPlease send your Meesho PDF first, then you can adjust the picklist.",
            reply_to_message_id=msg_id
        )
        return

    target_counts = parse_edited_picklist(edited_text)
    if not target_counts:
        bot.send_message(
            chat_id,
            "⚠️ Could not parse any quantities from your text.\n"
            "Please copy the previous picklist message, edit the numbers (e.g. change 4 to 2), and send it.",
            reply_to_message_id=msg_id
        )
        return

    known = batch["known"]
    anomalies = batch["anomalies"]
    pdf_source = batch["pdf_path"]

    # Partition into Factory Labels vs Deducted / At-Home Labels
    groups = defaultdict(list)
    for it in known:
        t, c, s, q, is_anom, orig_idx, raw = it
        groups[(t, s, c.lower())].append(it)

    factory_items = []
    deducted_items = []

    for key, label_list in groups.items():
        t, s, c_lower = key
        original_qty = sum(it[3] for it in label_list)
        # Default to original_qty if not mentioned in the user's edit
        desired_qty = target_counts.get(key, original_qty)

        kept_qty = 0
        for it in label_list:
            lbl_qty = it[3]
            if kept_qty + lbl_qty <= desired_qty:
                factory_items.append(it)
                kept_qty += lbl_qty
            else:
                deducted_items.append(it)

    if not deducted_items:
        bot.send_message(
            chat_id,
            "ℹ️ No items were deducted! The quantities you sent match or exceed the original batch.\n"
            "To cut off at-home stock, lower the quantity of the item (e.g. from 4 to 2).",
            reply_to_message_id=msg_id
        )
        return

    bot.send_chat_action(chat_id, "upload_document")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_pdf_path = os.path.join(tmp_dir, "meesho_adjusted.pdf")
        out_txt_path = os.path.join(tmp_dir, "meesho_factory_picklist.txt")

        # 1. Build New Factory Picklist Text
        new_picklist_text = build_picklist_text(factory_items, anomalies, tail_items=deducted_items)
        with open(out_txt_path, "w", encoding="utf-8") as f:
            f.write(new_picklist_text)

        # 2. Reorder PDF: Factory Labels -> Anomalies -> Deducted / At-Home Labels at the tail!
        all_reordered = factory_items + anomalies + deducted_items
        reordered_indices = [it[5] for it in all_reordered]

        pdf = pypdfium2.PdfDocument(pdf_source)
        new_pdf = pypdfium2.PdfDocument.new()
        new_pdf.import_pages(pdf, reordered_indices)
        new_pdf.save(out_pdf_path)
        new_pdf.close()
        pdf.close()

        # Summarize deducted pieces
        start_tail = len(factory_items) + len(anomalies) + 1
        end_tail = len(all_reordered)

        deducted_summary = defaultdict(int)
        for t, c, s, q, *_ in deducted_items:
            deducted_summary[f"{t} {c} {s}"] += q

        summary_lines = [
            "✂️ *Picklist & PDF Adjusted Successfully!*\n",
            f"🏭 *Factory Picklist:* {len(factory_items)} pcs (Pages 1 to {start_tail-1})",
            f"🏠 *At-Home Stock (Tail):* {len(deducted_items)} pcs (Pages {start_tail} to {end_tail})",
        ]
        for desc, cnt in sorted(deducted_summary.items()):
            summary_lines.append(f"   • `{desc}`: *{cnt} pcs*")

        summary_lines.append(
            f"\n💡 *How to pack:* The printer outputs all labels together. "
            f"Simply grab the *LAST {len(deducted_items)} label(s)* from the bottom/tail of the printed stack for your at-home stock!"
        )

        bot.send_message(chat_id, "\n".join(summary_lines), reply_to_message_id=msg_id)

        # Send New Factory Picklist preview in chat
        if len(new_picklist_text) <= 3800:
            bot.send_message(chat_id, f"📋 *New Factory Picklist:*\n\n```\n{new_picklist_text}\n```")

        # Send TXT Document
        bot.send_document(
            chat_id,
            out_txt_path,
            caption=f"📄 Factory Picklist: {len(factory_items)} pcs to pick"
        )

        # Send Adjusted PDF
        bot.send_document(
            chat_id,
            out_pdf_path,
            caption=f"📑 Adjusted PDF: {len(all_reordered)} pages (At-Home stock at Pages {start_tail}-{end_tail})"
        )


# ── Update Processing Worker ───────────────────────────────────────────

def process_single_message(bot, item):
    global job_counter
    msg = item.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    text = msg.get("text", "").strip()

    # 1. Commands
    if text in ("/start", "/help"):
        mode = user_modes.get(chat_id, "both")
        welcome = (
            "👋 *Meesho Label & Picklist Bot*\n\n"
            "📄 *Send a PDF:* Generates clean Picklist + Rearranged PDF.\n\n"
            "✂️ *Adjust for At-Home Stock:*\n"
            "Already have some pieces in stock? Use `/adjust`!\n"
            "1. Copy the picklist text.\n"
            "2. Change the numbers of the items you have at home.\n"
            "3. Send `/adjust` with the text (or just send the text).\n"
            "👉 *The bot will move your at-home pieces to the VERY END (TAIL) of the PDF*, "
            "so you can easily grab the tail of the printed stack!\n\n"
            f"⚙️ *Current Mode:* `{mode.upper()}`\n"
            "• `/both` - Picklist TXT + Rearranged PDF (default)\n"
            "• `/txt`  - Picklist TXT only"
        )
        bot.send_message(chat_id, welcome, reply_to_message_id=msg_id)
        return

    elif text == "/txt":
        user_modes[chat_id] = "txt"
        bot.send_message(chat_id, "⚡ Mode set to: *TXT Only*.", reply_to_message_id=msg_id)
        return

    elif text == "/both":
        user_modes[chat_id] = "both"
        bot.send_message(chat_id, "📑 Mode set to: *Both (TXT + PDF)*.", reply_to_message_id=msg_id)
        return

    elif text.startswith("/adjust") or text.startswith("/edit"):
        content = re.sub(r"^/(adjust|edit)\b", "", text, flags=re.I).strip()
        if content:
            handle_adjustment(bot, chat_id, msg_id, content)
        else:
            user_waiting_adjust.add(chat_id)
            bot.send_message(
                chat_id,
                "✏️ *Ready to Adjust Picklist!*\n\n"
                "Please copy the previous picklist message, edit the numbers of the stock you have at home, "
                "and paste it here.\n\n"
                "*(Tip: You can paste the whole picklist or just the lines you changed, e.g. `P XL Maroon 2`)*",
                reply_to_message_id=msg_id
            )
        return

    # Check if user was waiting to send adjustment text OR if message looks like a picklist
    if chat_id in user_waiting_adjust or ("[" in text and any(s in text.upper() for s in ("M", "L", "XL", "XXL", "XXXL"))):
        user_waiting_adjust.discard(chat_id)
        handle_adjustment(bot, chat_id, msg_id, text)
        return

    # 2. PDF Document Handling
    doc = msg.get("document")
    if doc:
        file_name = doc.get("file_name", "labels.pdf")
        mime_type = doc.get("mime_type", "")

        if not (file_name.lower().endswith(".pdf") or "pdf" in mime_type.lower()):
            bot.send_message(chat_id, "⚠️ Please send a PDF file.", reply_to_message_id=msg_id)
            return

        job_counter += 1
        my_job_id = job_counter
        active_job_id[chat_id] = my_job_id

        def check_cancelled():
            return active_job_id.get(chat_id) != my_job_id

        with processing_lock:
            if check_cancelled():
                return

            bot.send_chat_action(chat_id, "upload_document")
            bot.send_message(chat_id, "⏳ Downloading & processing PDF...", reply_to_message_id=msg_id)

            with tempfile.TemporaryDirectory() as tmp_dir:
                local_in = os.path.join(tmp_dir, "input.pdf")
                local_out_pdf = os.path.join(tmp_dir, "meesho_rearranged.pdf")
                local_out_txt = os.path.join(tmp_dir, "meesho_picklist.txt")

                file_info = bot.get_file(doc["file_id"])
                if not file_info or not file_info.get("ok"):
                    bot.send_message(chat_id, "❌ Could not download file from Telegram.", reply_to_message_id=msg_id)
                    return

                if check_cancelled():
                    return

                server_path = file_info["result"]["file_path"]
                bot.download_file(server_path, local_in)

                if check_cancelled():
                    return

                try:
                    t0 = time.time()
                    res = process_initial_pdf(local_in, local_out_pdf, check_cancelled)
                    if res[0] is None:
                        return

                    picklist_text, total_pages, multi_qty_count, known, anomalies = res
                    duration = time.time() - t0

                    if check_cancelled():
                        return

                    with open(local_out_txt, "w", encoding="utf-8") as f:
                        f.write(picklist_text)

                    # Save to persistent user cache for /adjust command
                    cache_dir = os.path.join(tempfile.gettempdir(), "meesho_bot_cache")
                    os.makedirs(cache_dir, exist_ok=True)
                    cached_pdf_path = os.path.join(cache_dir, f"batch_{chat_id}.pdf")
                    shutil.copyfile(local_in, cached_pdf_path)

                    user_last_batch[chat_id] = {
                        "pdf_path": cached_pdf_path,
                        "known": known,
                        "anomalies": anomalies,
                        "total_pages": total_pages,
                        "timestamp": time.time()
                    }

                    mode = user_modes.get(chat_id, "both")

                    # 1. Send Picklist In Chat
                    if len(picklist_text) <= 4000:
                        bot.send_message(
                            chat_id,
                            f"📋 *Picklist Generated* ({total_pages} orders in {duration:.1f}s):\n\n```\n{picklist_text}\n```"
                        )
                    else:
                        preview = picklist_text[:3800] + "\n... (full picklist in file below)"
                        bot.send_message(chat_id, f"📋 *Picklist Preview*:\n\n```\n{preview}\n```")

                    # 2. Send TXT File
                    bot.send_document(chat_id, local_out_txt, caption=f"📄 Picklist: {total_pages} orders ({duration:.1f}s)")

                    # 3. Send Rearranged PDF (if both mode)
                    if mode == "both":
                        alert_note = f" | ⚠️ {multi_qty_count} multi-qty alert(s)" if multi_qty_count > 0 else ""
                        bot.send_document(
                            chat_id,
                            local_out_pdf,
                            caption=f"📑 Rearranged PDF: {total_pages} pages sorted{alert_note}"
                        )

                    # 4. Helpful tip about adjusting
                    bot.send_message(
                        chat_id,
                        "💡 *Tip:* Have some of these pieces in stock at home?\n"
                        "Send `/adjust` to deduct them and move their labels to the very tail of the PDF!"
                    )

                except Exception as e:
                    bot.send_message(chat_id, f"❌ Error processing PDF: {e}", reply_to_message_id=msg_id)
                finally:
                    gc.collect()


# ── Main Entrypoint ────────────────────────────────────────────────────

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
    token = get_bot_token()
    bot = TelegramBot(token)

    port = int(os.environ.get("PORT", 8080))
    start_keep_alive_server(port)

    # Clean webhook and drain backlog on startup
    print("[Startup] Wiping webhook and dropping any stuck Telegram updates...")
    bot.delete_webhook(drop_pending=True)

    init_updates = bot.get_updates(offset=-1, timeout=0)
    offset = None
    if init_updates and init_updates.get("result"):
        last_id = init_updates["result"][-1]["update_id"]
        offset = last_id + 1
        bot.get_updates(offset=offset, timeout=0)
        print(f"[Startup] Purged all backlog up to update #{last_id}. Starting completely fresh!")
    else:
        print("[Startup] Telegram queue is completely clean!")

    print("=" * 60)
    print("  Meesho Telegram Bot is running!")
    print("  Engine: pypdfium2 (~2MB RAM, ultra-fast).")
    print("  Feature: /adjust (At-Home stock moved to PDF tail).")
    print("=" * 60)

    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=20)
            if not updates or not updates.get("ok"):
                time.sleep(1)
                continue

            results = updates.get("result", [])
            if not results:
                continue

            for item in results:
                update_id = item["update_id"]
                offset = update_id + 1

                # Acknowledge to Telegram immediately to prevent any looping
                bot.get_updates(offset=offset, timeout=0)

                if update_id in processed_update_ids:
                    continue
                processed_update_ids.add(update_id)
                if len(processed_update_ids) > 1000:
                    processed_update_ids.clear()

                process_single_message(bot, item)

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            print(f"[Loop Notice] {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
