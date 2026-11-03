#!/usr/bin/env python3
"""Search & download SFX from multiple open sources using the Python standard library.

Uses the shell's http(s)_proxy settings.

  sfx.py freesound "<query>" [--license cc0|by|any] [--sort relevance|downloads|rating|short] [-n N] [-p PAGE] [--pages K]
                                       relevance (default) is re-ranked by downloads across K pages
  sfx.py commons "<query>" [--license cc0|by|any] [-n N]
  sfx.py mixkit "<query>" [-n N]
  sfx.py all "<query>" [--license cc0|by|any] [-n N]      # fan out across every source, merged ranking
  sfx.py music "<query>" [-n N] [--duration short|long]    # BGM / music beds (longer, loopable)
  sfx.py get <out_dir> <key>...        keys: fs:<id> | wm:<id> | mk:<id>
  sfx.py analyze <file>...             # ffmpeg/ffprobe: duration, loudness, silence map
"""
import argparse, html, json, re, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) sfx-sources"}
CACHE = Path.home() / ".cache" / "sfx-sources"
INDEX = CACHE / "index.json"  # search results, so `get` knows title + download url
FS_LICENSE = {"cc0": '"Creative Commons 0"', "by": '("Attribution" OR "Creative Commons 0")', "any": None}
FS_LIC_SHORT = {"Creative Commons 0": "CC0", "Attribution": "BY", "Attribution NonCommercial": "BY-NC", "Sampling+": "Sampling+"}
FS_SORT = {"downloads": "Downloads (most first)", "rating": "Rating (highest first)",
           "relevance": "Automatic by relevance", "short": "Duration (shortest first)"}

# Openverse aggregates many providers under one key-free API and reports license per result.
# Mixkit ships its own license (free for commercial use, no attribution) and has no
# per-item download stats, so its ranking leans on the site's own relevance order.
MK_LICENSE = "Mixkit Free License"


def fetch(url, timeout=60, tries=3):
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # Openverse throttles anonymous callers hard (429) and does not send a
            # Retry-After, so back off on our own rather than failing the search.
            if e.code == 429:
                if n == tries - 1:
                    raise
                time.sleep(5 * (n + 1))
                continue
            raise e if isinstance(e, OSError) else OSError(repr(e))
        except Exception as e:
            if n == tries - 1:
                raise e if isinstance(e, OSError) else OSError(repr(e))
            time.sleep(1 + n)


KEY_PREFIXES = ("fs", "wm", "mk")


def load_index():
    try:
        return json.loads(INDEX.read_text())
    except (OSError, ValueError):
        return {}


def save_index(rows):
    CACHE.mkdir(parents=True, exist_ok=True)
    idx = load_index()
    idx.update({r["key"]: r for r in rows})
    # Drop entries from sources this build no longer supports, so the cache cannot
    # accumulate keys that `get` would only ever reject as unknown.
    idx = {k: v for k, v in idx.items() if k.split(":", 1)[0] in KEY_PREFIXES}
    INDEX.write_text(json.dumps(idx, ensure_ascii=False))


def clean_field(s, limit=None):
    """Flatten a value so it cannot break the tab-separated output.

    A title or description containing a tab or newline would silently shift every
    later column, so tabs and line breaks become spaces.
    """
    s = re.sub(r"[\t\r\n]+", " ", str(s or "")).strip()
    if limit:
        s = s[:limit]
    return s


def show(rows, a, lic=False, hint=""):
    """Print results, as tab-separated columns or as JSON.

    The license column is hidden by default: the search already filtered on it, and
    the useful default view is the title, length and popularity signal. Pass
    `--licenses` when the licence has to be shown, and `--json` for machine-readable
    output, which always carries the licence.
    """
    save_index(rows)
    if getattr(a, "json", False):
        print(json.dumps([json_row(r) for r in rows], ensure_ascii=False, indent=2))
        return
    want_lic = lic or getattr(a, "licenses", False)
    for r in rows:
        cols = [r["key"], clean_field(r["title"]), r["dur"]]
        if "stats" in r:
            cols.append(r["stats"])
        if want_lic:
            cols.append(clean_field(r.get("license", "?")))
        if r.get("desc"):
            cols.append(clean_field(r["desc"]))
        if getattr(a, "urls", False):
            cols.append(r["preview"])
        print("\t".join(cols))
    if not rows:
        print("(none)" + hint)


def json_row(r):
    """One result as a plain dict, with the preview URL always included."""
    out = {"key": r["key"], "title": clean_field(r["title"]), "duration": r["dur"],
           "license": r.get("license", "?"), "author": r.get("author", "?"),
           "description": clean_field(r.get("desc", "")), "url": r.get("preview", "")}
    if r.get("provider"):
        out["provider"] = r["provider"]
    if "stats" in r:
        out["stats"] = r["stats"]
    if "downloads" in r:
        out["downloads"] = r["downloads"]
    return out


def freesound(a):
    rows = freesound_rows(a)
    show(rows[: a.n], a)


def freesound_rows(a):
    # Pull several relevance pages and rank them by downloads, since sorting by downloads
    # server-side lets globally popular but off-topic sounds (gunshots, ambiences) win on a loose tag match.
    q = {"q": a.query, "s": FS_SORT[a.sort]}
    if FS_LICENSE[a.license]:
        q["f"] = "license:" + FS_LICENSE[a.license]
    pages = range(a.page, a.page + a.pages)
    with ThreadPoolExecutor(len(pages)) as ex:
        htmls = list(ex.map(lambda n: fetch("https://freesound.org/search/?" + urllib.parse.urlencode({**q, "page": n})).decode(), pages))
    rows = []
    for blk in (b for h in htmls for b in h.split('class="bw-search__result"')[1:]):
        g = lambda k: (re.search(rf'data-{k}="([^"]*)"', blk) or [None, ""])[1]
        lic = re.search(r'title="License: ([^"]+)"', blk)
        rt = re.search(r'aria-label="Average rating of ([\d.]+)"', blk)
        rn = re.search(r'bw-rating__count[^>]*>\((\d+)\)', blk)
        desc = re.search(r'<div class="max-h-60px[^"]*"\s*>\s*(.*?)\s*</div>', blk, re.S)
        sid, user, dl = g("sound-id"), g("username"), int(g("num-downloads") or 0)
        rows.append({"key": f"fs:{sid}", "title": html.unescape(g("title")), "dur": f"{float(g('duration') or 0):.1f}s",
                     "author": user, "license": FS_LIC_SHORT.get(lic.group(1), lic.group(1)) if lic else "?", "preview": g("mp3").replace("-lq.mp3", "-hq.mp3"),
                     "url": g("mp3").replace("-lq.mp3", "-hq.mp3"), "downloads": dl,
                     "stats": f"↓{dl} ★{rt[1]}({rn[1]})" if rt and rn else f"↓{dl} ★-",
                     # the uploader's own words (mic, source, processing) are the closest thing to hearing it
                     "desc": html.unescape(re.sub(r"<[^>]+>|\s+", " ", desc[1])).strip()[:90] if desc else ""})
    if a.sort == "relevance":
        rows.sort(key=lambda r: -r["downloads"])
    return rows


def commons(a):
    rows = commons_rows(a)
    show(rows[: a.n], a)


def commons_rows(a):
    """Search Wikimedia Commons for audio via its MediaWiki API.

    Commons is a script-accessible aggregator with no bot challenge and no API key.
    Each file carries a real duration and a license tag, so it filters well when you
    need a specific length or a commercial-safe license.
    """
    params = {
        "action": "query", "generator": "search",
        "gsrsearch": f"filetype:audio {a.query}",
        "gsrnamespace": 6, "gsrlimit": min(max(a.n, 20), 50),
        "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
        "format": "json", "formatversion": 2,
    }
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    data = json.loads(fetch(url))
    pages = (data.get("query") or {}).get("pages") or []
    if isinstance(pages, dict):
        pages = list(pages.values())
    rows = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        em = ii.get("extmetadata") or {}
        dur = ii.get("duration") or 0
        lic_raw = strip_html((em.get("LicenseShortName") or {}).get("value") or "")
        lic = lic_short(lic_raw)
        if a.license != "any" and not lic_ok(lic, a.license):
            continue
        title = p.get("title") or ""
        if title.startswith("File:"):
            title = title[5:]
        artist = strip_html((em.get("Artist") or {}).get("value") or "") or "Wikimedia Commons"
        rows.append({"key": f"wm:{p.get('pageid')}", "title": title,
                     "dur": f"{float(dur):.1f}s" if dur else "?",
                     "author": artist[:60], "license": lic,
                     "preview": ii.get("url") or "", "url": ii.get("url") or "",
                     "provider": "wikimedia", "stats": f"{int(ii.get('size') or 0) // 1024}KB",
                     "desc": strip_html((em.get("ImageDescription") or {}).get("value") or "")[:90],
                     "mime": ii.get("mime") or ""})
    return rows


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def lic_short(raw):
    """Map a Commons license label onto the short form used across this tool."""
    low = (raw or "").lower()
    if "cc0" in low or "zero" in low or "public domain" in low:
        return "CC0"
    if "by-sa" in low:
        return "BY-SA"
    if "by-nc-sa" in low:
        return "BY-NC-SA"
    if "by-nc" in low:
        return "BY-NC"
    if "by-nd" in low:
        return "BY-ND"
    if low.startswith("by") or "attribution" in low:
        return "BY"
    return raw or "?"


def lic_ok(lic, want):
    """Does a short license satisfy the requested --license filter?"""
    if want == "any":
        return True
    if want == "cc0":
        return lic in ("CC0", "PDM")
    # want == "by": CC0 plus any attribution variant.
    return lic in ("CC0", "PDM", "BY", "BY-SA", "BY-NC", "BY-NC-SA", "BY-ND")


def mixkit(a):
    rows = mixkit_rows(a)
    show(rows[: a.n], a)


def mixkit_rows(a):
    """Scrape Mixkit's free sound-effect grid.

    Only preview MP3s are fetchable without a session, which is what we hand back; the
    preview is a real audio file, so it is usable for auditioning and cutting to length.
    """
    url = "https://mixkit.co/free-sound-effects/" + urllib.parse.quote(a.query.replace(" ", "-")) + "/"
    doc = fetch(url).decode("utf-8", "replace")
    rows = []
    for blk in doc.split('class="item-grid__item"')[1:]:
        title = re.search(r'item-grid-card__title[^>]*>\s*(.*?)\s*</h2>', blk, re.S)
        prev = re.search(r'data-audio-player-preview-url-value="([^"]+)"', blk)
        iid = re.search(r'data-audio-player-item-id-value="([^"]+)"', blk)
        dur = re.search(r'data-test-id="duration"[^>]*>\s*([\d:]+)\s*<', blk)
        wave = re.search(r'data-audio-player-waveform-url-value="([^"]+)"', blk)
        if not (prev and iid):
            continue
        tags = re.findall(r'meta-links__link[^>]*href="[^"]*">([^<]+)</a>', blk)
        # The waveform JSON carries the exact sample count, which beats the rounded
        # 0:02 label when a clip has to land on a specific video cut.
        exact = ""
        if wave:
            try:
                w = json.loads(fetch(wave.group(1)))
                if w.get("sample_rate") and w.get("length") and w.get("samples_per_pixel"):
                    exact = f"{w['length'] * w['samples_per_pixel'] / w['sample_rate']:.1f}s"
            except Exception:
                pass
        rows.append({"key": f"mk:{iid.group(1)}",
                     "title": html.unescape(re.sub(r"\s+", " ", title.group(1)).strip()) if title else "(untitled)",
                     "dur": exact or (dur.group(1) if dur else "?"),
                     "author": "Mixkit", "license": MK_LICENSE,
                     "preview": prev.group(1), "url": prev.group(1),
                     "desc": ", ".join(t.strip() for t in tags[:4])})
    return rows


def all_sources(a):
    """Fan out across every source and merge into one ranked list.

    Each source is queried concurrently and a source that fails (rate limit, layout
    change) is skipped rather than sinking the whole search.
    """
    def run_freesound():
        return freesound_rows(_Sub(a, license=a.license, sort=a.sort, urls=False))

    def run_commons():
        return commons_rows(_Sub(a, n=30))

    def run_mixkit():
        return mixkit_rows(_Sub(a, license=a.license))

    jobs = [run_freesound, run_commons, run_mixkit]
    # Freesound's download count is the only real popularity signal we have, but it is
    # on a completely different scale from the other two sources, so a plain sort would
    # bury every Mixkit and Commons result below the Freesound cut-off. Rank within
    # each source first, then interleave, so one source cannot crowd out the others.
    per_source = []
    with ThreadPoolExecutor(len(jobs)) as ex:
        futs = [ex.submit(fn) for fn in jobs]
        for fut in futs:
            try:
                per_source.append(fut.result())
            except Exception:
                continue
    for rows_src in per_source:
        rows_src.sort(key=lambda r: -r.get("downloads", 0))
    merged = []
    for i in range(max((len(r) for r in per_source), default=0)):
        for rows_src in per_source:
            if i < len(rows_src):
                merged.append(rows_src[i])
    show(merged[: a.n], a)


class _Sub:
    """Minimal attribute view over the parent args, for reusing a source function."""

    def __init__(self, parent, **over):
        self.__dict__.update(parent.__dict__)
        self.__dict__.update(over)


def music(a):
    """Search for BGM beds: longer, loopable material rather than a one-shot effect.

    Commons holds long-form audio (speech, music, field recordings) with duration and
    license attached, so the length filter is applied here rather than at the API.
    """
    rows = commons_rows(_Sub(a, n=50))
    # A bed is only useful if it is long enough to loop and short enough to sit under a
    # scene; drop the one-shot effects that match the same keywords.
    def ok(r):
        try:
            secs = float(r["dur"].rstrip("s"))
        except ValueError:
            return True
        if a.duration == "long":
            return secs >= 30
        return secs < 30
    rows = [r for r in rows if ok(r)]
    show(rows[: a.n], a)


def get(a):
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    idx = load_index()

    def one(key):
        src, sep, ref = key.partition(":")
        if src not in ("fs", "wm", "mk") or not sep:
            return key, None, "unsupported key — use fs:, wm: or mk:"
        r = idx.get(key)
        if not r:
            return key, None, "unknown key — search first"
        slug = re.sub(r"[^\w]+", "-", r["title"]).strip("-")[:40]
        dst = out / f"{src}_{ref}{'_' + slug if slug else ''}{Path(urllib.parse.urlparse(r['url']).path).suffix}"
        if not dst.exists():
            tmp = dst.with_suffix(dst.suffix + ".part")
            tmp.write_bytes(fetch(r["url"], timeout=300))
            tmp.rename(dst)
        lic = r.get("license", "CC0")
        if lic in ("CC0", "PDM", MK_LICENSE):
            return key, dst, None
        return key, dst, f"credit {r.get('author', '?')}"

    def safe(key):
        try:
            return one(key)
        except Exception as e:  # one bad file shouldn't sink the batch
            return key, None, repr(e)

    # Freesound caps ~80 KB/s per connection, so parallelism is the only speed lever.
    with ThreadPoolExecutor(6) as ex:
        for key, dst, note in ex.map(safe, a.keys):
            print(f"FAIL {key}: {note}" if not dst else f"{dst.name}  [{note}]" if note else dst.name)


def analyze(a):
    """Measure duration, loudness and silence map with ffprobe/ffmpeg.

    Download counts say a clip is popular, not that it fits. This gives the numbers you
    actually need to cut it to a scene: how long it runs, where it peaks, and how much
    silence it carries.
    """
    import shutil, subprocess
    if not shutil.which("ffprobe"):
        sys.exit("ffprobe not found — install ffmpeg (brew install ffmpeg)")
    results = []
    for path in a.files:
        p = Path(path)
        if not p.exists():
            if a.json:
                results.append({"file": str(path), "error": "no such file"})
            else:
                print(f"FAIL {path}: no such file")
            continue
        try:
            dur = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(p)],
                capture_output=True, text=True, timeout=60).stdout.strip()
        except Exception as e:
            if a.json:
                results.append({"file": str(path), "error": repr(e)})
            else:
                print(f"FAIL {path}: {e}")
            continue
        rec = {"file": str(path), "duration_seconds": float(dur) if dur else None}
        if a.loudness:
            try:
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-i", str(p), "-af", "volumedetect", "-f", "null", "/dev/null"],
                    capture_output=True, text=True, timeout=180).stderr
                for line in r.splitlines():
                    if "mean_volume" in line or "max_volume" in line:
                        val = line.strip().split("] ")[-1]
                        if "mean_volume" in val:
                            rec["mean_volume_db"] = float(val.split(":")[1].strip().split()[0])
                        elif "max_volume" in val:
                            rec["max_volume_db"] = float(val.split(":")[1].strip().split()[0])
                        if not a.json:
                            print("  " + val)
            except Exception:
                pass
        if a.silence:
            try:
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-i", str(p),
                     "-af", "silencedetect=noise=-45dB:d=0.15", "-f", "null", "/dev/null"],
                    capture_output=True, text=True, timeout=180).stderr
                sil = [l.strip() for l in r.splitlines() if "silence_" in l]
                regions = []
                for line in sil[:12]:
                    parts = line.split("] ")[-1]
                    regions.append(parts)
                    if not a.json:
                        print("  " + parts)
                if not sil and not a.json:
                    print("  (no silence detected above -45dB)")
                rec["silence_regions"] = regions
            except Exception:
                pass
        if not a.json:
            print(f"{p.name}\t{dur}s")
        results.append(rec)
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    f = sp.add_parser("freesound"); f.add_argument("query"); f.add_argument("--license", choices=FS_LICENSE, default="any")
    f.add_argument("--sort", choices=FS_SORT, default="relevance"); f.add_argument("-n", type=int, default=8); f.add_argument("--urls", action="store_true")
    f.add_argument("-p", "--page", type=int, default=1)
    f.add_argument("--pages", type=int, default=3, help="relevance pages (15 each) to rank by downloads"); f.set_defaults(fn=freesound)

    o = sp.add_parser("commons"); o.add_argument("query"); o.add_argument("--license", choices=FS_LICENSE, default="any")
    o.add_argument("-n", type=int, default=8); o.add_argument("--urls", action="store_true")
    o.set_defaults(fn=commons)

    m = sp.add_parser("mixkit"); m.add_argument("query"); m.add_argument("-n", type=int, default=8)
    m.add_argument("--urls", action="store_true"); m.add_argument("--license", choices=FS_LICENSE, default="any"); m.set_defaults(fn=mixkit)

    al = sp.add_parser("all"); al.add_argument("query"); al.add_argument("--license", choices=FS_LICENSE, default="any")
    al.add_argument("--sort", choices=FS_SORT, default="relevance"); al.add_argument("-n", type=int, default=10); al.add_argument("--urls", action="store_true")
    al.add_argument("-p", "--page", type=int, default=1); al.add_argument("--pages", type=int, default=3); al.set_defaults(fn=all_sources)

    mu = sp.add_parser("music"); mu.add_argument("query"); mu.add_argument("--license", choices=FS_LICENSE, default="any")
    mu.add_argument("-n", type=int, default=10); mu.add_argument("--urls", action="store_true")
    mu.add_argument("--duration", choices=["short", "long"], default="long"); mu.set_defaults(fn=music)

    # Output flags shared by every search command.
    for sub in (f, o, m, al, mu):
        sub.add_argument("--json", action="store_true", help="machine-readable JSON output")
        sub.add_argument("--licenses", action="store_true", help="include the license column")

    g = sp.add_parser("get"); g.add_argument("out_dir"); g.add_argument("keys", nargs="+"); g.set_defaults(fn=get)

    an = sp.add_parser("analyze"); an.add_argument("files", nargs="+"); an.add_argument("--loudness", action="store_true")
    an.add_argument("--silence", action="store_true"); an.add_argument("--json", action="store_true")
    an.set_defaults(fn=analyze)

    a = ap.parse_args()
    try:
        a.fn(a)
    except OSError as e:
        sys.exit(f"network error: {getattr(e, 'reason', e)}" + (" — try clash_freesound.py --fix" if a.cmd in ("freesound", "all") else ""))


if __name__ == "__main__":
    main()
