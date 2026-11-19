"""
Meesho Telegram Bot (Ultra-Fast pypdfium2, Zero-Loop, Picklist Adjustment & Courier Split)
==========================================================================================
Features:
  1. Ultra-Low RAM: Google's C++ PDFium engine (pypdfium2, ~2MB RAM).
  2. Mathematical Zero-Loop Guarantee with immediate Telegram confirmation.
  3. Built-in Keep-Alive server for UptimeRobot (24/7 on Render free tier).
  4. /adjust Command:
     - Copy picklist, edit numbers for stock you have at home.
     - Bot automatically moves at-home labels to the end of the PDF!
  5. Courier Partner Extraction (/ex, /del, /sf, /vl, /courier):
     - Separate orders by courier (e.g. /ex for Xpressbees).
     - Selected courier orders move to the FIRST PART (TOP) of the PDF and picklist,
       in the exact same organized order (Type -> Size -> Colour).
     - ANY at-home stock for that courier is placed at the VERY END OF THAT COURIER'S BLOCK!
       So you can grab the courier stack off the printer and peel off its at-home labels immediately!
     - Remaining orders follow underneath (with any other at-home stock at the very end of the PDF).
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

COURIER_CMD_MAP = {
    "/ex": "Xpressbees",
    "/xb": "Xpressbees",
    "/xpressbees": "Xpressbees",
    "/expressbee": "Xpressbees",
    "/expressbees": "Xpressbees",
    "/xpressbee": "Xpressbees",
    "/del": "Delhivery",
    "/delhivery": "Delhivery",
    "/dl": "Delhivery",
    "/sf": "Shadowfax",
    "/shadowfax": "Shadowfax",
    "/shadow": "Shadowfax",
    "/vl": "Valmo",
    "/valmo": "Valmo",
    "/ecom": "Ecom Express",
    "/ecomexpress": "Ecom Express",
    "/dtdc": "DTDC",
    "/bluedart": "BlueDart",
}

processed_update_ids = set()
active_job_id = {}
user_modes = {}
user_last_batch = {}        # chat_id -> dict with pdf_path, known, anomalies, factory_items, tail_items, last_courier
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


# ── Fast & Ultra-Low RAM PDF & SKU Helpers ─────────────────────────────

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

def detect_courier(page_text):
    """Detects shipping courier partner from Meesho label text."""
    text_lower = page_text.lower()
    if (
        re.search(r"\b(xpressbees|xpress\s*bees)\b", text_lower)
        or re.search(r"\bXB[-_ ]", page_text)
        or re.search(r"\bXB\b", page_text)
    ):
        return "Xpressbees"
    elif "delhivery" in text_lower:
        return "Delhivery"
    elif "shadowfax" in text_lower or re.search(r"\bSF\d{6,}", page_text):
        return "Shadowfax"
    elif "valmo" in text_lower or re.search(r"\bVL\d{6,}", page_text):
        return "Valmo"
    elif "ecom express" in text_lower or "ecomexpress" in text_lower:
        return "Ecom Express"
    elif "dtdc" in text_lower:
        return "DTDC"
    elif "bluedart" in text_lower or "blue dart" in text_lower:
        return "BlueDart"

    m = re.search(r"(?:prepaid|cod)[^\n]*\n+([A-Za-z\s]+?)(?:Pickup|Destination|\n)", page_text, re.I)
    if m:
        cand = m.group(1).strip()
        if cand and len(cand) > 2 and cand.lower() not in ("if", "return", "asj"):
            return cand.title()

    return "Other"

def parse_user_picklist_robust(text):
    """
    Parses user's edited picklist with 100% tolerance to any headers, spacing, or formatting.
    Extracts:
      1. section_targets: { (section, t, s, c): desired_qty }
      2. global_targets:  { (t, s, c): total_desired_qty }
    """
    VALID_SIZES = {"M", "L", "XL", "XXL", "XXXL", "2XL", "3XL"}
    section_targets = {}
    global_accum = defaultdict(int)
    current_section = "ALL"
    current_type = None
    current_size = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lower = line.lower()
        # Skip summaries, alerts, totals
        if any(lower.startswith(p) for p in (
            "total", "grand total", "factory", "anomalies", "at-home",
            "🏠", "🏭", "📦", "🚚", "📊", "(pages", "!", "*"
        )):
            continue

        # Courier section detection
        if "ORDERS" in line.upper():
            if any(c in line.upper() for c in ("XPRESSBEES", "EXPRESS", "XB", "DELHIVERY", "SHADOWFAX", "VALMO", "ECOM", "DTDC", "BLUEDART")):
                current_section = "COURIER"
                current_type = None
                current_size = None
                continue
            if "OTHER" in line.upper() or "REMAINING" in line.upper():
                current_section = "OTHER"
                current_type = None
                current_size = None
                continue

        if line.startswith("-") or line.startswith("="):
            continue

        # Type header like [ P ] or [P] or [ TSHIRT ]
        m_type = re.match(r"^\[\s*([A-Za-z0-9_-]+)\s*\]", line)
        if m_type:
            current_type = m_type.group(1).upper()
            current_size = None
            continue

        # Size line like M, L, XL, XXL, XXXL
        parts = line.split()
        if len(parts) == 1 and parts[0].upper() in VALID_SIZES:
            current_size = parts[0].upper()
            continue

        # Inline format with Type, Size, Colour, Qty: e.g. 'P XL Maroon - 2' or 'P XL Maroon 2'
        m_inline = re.match(r"^([A-Za-z0-9_-]+)\s+(M|L|XL|XXL|XXXL|2XL|3XL)\s+([A-Za-z\s]+?)[\s\-:]+(\d+)\s*$", line, re.I)
        if m_inline:
            t = m_inline.group(1).upper()
            s = m_inline.group(2).upper()
            c = re.sub(r"\s+", " ", m_inline.group(3)).strip().lower()
            q = int(m_inline.group(4))
            section_targets[(current_section, t, s, c)] = q
            global_accum[(t, s, c)] += q
            continue

        # Inline format with Size, Colour, Qty: e.g. 'XL Maroon 2' or 'XL Maroon - 2'
        m_size_col = re.match(r"^(M|L|XL|XXL|XXXL|2XL|3XL)\s+([A-Za-z\s]+?)[\s\-:]+(\d+)\s*$", line, re.I)
        if m_size_col:
            s = m_size_col.group(1).upper()
            c = re.sub(r"\s+", " ", m_size_col.group(2)).strip().lower()
            q = int(m_size_col.group(3))
            t = current_type if current_type else "P"
            section_targets[(current_section, t, s, c)] = q
            global_accum[(t, s, c)] += q
            continue

        # Standard block format: e.g. 'Maroon - 2' or 'Maroon-2' or 'Maroon 2'
        clean = re.sub(r"\(ALERT:.*?\)", "", line, flags=re.I).strip()
        m_item = re.match(r"^([A-Za-z\s]+?)[\s\-:]+(\d+)\s*$", clean)
        if m_item and current_type and current_size:
            c = re.sub(r"\s+", " ", m_item.group(1)).strip().lower()
            q = int(m_item.group(2))
            section_targets[(current_section, current_type, current_size, c)] = q
            global_accum[(current_type, current_size, c)] += q

    return section_targets, dict(global_accum)

def partition_labels_ultimate(known_items, section_targets, global_targets, target_courier=None):
    """
    Partitions labels into factory_items vs deducted_items.
    Matches section targets if present, falls back cleanly to global targets.
    """
    groups = defaultdict(list)
    for it in known_items:
        t, c, s, q, is_anom, orig_idx, raw, courier = it
        groups[(t, s, c.lower())].append(it)

    factory_items = []
    deducted_items = []

    def is_courier_item(it):
        if not target_courier:
            return False
        c = it[7].lower() if len(it) > 7 else ""
        t_low = target_courier.lower()
        if t_low in ("xpressbees", "xb", "express", "expressbee", "expressbees"):
            return c in ("xpressbees", "xb")
        return t_low in c

    for key, lbl_list in groups.items():
        t, s, c_lower = key
        orig_q = sum(it[3] for it in lbl_list)

        c_target = section_targets.get(("COURIER", t, s, c_lower))
        o_target = section_targets.get(("OTHER", t, s, c_lower))

        if c_target is not None or o_target is not None:
            c_lbls = [it for it in lbl_list if is_courier_item(it)]
            o_lbls = [it for it in lbl_list if not is_courier_item(it)]

            # 1. Courier labels
            desired_c = c_target if c_target is not None else sum(it[3] for it in c_lbls)
            kept_c = 0
            for it in c_lbls:
                if kept_c + it[3] <= desired_c:
                    factory_items.append(it)
                    kept_c += it[3]
                else:
                    deducted_items.append(it)

            # 2. Other labels
            desired_o = o_target if o_target is not None else sum(it[3] for it in o_lbls)
            kept_o = 0
            for it in o_lbls:
                if kept_o + it[3] <= desired_o:
                    factory_items.append(it)
                    kept_o += it[3]
                else:
                    deducted_items.append(it)

        else:
            # Global target (or single picklist)
            desired_q = global_targets.get(key, orig_q)
            if target_courier:
                # Prioritize courier labels for deduction so at-home stock is for that courier
                lbl_list.sort(key=lambda x: 1 if is_courier_item(x) else 0)

            kept = 0
            for it in lbl_list:
                if kept + it[3] <= desired_q:
                    factory_items.append(it)
                    kept += it[3]
                else:
                    deducted_items.append(it)

    return factory_items, deducted_items


# ── Picklist Formatter Functions ───────────────────────────────────────

def build_section_text(items):
    """Builds clean, compact Type -> Size -> Colour picklist lines for a list of items."""
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

    return "\n".join(lines).rstrip()

def build_picklist_text(items, anomalies=None, tail_items=None):
    """Generates complete picklist text for a standard single-group batch."""
    lines = []
    if items:
        lines.append(build_section_text(items))
        lines.append("")

    total_pcs = sum(it[3] for it in items)
    total_labels = len(items)

    if total_pcs != total_labels:
        lines.append(f"Total : {total_labels} labels ({total_pcs} pcs)")
    else:
        lines.append(f"Total : {total_labels}")

    if anomalies:
        lines.append(f"Anomalies : {len(anomalies)}")
        for a in anomalies:
            desc = a[6] if len(a) > 6 and a[6] else a[1]
            lines.append(f"   {desc}")

    if tail_items:
        start_tail = len(items) + (len(anomalies) if anomalies else 0) + 1
        end_tail = len(items) + (len(anomalies) if anomalies else 0) + len(tail_items)
        lines.append("")
        lines.append("=" * 42)
        lines.append(f"🏠 AT-HOME / TAIL LABELS: {len(tail_items)} pcs")
        lines.append(f"(Pages {start_tail} to {end_tail} at the VERY END of PDF)")
        lines.append("=" * 42)

        tail_summary = defaultdict(int)
        for t, c, s, q, *_ in tail_items:
            tail_summary[f"{t} {c} {s}"] += q
        for desc, cnt in sorted(tail_summary.items()):
            lines.append(f"   • {desc:<20} {cnt} pcs")

    return "\n".join(lines)

def build_courier_split_picklist_text(courier_name, courier_factory, courier_at_home,
                                      other_factory, other_at_home, anomalies=None):
    """
    Generates dual-section picklist:
    - Selected courier on top (with its at-home stock at the end of its block!)
    - Remaining courier orders below (with any other at-home stock at the very end of the PDF).
    """
    lines = []

    # Total courier numbers
    n_cf = len(courier_factory)
    n_ca = len(courier_at_home)
    total_c_labels = n_cf + n_ca
    total_c_pcs = sum(it[3] for it in courier_factory) + sum(it[3] for it in courier_at_home)
    c_range = f"Pages 1 to {total_c_labels}"

    lines.append("=" * 42)
    lines.append(f"🚚 {courier_name.upper()} ORDERS ({total_c_pcs} pcs - {c_range})")
    lines.append("=" * 42)
    lines.append("")

    if n_ca > 0:
        lines.append(f"🏭 FACTORY ORDERS ({sum(it[3] for it in courier_factory)} pcs - Pages 1 to {n_cf}):")
        lines.append("")

    if courier_factory:
        lines.append(build_section_text(courier_factory))
        lines.append("")

    # At-Home stock for this courier placed at the VERY END of this courier's block!
    if n_ca > 0:
        c_at_home_start = n_cf + 1
        c_at_home_end = total_c_labels
        lines.append("=" * 42)
        lines.append(f"🏠 {courier_name.upper()} AT-HOME STOCK: {sum(it[3] for it in courier_at_home)} pcs")
        lines.append(f"(Pages {c_at_home_start} to {c_at_home_end} at END of {courier_name})")
        lines.append("=" * 42)
        c_tail_summary = defaultdict(int)
        for t, c, s, q, *_ in courier_at_home:
            c_tail_summary[f"{t} {c} {s}"] += q
        for desc, cnt in sorted(c_tail_summary.items()):
            lines.append(f"   • {desc:<20} {cnt} pcs")
        lines.append("")

    # Other couriers
    n_of = len(other_factory)
    n_oa = len(other_at_home)
    total_o_labels = n_of + n_oa
    total_o_pcs = sum(it[3] for it in other_factory) + sum(it[3] for it in other_at_home)

    if total_o_labels > 0:
        start_other = total_c_labels + 1
        end_other = total_c_labels + total_o_labels
        lines.append("=" * 42)
        lines.append(f"📦 OTHER COURIER ORDERS ({total_o_pcs} pcs - Pages {start_other} to {end_other})")
        lines.append("=" * 42)
        lines.append("")

        if n_oa > 0:
            lines.append(f"🏭 FACTORY ORDERS ({sum(it[3] for it in other_factory)} pcs - Pages {start_other} to {start_other + n_of - 1}):")
            lines.append("")

        if other_factory:
            lines.append(build_section_text(other_factory))
            lines.append("")

        if n_oa > 0:
            o_at_home_start = total_c_labels + n_of + (len(anomalies) if anomalies else 0) + 1
            o_at_home_end = end_other + (len(anomalies) if anomalies else 0)
            lines.append("=" * 42)
            lines.append(f"🏠 OTHER AT-HOME STOCK: {sum(it[3] for it in other_at_home)} pcs")
            lines.append(f"(Pages {o_at_home_start} to {o_at_home_end} at VERY END of PDF)")
            lines.append("=" * 42)
            o_tail_summary = defaultdict(int)
            for t, c, s, q, *_ in other_at_home:
                o_tail_summary[f"{t} {c} {s}"] += q
            for desc, cnt in sorted(o_tail_summary.items()):
                lines.append(f"   • {desc:<20} {cnt} pcs")
            lines.append("")

    if anomalies:
        lines.append(f"Anomalies : {len(anomalies)}")
        for a in anomalies:
            desc = a[6] if len(a) > 6 and a[6] else a[1]
            lines.append(f"   {desc}")
        lines.append("")

    # Summary
    lines.append("=" * 42)
    lines.append(f"📊 BATCH SUMMARY: {total_c_labels + total_o_labels} orders ({total_c_pcs + total_o_pcs} pcs)")
    lines.append("=" * 42)
    c_tail_note = f" ({n_cf} factory + {n_ca} at-home)" if n_ca > 0 else ""
    lines.append(f"   • {courier_name}: {total_c_pcs} pcs{c_tail_note} -> Pages 1 to {total_c_labels}")
    if total_o_labels > 0:
        o_tail_note = f" ({n_of} factory + {n_oa} at-home)" if n_oa > 0 else ""
        lines.append(f"   • Other Couriers: {total_o_pcs} pcs{o_tail_note} -> Pages {total_c_labels+1} to {total_c_labels+total_o_labels}")

    return "\n".join(lines)


# ── PDF Processing Engine ──────────────────────────────────────────────

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

        courier = detect_courier(text)
        sku_line = extract_sku_line(text)
        if sku_line is None:
            items.append(("UNKNOWN", f"PAGE_{idx+1}", "", 1, True, idx, "", courier))
            continue

        ttype, colour, size, qty, is_anomaly = parse_sku(sku_line)
        items.append((ttype, colour, size, qty, is_anomaly, idx, sku_line, courier))

    if check_cancelled():
        pdf.close()
        return None, None, None, None, None

    known = [it for it in items if not it[4]]
    anomalies = [it for it in items if it[4]]

    def sort_key(item):
        t, c, s, qty, *_ = item
        return (t.lower(), SIZE_ORDER.get(s, 99), c.lower())

    known.sort(key=sort_key)
    anomalies.sort(key=lambda x: x[6].lower() if len(x) > 6 and x[6] else x[1].lower())

    ordered = known + anomalies
    picklist_text = build_picklist_text(known, anomalies)

    multi_qty_alerts = []
    for new_page_idx, it in enumerate(ordered):
        t, c, s, qty, is_anom, orig_idx, raw, *rest = it
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
        except Exception as e:
            print(f"[Telegram] sendMessage notice: {e}")
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
            data["caption"] = caption[:1000] # Safe limit under Telegram 1024
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id

        filename = os.path.basename(file_path)
        for attempt in range(3):
            try:
                with open(file_path, "rb") as f:
                    files = {"document": (filename, f)}
                    resp = requests.post(url, data=data, files=files, timeout=120)

                if resp.status_code == 200:
                    res_json = resp.json()
                    if res_json.get("ok"):
                        print(f"[Telegram] Sent document successfully: {filename}")
                        return res_json
                    else:
                        print(f"[Telegram] sendDocument error response: {resp.text}")
                elif resp.status_code == 429:
                    retry_after = 3
                    try:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                    except Exception:
                        pass
                    print(f"[Telegram] Rate limited (429). Retrying after {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                else:
                    print(f"[Telegram] sendDocument failed (code {resp.status_code}): {resp.text}")
            except Exception as e:
                print(f"[Telegram] sendDocument exception (attempt {attempt+1}/3): {e}")
                time.sleep(2)

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


# ── Courier Separation Handler (/ex, /del, /sf, /vl, /courier) ─────────

def handle_courier_split(bot, chat_id, msg_id, target_courier):
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous PDF found in memory!\n"
            "Please send your Meesho PDF first, then you can extract courier orders.",
            reply_to_message_id=msg_id
        )
        return

    pdf_source = batch["pdf_path"]
    anomalies = batch.get("anomalies", [])
    known = batch["known"]

    # Deducted / At-home items (from /adjust)
    deducted_items = batch.get("tail_items", [])
    # Active factory items
    factory_items = batch.get("factory_items") if batch.get("factory_items") is not None else known

    # Tally available couriers across all known items
    courier_counts = defaultdict(int)
    for it in known:
        cname = it[7] if len(it) > 7 else "Other"
        courier_counts[cname] += it[3]

    # If target_courier is empty, list all couriers in batch
    if not target_courier:
        lines = ["🚚 *Available Couriers in Current Batch:*\n"]
        for cname, cnt in sorted(courier_counts.items(), key=lambda x: -x[1]):
            sc = "/ex" if cname.lower() == "xpressbees" else (
                "/del" if cname.lower() == "delhivery" else (
                    "/sf" if cname.lower() == "shadowfax" else (
                        "/vl" if cname.lower() == "valmo" else f"/courier {cname.lower()}"
                    )
                )
            )
            lines.append(f"• *{cname}:* {cnt} pcs (Tap {sc})")
        lines.append("\n👉 Tap any command above to move that courier's orders to the TOP of the PDF and picklist!")
        bot.send_message(chat_id, "\n".join(lines), reply_to_message_id=msg_id)
        return

    target_lower = target_courier.lower()

    def is_target_courier(it):
        c = it[7].lower() if len(it) > 7 else "other"
        if target_lower in ("xpressbees", "xb", "expressbee", "expressbees", "ex"):
            return c in ("xpressbees", "xb")
        elif target_lower in ("delhivery", "del", "dl"):
            return "delhivery" in c
        elif target_lower in ("shadowfax", "sf", "shadow"):
            return "shadowfax" in c
        elif target_lower in ("valmo", "vl"):
            return "valmo" in c
        elif target_lower in ("ecom", "ecom express", "ecomexpress"):
            return "ecom" in c
        return target_lower in c

    # Separate Factory items
    courier_factory = [it for it in factory_items if is_target_courier(it)]
    other_factory = [it for it in factory_items if not is_target_courier(it)]

    # Separate At-Home (deducted) items
    courier_at_home = [it for it in deducted_items if is_target_courier(it)]
    other_at_home = [it for it in deducted_items if not is_target_courier(it)]

    total_c_labels = len(courier_factory) + len(courier_at_home)
    if total_c_labels == 0:
        lines = [
            f"⚠️ No orders found for *{target_courier.title()}* in this batch!\n",
            "🚚 *Available Couriers in this batch:*"
        ]
        for cname, cnt in sorted(courier_counts.items(), key=lambda x: -x[1]):
            lines.append(f"• *{cname}:* {cnt} pcs")
        bot.send_message(chat_id, "\n".join(lines), reply_to_message_id=msg_id)
        return

    # Sort each list in standard hierarchy: Type -> Size -> Colour
    def sort_key(it):
        t, c, s, qty, *_ = it
        return (t.lower(), SIZE_ORDER.get(s, 99), c.lower())

    courier_factory.sort(key=sort_key)
    courier_at_home.sort(key=sort_key)
    other_factory.sort(key=sort_key)
    other_at_home.sort(key=sort_key)

    c_display_name = (courier_factory or courier_at_home)[0][7]

    bot.send_chat_action(chat_id, "upload_document")

    with tempfile.TemporaryDirectory() as tmp_dir:
        clean_cname = re.sub(r"[^A-Za-z0-9]", "_", c_display_name).lower()
        out_pdf_path = os.path.join(tmp_dir, f"meesho_{clean_cname}_separated.pdf")
        out_txt_path = os.path.join(tmp_dir, f"meesho_{clean_cname}_picklist.txt")

        # 1. Build Split Picklist Text
        picklist_text = build_courier_split_picklist_text(
            c_display_name, courier_factory, courier_at_home,
            other_factory, other_at_home, anomalies=anomalies
        )

        n_cf = len(courier_factory)
        n_ca = len(courier_at_home)
        n_of = len(other_factory)
        n_oa = len(other_at_home)
        n_anom = len(anomalies)

        # REORDER SEQUENCE:
        # [courier_factory] -> [courier_at_home (at END of courier!)] -> [other_factory] -> [anomalies] -> [other_at_home]
        all_reordered = courier_factory + courier_at_home + other_factory + anomalies + other_at_home

        # Multi-qty alerts with updated page numbers
        multi_qty_alerts = []
        for new_page_idx, it in enumerate(all_reordered):
            if it[3] > 1:
                multi_qty_alerts.append(f"   Page {new_page_idx + 1}: {it[0]} {it[1]} {it[2]} -> Qty {it[3]}")

        if multi_qty_alerts:
            picklist_text += "\n\n! ! ! MULTI-QTY ALERTS ! ! !\n" + "\n".join(multi_qty_alerts)

        with open(out_txt_path, "w", encoding="utf-8") as f:
            f.write(picklist_text)

        # 2. Build Reordered PDF
        reordered_indices = [it[5] for it in all_reordered]
        pdf = pypdfium2.PdfDocument(pdf_source)
        new_pdf = pypdfium2.PdfDocument.new()
        new_pdf.import_pages(pdf, reordered_indices)
        new_pdf.save(out_pdf_path)
        new_pdf.close()
        pdf.close()
        del new_pdf
        del pdf
        gc.collect()

        # 3. Update state
        batch["last_courier"] = c_display_name

        # 4. Summary Lines
        c_pcs = sum(it[3] for it in courier_factory) + sum(it[3] for it in courier_at_home)
        o_pcs = sum(it[3] for it in other_factory) + sum(it[3] for it in other_at_home)

        summary_lines = [
            f"🚚 *{c_display_name} Orders Extracted to Top!*\n",
            f"• 🚚 *{c_display_name} Total:* {c_pcs} pcs ({total_c_labels} labels) -> *Pages 1 to {total_c_labels}*",
        ]

        if n_ca > 0:
            c_tail_start = n_cf + 1
            c_tail_end = total_c_labels
            summary_lines.append(
                f"   └─ 🏭 *Factory:* {sum(it[3] for it in courier_factory)} pcs (Pages 1 to {n_cf})\n"
                f"   └─ 🏠 *At-Home:* {sum(it[3] for it in courier_at_home)} pcs (Pages {c_tail_start} to {c_tail_end} at END of {c_display_name}!)"
            )

        if total_o_labels := (n_of + n_oa):
            start_o = total_c_labels + 1
            end_o = total_c_labels + total_o_labels
            summary_lines.append(f"• 📦 *Remaining Orders:* {o_pcs} pcs ({total_o_labels} labels) -> *Pages {start_o} to {end_o}*")

            if n_oa > 0:
                o_tail_start = total_c_labels + n_of + n_anom + 1
                o_tail_end = len(all_reordered)
                summary_lines.append(
                    f"   └─ 🏠 *Other At-Home:* {sum(it[3] for it in other_at_home)} pcs (Pages {o_tail_start} to {o_tail_end} at VERY END of PDF)"
                )

        if n_ca > 0:
            summary_lines.append(
                f"\n💡 *How to pack:* Grab the top *{c_display_name}* stack (Pages 1 to {total_c_labels}). "
                f"Peel off the **LAST {n_ca} label(s)** (Pages {n_cf+1} to {total_c_labels}) for your at-home stock!"
            )
        else:
            summary_lines.append(
                f"\n💡 *How to pack:* All *{c_display_name}* labels are printed FIRST at the top of the stack (Pages 1 to {total_c_labels}) in clean organised order!"
            )

        bot.send_message(chat_id, "\n".join(summary_lines), reply_to_message_id=msg_id)

        # Send preview in chat if under Telegram length limit
        if len(picklist_text) <= 3800:
            bot.send_message(chat_id, f"📋 *Separated Picklist:*\n\n```\n{picklist_text}\n```")

        # Send TXT Document
        bot.send_document(
            chat_id,
            out_txt_path,
            caption=f"📄 Picklist: {c_display_name} ({c_pcs} pcs) + Others ({o_pcs} pcs)"
        )

        # Send PDF Document
        tail_caption = f" | {c_display_name} at-home at tail of block" if n_ca > 0 else ""
        bot.send_document(
            chat_id,
            out_pdf_path,
            caption=f"📑 Separated PDF: {c_display_name} (Pages 1 to {total_c_labels}) on top{tail_caption}"
        )


# ── Picklist Adjustment Handler (At-Home Stock) ────────────────────────

def handle_adjustment(bot, chat_id, msg_id, edited_text):
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous PDF found in memory!\nPlease send your Meesho PDF first, then you can adjust the picklist.",
            reply_to_message_id=msg_id
        )
        return

    section_targets, global_targets = parse_user_picklist_robust(edited_text)
    if not global_targets:
        bot.send_message(
            chat_id,
            "⚠️ Could not parse any quantities from your text.\n"
            "Please copy the previous picklist message, edit the numbers (e.g. change 4 to 2), and send it.",
            reply_to_message_id=msg_id
        )
        return

    known = batch["known"]
    anomalies = batch.get("anomalies", [])
    pdf_source = batch["pdf_path"]
    last_courier = batch.get("last_courier")

    # Partition labels into factory_items vs deducted_items (respecting section edits)
    factory_items, deducted_items = partition_labels_ultimate(known, section_targets, global_targets, target_courier=last_courier)

    if not deducted_items:
        bot.send_message(
            chat_id,
            "ℹ️ No items were deducted! The quantities you sent match or exceed the original batch.\n"
            "To cut off at-home stock, lower the quantity of the item (e.g. from 4 to 2).",
            reply_to_message_id=msg_id
        )
        return

    # Update state
    batch["factory_items"] = factory_items
    batch["tail_items"] = deducted_items

    # If user had courier split active, rebuild with courier split and at-home labels at end of courier!
    if last_courier:
        handle_courier_split(bot, chat_id, msg_id, last_courier)
        return

    # Standard adjustment (without courier split)
    bot.send_chat_action(chat_id, "upload_document")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_pdf_path = os.path.join(tmp_dir, "meesho_adjusted.pdf")
        out_txt_path = os.path.join(tmp_dir, "meesho_factory_picklist.txt")

        new_picklist_text = build_picklist_text(factory_items, anomalies, tail_items=deducted_items)
        with open(out_txt_path, "w", encoding="utf-8") as f:
            f.write(new_picklist_text)

        all_reordered = factory_items + anomalies + deducted_items
        reordered_indices = [it[5] for it in all_reordered]

        pdf = pypdfium2.PdfDocument(pdf_source)
        new_pdf = pypdfium2.PdfDocument.new()
        new_pdf.import_pages(pdf, reordered_indices)
        new_pdf.save(out_pdf_path)
        new_pdf.close()
        pdf.close()
        del new_pdf
        del pdf
        gc.collect()

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
            f"\n💡 *How to pack:* Grab the *LAST {len(deducted_items)} label(s)* from the bottom/tail of the printed stack for your at-home stock!"
        )

        bot.send_message(chat_id, "\n".join(summary_lines), reply_to_message_id=msg_id)

        if len(new_picklist_text) <= 3800:
            bot.send_message(chat_id, f"📋 *New Factory Picklist:*\n\n```\n{new_picklist_text}\n```")

        bot.send_document(
            chat_id,
            out_txt_path,
            caption=f"📄 Factory Picklist: {len(factory_items)} pcs to pick"
        )

        bot.send_document(
            chat_id,
            out_pdf_path,
            caption=f"📑 Adjusted PDF: {len(all_reordered)} pages (At-Home stock at Pages {start_tail}-{end_tail})"
        )


def handle_reset_courier(bot, chat_id, msg_id):
    """Resets courier separation back to all combined orders."""
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous PDF found in memory!\nPlease send your Meesho PDF first.",
            reply_to_message_id=msg_id
        )
        return

    batch["last_courier"] = None
    pdf_source = batch["pdf_path"]
    anomalies = batch.get("anomalies", [])
    tail_items = batch.get("tail_items", [])
    active_items = batch.get("factory_items") if batch.get("factory_items") is not None else batch["known"]

    def sort_key(it):
        t, c, s, qty, *_ = it
        return (t.lower(), SIZE_ORDER.get(s, 99), c.lower())

    active_items.sort(key=sort_key)

    bot.send_chat_action(chat_id, "upload_document")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_pdf_path = os.path.join(tmp_dir, "meesho_rearranged.pdf")
        out_txt_path = os.path.join(tmp_dir, "meesho_picklist.txt")

        picklist_text = build_picklist_text(active_items, anomalies, tail_items=tail_items)
        all_reordered = active_items + anomalies + tail_items

        with open(out_txt_path, "w", encoding="utf-8") as f:
            f.write(picklist_text)

        reordered_indices = [it[5] for it in all_reordered]
        pdf = pypdfium2.PdfDocument(pdf_source)
        new_pdf = pypdfium2.PdfDocument.new()
        new_pdf.import_pages(pdf, reordered_indices)
        new_pdf.save(out_pdf_path)
        new_pdf.close()
        pdf.close()
        del new_pdf
        del pdf
        gc.collect()

        bot.send_message(
            chat_id,
            f"🔄 *Reset to All Couriers Combined!*\nTotal orders: {len(active_items)} pcs",
            reply_to_message_id=msg_id
        )

        if len(picklist_text) <= 3800:
            bot.send_message(chat_id, f"📋 *Combined Picklist:*\n\n```\n{picklist_text}\n```")

        bot.send_document(
            chat_id,
            out_txt_path,
            caption=f"📄 Picklist: {len(active_items)} orders"
        )
        bot.send_document(
            chat_id,
            out_pdf_path,
            caption=f"📑 Rearranged PDF: {len(all_reordered)} pages"
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
            "👋 *Meesho Label, Picklist & Courier Bot*\n\n"
            "📄 *Send a PDF:* Generates clean Picklist + Rearranged PDF.\n\n"
            "✂️ *Adjust for At-Home Stock:*\n"
            "Use `/adjust` to cut off stock you have at home.\n\n"
            "🚚 *Courier Partner Extraction:*\n"
            "Extract any courier company to the **TOP of the stack** in organised order:\n"
            "• `/ex` or `/xb` - Xpressbees on top\n"
            "• `/del` or `/delhivery` - Delhivery on top\n"
            "• `/sf` or `/shadowfax` - Shadowfax on top\n"
            "• `/vl` or `/valmo` - Valmo on top\n"
            "• `/courier` - View all couriers in batch\n"
            "• `/all` - Reset to all couriers combined\n\n"
            "💡 *Smart At-Home Tail Placement:*\n"
            "When extracting a courier (e.g. `/ex`), any at-home pieces for that courier are placed "
            "**at the very end of that courier's stack** so you can grab them off immediately!\n\n"
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
                "*(Tip: You can paste the whole picklist or just the lines you changed, e.g. `P XL Maroon - 2`)*",
                reply_to_message_id=msg_id
            )
        return

    # Check for Courier Split commands (/ex, /xb, /del, /sf, /vl, /courier)
    cmd = text.split()[0].lower() if text else ""
    if cmd in COURIER_CMD_MAP:
        target_courier = COURIER_CMD_MAP[cmd]
        handle_courier_split(bot, chat_id, msg_id, target_courier)
        return

    if cmd in ("/courier", "/couriers"):
        parts = text.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        handle_courier_split(bot, chat_id, msg_id, target)
        return

    if cmd in ("/all", "/reset", "/reset_courier"):
        handle_reset_courier(bot, chat_id, msg_id)
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

                    # Save to persistent user cache for /adjust and courier commands
                    cache_dir = os.path.join(tempfile.gettempdir(), "meesho_bot_cache")
                    os.makedirs(cache_dir, exist_ok=True)
                    cached_pdf_path = os.path.join(cache_dir, f"batch_{chat_id}.pdf")
                    shutil.copyfile(local_in, cached_pdf_path)

                    user_last_batch[chat_id] = {
                        "pdf_path": cached_pdf_path,
                        "known": known,
                        "anomalies": anomalies,
                        "total_pages": total_pages,
                        "factory_items": known,
                        "tail_items": [],
                        "last_courier": None,
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

                    # 4. Courier Breakdown & Helpful Tips
                    courier_counts = defaultdict(int)
                    for it in known:
                        cname = it[7] if len(it) > 7 else "Other"
                        courier_counts[cname] += it[3]

                    courier_summary = ", ".join(f"{c}: {cnt}" for c, cnt in sorted(courier_counts.items(), key=lambda x: -x[1]))

                    tips = (
                        f"🚚 *Couriers in batch:* {courier_summary}\n\n"
                        "💡 *Quick Commands:*\n"
                        "• `/ex` - Put Xpressbees orders on top\n"
                        "• `/del` - Put Delhivery orders on top\n"
                        "• `/sf` - Put Shadowfax orders on top\n"
                        "• `/vl` - Put Valmo orders on top\n"
                        "• `/adjust` - Move at-home stock to tail of PDF"
                    )
                    bot.send_message(chat_id, tips)

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
    print("  Features:")
    print("    • /adjust (Move at-home pieces to PDF tail)")
    print("    • /ex, /del, /sf, /vl (Courier partner extraction to top)")
    print("    • Courier At-Home labels at end of courier stack!")
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
