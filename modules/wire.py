"""
WireAggregator — "The Wire": a cross-spectrum news aggregator.

Truly free / no subscriptions / runs anywhere:
  • Sources      → curated RSS feeds (parsed with stdlib xml.etree) + GDELT (free API)
  • Grouping     → pure-Python TF-IDF cosine + named-entity gate (no model, no key)
  • Neutral take → consensus headline = the article closest to the cluster centroid
                   (drawn from the middle of the left/center/right spread = non-partisan)
  • Ticker       → AP-sourced wire via Google News RSS (free), for the bottom crawl

Categories: world, science, tech, markets   (no sports)
Bias buckets: left, center, right, independent
Refreshes every 5 minutes in a background thread.
"""

import re
import math
import html
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

import requests

HEADERS = {'User-Agent': 'TheWire/1.0 (+news aggregator)'}
REFRESH_SECONDS = 300          # 5-minute heartbeat
TICKER_SECONDS  = 90           # AP crawl refreshes faster
MAX_AGE_HOURS   = 24           # only cluster reasonably fresh news
SIM_THRESHOLD   = 0.20         # TF-IDF cosine merge threshold
ATOM            = '{http://www.w3.org/2005/Atom}'
MEDIA_NS        = 'http://search.yahoo.com/mrss/'

# ── Source registry: (name, rss_url, bias, category) ────────────────────────────
SOURCES = [
    # ── World / Political ──
    ('The Guardian',        'https://www.theguardian.com/world/rss',                       'left',        'world'),
    ('Vox',                 'https://www.vox.com/rss/index.xml',                           'left',        'world'),
    ('The Intercept',       'https://theintercept.com/feed/?rss',                          'left',        'world'),
    ('HuffPost',            'https://www.huffpost.com/section/front-page/feed',            'left',        'world'),
    ('NPR',                 'https://feeds.npr.org/1001/rss.xml',                          'center',      'world'),
    ('BBC',                 'http://feeds.bbci.co.uk/news/world/rss.xml',                  'center',      'world'),
    ('PBS NewsHour',        'https://www.pbs.org/newshour/feeds/rss/headlines',            'center',      'world'),
    ('The Hill',            'https://thehill.com/news/feed/',                              'center',      'world'),
    ('Al Jazeera',          'https://www.aljazeera.com/xml/rss/all.xml',                   'independent', 'world'),
    ('Fox News',            'https://moxie.foxnews.com/google-publisher/world.xml',        'right',       'world'),
    ('New York Post',       'https://nypost.com/feed/',                                    'right',       'world'),
    ('National Review',     'https://www.nationalreview.com/feed/',                        'right',       'world'),
    ('Washington Examiner', 'https://www.washingtonexaminer.com/feed',                     'right',       'world'),
    ('The Daily Wire',      'https://www.dailywire.com/feeds/rss.xml',                     'right',       'world'),
    ('Reason',              'https://reason.com/feed/',                                    'independent', 'world'),
    ('RealClearPolitics',   'https://www.realclearpolitics.com/index.xml',                 'independent', 'world'),
    # ── Science ──
    ('Science Daily',       'https://www.sciencedaily.com/rss/all.xml',                    'center',      'science'),
    ('Phys.org',            'https://phys.org/rss-feed/',                                  'center',      'science'),
    ('Scientific American', 'http://rss.sciam.com/ScientificAmerican-Global',              'center',      'science'),
    ('NASA',                'https://www.nasa.gov/feed/',                                  'center',      'science'),
    ('Live Science',        'https://www.livescience.com/feeds/all',                       'center',      'science'),
    ('New Scientist',       'https://www.newscientist.com/feed/home/',                     'center',      'science'),
    ('Nature',              'https://www.nature.com/nature.rss',                           'center',      'science'),
    # ── Technology ──
    ('The Verge',           'https://www.theverge.com/rss/index.xml',                      'left',        'tech'),
    ('Ars Technica',        'http://feeds.arstechnica.com/arstechnica/index',              'center',      'tech'),
    ('TechCrunch',          'https://techcrunch.com/feed/',                                'center',      'tech'),
    ('Wired',               'https://www.wired.com/feed/rss',                              'left',        'tech'),
    ('Engadget',            'https://www.engadget.com/rss.xml',                            'center',      'tech'),
    ('Hacker News',         'https://hnrss.org/frontpage',                                 'independent', 'tech'),
    ('MIT Tech Review',     'https://www.technologyreview.com/feed/',                      'center',      'tech'),
    # ── Financial Markets ──
    ('CNBC',                'https://www.cnbc.com/id/100003114/device/rss/rss.html',       'center',      'markets'),
    ('CNBC Markets',        'https://www.cnbc.com/id/20910258/device/rss/rss.html',        'center',      'markets'),
    ('MarketWatch',         'http://feeds.marketwatch.com/marketwatch/topstories/',        'center',      'markets'),
    ('Yahoo Finance',       'https://finance.yahoo.com/news/rssindex',                     'center',      'markets'),
    ('Seeking Alpha',       'https://seekingalpha.com/market_currents.xml',                'center',      'markets'),
    ('Forbes',              'https://www.forbes.com/business/feed/',                       'right',       'markets'),
    ('Business Insider',    'https://www.businessinsider.com/rss',                         'center',      'markets'),
]

# AP-sourced wire for the bottom ticker (Google News RSS, free, links back to AP)
AP_TICKER_URL = ('https://news.google.com/rss/search?q=when:3h%20source:apnews.com'
                 '&hl=en-US&gl=US&ceid=US:en')

# GDELT free DOC API — recent global English articles, for breaking-event piggybacking
GDELT_URL = ('https://api.gdeltproject.org/api/v2/doc/doc?query=sourcelang:english'
             '&mode=ArtList&maxrecords=60&timespan=90min&format=json&sort=DateDesc')

# Light keyword routing for un-categorized (GDELT) items
_CAT_KEYWORDS = {
    'markets': ('stock', 'nasdaq', 'dow ', 's&p', 'earnings', 'fed ', 'inflation', 'market',
                'shares', 'ipo', 'crypto', 'bitcoin', 'economy', 'rate cut', 'rate hike'),
    'tech':    ('ai ', 'artificial intelligence', 'chip', 'software', 'app ', 'google', 'apple',
                'microsoft', 'openai', 'startup', 'cyber', 'robot', 'semiconductor'),
    'science': ('study', 'researchers', 'scientists', 'nasa', 'space', 'climate', 'fossil',
                'species', 'quantum', 'physics', 'telescope', 'vaccine', 'dna'),
}

_STOPWORDS = set((
    "the a an and or but of to in on for with at by from as is are was were be been being this that "
    "these those it its it's he she they them his her their you your we our us i me my will would can "
    "could should may might must not no nor so than then there here over under after before about into "
    "out up down off new says say said report reports amid after new latest breaking watch live update "
    "video photos how why what when who which more most via just like get got make made one two"
).split())

_ENTITY_RE = re.compile(r'\b([A-Z][A-Za-z][A-Za-z.&\'-]+|\$[A-Z]{1,5}|[0-9]{4})\b')
_WORD_RE   = re.compile(r"[a-z][a-z'&]+")
_TAG_RE    = re.compile(r'<[^>]+>')
_IMG_RE    = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_YT_RE     = re.compile(r'(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)', re.I)

BIAS_ORDER = ['left', 'center', 'right', 'independent']


def _clean(text):
    if not text:
        return ''
    text = _TAG_RE.sub(' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def _tokens(text):
    return [w for w in _WORD_RE.findall((text or '').lower()) if w not in _STOPWORDS and len(w) > 2]


def _entities(title):
    return set(m.group(1).lower() for m in _ENTITY_RE.finditer(title or ''))


def _ts(entry):
    for tag in ('pubDate', 'published', ATOM + 'published', ATOM + 'updated', 'updated', 'dc:date'):
        raw = entry.findtext(tag)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                try:
                    return datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
                except Exception:
                    pass
    return time.time()


def _media(entry, description):
    """Extract (image_url, video_url) from an RSS/Atom entry."""
    image = video = ''
    for el in entry.iter():
        tag = el.tag.split('}')[-1]
        url = el.get('url') or el.get('href') or ''
        typ = (el.get('type') or '') + (el.get('medium') or '')
        if tag in ('content', 'thumbnail', 'enclosure') and url:
            if 'video' in typ or _YT_RE.search(url):
                video = video or url
            elif 'image' in typ or re.search(r'\.(jpg|jpeg|png|webp|gif)', url, re.I) or tag == 'thumbnail':
                image = image or url
    if not image:
        m = _IMG_RE.search(description or '')
        if m:
            image = m.group(1)
    if not video and _YT_RE.search(description or ''):
        m = re.search(r'(https?://[^\s"\']*(?:youtube\.com|youtu\.be)[^\s"\']*)', description or '')
        if m:
            video = m.group(1)
    return image, video


def _link(entry):
    link = entry.findtext('link')
    if link:
        return link.strip()
    for le in entry.findall(ATOM + 'link'):
        if le.get('rel') in (None, 'alternate') and le.get('href'):
            return le.get('href')
    return ''


def _parse_feed(source):
    name, url, bias, category = source
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200 or not r.content:
            return out
        root = ET.fromstring(r.content)
    except Exception:
        return out
    entries = root.findall('.//item') or root.findall('.//' + ATOM + 'entry')
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    for e in entries[:40]:
        title = _clean(e.findtext('title') or e.findtext(ATOM + 'title') or '')
        if not title:
            continue
        desc = (e.findtext('description') or e.findtext('summary')
                or e.findtext(ATOM + 'summary') or e.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or '')
        ts = _ts(e)
        if ts < cutoff:
            continue
        image, video = _media(e, desc)
        out.append({
            'title': title, 'summary': _clean(desc)[:400], 'url': _link(e),
            'ts': ts, 'image': image, 'video': video,
            'source': name, 'bias': bias, 'category': category,
        })
    return out


def _parse_gdelt():
    out = []
    try:
        r = requests.get(GDELT_URL, headers=HEADERS, timeout=10)
        data = r.json()
    except Exception:
        return out
    for a in (data.get('articles') or [])[:60]:
        title = _clean(a.get('title', ''))
        if not title:
            continue
        low = title.lower()
        category = 'world'
        for cat, kws in _CAT_KEYWORDS.items():
            if any(k in low for k in kws):
                category = cat
                break
        out.append({
            'title': title, 'summary': '', 'url': a.get('url', ''),
            'ts': time.time(), 'image': a.get('socialimage', '') or '', 'video': '',
            'source': a.get('domain', 'wire'), 'bias': 'independent', 'category': category,
        })
    return out


def _vectorize(articles):
    """Pure-Python TF-IDF; title tokens weighted ×3. Returns list of (vec_dict, norm)."""
    doc_tokens = []
    for a in articles:
        toks = _tokens(a['title']) * 3 + _tokens(a['summary'])
        doc_tokens.append(toks)
    n = len(articles)
    df = Counter()
    for toks in doc_tokens:
        for t in set(toks):
            df[t] += 1
    idf = {t: math.log((n + 1) / (d + 1)) + 1 for t, d in df.items()}
    vecs = []
    for toks in doc_tokens:
        tf = Counter(toks)
        v = {t: c * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append((v, norm))
    return vecs


def _cosine(a, b):
    va, na = a
    vb, nb = b
    if len(va) > len(vb):
        va, vb = vb, va
        na, nb = nb, na
    s = 0.0
    for t, w in va.items():
        o = vb.get(t)
        if o:
            s += w * o
    return s / (na * nb)


class WireAggregator:
    def __init__(self):
        self._lock = threading.Lock()
        self._clusters = []        # list of cluster dicts
        self._ticker = []          # list of {title, url}
        self.updated = 0
        self.ticker_updated = 0
        self.running = False
        self.last_error = ''

    # ── lifecycle ──
    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._ticker_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self.refresh()
            except Exception as exc:
                self.last_error = str(exc)
            time.sleep(REFRESH_SECONDS)

    def _ticker_loop(self):
        while self.running:
            try:
                self._refresh_ticker()
            except Exception:
                pass
            time.sleep(TICKER_SECONDS)

    # ── ingest + cluster ──
    def refresh(self):
        articles = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            for batch in pool.map(_parse_feed, SOURCES):
                articles.extend(batch)
        articles.extend(_parse_gdelt())
        # de-dup identical URLs / titles
        seen, deduped = set(), []
        for a in articles:
            key = (a['url'] or a['title']).lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(a)
        articles = deduped
        if not articles:
            return

        vecs = _vectorize(articles)
        order = sorted(range(len(articles)), key=lambda i: articles[i]['ts'], reverse=True)
        clusters = []   # {'members':[idx], 'rep': idx, 'ents': set, 'cat': str}
        for i in order:
            a = articles[i]
            ents_i = _entities(a['title'])
            best, best_sim = None, 0.0
            for c in clusters:
                if c['cat'] != a['category']:
                    continue
                if not (ents_i & c['ents']):
                    continue
                sim = _cosine(vecs[i], vecs[c['rep']])
                if sim > best_sim:
                    best_sim, best = sim, c
            if best and best_sim >= SIM_THRESHOLD:
                best['members'].append(i)
                best['ents'] |= ents_i
            else:
                clusters.append({'members': [i], 'rep': i, 'ents': ents_i, 'cat': a['category']})

        built = [self._build_cluster(c, articles, vecs) for c in clusters]
        # rank: more sources + broader bias spread + fresher = higher
        built.sort(key=lambda c: (c['source_count'], c['bias_breadth'], c['latest_ts']), reverse=True)
        with self._lock:
            self._clusters = built
            self.updated = int(time.time())

    def _build_cluster(self, c, articles, vecs):
        members = [articles[i] for i in c['members']]
        # consensus pick = member closest to the cluster centroid (most representative / least fringe)
        if len(c['members']) > 1:
            centroid = {}
            for i in c['members']:
                for t, w in vecs[i][0].items():
                    centroid[t] = centroid.get(t, 0) + w
            cnorm = math.sqrt(sum(x * x for x in centroid.values())) or 1.0
            rep_idx = max(c['members'], key=lambda i: _cosine(vecs[i], (centroid, cnorm)))
        else:
            rep_idx = c['members'][0]
        rep = articles[rep_idx]

        bias_counts = Counter(m['bias'] for m in members)
        sources = sorted({m['source'] for m in members})
        hero_img = next((m['image'] for m in members if m['image']), '')
        hero_vid = next((m['video'] for m in members if m['video']), '')
        summary = rep['summary'] or next((m['summary'] for m in members if m['summary']), '')

        # blindspot: 3+ sources but coverage missing from a major side
        blindspot = ''
        if len(sources) >= 3:
            sides = {b for b in bias_counts if b in ('left', 'right')}
            if sides == {'left'}:
                blindspot = 'right'
            elif sides == {'right'}:
                blindspot = 'left'

        seen_src, outlets = set(), []
        for m in sorted(members, key=lambda m: BIAS_ORDER.index(m['bias']) if m['bias'] in BIAS_ORDER else 9):
            if m['source'] in seen_src:
                continue
            seen_src.add(m['source'])
            outlets.append({'source': m['source'], 'bias': m['bias'], 'title': m['title'], 'url': m['url']})

        return {
            'id': str(rep_idx) + '-' + str(int(rep['ts'])),
            'headline': rep['title'],
            'summary': summary[:280],
            'category': c['cat'],
            'url': rep['url'],
            'image': hero_img,
            'video': hero_vid,
            'source_count': len(sources),
            'bias_counts': {b: bias_counts.get(b, 0) for b in BIAS_ORDER},
            'bias_breadth': len([b for b in ('left', 'center', 'right') if bias_counts.get(b)]),
            'blindspot': blindspot,
            'latest_ts': max(m['ts'] for m in members),
            'outlets': outlets,
        }

    def _refresh_ticker(self):
        try:
            r = requests.get(AP_TICKER_URL, headers=HEADERS, timeout=8)
            root = ET.fromstring(r.content)
        except Exception:
            return
        items = []
        for it in root.findall('.//item')[:25]:
            title = _clean(it.findtext('title') or '')
            # Google News appends " - AP" etc; trim trailing source
            title = re.sub(r'\s+-\s+[^-]+$', '', title).strip()
            if title:
                items.append({'title': title, 'url': (it.findtext('link') or '').strip()})
        if items:
            with self._lock:
                self._ticker = items
                self.ticker_updated = int(time.time())

    # ── public read API ──
    def get_feed(self, category=None, wall_size=6, story_limit=40):
        with self._lock:
            clusters = list(self._clusters)
            updated = self.updated
        if category and category != 'all':
            clusters = [c for c in clusters if c['category'] == category]
        # Media Wall = top stories that have a picture or video
        wall = [c for c in clusters if c['image'] or c['video']][:wall_size]
        wall_ids = {c['id'] for c in wall}
        stories = [c for c in clusters if c['id'] not in wall_ids][:story_limit]
        return {
            'updated': updated,
            'wall': wall,
            'stories': stories,
            'counts': {
                cat: sum(1 for c in self._clusters_snapshot() if c['category'] == cat)
                for cat in ('world', 'science', 'tech', 'markets')
            },
        }

    def _clusters_snapshot(self):
        with self._lock:
            return list(self._clusters)

    def get_ticker(self):
        with self._lock:
            return {'updated': self.ticker_updated, 'items': list(self._ticker)}
