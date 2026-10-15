"""Download the free DB-IP "IP to City Lite" database (CC BY 4.0, https://db-ip.com) for algo_rhythm.places:

    python tools/fetch_geoip.py DEST.mmdb

It is published monthly; this takes this month's, else last month's. The server image runs it at build time.
"""
import datetime
import gzip
import shutil
import sys
import urllib.request
from pathlib import Path


def main(dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    last = today.replace(day=1) - datetime.timedelta(days=1)
    for d in (today, last):
        url = f"https://download.db-ip.com/free/dbip-city-lite-{d:%Y-%m}.mmdb.gz"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8 (algo-rhythm build)"})  # the default is refused
            with urllib.request.urlopen(req, timeout=120) as r, gzip.open(r) as g, open(dest, "wb") as out:
                shutil.copyfileobj(g, out)
            print(f"{url} -> {dest} ({dest.stat().st_size // 1_000_000} MB)")
            return 0
        except Exception as e:
            print(f"{url}: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
