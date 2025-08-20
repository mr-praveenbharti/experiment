# app.py — HP e-Gazette Live (Streamlit)
# - One file: includes scraper + UI
# - Pager-aware, dedupe, header validation, robust retries
# - Nice table via st-aggrid if installed (fallback to st.dataframe)
# - Optional auto-refresh (streamlit-autorefresh)

from __future__ import annotations
import re, csv, io, sys, time, logging
from typing import List, Dict, Tuple, Optional

import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# ---------- Robust Retry import (urllib3 v1/v2 or vendored) ----------
Retry = None
try:
    from urllib3.util.retry import Retry as _Retry
    Retry = _Retry
except Exception:
    try:
        from requests.packages.urllib3.util.retry import Retry as _RetryVendored
        Retry = _RetryVendored
    except Exception:
        Retry = None  # retries unavailable; proceed without

# ---------- Constants ----------
BASE_URL = "https://rajpatrahimachal.nic.in/LNotific.aspx"
GRID_ID  = "ContentPlaceHolder1_GVNotification"
HTTP_TIMEOUT = 30  # seconds

# persistent session with UA + retries
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; egazette/1.0)"})

def setup_retries(sess, total=5, backoff_factor=0.5,
                  statuses=(429, 500, 502, 503, 504)):
    if Retry is None:  # no retries available
        return
    kw = dict(total=total, read=total, connect=total, status=total,
              backoff_factor=backoff_factor, status_forcelist=statuses)
    try:
        retry = Retry(**kw, allowed_methods=frozenset(["GET","POST"]))  # newer urllib3
    except TypeError:
        retry = Retry(**kw, method_whitelist=frozenset(["GET","POST"]))  # older urllib3
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

setup_retries(session)

# ---------- Logging (to sidebar text area) ----------
class StreamlitLogHandler(logging.Handler):
    def __init__(self, buffer_key="log_buffer"):
        super().__init__()
        self.buffer_key = buffer_key
        if self.buffer_key not in st.session_state:
            st.session_state[self.buffer_key] = []

    def emit(self, record):
        try:
            msg = self.format(record)
            st.session_state[self.buffer_key].append(msg)
            # keep buffer small
            if len(st.session_state[self.buffer_key]) > 400:
                st.session_state[self.buffer_key] = st.session_state[self.buffer_key][-400:]
        except Exception:
            pass

def get_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("egazette")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    # attach only once
    if not any(isinstance(h, StreamlitLogHandler) for h in logger.handlers):
        h = StreamlitLogHandler()
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        h.setFormatter(fmt)
        logger.addHandler(h)
        logger.propagate = False
    # quiet libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return logger

# ---------- HTML parsing helpers ----------
def soupify(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")

def extract_hidden_fields(soup: BeautifulSoup) -> Dict[str, str]:
    fields = {}
    for tag in soup.find_all("input", type="hidden"):
        name = tag.get("name")
        if name:
            fields[name] = tag.get("value", "")
    return fields

def parse_headers(soup: BeautifulSoup) -> List[str]:
    table = soup.find("table", {"id": GRID_ID})
    if not table: return []
    header_tr = table.find("tr")
    if not header_tr: return []
    return [cell.get_text(strip=True) for cell in header_tr.find_all(["th", "td"])]

def is_pager_row(tr) -> bool:
    if tr.find("a", href=re.compile(r"Page\$\d+")): return True
    if tr.find("span", string=re.compile(r"^\d+$")): return True
    return False

def parse_table_rows(soup: BeautifulSoup) -> List[List[str]]:
    table = soup.find("table", {"id": GRID_ID})
    if not table: return []
    rows = []
    for i, tr in enumerate(table.find_all("tr")):
        if i == 0:   # header
            continue
        if is_pager_row(tr):
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        rows.append([td.get_text(strip=True) for td in tds])
    return rows

def page_signature(rows: List[List[str]]) -> Optional[Tuple[str, ...]]:
    return tuple(rows[0]) if rows else None

def extract_popup_url(html: str) -> str:
    m = re.search(r"window\.open\('([^']+)'", html)
    if not m: return ""
    rel = m.group(1)
    return rel if rel.startswith("http") else "https://rajpatrahimachal.nic.in/" + rel.lstrip("/")

def merge_headers_if_needed(base_headers: List[str], soup: BeautifulSoup) -> List[str]:
    page_headers = parse_headers(soup)
    if not page_headers:
        return base_headers
    has_popup = "Popup URL" in base_headers
    core = [h for h in base_headers if h != "Popup URL"]
    if page_headers != core:
        merged = core[:]
        for h in page_headers:
            if h not in merged:
                merged.append(h)
        if has_popup:
            merged.append("Popup URL")
        return merged
    return base_headers

def find_next_page_arg(soup: BeautifulSoup, current_page: int) -> Optional[str]:
    table = soup.find("table", {"id": GRID_ID})
    if not table: return None
    next_num = None
    for tr in table.find_all("tr"):
        if is_pager_row(tr):
            # numeric links first
            for a in tr.find_all("a", href=True):
                m = re.search(r"Page\$(\d+)", a["href"])
                if not m: continue
                n = int(m.group(1))
                if n > current_page and (next_num is None or n < next_num):
                    next_num = n
            if next_num is not None:
                return f"Page${next_num}"
            # Next / > / >>
            for a in tr.find_all("a", href=True):
                if a.get_text(strip=True).lower() in {"next", ">", ">>"}:
                    m = re.search(r"Page\$(\d+)", a["href"])
                    if m: return f"Page${int(m.group(1))}"
            break
    return None

# ---------- HTTP helpers ----------
def safe_get(url: str, logger: logging.Logger) -> Optional[requests.Response]:
    try:
        logger.debug(f"GET {url}")
        r = session.get(url, timeout=HTTP_TIMEOUT)
        logger.debug(f"GET -> {r.status_code} ({len(r.content)} bytes)")
        return r
    except requests.RequestException as e:
        logger.error(f"GET failed: {e}")
        return None

def safe_post(url: str, data: Dict[str, str], logger: logging.Logger) -> Optional[requests.Response]:
    ev_t = data.get("__EVENTTARGET")
    ev_a = data.get("__EVENTARGUMENT")
    logger.debug(f"POST {url} | EVENTTARGET={ev_t} EVENTARG={ev_a}")
    try:
        r = session.post(url, data=data, timeout=HTTP_TIMEOUT)
        logger.debug(f"POST -> {r.status_code} ({len(r.content)} bytes)")
        return r
    except requests.RequestException as e:
        logger.error(f"POST failed ({ev_t}:{ev_a}): {e}")
        return None

# ---------- Scraper (returns dept_name, DataFrame) ----------
def scrape_department(dept_id: int, verbose: bool=False) -> Tuple[str, pd.DataFrame]:
    logger = get_logger(verbose)

    url = f"{BASE_URL}?ID={dept_id}"
    r = safe_get(url, logger)
    if not r or r.status_code != 200:
        raise RuntimeError("Initial GET failed")
    soup = soupify(r.text)

    name_tag = soup.find("span", {"id": "ContentPlaceHolder1_lbldepart"})
    dept_name = name_tag.get_text(strip=True) if name_tag else f"Dept_{dept_id}"
    logger.info(f"Department: {dept_name} (ID {dept_id})")

    # Reveal grid
    payload = extract_hidden_fields(soup)
    payload["__EVENTTARGET"]   = "ctl00$ContentPlaceHolder1$Lnktosite"
    payload["__EVENTARGUMENT"] = ""
    r = safe_post(url, data=payload, logger=logger)
    if not r or r.status_code != 200:
        raise RuntimeError("Grid reveal failed")
    soup = soupify(r.text)

    if not soup.find("table", {"id": GRID_ID}):
        raise RuntimeError("Grid not found for this department")

    headers = parse_headers(soup) or []
    if "Popup URL" not in headers:
        headers.append("Popup URL")
    logger.debug(f"Initial headers: {headers}")

    page = 1
    seen_rows_global = set()
    records: List[Dict[str, str]] = []

    while True:
        logger.info(f"Page {page}…")
        headers = merge_headers_if_needed(headers, soup)

        rows = parse_table_rows(soup)
        if not rows:
            logger.info("No rows; stopping.")
            break

        # dedupe page + global without breaking Select$idx
        page_seen = set()
        unique_entries: List[Tuple[int, List[str]]] = []
        dup_count = 0
        for idx, cols in enumerate(rows):
            sig = tuple(cols)
            if sig in page_seen or sig in seen_rows_global:
                dup_count += 1
                continue
            page_seen.add(sig); seen_rows_global.add(sig)
            unique_entries.append((idx, cols))
        logger.info(f"rows={len(rows)} unique={len(unique_entries)} skipped_dups={dup_count}")

        # select each row to capture popup url
        for idx, cols in unique_entries:
            p2 = extract_hidden_fields(soup)
            p2["__EVENTTARGET"]   = "ctl00$ContentPlaceHolder1$GVNotification"
            p2["__EVENTARGUMENT"] = f"Select${idx}"
            detail = safe_post(url, data=p2, logger=logger)
            popup = extract_popup_url(detail.text) if (detail and detail.status_code == 200) else ""

            # map / pad to headers
            base_cols = len(headers) - 1
            rec = {headers[i]: (cols[i] if i < len(cols) else "") for i in range(base_cols)}
            rec["Popup URL"] = popup
            records.append(rec)

        # next page (pager-aware, fallback blind)
        prev_sig = page_signature(rows)
        pnext = extract_hidden_fields(soup)
        pnext["__EVENTTARGET"]   = "ctl00$ContentPlaceHolder1$GVNotification"
        arg = find_next_page_arg(soup, page) or f"Page${page+1}"
        pnext["__EVENTARGUMENT"] = arg
        logger.info(f"Next: {arg}")

        r_next = safe_post(url, data=pnext, logger=logger)
        if not r_next or r_next.status_code != 200:
            logger.info("Pagination failed or non-200; stopping.")
            break

        soup_next = soupify(r_next.text)
        rows_next = parse_table_rows(soup_next)
        sig_next  = page_signature(rows_next)
        if sig_next is None or sig_next == prev_sig:
            logger.info("Reached last page.")
            break

        soup = soup_next
        page += 1

    df = pd.DataFrame.from_records(records, columns=headers) if records else pd.DataFrame(columns=headers)
    return dept_name, df

# ---------- Streamlit UI ----------
st.set_page_config(page_title="HP e-Gazette — Live", layout="wide")
st.title("📜 Himachal Pradesh e-Gazette — Live")
st.caption("Scrape notifications per department and view them in a dynamic, Excel-like table.")

with st.sidebar:
    st.header("Controls")
    dept_id  = st.number_input("Department ID", min_value=1, value=1, step=1)
    verbose  = st.checkbox("Verbose logs", value=False)
    # Optional auto-refresh if helper is available
    auto     = st.checkbox("Auto-refresh", value=False)
    interval_min = st.number_input("Refresh every (minutes)", min_value=1, value=5, step=1, disabled=not auto)
    # Cache control
    col_a, col_b = st.columns(2)
    with col_a:
        run_btn = st.button("🔄 Scrape now", type="primary", use_container_width=True)
    with col_b:
        clear_cache = st.button("♻️ Clear Cache", use_container_width=True)
    st.markdown("---")
    st.subheader("Logs")
    log_box = st.empty()

# auto-refresh if library available
if auto:
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=interval_min * 60 * 1000, key="egz_autorefresh")
    except Exception:
        st.info("Install `streamlit-autorefresh` for auto-refresh: `pip install streamlit-autorefresh`")

# cache wrapper (5 min default)
@st.cache_data(ttl=300, show_spinner=False)
def cached_scrape(_dept_id: int, _verbose: bool) -> Tuple[str, pd.DataFrame]:
    return scrape_department(_dept_id, verbose=_verbose)

if clear_cache:
    cached_scrape.clear()

# run scrape initially or on demand
if run_btn or "egz_data" not in st.session_state:
    try:
        with st.spinner("Scraping…"):
            dept_name, df = cached_scrape(int(dept_id), verbose)
        st.session_state["egz_data"] = (dept_name, df)
    except Exception as e:
        st.error(f"Scrape failed: {e}")
        st.stop()

# show logs in sidebar (if any)
if "log_buffer" in st.session_state and st.session_state["log_buffer"]:
    log_text = "\n".join(st.session_state["log_buffer"][-120:])
    log_box.text_area("Recent log", log_text, height=220)

# display results
dept_name, df = st.session_state["egz_data"]
st.subheader(f"Department: {dept_name}  (ID: {dept_id})")

if df.empty:
    st.warning("No rows found for this department yet.")
else:
    # Preferred: st-aggrid if installed
    used_aggrid = False
    try:
        from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
        gb = GridOptionsBuilder.from_dataframe(df)
        gb.configure_default_column(resizable=True, filter=True, sortable=True, min_column_width=120)
        gb.configure_grid_options(domLayout="normal")
        go = gb.build()
        st.write("**Table** (Excel-like: sortable, filterable)")
        AgGrid(df, gridOptions=go, update_mode=GridUpdateMode.NO_UPDATE, height=560, fit_columns_on_grid_load=True)
        used_aggrid = True
    except Exception:
        pass

    if not used_aggrid:
        st.write("**Table**")
        st.dataframe(df, use_container_width=True, height=560)

    # Downloads
    left, mid, right = st.columns(3)
    with left:
        st.download_button(
            "⬇️ Download CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"egazette_{dept_name}_{dept_id}.csv",
            mime="text/csv",
            use_container_width=True
        )
    with mid:
        # Styled HTML (sticky header, zebra)
        html_table = df.to_html(index=False, border=0, classes="table", escape=False)
        html_full = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>e-Gazette — {dept_name}</title>
<style>
  body {{ font-family: Inter, Segoe UI, system-ui, Arial; margin: 16px; }}
  .wrap {{ overflow:auto; max-height:75vh; border:1px solid #e2e8f0; border-radius:8px; }}
  table {{ border-collapse:collapse; width:max-content; max-width:100%; }}
  th, td {{ border:1px solid #e2e8f0; padding:6px 10px; font-size:13px; white-space:nowrap; }}
  thead th {{ position:sticky; top:0; background:#f8fafc; border-bottom:2px solid #cbd5e1; z-index:2; }}
  tbody tr:nth-child(even) {{ background:#fcfdff; }}
</style></head><body>
<h2>e-Gazette — {dept_name} (ID: {dept_id})</h2>
<div class="wrap">{html_table}</div>
</body></html>"""
        st.download_button(
            "⬇️ Download styled HTML",
            data=html_full.encode("utf-8"),
            file_name=f"egazette_{dept_name}_{dept_id}.html",
            mime="text/html",
            use_container_width=True
        )
    with right:
        # Optional XLSX
        xlsx_buf = io.BytesIO()
        with pd.ExcelWriter(xlsx_buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Notifications")
            # Light formatting
            wb  = writer.book
            ws  = writer.sheets["Notifications"]
            fmt = wb.add_format({"text_wrap": False, "font_size": 11})
            ws.set_column(0, len(df.columns)-1, 24, fmt)
        st.download_button(
            "⬇️ Download Excel (.xlsx)",
            data=xlsx_buf.getvalue(),
            file_name=f"egazette_{dept_name}_{dept_id}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

st.caption("Tip: install **st-aggrid** for the rich grid, and **streamlit-autorefresh** for timed refreshes.")
