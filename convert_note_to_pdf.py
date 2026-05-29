import sys
import os
import io
import re
import json
import threading
import time
import base64
import struct
import supernotelib as sn
from supernotelib.converter import PdfConverter, TextConverter


# ── Text block constants (from supernote binary format) ──────────────────────
TEXT_BLOCK_SIGNATURE = '040000006E6F6E65000000000300000000000000'
TEXT_BLOCK_MARKERS   = ['7C05000050070000', '80070000000A0000']
TEXTBOX_CONTENT_IDX  = 12
TITLE_STYLE_IDX      = 2


def find_all_hex_in_bytes(data: bytes, hex_str: str) -> list:
    needle = bytes.fromhex(hex_str)
    positions = []
    start = 0
    while True:
        pos = data.find(needle, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    return positions


def read_endian_int(data: bytes, pos: int, num_bytes: int = 4) -> tuple:
    if pos + num_bytes > len(data):
        return False, 0
    val = int.from_bytes(data[pos:pos + num_bytes], byteorder='little')
    return True, val


# ── Streaming helpers ────────────────────────────────────────────────────────

def _read_chunk(f, pos: int, size: int) -> bytes:
    f.seek(pos)
    return f.read(size)


def _get_last_chunk(f, file_size: int):
    """Read the footer block. Uses footer address from last 4 bytes (fast, works
    on any file size). Falls back to tail-scanning for older format files."""
    # Primary: Supernote X stores footer address in last 4 bytes
    try:
        f.seek(file_size - 4)
        footer_addr = int.from_bytes(f.read(4), 'little')
        if 0 < footer_addr < file_size - 4:
            f.seek(footer_addr)
            sz = int.from_bytes(f.read(4), 'little')
            if 0 < sz < 50_000_000:
                content = f.read(sz)
                if b'<PAGE' in content or b'<TITLE_' in content:
                    return content, footer_addr + 4
    except Exception:
        pass

    # Fallback: tail scan (works for older firmware)
    TAIL_SCAN = min(file_size, 10_000_000)
    f.seek(file_size - TAIL_SCAN)
    tail_region = f.read(TAIL_SCAN)
    tail_offsets = [m.start() for m in re.finditer(b'>tail', tail_region)]

    if len(tail_offsets) < 2:
        f.seek(0)
        full = f.read()
        all_tails = [m.start() for m in re.finditer(b'>tail', full)]
        if len(all_tails) < 2:
            return b'', 0
        start = all_tails[-2] + 5
        return full[start: all_tails[-1] + 10], start

    abs_offsets = [file_size - TAIL_SCAN + o for o in tail_offsets]
    chunk_start = abs_offsets[-2] + 5
    chunk_end   = abs_offsets[-1] + 10
    f.seek(chunk_start)
    return f.read(chunk_end - chunk_start), chunk_start


def _decode_textbox_streaming(f, stroke_addr: int, stroke_size: int) -> list:
    LOOKBACK = 65_536
    stroke_data = _read_chunk(f, stroke_addr, stroke_size + 4)
    sig_needle  = bytes.fromhex(TEXT_BLOCK_SIGNATURE)
    sig_offset  = stroke_data.find(sig_needle)
    if sig_offset == -1:
        return []

    sig_abs      = stroke_addr + sig_offset
    window_start = max(0, sig_abs - LOOKBACK)
    window       = _read_chunk(f, window_start, sig_abs - window_start)

    marker_pos_in_window = -1
    marker_len = 0
    for marker_hex in TEXT_BLOCK_MARKERS:
        needle = bytes.fromhex(marker_hex)
        best = -1
        pos  = 0
        while True:
            p = window.find(needle, pos)
            if p == -1:
                break
            best = p
            pos  = p + 1
        if best != -1:
            marker_pos_in_window = best
            marker_len = len(needle)
            break

    if marker_pos_in_window == -1:
        return []

    marker_abs = window_start + marker_pos_in_window
    payload    = _read_chunk(f, marker_abs + marker_len + 5, 10_240)

    ok, block_size = read_endian_int(payload, 0)
    if not ok or not (10 < block_size < 10_000):
        return []
    title_block = payload[4: 4 + block_size]

    decoded_values = []
    try:
        for b64_val in title_block.decode('ascii').split(','):
            if b64_val.strip():
                try:
                    decoded_values.append(base64.b64decode(b64_val).decode('utf-8'))
                except Exception:
                    decoded_values.append('')
    except Exception:
        return []
    return decoded_values


# ── Textbox extraction ───────────────────────────────────────────────────────

def extract_textbox_text(note_path: str, page_count: int) -> dict:
    results = {}
    try:
        f = open(note_path, 'rb')
        f.seek(0, 2)
        file_size = f.tell()

        last_chunk, _ = _get_last_chunk(f, file_size)
        if not last_chunk:
            f.close(); return results

        def read_int(pos, n=4):
            d = _read_chunk(f, pos, n)
            return (True, int.from_bytes(d, 'little')) if len(d) >= n else (False, 0)

        for page_idx in range(10_000):
            m = re.search(rb'<PAGE' + str(page_idx + 1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break

            page_meta_raw = _read_chunk(f, int(m.group(1)), 3000)
            tp_m = re.search(rb'<TOTALPATH:(\d+)>', page_meta_raw)
            if not tp_m:
                continue
            tp_addr = int(tp_m.group(1))
            if tp_addr == 0:
                continue

            _, nstrokes = read_int(tp_addr + 4)
            if nstrokes <= 0 or nstrokes > 100_000:
                continue

            a_position = tp_addr + 8
            page_texts = []

            for _ in range(nstrokes):
                ok, stroke_size = read_int(a_position)
                if not ok or stroke_size <= 0 or stroke_size > 10_000_000:
                    break

                peek      = _read_chunk(f, a_position, min(stroke_size + 4, 512))
                sig_bytes = bytes.fromhex(TEXT_BLOCK_SIGNATURE)
                if sig_bytes in peek or stroke_size > 512:
                    decoded = _decode_textbox_streaming(f, a_position, stroke_size)
                    if len(decoded) > TEXTBOX_CONTENT_IDX:
                        if decoded[TITLE_STYLE_IDX] == '0':
                            text = decoded[TEXTBOX_CONTENT_IDX].strip()
                            if text and text.lower() != 'none':
                                page_texts.append(text)

                a_position += stroke_size + 4

            if page_texts:
                results[page_idx] = page_texts

        f.close()

    except Exception as e:
        print(f"\n  Warning: textbox extraction failed ({e})")

    return results


# ── Heading extraction ───────────────────────────────────────────────────────

def extract_headings(note_path: str) -> dict:
    results = {}
    try:
        f = open(note_path, 'rb')
        f.seek(0, 2)
        file_size = f.tell()
        last_chunk, _ = _get_last_chunk(f, file_size)

        def read_int(pos, n=4):
            d = _read_chunk(f, pos, n)
            return int.from_bytes(d, 'little') if len(d) >= n else 0

        title_addrs = {}
        star_pages  = set()

        for tm in re.finditer(rb'<TITLE_(\d+):(\d+)>', last_chunk):
            name  = tm.group(1).decode()
            addr  = int(tm.group(2))
            if len(name) < 8:
                continue
            t_page_1 = int(name[0:4])
            y        = int(name[4:8])
            if addr not in title_addrs:
                title_addrs[addr] = (t_page_1, y)

        for page_idx in range(10_000):
            m = re.search(rb'<PAGE' + str(page_idx + 1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break
            page_meta = _read_chunk(f, int(m.group(1)), 4_096)
            if re.search(rb'<FIVESTAR:([^>]+)>', page_meta):
                star_pages.add(page_idx)

        page_textboxes = {}
        tb_sig = bytes.fromhex(TEXT_BLOCK_SIGNATURE)
        for page_1 in sorted(set(p for p, _ in title_addrs.values())):
            page_idx = page_1 - 1
            m_pt = re.search(rb'<PAGE' + str(page_1).encode() + rb':(\d+)>', last_chunk)
            if not m_pt:
                continue
            page_meta_pt = _read_chunk(f, int(m_pt.group(1)), 3000)
            tp_m_pt = re.search(rb'<TOTALPATH:(\d+)>', page_meta_pt)
            if not tp_m_pt:
                continue
            tp_addr_pt = int(tp_m_pt.group(1))
            if tp_addr_pt == 0:
                continue
            nstrokes_pt = read_int(tp_addr_pt + 4)
            if nstrokes_pt <= 0 or nstrokes_pt > 100_000:
                continue
            a_pt = tp_addr_pt + 8
            seen_ts = set()
            boxes = []
            for _ in range(nstrokes_pt):
                ssz_pt = read_int(a_pt)
                if ssz_pt <= 0 or ssz_pt > 10_000_000:
                    break
                peek_pt = _read_chunk(f, a_pt, min(ssz_pt + 4, 512))
                if tb_sig in peek_pt or ssz_pt > 512:
                    decoded_pt = _decode_textbox_streaming(f, a_pt, ssz_pt)
                    if (len(decoded_pt) > TEXTBOX_CONTENT_IDX and
                            decoded_pt[TITLE_STYLE_IDX] == '0'):
                        text_pt = decoded_pt[TEXTBOX_CONTENT_IDX].strip()
                        ts_pt   = decoded_pt[3] if len(decoded_pt) > 3 else ''
                        rect_pt = decoded_pt[4].split(',') if len(decoded_pt) > 4 else []
                        if (text_pt and text_pt.lower() != 'none' and
                                ts_pt not in seen_ts and len(rect_pt) == 4):
                            seen_ts.add(ts_pt)
                            try:
                                tx = int(rect_pt[0]); ty = int(rect_pt[1])
                                tw = int(rect_pt[2]); th = int(rect_pt[3])
                                boxes.append({'text': text_pt,
                                              'tx': tx, 'ty': ty,
                                              'tw': tw, 'th': th})
                            except ValueError:
                                pass
                a_pt += ssz_pt + 4
            if boxes:
                page_textboxes[page_idx] = boxes

        page_words = {}
        for page_idx in range(10_000):
            m = re.search(rb'<PAGE' + str(page_idx + 1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break
            page_meta = _read_chunk(f, int(m.group(1)), 3000)
            rt_m = re.search(rb'<RECOGNTEXT:(\d+)>', page_meta)
            if not rt_m:
                continue
            rt_addr = int(rt_m.group(1))
            rt_size = read_int(rt_addr)
            if not 0 < rt_size < 500_000:
                continue
            b64_data = _read_chunk(f, rt_addr + 4, rt_size)
            try:
                txt = base64.b64decode(b64_data).decode('utf-8')
                obj = json.loads(txt)
                words = []
                def _collect(node):
                    if isinstance(node, dict):
                        if node.get('type') == 'Text' and 'words' in node:
                            for w in node['words']:
                                bb = w.get('bounding-box', {})
                                label = w.get('label', '')
                                if label.strip() and label not in (' ', '\n'):
                                    words.append({
                                        'label': label,
                                        'x': bb.get('x', 0),
                                        'y': bb.get('y', 0),
                                    })
                        for v in node.values():
                            _collect(v)
                    elif isinstance(node, list):
                        for v in node:
                            _collect(v)
                _collect(obj)
                page_words[page_idx] = words
            except Exception:
                pass

        SCALE = 12.1
        page_heading_counts = {}
        for addr, (page_1, y_canvas) in sorted(title_addrs.items(), key=lambda kv: (kv[1][0], kv[1][1])):
            page_idx = page_1 - 1
            page_heading_counts[page_idx] = page_heading_counts.get(page_idx, 0) + 1
            heading_n = page_heading_counts[page_idx]

            title_block_raw = _read_chunk(f, addr, 500).decode('ascii', 'replace')
            level_m = re.search(r'<TITLELEVEL:(\d+)>', title_block_raw)
            rect_m  = re.search(r'<TITLERECT:(\d+),(\d+),(\d+),(\d+)>', title_block_raw)
            style_m = re.search(r'<TITLESTYLE:(\d+)>', title_block_raw)

            level = int(level_m.group(1)) if level_m else 1
            style = int(style_m.group(1)) if style_m else 0

            if rect_m:
                rx = int(rect_m.group(1));  ry = int(rect_m.group(2))
                rw = int(rect_m.group(3));  rh = int(rect_m.group(4))
            else:
                rx, ry, rw, rh = 0, y_canvas, 0, 0

            text        = ''
            placeholder = True

            MAX_TB_DIST = 500
            boxes = page_textboxes.get(page_idx, [])

            def _rdist(rx, ry, rw, rh, tx, ty, tw, th):
                dx = max(0, rx - (tx + tw), tx - (rx + rw))
                dy = max(0, ry - (ty + th), ty - (ry + rh))
                return (dx * dx + dy * dy) ** 0.5

            best_box = min(boxes, key=lambda b: _rdist(rx, ry, rw, rh, b['tx'], b['ty'], b['tw'], b['th']), default=None)
            if best_box is not None and _rdist(rx, ry, rw, rh, best_box['tx'], best_box['ty'], best_box['tw'], best_box['th']) <= MAX_TB_DIST:
                text        = best_box['text']
                placeholder = False

            if not text:
                rx_s = rx / SCALE;  ry_s = ry / SCALE
                rw_s = rw / SCALE;  rh_s = rh / SCALE
                words = page_words.get(page_idx, [])
                matched = [
                    w['label'] for w in words
                    if (rx_s - 3  <= w['x'] <= rx_s + rw_s + 3 and
                        ry_s - 25 <= w['y'] <= ry_s + 2)
                ]
                if matched:
                    text        = ' '.join(matched).strip()
                    placeholder = False

            if not text:
                text = f'Heading {heading_n}'

            entry = {'level': level, 'text': text, 'y': ry,
                     'style': style, 'placeholder': placeholder}
            results.setdefault(page_idx, []).append(entry)

        for page_idx in star_pages:
            results.setdefault(page_idx, [])
            results[page_idx].insert(0, {'level': 0, 'text': '★', 'y': -1, 'style': 0})

        f.close()

    except Exception as e:
        print(f"\n  Warning: heading extraction failed ({e})")

    return results


# ── Keyword extraction ───────────────────────────────────────────────────────

def extract_keywords(note_path: str) -> dict:
    results = {}
    try:
        f = open(note_path, 'rb')
        f.seek(0, 2)
        file_size = f.tell()
        last_chunk, _ = _get_last_chunk(f, file_size)

        def read_int(pos, n=4):
            d = _read_chunk(f, pos, n)
            return int.from_bytes(d, 'little') if len(d) >= n else 0

        seen_page_addr = set()
        for m in re.finditer(rb'<KEYWORD_(\d{4})(\d+):(\d+)>', last_chunk):
            page_1 = int(m.group(1))
            addr   = int(m.group(3))
            key    = (page_1, addr)
            if key in seen_page_addr:
                continue
            seen_page_addr.add(key)
            kw_block_raw = _read_chunk(f, addr, 300).decode('ascii', 'replace')
            site_m = re.search(r'<KEYWORDSITE:(\d+)>', kw_block_raw)
            if not site_m:
                continue
            site_addr = int(site_m.group(1))
            kw_size   = read_int(site_addr)
            if not 0 < kw_size < 1000:
                continue
            kw_text = _read_chunk(f, site_addr + 4, kw_size).decode('utf-8', 'replace').strip()
            if kw_text:
                results.setdefault(page_1 - 1, []).append(kw_text)
        f.close()

    except Exception as e:
        print(f"\n  Warning: keyword extraction failed ({e})")

    return results


def _extract_recogntext(note_path: str, page_count: int) -> list:
    """
    Extract recognised handwriting text directly from RECOGNTEXT binary blocks.
    Used as fallback when supernotelib TextConverter returns nothing.
    Returns list of strings indexed by page (0-based).
    """
    import json as _json, base64 as _b64
    text_layers = [""] * page_count
    try:
        f = open(note_path, 'rb')
        f.seek(0, 2); file_size = f.tell()
        last_chunk, _ = _get_last_chunk(f, file_size)

        def read_int(pos, n=4):
            d = _read_chunk(f, pos, n)
            return int.from_bytes(d, 'little') if len(d) >= n else 0

        for page_idx in range(page_count):
            m = re.search(rb'<PAGE' + str(page_idx + 1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break
            page_meta = _read_chunk(f, int(m.group(1)), 3000)
            rt_m = re.search(rb'<RECOGNTEXT:(\d+)>', page_meta)
            if not rt_m:
                continue
            rt_addr = int(rt_m.group(1))
            rt_size = read_int(rt_addr)
            if not 0 < rt_size < 500_000:
                continue
            b64_data = _read_chunk(f, rt_addr + 4, rt_size)
            try:
                obj = _json.loads(_b64.b64decode(b64_data).decode('utf-8'))
                words = []
                def _collect(node):
                    if isinstance(node, dict):
                        if node.get('type') == 'Text':
                            label = node.get('label', '').strip()
                            if label:
                                words.append(label)
                        for v in node.values():
                            _collect(v)
                    elif isinstance(node, list):
                        for v in node:
                            _collect(v)
                _collect(obj)
                if words:
                    text_layers[page_idx] = ' '.join(words)
            except Exception:
                pass
        f.close()
    except Exception as e:
        print(f"\n  Warning: RECOGNTEXT fallback failed ({e})")
    return text_layers


def extract_external_links(note_path: str, page_count: int) -> dict:
    """
    Extract EXTERNALLINKINFO tap targets from each page.
    Returns dict: page_idx -> list of (x, y, w, h, target_page_idx)
    Coordinates are in Supernote canvas units (1404x1872).

    Resolves link destinations via page ID (not the cached page number),
    so links remain correct even if pages have been inserted or reordered.
    """
    links = {}
    try:
        import re as _re, base64 as _b64
        f = open(note_path, 'rb')
        f.seek(0, 2); file_size = f.tell()
        last_chunk, _ = _get_last_chunk(f, file_size)

        # ── Build page_id → page_idx map ──────────────────────────────────────
        page_id_map = {}
        for page_idx in range(page_count):
            m = _re.search(rb'<PAGE' + str(page_idx+1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break
            f.seek(int(m.group(1)))
            sz = int.from_bytes(f.read(4), 'little')
            content_block = f.read(sz).decode('ascii', 'replace')
            pid_m = _re.search(r'<PAGEID:([^>]+)>', content_block)
            if pid_m:
                page_id_map[pid_m.group(1)] = page_idx

        # ── Extract links per page ─────────────────────────────────────────────
        for page_idx in range(page_count):
            m = _re.search(rb'<PAGE' + str(page_idx+1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break
            f.seek(int(m.group(1)))
            sz = int.from_bytes(f.read(4), 'little')
            content_block = f.read(sz).decode('ascii', 'replace')

            eli_m = _re.search(r'<EXTERNALLINKINFO:(\d+)>', content_block)
            if not eli_m:
                continue
            eli_addr = int(eli_m.group(1))
            if eli_addr == 0:
                continue

            f.seek(eli_addr)
            eli_size = int.from_bytes(f.read(4), 'little')
            if not 0 < eli_size < 10_000_000:
                continue
            eli_data = f.read(eli_size).decode('utf-8', 'replace')

            page_links = []
            for entry in eli_data.split('|'):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    parts = [_b64.b64decode(p).decode('utf-8', 'replace')
                             for p in entry.split(',') if p.strip()]
                    if len(parts) < 7:
                        continue
                    target_page_id  = parts[1]
                    target_page_num = int(parts[2]) if parts[2].isdigit() else 0

                    if target_page_id in page_id_map:
                        target_idx = page_id_map[target_page_id]
                    elif target_page_num > 0:
                        target_idx = target_page_num - 1
                    else:
                        continue

                    x, y, w, h = int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6])
                    if w <= 0 or h <= 0:
                        continue
                    page_links.append((x, y, w, h, target_idx))
                except Exception:
                    continue

            if page_links:
                links[page_idx] = page_links

        f.close()
        total = sum(len(v) for v in links.values())
        if total:
            print(f"\n  Links: found {total} tap targets across {len(links)} pages")
    except Exception as e:
        print(f"\n  Warning: link extraction failed ({e})")
    return links


def inject_pdf_links(writer, note_links: dict, page_count: int,
                     canvas_w: int = 1404, canvas_h: int = 1872):
    """
    Add PDF link annotations from extracted note links.
    writer must already have pages loaded.
    note_links: page_idx -> [(x, y, w, h, target_page_idx), ...]
    """
    from pypdf.generic import (ArrayObject, DictionaryObject, FloatObject,
                                NameObject, NumberObject)

    if not note_links: return

    for page_idx, page_links in note_links.items():
        if page_idx >= len(writer.pages): continue
        page = writer.pages[page_idx]
        pdf_w = float(page.mediabox.width)
        pdf_h = float(page.mediabox.height)
        sx = pdf_w / canvas_w
        sy = pdf_h / canvas_h

        for (x, y, w, h, target_idx) in page_links:
            if target_idx >= len(writer.pages): continue

            x1 = x * sx
            y2 = pdf_h - y * sy
            x2 = (x + w) * sx
            y1 = pdf_h - (y + h) * sy

            annot = DictionaryObject()
            annot[NameObject("/Type")]    = NameObject("/Annot")
            annot[NameObject("/Subtype")] = NameObject("/Link")
            annot[NameObject("/Rect")]    = ArrayObject([
                FloatObject(x1), FloatObject(y1),
                FloatObject(x2), FloatObject(y2)
            ])
            annot[NameObject("/Border")]  = ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)])
            annot[NameObject("/F")]       = NumberObject(4)

            target_page_ref = writer.pages[target_idx].indirect_reference
            dest = ArrayObject([target_page_ref, NameObject("/Fit")])
            annot[NameObject("/Dest")] = dest

            annot_ref = writer._add_object(annot)
            if "/Annots" not in page:
                page[NameObject("/Annots")] = ArrayObject()
            annots = page["/Annots"]
            if hasattr(annots, "get_object"):
                annots = annots.get_object()
            annots.append(annot_ref)


# ── Text overlay / PDF helpers ────────────────────────────────────────────────

def build_text_overlay_pdf(text: str, page_width: float, page_height: float) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))
    c.setFillAlpha(0)
    c.setFont("Helvetica", 12)

    words = text.split()
    if not words:
        c.save()
        return buf.getvalue()

    max_chars_per_line = max(1, int(page_width / 7))
    lines = []
    current = []
    for word in words:
        current.append(word)
        if len(" ".join(current)) > max_chars_per_line:
            lines.append(" ".join(current[:-1]))
            current = [word]
    if current:
        lines.append(" ".join(current))

    if not lines:
        c.save()
        return buf.getvalue()

    line_height = page_height / max(len(lines), 1)
    for i, line in enumerate(lines):
        y = page_height - (i + 0.5) * line_height
        c.drawString(10, y, line)

    c.save()
    return buf.getvalue()


def build_pdf_with_toc(pdf_bytes: bytes, text_layers: list,
                       headings: dict, keywords: dict) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))

    # Build invisible text overlays for searchable text
    overlays = {}
    for i, page in enumerate(reader.pages):
        page_text = text_layers[i] if i < len(text_layers) else ""
        if page_text and page_text.strip():
            try:
                width  = float(page.mediabox.width)
                height = float(page.mediabox.height)
                overlays[i] = build_text_overlay_pdf(page_text, width, height)
            except Exception:
                pass

    reader2 = PdfReader(io.BytesIO(pdf_bytes))
    writer  = PdfWriter()
    writer.clone_document_from_reader(reader2)

    for i in overlays:
        if i < len(writer.pages):
            try:
                overlay_reader = PdfReader(io.BytesIO(overlays[i]))
                writer.pages[i].merge_page(overlay_reader.pages[0])
            except Exception:
                pass

    # Add outline (bookmarks) for headings and keyword-only pages
    all_outline_pages = sorted(set(list(headings.keys()) + list(keywords.keys())))
    for page_idx in all_outline_pages:
        if page_idx >= len(writer.pages):
            continue
        page_headings = headings.get(page_idx, [])
        kws = keywords.get(page_idx, [])
        kw_str = f"  [{', '.join(kws)}]" if kws else ""

        if page_headings:
            has_star = any(e['text'] == '★' for e in page_headings)
            for entry in page_headings:
                if entry['text'] == '★':
                    title = f"★  (p.{page_idx + 1}){kw_str}"
                else:
                    title = ("★ " if has_star else "") + entry['text'] + kw_str
                try:
                    writer.add_outline_item(title, page_idx, parent=None)
                except Exception as e:
                    print(f"\n  Warning: outline item failed for p{page_idx+1}: {e}")
        elif kws:
            title = f"(p.{page_idx + 1}){kw_str}"
            try:
                writer.add_outline_item(title, page_idx, parent=None)
            except Exception as e:
                print(f"\n  Warning: outline item failed for p{page_idx+1}: {e}")

    out_buf = io.BytesIO()
    writer.write(out_buf)
    result = out_buf.getvalue()

    check = PdfReader(io.BytesIO(result))
    if not check.outline and headings:
        print(f"\n  Warning: outline has {len(headings)} heading pages but 0 items in output PDF")

    return result


# ── Main converter ────────────────────────────────────────────────────────────

def convert_note_to_pdf(note_path: str, max_workers: int = 4,
                        quality: int = None, test_page: int = None,
                        out_dir: str = None):
    """Convert a Supernote .note file to a processed PDF with bookmarks,
    searchable text, and internal tap-target links.

    By default, output is written to an 'output' folder beside the .note file.
    Use out_dir to specify a different output folder.
    """

    if not os.path.exists(note_path):
        print(f"Error: File not found: {note_path}")
        sys.exit(1)

    note_dir   = os.path.dirname(os.path.abspath(note_path))
    note_name  = os.path.splitext(os.path.basename(note_path))[0]
    output_dir = os.path.abspath(out_dir) if out_dir else os.path.join(note_dir, 'output')
    os.makedirs(output_dir, exist_ok=True)

    pdf_path = os.path.join(output_dir, note_name + '.pdf')

    filename     = os.path.basename(note_path)
    file_size_mb = os.path.getsize(note_path) / (1024 * 1024)

    print(f"\n{'='*50}")
    print(f"  Supernote .note → PDF Converter")
    print(f"{'='*50}")
    print(f"  File : {filename}")
    print(f"  Size : {file_size_mb:.1f} MB")
    if test_page:
        print(f"  Mode : TEST — page {test_page} only (no PDF written)")
    print(f"{'='*50}\n")

    # Dependency check
    missing = []
    try: import pypdf
    except ImportError: missing.append("pypdf")
    try: import reportlab
    except ImportError: missing.append("reportlab")
    if missing:
        print(f"  Note: optional packages not installed: {', '.join(missing)}")
        print(f"  Run: pip install {' '.join(missing)}\n")

    _t_total = time.time()

    # ── Step 1: Load notebook ────────────────────────────────────────────────
    _t = time.time()
    print("[1/3] Loading notebook...", end='', flush=True)
    notebook = None
    try:
        notebook = sn.load_notebook(note_path, policy='loose')
    except Exception as e:
        print(f"\n  Warning: supernotelib cannot render this file ({e})")
        print(f"  File version may be newer than supernotelib supports.")
        print(f"  PDF render will be skipped; text/bookmarks extraction will still run.\n")

    if notebook is None:
        with open(note_path, "rb") as _f:
            _f.seek(0, 2); _fsz = _f.tell()
            _ts = min(_fsz, 2_000_000)
            _f.seek(_fsz - _ts); _tr = _f.read(_ts)
            _to = [m.start() for m in re.finditer(b">tail", _tr)]
            if len(_to) >= 2:
                _lc = _tr[_to[-2]+5:_to[-1]+10]
                page_count = len(re.findall(rb"<PAGE\d+:\d+>", _lc))
            else:
                page_count = 0
        print(f"  Detected {page_count} pages from binary reader.")
    else:
        page_count = len(notebook.pages)

    _t1 = time.time() - _t
    print(f" done.  ({page_count} pages)  [{_t1:.1f}s  |  {_t1/max(page_count,1)*1000:.0f}ms/page]")

    # ── Step 2: Extract text, headings, keywords ─────────────────────────────
    _t = time.time()
    print("[2/3] Extracting text & annotations...", end='', flush=True)

    if notebook is None:
        text_layers = [""] * page_count
        pages_with_rec = 0
    else:
        text_converter = TextConverter(notebook)
        text_layers    = []
        pages_with_rec = 0
        for i in range(page_count):
            try:
                page_text = text_converter.convert(i) or ""
            except Exception:
                page_text = ""
            text_layers.append(page_text)
            if page_text.strip():
                pages_with_rec += 1
        # Fallback to direct binary RECOGNTEXT reader for newer firmware
        if pages_with_rec == 0:
            text_layers = _extract_recogntext(note_path, page_count)

    textbox_dict = extract_textbox_text(note_path, page_count)
    for page_idx, tb_texts in textbox_dict.items():
        while len(text_layers) <= page_idx:
            text_layers.append("")
        if tb_texts:
            combined = text_layers[page_idx]
            if combined.strip():
                combined += "\n\n" + "\n".join(tb_texts)
            else:
                combined = "\n".join(tb_texts)
            text_layers[page_idx] = combined

    headings = extract_headings(note_path)
    keywords = extract_keywords(note_path)

    # Filter to test page if requested
    if test_page is not None:
        tp = test_page - 1
        text_layers = [text_layers[i] if i == tp else "" for i in range(len(text_layers))]
        headings    = {k: v for k, v in headings.items() if k == tp}
        keywords    = {k: v for k, v in keywords.items() if k == tp}

    pages_with_text = sum(1 for t in text_layers if t.strip())
    n_headings = sum(len(v) for v in headings.values())
    n_keywords = sum(len(v) for v in keywords.values())
    n_stars    = sum(1 for v in headings.values() if any(h['text'] == '★' for h in v))
    _t2 = time.time() - _t
    print(f" done.  [{_t2:.1f}s  |  {_t2/max(page_count,1)*1000:.0f}ms/page]")
    print(f"        {pages_with_text} pages with text  |  {len(textbox_dict)} textboxes  "
          f"|  {n_headings} headings  |  {n_stars} starred  |  {n_keywords} keywords")

    if test_page is not None:
        print(f"\n[3/3] Skipping PDF write (test mode).")
        print(f"\n✓ Total : {time.time()-_t_total:.1f}s\n")
        return

    # ── Step 3: Render PDF + embed text, bookmarks, links ────────────────────
    _t = time.time()
    done_flag = threading.Event()

    def spinner():
        avg   = 1.5
        est   = page_count * avg
        t0    = time.time()
        chars = ['|', '/', '-', '\\']
        i = 0
        while not done_flag.is_set():
            el  = time.time() - t0
            ep  = min(int(el / avg) + 1, page_count)
            pct = min(int(el / est * 100), 99) if est > 0 else 0
            print(f"\r[3/3] Rendering... {chars[i%4]}  page ~{ep}/{page_count}  ({pct}%)",
                  end='', flush=True)
            i += 1
            time.sleep(0.15)

    if notebook is None:
        pdf_bytes = None
        print("[3/3] Skipping PDF render (unsupported file version).")
    else:
        threading.Thread(target=spinner, daemon=True).start()
        converter = PdfConverter(notebook)
        _mw = min(max_workers, os.cpu_count() or 4)
        try:
            pdf_bytes = converter.convert(-1, False, enable_link=True, max_workers=_mw)
        except TypeError:
            pdf_bytes = converter.convert(-1, False, enable_link=True)
        done_flag.set()
        time.sleep(0.2)

    _t3r = time.time() - _t
    print(f"\r[3/3] Rendering...    all {page_count} pages done.  [{_t3r:.1f}s  |  {_t3r/max(page_count,1)*1000:.0f}ms/page]")

    # Extract and inject internal tap-target links
    note_links = extract_external_links(note_path, page_count)

    # Embed searchable text layer + PDF outline (bookmarks)
    if pdf_bytes is not None:
        print("[3/3] Embedding text & bookmarks...", end='', flush=True)
        if pages_with_text or headings:
            try:
                pdf_bytes = build_pdf_with_toc(pdf_bytes, text_layers, headings, keywords)
                n_toc = sum(len(v) for v in headings.values())
                print(f"\n  Bookmarks: {n_toc} heading(s) added to PDF outline", flush=True)
            except ImportError as e:
                print(f"\n  Warning: could not embed ({e}). Run: pip install pypdf reportlab")
            except Exception as e:
                print(f"\n  Warning: embedding failed ({type(e).__name__}: {e})")
        else:
            print(f"\n  No headings or text to embed.")

        # Inject tap-target links as PDF annotations
        if note_links:
            try:
                from pypdf import PdfReader, PdfWriter
                _r = PdfReader(io.BytesIO(pdf_bytes))
                _w = PdfWriter()
                _w.clone_document_from_reader(_r)
                inject_pdf_links(_w, note_links, page_count)
                _out = io.BytesIO(); _w.write(_out)
                pdf_bytes = _out.getvalue()
                n_links = sum(len(v) for v in note_links.values())
                print(f"\n  Links: {n_links} tap targets injected as PDF annotations")
            except Exception as _e:
                print(f"\n  Warning: link injection failed ({_e})")

        # Optional JPEG compression
        if quality:
            try:
                from PIL import Image as PILImage
                from pypdf import PdfReader, PdfWriter
                reader = PdfReader(io.BytesIO(pdf_bytes))
                writer = PdfWriter()
                writer.clone_document_from_reader(reader)
                for page in writer.pages:
                    xobjs = page.get('/Resources', {}).get('/XObject', {})
                    if hasattr(xobjs, 'get_object'): xobjs = xobjs.get_object()
                    for name in (xobjs or {}):
                        obj = xobjs[name].get_object()
                        if obj.get('/Subtype') == '/Image':
                            try:
                                d = obj.get_data()
                                w = int(obj['/Width']); h = int(obj['/Height'])
                                mode = 'L' if obj.get('/ColorSpace') == '/DeviceGray' else 'RGB'
                                img  = PILImage.frombytes(mode, (w, h), d)
                                buf  = io.BytesIO()
                                img.save(buf, 'JPEG', quality=quality, optimize=True)
                                obj.set_data(buf.getvalue())
                                obj['/Filter'] = '/DCTDecode'
                            except Exception:
                                pass
                out = io.BytesIO(); writer.write(out); pdf_bytes = out.getvalue()
            except Exception as e:
                print(f"\n  Warning: compression failed ({e})")

        with open(pdf_path, 'wb') as fout:
            fout.write(pdf_bytes)
        _t3 = time.time() - _t
        print(f" done.  [{_t3:.1f}s]")
    else:
        print(f"\n  Skipped PDF write (no renderable content).")

    _total = time.time() - _t_total
    print(f"\n{'='*50}")
    if pdf_bytes is not None:
        size_mb = len(pdf_bytes) / (1024 * 1024)
        print(f"✓ PDF    → output/{note_name}.pdf  ({size_mb:.1f} MB)")
    else:
        print(f"⚠ PDF    → not created (unsupported file version)")
    print(f"✓ Total  : {_total//60:.0f}m {_total%60:.1f}s  ({_total/max(page_count,1)*1000:.0f}ms/page)")
    if pages_with_text:
        print(f"  Searchable text : {pages_with_text}/{page_count} pages")
    if n_headings:
        print(f"  Bookmarks       : {n_headings} heading(s), {n_stars} starred")
    if note_links:
        print(f"  Internal links  : {sum(len(v) for v in note_links.values())} tap targets")
    print(f"{'='*50}\n")


# ── Quick TOC ─────────────────────────────────────────────────────────────────

def print_toc(note_path: str):
    print(f"\nScanning: {os.path.basename(note_path)}")
    print(f"{'='*50}")

    headings = extract_headings(note_path)
    keywords = extract_keywords(note_path)

    if not headings and not keywords:
        print("  No headings, stars, or keywords found.")
        return

    all_pages  = sorted(set(list(headings.keys()) + list(keywords.keys())))
    n_headings = sum(len(v) for v in headings.values())
    n_stars    = sum(1 for v in headings.values() if any(h['text'] == '★' for h in v))
    n_keywords = sum(len(v) for v in keywords.values())

    print(f"  {n_headings} heading(s)  |  {n_stars} starred  |  {n_keywords} keyword(s)  across {len(all_pages)} page(s)")
    print(f"{'='*50}\n")

    for page_idx in all_pages:
        has_star   = any(h['text'] == '★' for h in headings.get(page_idx, []))
        page_heads = [h for h in headings.get(page_idx, []) if h['text'] != '★']
        page_kws   = keywords.get(page_idx, [])
        star_pfx   = "★ " if has_star else "  "

        if page_heads:
            for h in page_heads:
                indent      = "  " * max(0, h['level'] - 1)
                placeholder = " (?)" if h.get('placeholder') else ""
                print(f"  p.{page_idx + 1:<4d}  {star_pfx}{indent}{h['text']}{placeholder}")
                star_pfx = "  "
        elif has_star:
            print(f"  p.{page_idx + 1:<4d}  ★")

        if page_kws:
            print(f"              keywords: {', '.join(f'[{k}]' for k in page_kws)}")

    if keywords:
        print(f"\n{'='*50}")
        print("KEYWORDS INDEX")
        print(f"{'='*50}")
        term_pages = {}
        for page_idx, kws in sorted(keywords.items()):
            for kw in kws:
                term_pages.setdefault(kw, []).append(page_idx + 1)
        for term in sorted(term_pages, key=lambda t: t.lower()):
            print(f"  {term:<30}  {', '.join(f'p.{p}' for p in term_pages[term])}")
        print()


# ── Textbox search ────────────────────────────────────────────────────────────

def search_textboxes(note_path: str, terms: list) -> dict:
    results = {}
    terms_lower = [t.lower() for t in terms]
    try:
        f = open(note_path, 'rb')
        f.seek(0, 2)
        file_size = f.tell()
        last_chunk, _ = _get_last_chunk(f, file_size)

        def read_int(pos, n=4):
            d = _read_chunk(f, pos, n)
            return int.from_bytes(d, 'little') if len(d) >= n else 0

        for page_idx in range(10_000):
            m = re.search(rb'<PAGE' + str(page_idx + 1).encode() + rb':(\d+)>', last_chunk)
            if not m:
                break
            page_meta = _read_chunk(f, int(m.group(1)), 3000)
            tp_m = re.search(rb'<TOTALPATH:(\d+)>', page_meta)
            if not tp_m: continue
            tp_addr = int(tp_m.group(1))
            if tp_addr == 0: continue
            nstrokes = read_int(tp_addr + 4)
            if nstrokes <= 0 or nstrokes > 100_000: continue
            a = tp_addr + 8
            seen_ts = set()
            for _ in range(nstrokes):
                ssz = read_int(a)
                if ssz <= 0 or ssz > 10_000_000: break
                peek = _read_chunk(f, a, min(ssz + 4, 512))
                if bytes.fromhex(TEXT_BLOCK_SIGNATURE) in peek or ssz > 512:
                    decoded = _decode_textbox_streaming(f, a, ssz)
                    if (len(decoded) > TEXTBOX_CONTENT_IDX and
                            decoded[TITLE_STYLE_IDX] == '0'):
                        text = decoded[TEXTBOX_CONTENT_IDX].strip()
                        ts   = decoded[3] if len(decoded) > 3 else ''
                        if text and text.lower() != 'none' and ts not in seen_ts:
                            seen_ts.add(ts)
                            for term, tl in zip(terms, terms_lower):
                                if tl in text.lower():
                                    results.setdefault(page_idx, []).append((term, text[:80]))
                a += ssz + 4
        f.close()
    except Exception as e:
        print(f"\n  Warning: search failed ({e})")
    return results


def print_search(note_path: str, terms: list):
    print(f"\nSearching for: {', '.join(repr(t) for t in terms)}")
    print(f"{'='*50}")
    hits = search_textboxes(note_path, terms)
    if not hits:
        print("  No matches found.\n")
        return
    total = sum(len(v) for v in hits.values())
    print(f"  {total} match(es) across {len(hits)} page(s)\n")
    for page_idx in sorted(hits):
        for term, snippet in hits[page_idx]:
            print(f"  p.{page_idx + 1:<5}  [{term}]  {repr(snippet)}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Supernote .note → PDF  (with bookmarks, searchable text & links)")
    parser.add_argument("note_file", nargs="?", help="Path to .note file")
    parser.add_argument("--toc",     action="store_true",
                        help="Print TOC / keyword index only (no conversion)")
    parser.add_argument("--search",  nargs="+", metavar="TERM",
                        help="Search textboxes, e.g. --search noom 'project recovery'")
    parser.add_argument("--page",    type=int, default=None, metavar="N",
                        help="Test mode: process page N only (skips PDF write)")
    parser.add_argument("--workers", type=int, default=4, metavar="N",
                        help="PDF render workers (default 4, reduce if crashes)")
    parser.add_argument("--quality", type=int, default=None, metavar="N",
                        help="JPEG quality 1-95 for PDF compression (e.g. 60)")
    parser.add_argument("--out", default=None, metavar="FOLDER",
                        help="Output folder (default: ./output beside the .note file)")
    args = parser.parse_args()

    if args.note_file:
        note_path = args.note_file
    else:
        desktop    = os.path.join(os.path.expanduser("~"), "Desktop")
        note_files = [f for f in os.listdir(desktop) if f.endswith('.note')]
        if len(note_files) == 1:
            note_path = os.path.join(desktop, note_files[0])
        else:
            parser.print_help()
            sys.exit(1)

    if args.toc:
        print_toc(note_path)
    elif args.search:
        print_search(note_path, args.search)
    else:
        convert_note_to_pdf(note_path, max_workers=args.workers,
                            quality=args.quality, test_page=args.page,
                            out_dir=args.out)