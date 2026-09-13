#!/usr/bin/env python3
"""
Refresh all-time Spotify stream counts in songs.json from kworb.net.

kworb.net publishes Spotify's own cumulative play counts as plain HTML, updated
once a day (Spotify itself only refreshes them daily). This script pulls today's
numbers and writes them back into songs.json:

    streams       cumulative all-time plays  (int)
    daily         plays in the last day      (int)
    tier          1B+ / 500M-1B / 100-500M / <100M   (derived)
    streamsAsOf   date of this refresh
  meta.streamsAsOf / meta.streamsRefreshed

The printed cards don't change - they show only the tier. This just keeps the
number on the player's Stats screen current.

Usage
-----
    python build/refresh_streams.py songs.json               # full (all artists)
    python build/refresh_streams.py songs.json --fast        # global top-2500 only
    python build/refresh_streams.py songs.json \
        --spotify-id XXX --spotify-secret YYY                 # faster artist-id lookup

Artist -> Spotify-id resolution is cached in build/artist_ids.json, so only the
first full run is slow (~12-15 min). Later runs are ~5 min, or seconds with --fast.

Be polite to kworb: one request/second, real User-Agent, pages cached per run.
"""

import argparse
import base64
import datetime
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

UA = "songdeck-refresh/0.1 (personal music-trivia project)"
KWORB_GLOBAL = "https://kworb.net/spotify/songs.html"
KWORB_ARTIST = "https://kworb.net/spotify/artist/{id}_songs.html"
CACHE_PATH = os.path.join(os.path.dirname(__file__), "artist_ids.json")


# ----------------------------------------------------------------- helpers
def norm(s):
    s = unicodedata.normalize("NFKD", str(s) if s else "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    s = re.sub(r"\b(feat|ft|with)\b.*", "", s)
    s = re.sub(r"-\s*(remaster|remastered|mono|stereo|single version|radio edit).*", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def toks(s):
    return set(norm(s).split())


def fetch(url, tries=3, timeout=30):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    return ""


def parse_kworb_table(page, with_artist):
    """Yield (artist_or_None, title, total, daily) from a kworb songs table."""
    for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if with_artist:
            if len(cells) < 3 or " - " not in cells[0]:
                continue
            artist, title = cells[0].split(" - ", 1)
            total, daily = cells[1], cells[2]
        else:
            if len(cells) != 3:
                continue
            artist, title, (total, daily) = None, cells[0], (cells[1], cells[2])
        t = re.sub(r"[^0-9]", "", total)
        d = re.sub(r"[^0-9]", "", daily)
        if not t.isdigit():
            continue
        yield (norm(artist) if artist else None, title.strip(),
               int(t), int(d) if d.isdigit() else None)


def tier_of(streams):
    if streams >= 1_000_000_000:
        return "1B+"
    if streams >= 500_000_000:
        return "500M-1B"
    if streams >= 100_000_000:
        return "100-500M"
    return "<100M"


# ------------------------------------------------ artist -> spotify id
def spotify_token(cid, secret):
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["access_token"]


def id_via_spotify(name, token):
    q = urllib.parse.urlencode({"q": name, "type": "artist", "limit": 5})
    req = urllib.request.Request("https://api.spotify.com/v1/search?" + q,
                                 headers={"Authorization": "Bearer " + token})
    items = json.loads(urllib.request.urlopen(req, timeout=20).read())["artists"]["items"]
    for it in items:
        if norm(it["name"]) == norm(name):
            return it["id"]
    return items[0]["id"] if items else None


def id_via_musicbrainz(name):
    base = "https://musicbrainz.org/ws/2/"
    j = json.loads(fetch(base + "artist/?" + urllib.parse.urlencode(
        {"query": name, "fmt": "json", "limit": 1})))
    if not j.get("artists"):
        return None
    aid = j["artists"][0]["id"]
    time.sleep(1.1)
    rel = json.loads(fetch(base + f"artist/{aid}?inc=url-rels&fmt=json"))
    for r in rel.get("relations", []):
        u = r.get("url", {}).get("resource", "")
        if "open.spotify.com/artist/" in u:
            return u.split("/artist/")[-1].split("?")[0]
    return None


def load_cache():
    try:
        return json.load(open(CACHE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"), indent=1, ensure_ascii=False, sort_keys=True)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("songs_json")
    ap.add_argument("--fast", action="store_true",
                    help="global top-2500 page only; skip per-artist pages")
    ap.add_argument("--spotify-id", default=os.environ.get("SPOTIFY_CLIENT_ID"))
    ap.add_argument("--spotify-secret", default=os.environ.get("SPOTIFY_CLIENT_SECRET"))
    ap.add_argument("--sleep", type=float, default=1.1, help="seconds between requests")
    args = ap.parse_args()

    data = json.load(open(args.songs_json, encoding="utf-8"))
    songs = data.get("songs", {})
    if not songs:
        sys.exit("no songs in " + args.songs_json)
    today = datetime.date.today().isoformat()

    # 1) global page
    print("fetching kworb global top-2500 ...")
    g_pair, g_title = {}, {}
    for a, ti, total, daily in parse_kworb_table(fetch(KWORB_GLOBAL), with_artist=True):
        g_pair[(a, norm(ti))] = (total, daily)
        g_title.setdefault(norm(ti), []).append((a, total, daily))

    def apply(sid, total, daily):
        s = songs[sid]
        s["streams"] = total
        s["daily"] = daily
        s["tier"] = tier_of(total)
        s["streamsAsOf"] = today

    matched, pending = 0, []
    for sid, s in songs.items():
        na, nt = norm(s.get("artist", "")), norm(s.get("title", ""))
        hit = g_pair.get((na, nt))
        if not hit and nt in g_title and len(g_title[nt]) == 1:
            ka, kt, kd = g_title[nt][0]
            if toks(s.get("artist", "")) & set(ka.split()):
                hit = (kt, kd)
        if hit:
            apply(sid, *hit)
            matched += 1
        else:
            pending.append(sid)
    print(f"  matched {matched}/{len(songs)} from global page")

    # 2) per-artist pages
    if pending and not args.fast:
        cache = load_cache()
        token = None
        if args.spotify_id and args.spotify_secret:
            try:
                token = spotify_token(args.spotify_id, args.spotify_secret)
                print("  using Spotify API for artist-id lookup")
            except Exception as e:
                print(f"  Spotify auth failed ({e}); falling back to MusicBrainz")

        by_artist = {}
        for sid in pending:
            by_artist.setdefault(songs[sid].get("artist", ""), []).append(sid)

        print(f"resolving {len(by_artist)} artists + fetching kworb pages ...")
        for i, (artist, sids) in enumerate(sorted(by_artist.items()), 1):
            aid = cache.get(artist, "MISS")
            if aid == "MISS":
                try:
                    aid = (id_via_spotify(artist, token) if token
                           else id_via_musicbrainz(artist))
                except Exception as e:
                    print(f"  ! id lookup failed for {artist}: {e}")
                    aid = None
                cache[artist] = aid
                save_cache(cache)
                time.sleep(args.sleep)
            if not aid:
                continue
            try:
                page = fetch(KWORB_ARTIST.format(id=aid))
            except Exception as e:
                print(f"  ! kworb page failed for {artist} ({aid}): {e}")
                time.sleep(args.sleep)
                continue
            atitles = {}
            for _, ti, total, daily in parse_kworb_table(page, with_artist=False):
                atitles.setdefault(norm(ti), (total, daily))
            for sid in sids:
                hit = atitles.get(norm(songs[sid]["title"]))
                if hit:
                    apply(sid, *hit)
                    matched += 1
            if i % 25 == 0:
                print(f"  {i}/{len(by_artist)} artists")
            time.sleep(args.sleep)

    # 3) write back
    still = [sid for sid, s in songs.items() if s.get("streams") in (None, "", "PLACEHOLDER")]
    data.setdefault("meta", {})["streamsAsOf"] = today
    data["meta"]["streamsRefreshed"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    json.dump(data, open(args.songs_json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    print(f"\ndone: {matched}/{len(songs)} songs have a stream count.")
    if still:
        print(f"{len(still)} still missing (no kworb data): "
              + ", ".join(f'{songs[s]["artist"]} - {songs[s]["title"]}' for s in still[:15])
              + (" ..." if len(still) > 15 else ""))


if __name__ == "__main__":
    main()
