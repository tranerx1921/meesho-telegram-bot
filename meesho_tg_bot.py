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
import pypdfium2.raw as pdfium_c

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


# ── Flipkart PDF Processor Engine (pypdfium2 Ultra-Low RAM) ─────────────

FK_SIZE_ORDER = {"M": 0, "L": 1, "XL": 2, "2XL": 3, "3XL": 4}
FK_SIZE_LIST = ["M", "L", "XL", "2XL", "3XL"]
FK_KNOWN_SIZES = set(FK_SIZE_ORDER.keys()) | {"XXL", "XXXL"}
FK_LABEL_WIDTH = 213.0   # ~75mm / 3 inch
FK_LABEL_HEIGHT = 354.0  # ~125mm / 5 inch

FK_PRODUCT_PREFIXES = [
    "Full Plain",
    "Half-P",
    "50 Full",
    "Single Full",
    "Half Plain",
    "50 Half",
    "Single Half",
]

FK_FULL_PLAIN_NEW_COLOURS = {
    "Blue", "Coffee", "Pink", "Red", "Mustard", "Sea"
}

FK_TYPE_ORDER = {
    "Full Plain": 0, "Full Plain New": 1,
    "Half-P": 2, "50 Full": 3, "Single Full": 4,
    "Half Plain": 5, "50 Half": 6, "Single Half": 7
}

FK_ALL_TYPES = [
    "Full Plain New", "Full Plain", "Half-P", "50 Full",
    "Single Full", "Half Plain", "50 Half", "Single Half", "Unknown"
]


def fk_normalize_size(raw):
    s = raw.strip().upper()
    if s == "XXXL":
        return "3XL"
    if s == "XXL":
        return "2XL"
    return s


def fk_normalize_colour(raw):
    s = raw.strip().title()
    colour_aliases = {
        "Biege": "Biege",
        "Beige": "Biege",
        "L Biege": "Biege",
        "L Beige": "Biege",
        "Lbiege": "Biege",
        "Lbeige": "Biege",
        "Light Biege": "Biege",
        "Light Beige": "Biege",
        "Lblue": "Light Blue",
        "D Coffee": "Coffee",
    }
    return colour_aliases.get(s, s)


def fk_parse_sku_description(desc):
    desc = desc.strip()
    if not desc:
        return None
    tokens = desc.split()
    if not tokens:
        return None
    raw_size = tokens[-1].upper()
    if raw_size not in FK_KNOWN_SIZES:
        return None
    size = fk_normalize_size(raw_size)
    remainder = " ".join(tokens[:-1])
    for prefix in FK_PRODUCT_PREFIXES:
        if remainder.lower().startswith(prefix.lower()):
            colour = remainder[len(prefix):].strip()
            return (prefix, fk_normalize_colour(colour) if colour else "N/A", size)
    return ("Unknown", fk_normalize_colour(remainder) if remainder else "N/A", size)


def fk_extract_sku_items_from_page(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    items = []
    in_sku = False
    for line in lines:
        if "SKU ID" in line:
            in_sku = True
            continue
        if not in_sku:
            continue
        stripped = line.strip()
        if stripped == "QTY":
            continue
        if stripped.isdigit() and len(stripped) <= 2:
            continue
        if re.match(r"^(FM|SF|\d{10,})", stripped):
            in_sku = False
            continue
        if not stripped:
            in_sku = False
            continue
        m = re.match(r"^\d+\s+(.+?)(?:\s*\|\s*(.+))?$", stripped)
        if m:
            sku_desc = m.group(1).strip()
            parsed = fk_parse_sku_description(sku_desc)
            if parsed:
                items.append(parsed)
    return items


def fk_extract_barcode(text):
    m_awb = re.search(r"A\s*WB\s*No\.?\s*([A-Za-z0-9]+)", text)
    if m_awb:
        return m_awb.group(1).strip()
    m_fm = re.search(r"\b(FM[A-Za-z0-9]{8,15})\b", text)
    if m_fm:
        return m_fm.group(1).strip()
    m_sf = re.search(r"\b(SF[A-Za-z0-9]{8,15})\b", text)
    if m_sf:
        return m_sf.group(1).strip()
    m_od = re.search(r"\b(OD\d{16,20})\b", text)
    if m_od:
        return m_od.group(1).strip()
    return None


def is_flipkart_pdf(input_pdf_path):
    """Detects whether a PDF contains Flipkart shipping labels."""
    try:
        doc = pypdfium2.PdfDocument(input_pdf_path)
        pages_to_check = min(5, len(doc))
        for i in range(pages_to_check):
            pg = doc[i]
            tp = pg.get_textpage()
            txt = tp.get_text_range().replace("\r\n", "\n")
            tp.close()
            pg.close()
            if "SKU ID" in txt and (
                "Ordered through" in txt
                or "E-Kart" in txt
                or "HBD:" in txt
                or "Use Transparent Packaging" in txt
                or re.search(r"\b(OD\d{16,20}|FM[A-Za-z0-9]{8,15})\b", txt)
            ):
                doc.close()
                return True
        doc.close()
    except Exception:
        pass
    return False


def _fk_get_label_crop_rect_pdfium(page):
    """Auto-detects outer border line of a Flipkart label in PDFium bottom-up coordinates."""
    pw, ph = page.get_size()
    h_lines = []
    v_lines = []
    for obj in page.get_objects(max_depth=1):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            l, b, r, t = obj.get_bounds()
            w = r - l
            h = t - b
            if w > 150 and h < 3:
                h_lines.append((l, b, r, t))
            if h > 250 and w < 3:
                v_lines.append((l, b, r, t))
    if h_lines and v_lines:
        cx0 = max(0.0, min(x[0] for x in v_lines) - 1.0)
        cx1 = min(pw, max(x[2] for x in v_lines) + 1.0)
        cb0 = max(0.0, min(x[1] for x in h_lines) - 1.0)
        ct1 = min(ph + 1.0, max(x[3] for x in h_lines) + 1.0)
        return (cx0, cb0, cx1, ct1)

    if pw < 300:
        return (7.0, max(0.0, ph - 340.0), min(211.5, pw), ph)
    return (190.5, max(0.0, ph - 381.0), 404.2, max(0.0, ph - 28.5))


def build_flipkart_cropped_pdf(src_pdf_path, ordered_indices, out_pdf_path, check_cancelled=None):
    """Builds 3x5 inch (213x354) tightly border-cropped Flipkart output PDF using ~2-45MB RAM."""
    src_doc = pypdfium2.PdfDocument(src_pdf_path)
    new_pdf = pypdfium2.PdfDocument.new()
    new_pdf.import_pages(src_doc, ordered_indices)
    src_doc.close()
    del src_doc
    gc.collect()

    n_out = len(new_pdf)
    for i in range(n_out):
        if check_cancelled and check_cancelled():
            new_pdf.close()
            del new_pdf
            gc.collect()
            return False

        pg = new_pdf[i]
        cx0, cb0, cx1, ct1 = _fk_get_label_crop_rect_pdfium(pg)
        cw = max(1.0, cx1 - cx0)
        ch = max(1.0, ct1 - cb0)
        scale = min(FK_LABEL_WIDTH / cw, FK_LABEL_HEIGHT / ch)
        x_dst = (FK_LABEL_WIDTH - cw * scale) / 2.0
        y_dst = (FK_LABEL_HEIGHT - ch * scale) / 2.0
        dx = x_dst - cx0 * scale
        dy = y_dst - cb0 * scale

        mat = pdfium_c.FS_MATRIX(scale, 0.0, 0.0, scale, dx, dy)
        clip = pdfium_c.FS_RECTF(
            x_dst - 0.5,
            y_dst + ch * scale + 0.5,
            x_dst + cw * scale + 0.5,
            y_dst - 0.5
        )
        pdfium_c.FPDFPage_TransFormWithClip(pg.raw, mat, clip)
        pg.set_mediabox(0.0, 0.0, FK_LABEL_WIDTH, FK_LABEL_HEIGHT)
        pg.set_cropbox(0.0, 0.0, FK_LABEL_WIDTH, FK_LABEL_HEIGHT)
        pg.gen_content()
        pg.close()

    new_pdf.save(out_pdf_path)
    new_pdf.close()
    del new_pdf
    gc.collect()
    return True


def build_flipkart_picklist_text(singles, combos, anomalies=None, duplicates_skipped=None, tail_singles=None):
    """Generates exact Flipkart Colour-first / Size-second tree picklist (+ optional At-Home tail summary)."""
    lines = []
    separator = "─" * 48

    picklist_data = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for t, c, s, *_ in singles:
        picklist_data[t][c][s] += 1
    for c_items, *_ in combos:
        for t, c, s in c_items:
            picklist_data[t][c][s] += 1

    type_display = sorted(picklist_data.keys(), key=lambda t: FK_TYPE_ORDER.get(t, 99))

    for ttype in type_display:
        lines.append(separator)
        lines.append(f"  {ttype.upper()}")
        lines.append(separator)

        type_total = sum(sum(sz_d.values()) for sz_d in picklist_data[ttype].values())
        lines.append(f"  Total items: {type_total}")
        lines.append("")

        sorted_colours = sorted(picklist_data[ttype].keys(), key=lambda x: x.lower())
        for colour in sorted_colours:
            sizes_dict = picklist_data[ttype][colour]
            colour_total = sum(sizes_dict.values())
            lines.append(f"  ┌─ {colour} ({colour_total} pcs)")

            avail_sizes = [sz for sz in FK_SIZE_LIST if sz in sizes_dict]
            for i, sz in enumerate(avail_sizes):
                cnt = sizes_dict[sz]
                is_last = (i == len(avail_sizes) - 1)
                branch = "└──" if is_last else "├──"
                lines.append(f"  │  {branch} {sz:<14} × {cnt}")

            lines.append("  │")

        lines.append("")

    total_combo_pieces = sum(len(c[0]) for c in combos)
    anom_count = len(anomalies) if anomalies else 0
    dups_count = len(duplicates_skipped) if duplicates_skipped else 0

    lines.append(separator)
    grand_total_pieces = len(singles) + total_combo_pieces + anom_count
    total_bags = len(singles) + len(combos) + anom_count
    if dups_count:
        lines.append(f"  DUPLICATES SKIPPED  : {dups_count} (duplicate barcodes filtered out)")
    if anomalies:
        lines.append(f"  ANOMALIES           : {anom_count}")
        for a in anomalies:
            lines.append(f"    • {a[1]}")
    lines.append(f"  GRAND TOTAL PIECES  : {grand_total_pieces}")
    lines.append(f"  TOTAL LABELS / BAGS : {total_bags}")
    lines.append(separator)

    if tail_singles:
        start_tail = total_bags + 1
        end_tail = total_bags + len(tail_singles)
        lines.append("")
        lines.append("=" * 48)
        lines.append(f"🏠 AT-HOME / TAIL LABELS: {len(tail_singles)} pcs")
        lines.append(f"(Pages {start_tail} to {end_tail} at the VERY END of PDF)")
        lines.append("=" * 48)
        tail_summary = defaultdict(int)
        for t, c, s, *_ in tail_singles:
            tail_summary[f"{t} {c} {s}"] += 1
        for desc, cnt in sorted(tail_summary.items()):
            lines.append(f"   • {desc:<28} {cnt} pcs")

    return "\n".join(lines)


def parse_flipkart_user_picklist(text):
    """
    Parses edited Flipkart picklist (full copied tree, partial block, or inline shorthand).
    Returns {(type, colour, size): desired_qty}.
    """
    targets = {}
    current_type = None
    current_colour = None
    type_lookup = {t.lower(): t for t in FK_ALL_TYPES}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "AT-HOME" in line.upper() or line.startswith("🏠"):
            break

        if set(line) <= {"─", "-", "=", " "}:
            continue
        lower = line.lower()
        if any(lower.startswith(p) for p in ("total items:", "grand total", "total labels", "duplicates skipped", "anomalies", "!", "*")):
            continue

        cleaned = re.sub(r"[│├└┌]+", " ", line).strip()
        cleaned = re.sub(r"^[─\-]+\s*", "", cleaned).strip()
        if not cleaned:
            continue

        if cleaned.lower() in type_lookup:
            current_type = type_lookup[cleaned.lower()]
            current_colour = None
            continue

        matched_inline = False
        for tname in FK_ALL_TYPES:
            if cleaned.lower().startswith(tname.lower() + " "):
                rest = cleaned[len(tname):].strip()
                m_in = re.match(r"^(.+?)\s+(3XL|2XL|XXXL|XXL|XL|L|M)[\s×xX*\-:]+(\d+)\s*$", rest, re.I)
                if m_in:
                    col = fk_normalize_colour(m_in.group(1))
                    sz = fk_normalize_size(m_in.group(2))
                    qty = int(m_in.group(3))
                    targets[(tname, col, sz)] = qty
                    matched_inline = True
                    break
        if matched_inline:
            continue

        m_sq = re.match(r"^(3XL|2XL|XXXL|XXL|XL|L|M)[\s×xX*\-:]+(\d+)\s*$", cleaned, re.I)
        if m_sq and current_type and current_colour:
            sz = fk_normalize_size(m_sq.group(1))
            qty = int(m_sq.group(2))
            targets[(current_type, current_colour, sz)] = qty
            continue

        if line.lstrip().startswith(("┌", "+")) or ("pcs)" in cleaned.lower()):
            col_name = re.sub(r"\(\s*\d+\s*(?:pcs?)?\s*\)$", "", cleaned, flags=re.I).strip()
            if col_name:
                current_colour = fk_normalize_colour(col_name)
                continue

        if current_type and not re.search(r"\d", cleaned):
            current_colour = fk_normalize_colour(cleaned)

    return targets


def partition_flipkart_labels(known_singles, combos, targets):
    """
    Separates Flipkart singles into factory_singles vs tail_singles (at-home stock).
    Accounts for pieces already inside combos so deductions match total picklist counts.
    """
    combo_counts = defaultdict(int)
    for c_items, *_ in combos:
        for t, c, s in c_items:
            combo_counts[(t, c.lower(), s)] += 1

    single_groups = defaultdict(list)
    for it in known_singles:
        t, c, s, *_ = it
        single_groups[(t, c.lower(), s)].append(it)

    all_keys = set(single_groups.keys()) | set(combo_counts.keys())
    norm_targets = {(t, c.lower(), s): q for (t, c, s), q in targets.items()}

    factory_singles = []
    tail_singles = []
    locked_warnings = []

    for key in all_keys:
        s_list = single_groups.get(key, [])
        c_cnt = combo_counts.get(key, 0)
        orig_total = len(s_list) + c_cnt

        desired_total = norm_targets.get(key, orig_total)
        to_deduct = max(0, orig_total - desired_total)

        deduct_from_singles = min(to_deduct, len(s_list))
        if to_deduct > len(s_list):
            t, c_low, s = key
            locked_warnings.append(f"{t} {c_low.title()} {s}: {to_deduct - len(s_list)} pc(s) inside combo order(s)")

        keep_count = len(s_list) - deduct_from_singles
        factory_singles.extend(s_list[:keep_count])
        tail_singles.extend(s_list[keep_count:])

    def single_sort_key(item):
        t, c, s, *_ = item
        return (FK_TYPE_ORDER.get(t, 99), c.lower(), FK_SIZE_ORDER.get(s, 99))

    factory_singles.sort(key=single_sort_key)
    tail_singles.sort(key=single_sort_key)
    return factory_singles, tail_singles, locked_warnings


def process_flipkart_pdf(input_pdf_path, output_pdf_path, check_cancelled, source_filename="labels.pdf"):
    """Full Flipkart PDF processing (matching flipkart_sorter.py) on ultra-low RAM pypdfium2."""
    doc = pypdfium2.PdfDocument(input_pdf_path)
    n = len(doc)

    singles = []    # (ttype, colour, size, page_idx)
    combos = []     # (norm_items, page_idx)
    anomalies = []  # ("ANOMALY", label, "", page_idx)
    seen_barcodes = {}
    duplicates_skipped = []

    for idx in range(n):
        if check_cancelled():
            doc.close()
            return None

        pg = doc[idx]
        tp = pg.get_textpage()
        text = tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
        tp.close()
        pg.close()

        barcode = fk_extract_barcode(text)
        if barcode:
            if barcode in seen_barcodes:
                orig_pg = seen_barcodes[barcode]
                duplicates_skipped.append((barcode, source_filename, idx + 1, source_filename, orig_pg))
                continue
            seen_barcodes[barcode] = idx + 1

        sku_items = fk_extract_sku_items_from_page(text)
        if not sku_items:
            anomalies.append(("ANOMALY", f"UNKNOWN_{source_filename}_p{idx+1}", "", idx))
            continue

        norm_items = []
        for ttype, colour, size in sku_items:
            if ttype == "Full Plain" and colour in FK_FULL_PLAIN_NEW_COLOURS:
                ttype = "Full Plain New"
            norm_items.append((ttype, colour, size))

        if len(norm_items) == 1:
            t, c, s = norm_items[0]
            singles.append((t, c, s, idx))
        else:
            combos.append((norm_items, idx))

    doc.close()
    del doc
    gc.collect()

    if check_cancelled():
        return None

    def single_sort_key(item):
        t, c, s, *_ = item
        return (FK_TYPE_ORDER.get(t, 99), c.lower(), FK_SIZE_ORDER.get(s, 99))

    def combo_sort_key(c_item):
        items_list, _ = c_item
        t, c, s = items_list[0]
        return (-len(items_list), FK_TYPE_ORDER.get(t, 99), c.lower(), FK_SIZE_ORDER.get(s, 99))

    singles.sort(key=single_sort_key)
    anomalies.sort(key=lambda x: x[1].lower())
    combos.sort(key=combo_sort_key)

    picklist_text = build_flipkart_picklist_text(singles, combos, anomalies, duplicates_skipped)

    ordered_indices = (
        [it[3] for it in singles]
        + [c[1] for c in combos]
        + [a[3] for a in anomalies]
    )

    ok = build_flipkart_cropped_pdf(input_pdf_path, ordered_indices, output_pdf_path, check_cancelled)
    if not ok:
        return None

    return {
        "picklist_text": picklist_text,
        "singles": singles,
        "combos": combos,
        "anomalies": anomalies,
        "duplicates_skipped": duplicates_skipped,
        "total_bags": len(ordered_indices),
        "grand_total_pieces": len(singles) + sum(len(c[0]) for c in combos) + len(anomalies),
    }


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
        with requests.get(url, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(local_destination, "wb") as f:
                for chunk in r.iter_content(chunk_size=16384):
                    if chunk:
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


def send_picklist_messages(bot, chat_id, picklist_text, header_prefix="📋 *Picklist Generated*"):
    """Sends picklist text in chat, splitting cleanly across section boundaries if > 3800 chars."""
    if len(picklist_text) <= 3800:
        bot.send_message(chat_id, f"{header_prefix}:\n\n```\n{picklist_text}\n```")
        return

    chunks = []
    cur = []
    cur_len = 0
    for line in picklist_text.splitlines():
        if cur and cur_len + len(line) + 1 > 3600:
            chunks.append("\n".join(cur))
            cur = [line]
            cur_len = len(line) + 1
        else:
            cur.append(line)
            cur_len += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))

    for idx, ch in enumerate(chunks[:3]):
        part_note = f" (Part {idx+1}/{len(chunks)})" if len(chunks) > 1 else ""
        bot.send_message(chat_id, f"{header_prefix}{part_note}:\n\n```\n{ch}\n```")
    if len(chunks) > 3:
        bot.send_message(chat_id, "📄 Remaining picklist sections are in the attached file below.")


# ── Courier Separation Handler (/ex, /del, /sf, /vl, /courier) ─────────

def handle_courier_split(bot, chat_id, msg_id, target_courier):
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous PDF found in memory!\n"
            "Please send your PDF first.",
            reply_to_message_id=msg_id
        )
        return

    if batch.get("platform") == "flipkart":
        bot.send_message(
            chat_id,
            "ℹ️ Courier separation is only for Meesho batches (Flipkart uses a single courier).\n"
            "You can still use `/adjust` to move at-home stock to the end of your Flipkart PDF!",
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

def handle_flipkart_adjustment(bot, chat_id, msg_id, edited_text):
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous Flipkart PDF found in memory!\nPlease send your Flipkart PDF first.",
            reply_to_message_id=msg_id
        )
        return

    targets = parse_flipkart_user_picklist(edited_text)
    if not targets:
        bot.send_message(
            chat_id,
            "⚠️ Could not parse any Flipkart quantities from your text.\n"
            "Please copy the previous picklist message, lower the numbers for at-home items (e.g. `× 4` -> `× 2`), and paste it here.",
            reply_to_message_id=msg_id
        )
        return

    known_singles = batch["singles"]
    combos = batch.get("combos", [])
    anomalies = batch.get("anomalies", [])
    dups = batch.get("duplicates_skipped", [])
    pdf_source = batch["pdf_path"]

    factory_singles, tail_singles, locked_warnings = partition_flipkart_labels(known_singles, combos, targets)

    if not tail_singles:
        bot.send_message(
            chat_id,
            "ℹ️ No items were deducted! The quantities you sent match or exceed the original batch.\n"
            "To cut off at-home stock, lower the quantity of the item (e.g. from 4 to 2).",
            reply_to_message_id=msg_id
        )
        return

    batch["factory_singles"] = factory_singles
    batch["tail_singles"] = tail_singles

    bot.send_chat_action(chat_id, "upload_document")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_pdf_path = os.path.join(tmp_dir, "rearranged_output.pdf")
        out_txt_path = os.path.join(tmp_dir, "picklist.txt")

        new_picklist_text = build_flipkart_picklist_text(
            factory_singles, combos, anomalies, dups, tail_singles=tail_singles
        )
        with open(out_txt_path, "w", encoding="utf-8") as f:
            f.write(new_picklist_text)

        ordered_indices = (
            [it[3] for it in factory_singles]
            + [c[1] for c in combos]
            + [a[3] for a in anomalies]
            + [it[3] for it in tail_singles]
        )

        ok = build_flipkart_cropped_pdf(pdf_source, ordered_indices, out_pdf_path)
        if not ok:
            return

        factory_bags = len(factory_singles) + len(combos)
        start_tail = factory_bags + len(anomalies) + 1
        end_tail = len(ordered_indices)

        deducted_summary = defaultdict(int)
        for t, c, s, *_ in tail_singles:
            deducted_summary[f"{t} {c} {s}"] += 1

        summary_lines = [
            "✂️ *Flipkart Picklist & PDF Adjusted Successfully!*\n",
            f"🏭 *Factory Labels:* {factory_bags} label(s) (Pages 1 to {factory_bags})",
            f"🏠 *At-Home Stock (Tail):* {len(tail_singles)} label(s) (Pages {start_tail} to {end_tail})",
        ]
        for desc, cnt in sorted(deducted_summary.items()):
            summary_lines.append(f"   • `{desc}`: *{cnt} pcs*")

        if locked_warnings:
            summary_lines.append("\n⚠️ *Combo Note (cannot split single piece inside multi-item combo order):*")
            for w in locked_warnings:
                summary_lines.append(f"   • {w}")

        summary_lines.append(
            f"\n💡 *How to pack:* Grab the *LAST {len(tail_singles)} label(s)* from the bottom/tail of the printed stack for your at-home stock!"
        )

        bot.send_message(chat_id, "\n".join(summary_lines), reply_to_message_id=msg_id)
        send_picklist_messages(bot, chat_id, new_picklist_text, header_prefix="📋 *New Factory Picklist*")

        bot.send_document(
            chat_id,
            out_txt_path,
            caption=f"📄 Factory Picklist: {factory_bags} label(s) to pick"
        )
        mode = user_modes.get(chat_id, "both")
        if mode == "both":
            bot.send_document(
                chat_id,
                out_pdf_path,
                caption=f"📑 Adjusted Flipkart PDF: {len(ordered_indices)} labels (3x5 cropped, At-Home tail Pages {start_tail}-{end_tail})"
            )


def handle_adjustment(bot, chat_id, msg_id, edited_text):
    batch = user_last_batch.get(chat_id)
    if not batch or not os.path.exists(batch["pdf_path"]):
        bot.send_message(
            chat_id,
            "⚠️ No previous PDF found in memory!\nPlease send your PDF first, then you can adjust the picklist.",
            reply_to_message_id=msg_id
        )
        return

    if batch.get("platform") == "flipkart":
        handle_flipkart_adjustment(bot, chat_id, msg_id, edited_text)
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
            "⚠️ No previous PDF found in memory!\nPlease send your PDF first.",
            reply_to_message_id=msg_id
        )
        return

    if batch.get("platform") == "flipkart":
        bot.send_message(
            chat_id,
            "ℹ️ Flipkart batches use a single courier — no courier split active.",
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


def analyze_meesho_html(html_content):
    """Parses Meesho Ready to Ship table HTML and returns packed/unpacked stats."""
    soup = None
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
    except Exception:
        pass

    if soup:
        table = soup.find('table')
        sku_col, qty_col, size_col = 3, 5, 6
        if table:
            ths = table.find_all('th')
            for i, th in enumerate(ths):
                t = th.get_text(strip=True).lower()
                if 'sku' in t: sku_col = i
                elif 'quantity' in t or 'qty' in t: qty_col = i
                elif 'size' in t: size_col = i

        rows = soup.find_all('tr', attrs={'data-index': True})
        if not rows and table:
            tbody = table.find('tbody')
            if tbody:
                rows = tbody.find_all('tr')
    else:
        # Regex fallback when BeautifulSoup is unavailable
        row_chunks = re.findall(r'<tr[^>]*data-index[^>]*>(.*?)</tr>', html_content, flags=re.DOTALL | re.I)
        total_orders = len(row_chunks)
        packed_count = 0
        unpacked_count = 0
        unpacked_summary = defaultdict(int)
        for chunk in row_chunks:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', chunk, flags=re.DOTALL | re.I)
            if len(tds) < 5: continue
            clean_tds = [re.sub(r'<[^>]+>', ' ', td).strip() for td in tds]
            full_text = ' '.join(clean_tds)
            sku = re.sub(r'\s+', ' ', clean_tds[3]).strip() if len(clean_tds) > 3 else 'Unknown'
            size = clean_tds[6].strip() if len(clean_tds) > 6 else 'Unknown'
            qty_str = clean_tds[5].strip() if len(clean_tds) > 5 else '1'
            m = re.search(r'\d+', qty_str)
            qty = int(m.group()) if m else 1
            has_packet = bool(re.search(r'packet\s*id|pid[:\s]|packet\s*#', full_text, re.I))
            key = f'{sku} ({size})'
            if has_packet:
                packed_count += qty
            else:
                unpacked_count += qty
                unpacked_summary[key] += qty
        return total_orders, packed_count, unpacked_count, dict(unpacked_summary)

    total_orders = len(rows)
    packed_count = 0
    unpacked_count = 0
    unpacked_summary = defaultdict(int)

    for r in rows:
        tds = r.find_all('td')
        if len(tds) < 5:
            continue
        
        full_text = r.get_text(' ', strip=True)
        sku = tds[sku_col].get_text(strip=True) if len(tds) > sku_col else 'Unknown'
        sku = re.sub(r'\s+', ' ', sku).strip()
        size = tds[size_col].get_text(strip=True) if len(tds) > size_col else 'Unknown'
        qty_str = tds[qty_col].get_text(strip=True) if len(tds) > qty_col else '1'
        m = re.search(r'\d+', qty_str)
        qty = int(m.group()) if m else 1

        has_packet = bool(re.search(r'packet\s*id|pid[:\s]|packet\s*#', full_text, re.I))
        if not has_packet and len(tds) > 1:
            p_tags = tds[1].find_all('p')
            if len(p_tags) > 2:
                for p in p_tags[2:]:
                    if re.search(r'packet|\b\d{6,}\b', p.get_text(strip=True), re.I):
                        has_packet = True
                        break

        key = f'{sku} ({size})'
        if has_packet:
            packed_count += qty
        else:
            unpacked_count += qty
            unpacked_summary[key] += qty

    return total_orders, packed_count, unpacked_count, dict(unpacked_summary)


def handle_html_analysis(bot, chat_id, msg_id, doc):
    """Processes an uploaded Meesho Supplier Panel HTML file."""
    bot.send_chat_action(chat_id, "typing")
    file_info = bot.get_file(doc["file_id"])
    if not file_info or not file_info.get("ok"):
        bot.send_message(chat_id, "❌ Could not download HTML file from Telegram.", reply_to_message_id=msg_id)
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_in = os.path.join(tmp_dir, "panel.html")
        server_path = file_info["result"]["file_path"]
        bot.download_file(server_path, local_in)

        try:
            with open(local_in, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            total, packed, unpacked, summary = analyze_meesho_html(content)
        except Exception as e:
            bot.send_message(chat_id, f"❌ Failed to parse HTML file: {e}", reply_to_message_id=msg_id)
            return

    lines = [
        "📦 *Meesho Ready-to-Ship Scan Report*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *Total Orders Found:* `{total}`",
        f"✅ *Packed (Has Packet ID):* `{packed}`",
        f"🔴 *Unpacked Remaining:* `{unpacked}` pcs\n"
    ]
    if unpacked == 0:
        lines.append("🎉 *Awesome! All orders have Packet IDs and are PACKED!*")
    else:
        lines.append("📋 *Unpacked Items Breakdown:*")
        for k, v in sorted(summary.items(), key=lambda x: -x[1]):
            lines.append(f"• *{k}* - `{v} pcs`")
        lines.append("\n━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 *Tip:* To adjust your picklist, copy the unpacked items above and send `/adjust`!")

    bot.send_message(chat_id, "\n".join(lines), reply_to_message_id=msg_id)


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
            "👋 *Meesho & Flipkart Label & Picklist Bot*\n\n"
            "📄 *Send any Meesho or Flipkart PDF:* Auto-detects platform and generates Picklist + Sorted PDF (Flipkart labels auto-cropped to 3x5 inch).\n\n"
            "✂️ *Adjust for At-Home Stock:*\n"
            "Use `/adjust` (or just paste the edited picklist) to cut off stock you have at home and move those labels to the tail of the PDF.\n\n"
            "🚚 *Courier Partner Extraction (Meesho):*\n"
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
            batch = user_last_batch.get(chat_id, {})
            if batch.get("platform") == "flipkart":
                hint = "*(Tip: Paste the edited picklist or lines like `Full Plain Maroon XL × 2`)*"
            else:
                hint = "*(Tip: You can paste the whole picklist or just the lines you changed, e.g. `P XL Maroon - 2`)*"
            bot.send_message(
                chat_id,
                "✏️ *Ready to Adjust Picklist!*\n\n"
                "Please copy the previous picklist message, edit the numbers of the stock you have at home, "
                f"and paste it here.\n\n{hint}",
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

    if cmd in ("/scan", "/unpacked", "/bookmarklet"):
        msg_text = (
            "📱 *Meesho Mobile Unpacked Checker*\n\n"
            "Here is how to check unpacked orders directly on your Android phone:\n\n"
            "🔹 *Method 1: 1-Tap Mobile Chrome Bookmarklet (Instant)*\n"
            "Run directly while viewing `supplier.meesho.com`!\n"
            "It scans loaded orders, highlights unpacked items in yellow, and shows a floating HUD with packed vs unpacked counts.\n\n"
            "🔹 *Method 2: Send HTML Page to this Bot*\n"
            "In Android Chrome, tap `⋮ (Menu)` -> `Download` (downloads the HTML). Then send that `.html` file "
            "directly to this chat, and I'll analyze packed vs unpacked for you instantly!"
        )
        bot.send_message(chat_id, msg_text, reply_to_message_id=msg_id)
        return

    # Check if user was waiting to send adjustment text OR if message looks like a Meesho/Flipkart picklist
    cur_batch = user_last_batch.get(chat_id, {})
    looks_like_meesho = "[" in text and any(s in text.upper() for s in ("M", "L", "XL", "XXL", "XXXL"))
    looks_like_flipkart = (
        cur_batch.get("platform") == "flipkart"
        and any(s in text.upper() for s in ("M", "L", "XL", "2XL", "3XL"))
        and (
            any(sym in text for sym in ("┌", "├", "└", "×", "──"))
            or any(t.lower() in text.lower() for t in FK_ALL_TYPES)
        )
    )
    if chat_id in user_waiting_adjust or looks_like_meesho or looks_like_flipkart:
        user_waiting_adjust.discard(chat_id)
        handle_adjustment(bot, chat_id, msg_id, text)
        return

    # 2. Document Handling (PDF / Meesho HTML)
    doc = msg.get("document")
    if doc:
        file_name = doc.get("file_name", "labels.pdf")
        mime_type = doc.get("mime_type", "")

        # Check if Meesho HTML file
        if file_name.lower().endswith(".html") or file_name.lower().endswith(".htm") or "html" in mime_type.lower():
            handle_html_analysis(bot, chat_id, msg_id, doc)
            return

        if not (file_name.lower().endswith(".pdf") or "pdf" in mime_type.lower()):
            bot.send_message(chat_id, "⚠️ Please send a PDF or Meesho HTML file.", reply_to_message_id=msg_id)
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
                    cache_dir = os.path.join(tempfile.gettempdir(), "meesho_bot_cache")
                    os.makedirs(cache_dir, exist_ok=True)
                    cached_pdf_path = os.path.join(cache_dir, f"batch_{chat_id}.pdf")

                    if is_flipkart_pdf(local_in):
                        local_out_pdf = os.path.join(tmp_dir, "rearranged_output.pdf")
                        local_out_txt = os.path.join(tmp_dir, "picklist.txt")

                        fk_res = process_flipkart_pdf(
                            local_in, local_out_pdf, check_cancelled, source_filename=file_name
                        )
                        if fk_res is None or check_cancelled():
                            return

                        picklist_text = fk_res["picklist_text"]
                        duration = time.time() - t0
                        total_bags = fk_res["total_bags"]
                        total_pcs = fk_res["grand_total_pieces"]

                        with open(local_out_txt, "w", encoding="utf-8") as f:
                            f.write(picklist_text)

                        shutil.copyfile(local_in, cached_pdf_path)

                        user_last_batch[chat_id] = {
                            "platform": "flipkart",
                            "pdf_path": cached_pdf_path,
                            "singles": fk_res["singles"],
                            "combos": fk_res["combos"],
                            "anomalies": fk_res["anomalies"],
                            "duplicates_skipped": fk_res["duplicates_skipped"],
                            "factory_singles": fk_res["singles"],
                            "tail_singles": [],
                            "timestamp": time.time(),
                        }

                        mode = user_modes.get(chat_id, "both")

                        send_picklist_messages(
                            bot,
                            chat_id,
                            picklist_text,
                            header_prefix=f"📋 *Flipkart Picklist* ({total_pcs} pcs / {total_bags} labels in {duration:.1f}s)"
                        )

                        bot.send_document(
                            chat_id,
                            local_out_txt,
                            caption=f"📄 Flipkart Picklist: {total_pcs} pcs / {total_bags} labels ({duration:.1f}s)"
                        )

                        if mode == "both":
                            n_singles = len(fk_res["singles"])
                            n_combos = len(fk_res["combos"])
                            n_dups = len(fk_res["duplicates_skipped"])
                            parts_cap = [f"{total_bags} labels (3x5 cropped)"]
                            if n_combos > 0:
                                parts_cap.append(f"{n_singles} singles + {n_combos} combos at end")
                            if n_dups > 0:
                                parts_cap.append(f"{n_dups} duplicate(s) filtered")
                            bot.send_document(
                                chat_id,
                                local_out_pdf,
                                caption="📑 Flipkart Sorted PDF: " + " | ".join(parts_cap)
                            )

                        bot.send_message(
                            chat_id,
                            "💡 *Quick Command:*\n• `/adjust` - Cut off at-home stock & move those labels to tail of PDF"
                        )
                    else:
                        local_out_pdf = os.path.join(tmp_dir, "meesho_rearranged.pdf")
                        local_out_txt = os.path.join(tmp_dir, "meesho_picklist.txt")

                        res = process_initial_pdf(local_in, local_out_pdf, check_cancelled)
                        if res[0] is None:
                            return

                        picklist_text, total_pages, multi_qty_count, known, anomalies = res
                        duration = time.time() - t0

                        if check_cancelled():
                            return

                        with open(local_out_txt, "w", encoding="utf-8") as f:
                            f.write(picklist_text)

                        shutil.copyfile(local_in, cached_pdf_path)

                        user_last_batch[chat_id] = {
                            "platform": "meesho",
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
