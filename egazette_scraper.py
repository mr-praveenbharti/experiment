# egazette_scraper.py
from __future__ import annotations
import re, csv, logging, requests
from typing import List, Dict, Tuple, Optional
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

# --- robust Retry import for urllib3 v1/v2 / vendored ---
Retry = None
try:
    from urllib3.util.retry import Retry as _Retry
    Retry = _Retry
except Exception:
    try:
        from requests.packages.urllib3.util.retry import Retry as _RetryVendored
        Retry = _RetryVendored
    except Exception:
        Retry = None

BASE_URL = "https://rajpatrahimachal.nic.in/LNotific.aspx"
GRID_ID = "ContentPlaceHolder1_GVNotification"
HTTP_TIMEOUT = 30

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

def _setup_retries(sess, total=5, backoff_factor=0.5,
                   statuses=(429,500,502,503,504)):
    if Retry is None: return
    kw = dict(total=total, read=total, connect=total, status=total,
              backoff_factor=backoff_factor, status_forcelist=statuses)
    try:
        retry = Retry(**kw, allowed_methods=frozenset(["GET","POST"]))
    except TypeError:
        retry = Retry(**kw, method_whitelist=frozenset(["GET","POST"]))
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter); sess.mount("https://", adapter)

_setup_retries(session)
log = logging.getLogger("egazette")

# ---- low-level helpers ----
def _soup(html:str) -> BeautifulSoup: return BeautifulSoup(html, "html.parser")

def _hidden(soup:BeautifulSoup) -> Dict[str,str]:
    d={}
    for tag in soup.find_all("input", type="hidden"):
        name=tag.get("name")
        if name: d[name]=tag.get("value","")
    return d

def _headers(soup:BeautifulSoup) -> List[str]:
    t=soup.find("table", {"id": GRID_ID})
    if not t: return []
    head=t.find("tr")
    if not head: return []
    return [c.get_text(strip=True) for c in head.find_all(["th","td"])]

def _is_pager(tr) -> bool:
    if tr.find("a", href=re.compile(r"Page\$\d+")): return True
    if tr.find("span", string=re.compile(r"^\d+$")): return True
    return False

def _rows(soup:BeautifulSoup) -> List[List[str]]:
    t=soup.find("table", {"id": GRID_ID})
    if not t: return []
    out=[]
    for i,tr in enumerate(t.find_all("tr")):
        if i==0: continue
        if _is_pager(tr): continue
        tds=tr.find_all("td")
        if not tds: continue
        out.append([td.get_text(strip=True) for td in tds])
    return out

def _sig(rows:List[List[str]]): return tuple(rows[0]) if rows else None

def _popup(html:str) -> str:
    m=re.search(r"window\.open\('([^']+)'", html)
    if not m: return ""
    rel=m.group(1)
    return rel if rel.startswith("http") else "https://rajpatrahimachal.nic.in/"+rel.lstrip("/")

def _next_arg(soup:BeautifulSoup, page:int) -> Optional[str]:
    t=soup.find("table", {"id": GRID_ID})
    if not t: return None
    next_num=None
    for tr in t.find_all("tr"):
        if _is_pager(tr):
            for a in tr.find_all("a", href=True):
                m=re.search(r"Page\$(\d+)", a["href"])
                if not m: continue
                n=int(m.group(1))
                if n>page and (next_num is None or n<next_num):
                    next_num=n
            if next_num is not None: return f"Page${next_num}"
            for a in tr.find_all("a", href=True):
                if a.get_text(strip=True).lower() in {"next",">",">>"}:
                    m=re.search(r"Page\$(\d+)", a["href"])
                    if m: return f"Page${int(m.group(1))}"
            break
    return None

def _get(url, **kw):
    try: return session.get(url, timeout=HTTP_TIMEOUT, **kw)
    except requests.RequestException: return None

def _post(url, data, **kw):
    try: return session.post(url, data=data, timeout=HTTP_TIMEOUT, **kw)
    except requests.RequestException: return None

# ---- public API ----
def scrape_department(dept_id:int, verbose:bool=False) -> Dict[str,object]:
    """
    Returns dict: { 'dept_name': str, 'headers': [..], 'rows': [ {col:val,..}, ... ] }
    """
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    url=f"{BASE_URL}?ID={dept_id}"
    r=_get(url); 
    if not r or r.status_code!=200: raise RuntimeError("Initial GET failed")
    s=_soup(r.text)
    name_tag=s.find("span", {"id":"ContentPlaceHolder1_lbldepart"})
    dept_name=name_tag.get_text(strip=True) if name_tag else f"Dept_{dept_id}"

    # reveal grid
    p=_hidden(s); p["__EVENTTARGET"]="ctl00$ContentPlaceHolder1$Lnktosite"; p["__EVENTARGUMENT"]=""
    r=_post(url,p); 
    if not r or r.status_code!=200: raise RuntimeError("Grid reveal failed")
    s=_soup(r.text)
    if not s.find("table", {"id": GRID_ID}): raise RuntimeError("Grid not found")

    headers=_headers(s) or []
    if "Popup URL" not in headers: headers.append("Popup URL")

    page=1; seen=set(); records=[]
    while True:
        rows=_rows(s)
        if not rows: break
        sig_cur=_sig(rows)

        # dedupe (page+global) without breaking Select$idx
        page_seen=set(); unique=[]
        for idx, cols in enumerate(rows):
            key=tuple(cols)
            if key in page_seen or key in seen: continue
            page_seen.add(key); seen.add(key); unique.append((idx, cols))

        # select each row to grab popup url
        for idx, cols in unique:
            p2=_hidden(s); p2["__EVENTTARGET"]="ctl00$ContentPlaceHolder1$GVNotification"; p2["__EVENTARGUMENT"]=f"Select${idx}"
            d=_post(url,p2); popup=_popup(d.text) if (d and d.status_code==200) else ""
            base=len(headers)-1
            rec={headers[i]: (cols[i] if i<len(cols) else "") for i in range(base)}
            rec["Popup URL"]=popup
            records.append(rec)

        # pager-aware next
        pnext=_hidden(s); pnext["__EVENTTARGET"]="ctl00$ContentPlaceHolder1$GVNotification"
        arg=_next_arg(s,page) or f"Page${page+1}"
        pnext["__EVENTARGUMENT"]=arg
        r=_post(url,pnext)
        if not r or r.status_code!=200: break
        s_next=_soup(r.text); rows_next=_rows(s_next); sig_next=_sig(rows_next)
        if sig_next is None or sig_next==sig_cur: break
        s=s_next; page+=1

    return {"dept_name": dept_name, "headers": headers, "rows": records}
