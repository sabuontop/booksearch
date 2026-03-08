import requests
from bs4 import BeautifulSoup
import os
import re
import urllib.parse
import json
import subprocess
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import secrets
from functools import wraps
from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=5)
CORS(app)

# Rate limiter — protège contre le brute force
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://"
)

# Config
DOWNLOAD_DIR = os.environ.get('BOOKLORE_DIR', '/srv/booklore/bookdrop')
CONFIG_FILE = os.environ.get('CONFIG_FILE', 'config.json')
SEARCH_PASSWORD = os.environ.get('SEARCH_PASSWORD', 'Sabuuu92i@08')

# ── Auth ──────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Non autorisé'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)


# ── BookDownloader ─────────────────────────────────────────────
class BookDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept-Language': 'fr,fr-FR;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        self.session.headers.update(self.headers)
        self.annas_domains = [
            "https://fr.annas-archive.pk",
            "https://fr.annas-archive.org",
            "https://fr.annas-archive.vg",
            "https://fr.annas-archive.gd"
        ]
        self.current_annas = self.annas_domains[0]

    def _get_cover(self, title):
        """Couverture OpenLibrary (gratuit, sans API key)"""
        return f"https://covers.openlibrary.org/b/title/{urllib.parse.quote(title)}-M.jpg"

    def search_annasarchive(self, query):
        results = []
        # Try multiple mirrors in order until one works
        mirrors = [
            "https://fr.annas-archive.se",
            "https://fr.annas-archive.gs",
            "https://fr.annas-archive.pk",
            "https://fr.annas-archive.org",
        ]
        for mirror in mirrors:
            try:
                r = self.session.get(f"{mirror}/search", params={'q': query}, timeout=15)
                if r.status_code != 200:
                    continue
                self.current_annas = mirror
                soup = BeautifulSoup(r.text, 'html.parser')

                # Anna's Archive uses Tailwind — rows are flex divs containing /md5/ links
                # Multiple valid selectors to catch current + future layouts
                items = soup.select('div.flex.pt-3.pb-3') or \
                        soup.select('div[class*="border-b"]') or \
                        [a.find_parent('div') for a in soup.find_all('a', href=re.compile(r'/md5/')) if a.find_parent('div')]

                seen_ids = set()
                for item in items[:30]:
                    if not item: continue
                    md5_link = item.find('a', href=re.compile(r'/md5/'))
                    if not md5_link: continue

                    md5 = md5_link['href'].split('/')[-1].split('?')[0]
                    if md5 in seen_ids: continue
                    seen_ids.add(md5)

                    # Title: look for h3 first, else use the link text
                    h3 = item.find('h3')
                    raw_title = h3.get_text(' ', strip=True) if h3 else md5_link.get_text(' ', strip=True)
                    raw_title = raw_title.strip()
                    if not raw_title or len(raw_title) < 2 or raw_title.lower() in ['save', 'lire plus…', 'read more…']:
                        continue

                    # Cover image
                    img = item.find('img')
                    cover_url = ''
                    if img:
                        src = img.get('src') or img.get('data-src') or ''
                        cover_url = src if src.startswith('http') else (mirror + src if src.startswith('/') else '')

                    # Metadata line — the gray info string "fr, epub, 1.2MB, ..."
                    info_raw = ''
                    for div in item.find_all('div'):
                        cls = ' '.join(div.get('class', []))
                        txt = div.get_text(' ', strip=True)
                        if any(k in cls for k in ['gray', 'muted', 'text-sm', 'overflow-hidden']) and any(f in txt.lower() for f in ['epub','pdf','mobi','mb','kb']):
                            info_raw = txt
                            break

                    # Parse: "French [fr], epub, 1.2MB, ..."
                    fmt, size, author = '', '', ''
                    if info_raw:
                        parts = [p.strip() for p in info_raw.split(',')]
                        for p in parts:
                            pl = p.lower()
                            if any(x in pl for x in ['epub','pdf','mobi','azw3']) and not fmt:
                                fmt = p.strip()
                            elif re.search(r'\d+\s*[kmg]b', pl) and not size:
                                size = p.strip()
                            elif '"' in p or "'" in p:
                                # Likely "Title" by Author pattern — extract author name
                                m = re.search(r'by (.+)$', p)
                                if m: author = m.group(1).strip()

                    info_parts = [x for x in [fmt.upper(), size, author] if x]
                    info = ' · '.join(info_parts) if info_parts else ''

                    # Clean title
                    clean = re.sub(r'\[.*?\]|\(.*?\)', '', raw_title).strip()
                    clean = clean.split('/')[-1].strip()

                    if not cover_url:
                        cover_url = self._get_cover(clean)

                    results.append({
                        'source': "Anna's Archive",
                        'title': clean,
                        'info': info,
                        'url': mirror + md5_link['href'],
                        'id': md5,
                        'coverUrl': cover_url
                    })

                if results:
                    break  # Got results from this mirror, stop trying others

            except Exception as e:
                print(f"Anna's Archive mirror {mirror} failed: {e}")
                continue

        def get_score(res):
            t = res.get('info', '').lower()
            if 'epub' in t: return 0
            if 'pdf' in t: return 1
            return 2
        results.sort(key=get_score)
        return results

    def search_libgen(self, query):
        results = []
        mirrors = [
            "https://libgen.li/index.php",
            "https://libgen.is/search.php",
            "https://libgen.rs/search.php",
        ]
        
        for mirror_url in mirrors:
            try:
                base = '/'.join(mirror_url.split('/')[:3])  # e.g. https://libgen.li
                params = {'req': query, 'column': 'def', 'res': 25}
                if 'libgen.is' in mirror_url or 'libgen.rs' in mirror_url:
                    params = {'req': query, 'column': 'def', 'res': 25, 'sort': 'def'}

                r = self.session.get(mirror_url, params=params, timeout=15, verify=False)
                if r.status_code != 200:
                    continue
                
                soup = BeautifulSoup(r.text, 'html.parser')

                # Try table id selectors in order of preference
                table = soup.find('table', id='tablelibgen') or \
                        soup.find('table', attrs={'class': re.compile(r'c$|catalog', re.I)}) or \
                        soup.find('table', id='search_res')

                if not table:
                    # Last resort: find largest table on page
                    tables = soup.find_all('table')
                    table = max(tables, key=lambda t: len(t.find_all('tr')), default=None) if tables else None

                if not table:
                    continue

                rows = table.find_all('tr')[1:]  # skip header
                if not rows:
                    continue

                seen_md5s = set()
                for tr in rows:
                    tds = tr.find_all('td')
                    if len(tds) < 5: continue

                    # Extract author (col 1), title (col 2)
                    author_td = tds[1] if len(tds) > 1 else None
                    title_td = tds[2] if len(tds) > 2 else tds[0]

                    author = author_td.get_text(' ', strip=True)[:80] if author_td else ''
                    title_link = title_td.find('a', href=re.compile(r'book/index|/md5/', re.I)) or title_td.find('a')
                    if not title_link: continue
                    
                    title_text = title_link.get_text(' ', strip=True)
                    title = re.sub(r'\s*[:\-]\s*volume.*', '', title_text, flags=re.I).strip()

                    # Extension and size: try common column positions
                    ext = size = ''
                    for col_idx in [8, 7, 6]:
                        if len(tds) > col_idx:
                            val = tds[col_idx].get_text(strip=True).lower()
                            if val in ('epub','pdf','mobi','azw3','djvu','fb2','txt','doc','docx'):
                                ext = val
                                break
                    for col_idx in [9, 8, 7]:
                        if len(tds) > col_idx:
                            val = tds[col_idx].get_text(strip=True)
                            if re.search(r'\d+\s*[kmg]b', val, re.I):
                                size = val
                                break

                    # Download link — find get.php or /get/ or actual md5 param
                    dl_link = None
                    for a in tr.find_all('a', href=True):
                        href = a['href']
                        if 'get.php' in href or '/get/' in href or 'md5=' in href.lower():
                            dl_link = href
                            break

                    if not dl_link: continue

                    # Make absolute URL
                    if dl_link.startswith('http'):
                        full_dl = dl_link
                    elif dl_link.startswith('/'):
                        full_dl = base + dl_link
                    else:
                        full_dl = base + '/' + dl_link

                    # Extract md5 for dedup
                    md5_match = re.search(r'md5=([a-f0-9]+)', dl_link, re.I)
                    md5 = md5_match.group(1).lower() if md5_match else dl_link

                    if md5 in seen_md5s: continue
                    seen_md5s.add(md5)

                    info_parts = [x.upper() for x in [ext] if x] + [size]
                    info = ' · '.join(p for p in info_parts if p)

                    results.append({
                        'source': 'LibGen',
                        'title': f"{title} — {author}" if author else title,
                        'info': info,
                        'url': full_dl,
                        'id': md5,
                        'coverUrl': self._get_cover(title)
                    })

                if results:
                    break  # Found results, stop trying mirrors

            except Exception as e:
                print(f"LibGen mirror {mirror_url} failed: {e}")
                continue

        return results

    def search_bookys(self, query):
        """Scrape bookys-ebooks.com — protégé par Cloudflare, utilise cloudscraper"""
        results = []
        base_urls = [
            "https://www6.bookys-ebooks.com",
            "https://www.bookys-ebooks.com",
            "https://bookys-ebooks.com",
        ]
        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'linux', 'desktop': True}
            )
            headers = {'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.5'}

            html = None
            base = base_urls[0]
            for b in base_urls:
                try:
                    r = scraper.get(f"{b}/?s={urllib.parse.quote(query)}", headers=headers, timeout=20)
                    if r.status_code == 200 and len(r.text) > 2000:
                        html = r.text
                        base = b
                        break
                except Exception as e:
                    print(f"Bookys {b}: {e}")
                    continue

            if not html:
                return results

            soup = BeautifulSoup(html, 'html.parser')

            # Multiple layout strategies:
            # 1. Standard WordPress articles (most common)
            articles = soup.select('article.post, article.type-post, article.hentry')
            # 2. bys- prefixed classes (newer layout)
            if not articles:
                articles = soup.select('[class*="bys-"]')
            # 3. Generic .news or .item classes
            if not articles:
                articles = soup.select('.news, .item, .book-item')
            # 4. Any article tag
            if not articles:
                articles = soup.find_all('article')
            # 5. Last fallback: divs containing internal links
            if not articles:
                articles = [a.find_parent('div') for a in soup.find_all('a', href=re.compile(f'^{re.escape(base)}/')) if a.find_parent('div')]
                articles = [a for a in articles if a][:20]

            seen_hrefs = set()
            for item in articles[:25]:
                # Get the main link
                # Prioritize the first internal link (not a category, not a tag)
                main_link = None
                for a in item.find_all('a', href=True):
                    href = a['href']
                    if not href.startswith('http'):
                        href = base + href
                    # Skip category/tag/author links
                    if any(x in href for x in ['/category/', '/tag/', '/author/', '#']):
                        continue
                    # Must point to the same domain
                    if any(b.replace('https://', '').replace('http://', '') in href for b in base_urls):
                        main_link = (a, href)
                        break
                
                if not main_link: continue
                a_tag, href = main_link
                if href in seen_hrefs: continue
                seen_hrefs.add(href)

                # Title
                title_el = item.find(['h1','h2','h3','h4'])
                if title_el:
                    title = title_el.get_text(' ', strip=True)
                else:
                    title = a_tag.get('title') or a_tag.get_text(' ', strip=True)
                title = re.sub(r'[\r\n\t]+', ' ', title).strip()
                if not title or len(title) < 3: continue

                # Cover image (try data-src for lazy loading, then src)
                img = item.find('img')
                cover_url = ''
                if img:
                    cover_url = img.get('data-lazy-src') or img.get('data-src') or img.get('src') or ''
                    if cover_url and not cover_url.startswith('http'):
                        cover_url = base + cover_url
                    # Skip base64 placeholders and tiny images
                    if cover_url.startswith('data:') or 'blank' in cover_url.lower():
                        cover_url = ''

                if not cover_url:
                    cover_url = self._get_cover(title)

                # Info: author, category, format
                info_parts = []
                for sel in ['.entry-meta', '.post-meta', '.news-meta', '[class*="meta"]', '[class*="author"]', '[class*="cat"]']:
                    el = item.select_one(sel)
                    if el:
                        txt = el.get_text(' ', strip=True)[:80]
                        if txt: info_parts.append(txt)
                        break
                # Also check for explicit format tags
                for tag_el in item.find_all(class_=re.compile(r'format|epub|pdf', re.I)):
                    t = tag_el.get_text(strip=True)
                    if t and t not in info_parts: info_parts.append(t)

                info = ' · '.join(info_parts) if info_parts else 'Ebook FR'

                results.append({
                    'source': 'Bookys',
                    'title': title,
                    'info': info,
                    'url': href,
                    'id': href,
                    'coverUrl': cover_url,
                    'bookys_direct': True
                })
        except ImportError:
            print("cloudscraper not installed — run: pip install cloudscraper")
        except Exception as e:
            print(f"Bookys Error: {e}")
        return results

    def get_annas_details(self, md5_url):
        try:
            r = self.session.get(md5_url, timeout=20)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                links = {'slow': [], 'external': []}
                for a in soup.find_all('a', href=re.compile(r'/slow_download/')):
                    slow_url = self.current_annas + a['href']
                    wait_time = 0
                    try:
                        slow_page = self.session.get(slow_url, timeout=10)
                        m = re.search(r'([0-9]+)\s*secondes', slow_page.text)
                        if m: wait_time = int(m.group(1))
                    except: pass
                    links['slow'].append({'text': a.get_text().strip(), 'url': slow_url, 'wait_time': wait_time})
                
                ext_div = soup.find(['h3', 'div'], string=re.compile(r'téléchargements externes|external downloads', re.I))
                if not ext_div:
                    for tag in soup.find_all(['h3', 'div']):
                        if any(x in tag.get_text().lower() for x in ['externe', 'external']):
                            ext_div = tag
                            break
                if ext_div:
                    container = ext_div.find_parent()
                    for a in container.find_all('a', href=True):
                        href, text = a['href'], a.get_text().strip()
                        if 'z-lib' in href.lower() or 'z-library' in href.lower():
                            match = re.search(r'/md5/([a-f0-9]+)', href)
                            if match:
                                href = f"https://z-lib.sk/md5/{match.group(1)}"
                                text = "Z-Library"
                        if any(x in href.lower() for x in ['libgen', 'z-lib', 'ipfs', 'library.lol']):
                            links['external'].append({'text': text, 'url': href})
                return links
        except Exception as e:
            print(f"Error Anna details: {e}")
        return None

    def download_file(self, url, filename):
        try:
            filename = re.sub(r'[\\/*?:"<>|]', "", filename)
            if not os.path.exists(DOWNLOAD_DIR): 
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            
            with self.session.get(url, stream=True, timeout=60, verify=False) as r:
                r.raise_for_status()
                
                ext = ""
                ct = r.headers.get('Content-Type', '').lower()
                if 'application/pdf' in ct: ext = '.pdf'
                elif 'epub' in ct: ext = '.epub'
                elif 'mobi' in ct: ext = '.mobi'
                
                cd = r.headers.get('Content-Disposition', '')
                if 'filename=' in cd:
                    m = re.search(r'filename=["\']?([^"\';]+)', cd)
                    if m: 
                        real_ext = os.path.splitext(m.group(1))[-1].lower()
                        if real_ext in ['.pdf', '.epub', '.mobi', '.azw3']: ext = real_ext

                chunk_iter = r.iter_content(chunk_size=8192)
                try:
                    first_chunk = next(chunk_iter)
                except StopIteration:
                    first_chunk = b""
                
                if first_chunk.startswith(b"%PDF"):
                    ext = '.pdf'
                elif b"BOOKMOBI" in first_chunk[:1024]:
                    ext = '.mobi'
                elif first_chunk.startswith(b"PK\x03\x04"):
                    if b"mimetypeapplication/epub+zip" in first_chunk[:1024] or not ext:
                        ext = '.epub' if b"mimetype" in first_chunk else ext
                
                name, current_ext = os.path.splitext(filename)
                if ext and current_ext.lower() != ext:
                    filename = name + ext
                    
                filepath = os.path.join(DOWNLOAD_DIR, filename)
                with open(filepath, 'wb') as f:
                    if first_chunk: f.write(first_chunk)
                    for chunk in chunk_iter:
                        if chunk: f.write(chunk)
            return True, filename
        except Exception as e:
            return False, str(e)

    def download_slow(self, slow_url, filename):
        import time
        headers = {'Referer': self.current_annas + "/", 'Upgrade-Insecure-Requests': '1'}
        try:
            r = self.session.get(slow_url, headers=headers, timeout=30)
            if "DDoS-Guard" in r.text or "Cloudflare" in r.text:
                return False, "La sécurité bloque le téléchargement automatique. Utilisez un miroir externe."
                
            soup = BeautifulSoup(r.text, 'html.parser')
            text_content = soup.get_text()
            
            m = re.search(r'([0-9]+)\s*second', text_content, re.I)
            wait_time = int(m.group(1)) if m else 60
            
            time.sleep(wait_time + 2)
            
            r = self.session.get(slow_url, headers=headers, timeout=30)
            soup = BeautifulSoup(r.text, 'html.parser')
            final_link = None
            for a in soup.find_all('a', href=True):
                if any(x in a.get_text().lower() for x in ['download', 'télécharger']) or '/get/' in a['href']:
                    final_link = a['href']
                    if not final_link.startswith('http'): final_link = self.current_annas + final_link
                    break
            if final_link: return self.download_file(final_link, filename)
            return False, "Lien final non trouvé après attente."
        except Exception as e:
            return False, str(e)

    def download_external(self, url, filename, config):
        mirrors = ["libgen.li", "libgen.is", "libgen.rs", "libgen.st", "libgen.gs", "library.lol", "z-lib.sk", "libgen"]
        try:
            import requests
            session = self.session
            
            verify_cert = True
            if "library.lol" in url.lower() or "libgen" in url.lower():
                verify_cert = False
            if any(d in url.lower() for d in mirrors):
                import urllib3
                if not verify_cert: urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                
                try:
                    r = session.get(url, timeout=12, verify=verify_cert) 
                except requests.exceptions.Timeout:
                    return False, f"Le serveur miroir est hors ligne ou bloqué (Timeout)."
                except requests.exceptions.SSLError:
                    try:
                        r = session.get(url, timeout=12, verify=False)
                    except:
                        return False, "Erreur de connexion sécurisée au miroir."
                except Exception as e:
                    return False, f"Erreur de connexion : {str(e)}"
                    
                soup = BeautifulSoup(r.text, 'html.parser')
                z_down = soup.find('a', class_=re.compile(r'addDownloadedBook|download-button|btn-primary', re.I))
                if not z_down: z_down = soup.find('a', string=re.compile(r'download|télécharger', re.I))
                if z_down and z_down.get('href') and not z_down['href'].startswith('#'):
                    final_url = z_down['href']
                    if not final_url.startswith('http'): final_url = "/".join(url.split('/')[:3]) + ("/" if not final_url.startswith("/") else "") + final_url
                    return self.download_file(final_url, filename)
                get_link = soup.find('a', string=re.compile(r'GET', re.I))
                if not get_link:
                    lol = soup.find('a', href=re.compile(r'library\.lol|libgen'))
                    if lol:
                        soup = BeautifulSoup(session.get(lol['href'], timeout=12, verify=False).text, 'html.parser')
                        get_link = soup.find('a', string=re.compile(r'GET', re.I))
                if get_link:
                    final_url = get_link['href']
                    if not final_url.startswith('http'): final_url = "/".join(url.split('/')[:3]) + ("/" if not final_url.startswith("/") else "") + final_url
                    return self.download_file(final_url, filename)
            return False, "Automation not available for this link."
        except Exception as e:
            return False, str(e)


downloader = BookDownloader()


# ── Routes ────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == SEARCH_PASSWORD:
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Mot de passe incorrect. (5 tentatives max / 15 min)'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/trending')
@login_required
def api_trending():
    def olb(t):
        return f"https://covers.openlibrary.org/b/title/{urllib.parse.quote(t)}-M.jpg"
    return jsonify([
        {"title": "The Housemaid",    "source": "LibGen",         "info": "Freida McFadden",  "url": "/search?q=The Housemaid Freida McFadden",    "coverUrl": olb("The Housemaid")},
        {"title": "L'Etranger",       "source": "Anna's Archive", "info": "Albert Camus",     "url": "/search?q=L'Etranger Albert Camus",          "coverUrl": olb("L'Etranger")},
        {"title": "Le Petit Prince",  "source": "LibGen",         "info": "Saint-Exupery",    "url": "/search?q=Le Petit Prince Saint-Exupery",    "coverUrl": olb("Le Petit Prince")},
        {"title": "Atomic Habits",    "source": "Anna's Archive", "info": "James Clear",      "url": "/search?q=Atomic Habits James Clear",        "coverUrl": olb("Atomic Habits")},
        {"title": "The Women",        "source": "LibGen",         "info": "Kristin Hannah",   "url": "/search?q=The Women Kristin Hannah",         "coverUrl": olb("The Women")},
        {"title": "Harry Potter",     "source": "LibGen",         "info": "J.K. Rowling",     "url": "/search?q=Harry Potter Rowling",             "coverUrl": olb("Harry Potter")},
        {"title": "Dune",             "source": "Anna's Archive", "info": "Frank Herbert",    "url": "/search?q=Dune Frank Herbert",               "coverUrl": olb("Dune")},
        {"title": "1984",             "source": "LibGen",         "info": "George Orwell",    "url": "/search?q=1984 George Orwell",               "coverUrl": olb("1984")},
    ])

@app.route('/api/search')
@login_required
def api_search():
    query = request.args.get('q', '')
    source = request.args.get('source', 'all')  # all | annas | libgen | bookys
    if not query:
        return jsonify([])

    results = []
    if source in ('all', 'annas'):
        results += downloader.search_annasarchive(query)
    if source in ('all', 'libgen'):
        results += downloader.search_libgen(query)
    if source in ('all', 'bookys'):
        results += downloader.search_bookys(query)

    seen = set()
    unique = []
    for r in results:
        rid = r.get('id') or r.get('url')
        if rid not in seen:
            unique.append(r)
            seen.add(rid)
    def score(r):
        i = r.get('info', '').lower()
        return 0 if 'epub' in i else (1 if 'pdf' in i else 2)
    unique.sort(key=score)
    return jsonify(unique)

@app.route('/api/details')
@login_required
def api_details():
    url = request.args.get('url', '')
    return jsonify(downloader.get_annas_details(url) if url else None)

@app.route('/api/download', methods=['POST'])
@login_required
def api_download():
    data = request.json
    url, filename, dtype = data.get('url'), data.get('filename'), data.get('type')
    config = load_config()
    if dtype == 'external':
        success, msg = downloader.download_external(url, filename, config)
    elif dtype == 'slow':
        success, msg = downloader.download_slow(url, filename)
    else:
        success, msg = downloader.download_file(url, filename)
        
    if success:
        try:
            import subprocess
            subprocess.run(["curl", "-H", "Tags: green_book", "-H", "Title: Nouveau livre !", "-d", f"{filename} a été téléchargé", "https://ntfy.sh/sabu"], timeout=5)
        except Exception as e:
            print("Erreur curl:", e)
            
    return jsonify({'success': success, 'message': msg})

@limiter.request_filter
def exempt_health():
    return request.path == '/health'

@app.route('/health')
def health():
    return 'OK', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
