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
DOWNLOAD_DIR = os.environ.get('BOOKLORE_DIR', '/opt/booklore/bookdrop')
CONFIG_FILE = os.environ.get('CONFIG_FILE', 'config.json')
SEARCH_PASSWORD = os.environ.get('SEARCH_PASSWORD', 'changeme')

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
        url = f"{self.current_annas}/search"
        params = {'q': query}
        try:
            r = self.session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                for a in soup.find_all('a', href=re.compile(r'/md5/')):
                    h3 = a.find('h3')
                    raw_title = h3.get_text().strip() if h3 else a.get_text().strip()
                    if not raw_title or raw_title.lower() in ["save", "lire plus\u2026"]:
                        continue

                    # Couverture depuis Anna's Archive
                    cover_img = a.find('img')
                    cover_url = ""
                    if cover_img and cover_img.get('src'):
                        src = cover_img['src']
                        cover_url = src if src.startswith('http') else (self.current_annas + src if src.startswith('/') else "")

                    parent = a.find_parent()
                    author, info = "", ""
                    meta_divs = parent.find_all('div', class_=re.compile(r'text-gray-500|text-sm|italic', re.I))
                    for div in meta_divs:
                        txt = div.get_text().strip()
                        if any(x in txt.lower() for x in ['epub', 'pdf', 'mobi', 'azw3']):
                            info = txt
                        elif not author and len(txt) > 2:
                            author = txt

                    clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', raw_title).strip()
                    clean_title = clean_title.split('/')[-1].split('\\')[-1].strip()

                    if info:
                        info = re.sub(r'(lgli|zlib|nexusstc|upload|md5)/.*?\s', '', info, flags=re.I).strip()
                        if '/' in info: info = info.split('/')[-1]
                        info = re.sub(r'\.(epub|pdf|mobi|azw3)\b', '', info, flags=re.I).strip()

                    display_title = clean_title
                    if author and author.lower() not in clean_title.lower():
                        display_title = f"{clean_title} - {author}"

                    if not cover_url:
                        cover_url = self._get_cover(clean_title)

                    results.append({
                        'source': "Anna's Archive",
                        'title': display_title,
                        'info': info,
                        'url': self.current_annas + a['href'],
                        'id': a['href'].split('/')[-1],
                        'coverUrl': cover_url
                    })

                seen = set()
                results = [r for r in results if r['id'] not in seen and not seen.add(r['id'])]

                def get_score(res):
                    t = res.get('info', '').lower()
                    if 'epub' in t: return 0
                    if 'pdf' in t: return 1
                    return 2
                results.sort(key=get_score)

        except Exception as e:
            print(f"Error Anna's Archive: {e}")
        return results

    def search_libgen(self, query):
        url = "https://libgen.li/index.php"
        params = {'req': query, 'column': 'def'}
        results = []
        try:
            r = self.session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                table = soup.find('table', id='tablelibgen')
                if table:
                    for tr in table.find_all('tr')[1:]:
                        tds = tr.find_all('td')
                        if len(tds) < 10: continue
                        author = tds[1].get_text().strip()
                        title_link = tds[2].find('a')
                        if not title_link: continue
                        title = re.sub(r'\(.*?\)', '', title_link.get_text().strip()).strip()
                        ext = tds[8].get_text().strip().lower()
                        size = tds[9].get_text().strip()
                        dl_link = tds[2].find('a', href=re.compile(r'get\.php'))
                        if not dl_link: continue
                        md5 = dl_link['href'].split('md5=')[-1]
                        results.append({
                            'source': 'LibGen',
                            'title': f"{title} - {author}",
                            'info': f"{ext.upper()} | {size}",
                            'url': f"https://libgen.li/{dl_link['href']}",
                            'id': md5,
                            'coverUrl': self._get_cover(title)
                        })
        except Exception as e:
            print(f"LibGen Error: {e}")
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
        mirrors = ["libgen.li", "libgen.is", "libgen.rs", "libgen.st", "libgen.gs", "library.lol", "z-lib.sk", "z-lib", "zlibrary", "libgen"]
        try:
            import requests
            import cloudscraper
            session = self.session
            
            # Use cloudscraper for Z-Library to bypass Cloudflare
            if "z-lib" in url.lower():
                scraper = cloudscraper.create_scraper(
                    browser={
                        'browser': 'chrome',
                        'platform': 'windows',
                        'desktop': True
                    }
                )
                session = scraper
            
            verify_cert = True
            if "library.lol" in url.lower() or "libgen" in url.lower():
                verify_cert = False
                
            if any(d in url.lower() for d in mirrors):
                import urllib3
                if not verify_cert: urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                
                try:
                    r = session.get(url, timeout=15, verify=verify_cert) 
                    
                    # If Cloudflare still blocks us with 403 or 503 despite cloudscraper
                    if r.status_code in [403, 503] and "cloudflare" in r.text.lower():
                        return False, "La protection Cloudflare de Z-Library bloque l'automatisation. Utilisez un miroir Libgen ou le téléchargement lent."
                        
                except requests.exceptions.Timeout:
                    return False, f"Le serveur miroir est hors ligne ou bloqué (Timeout)."
                except requests.exceptions.SSLError:
                    try:
                        r = session.get(url, timeout=15, verify=False)
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
    if not query:
        return jsonify([])
    annas = downloader.search_annasarchive(query)
    libgen = downloader.search_libgen(query)
    results = annas + libgen
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
    return jsonify({'success': success, 'message': msg})

@limiter.request_filter
def exempt_health():
    return request.path == '/health'

@app.route('/health')
def health():
    return 'OK', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
