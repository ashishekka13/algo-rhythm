"""Where a party is, at the level the MC may say it out loud: "Bengaluru" for a city most people know, else the
country ("India"). Never anything finer, and the address it came from is not kept.

    loc = places.lookup(ip)                 {"city": "Bengaluru", "country": "India"} or None (offline database)
    loc = places.parse("Bangalore, India")  the host's own words, tidied: {"city": "Bengaluru", "country": "India"}
    say, key = places.spoken(loc)           ("Bengaluru", "bengaluru"): the name the MC uses, and its key for
                                            matching stored MC lines (algo_rhythm.mc_lines)

IP lookups use the free DB-IP "IP to City Lite" database (CC BY 4.0, https://db-ip.com), read from GEOIP_DB
(tools/fetch_geoip.py puts it in the server image). Without it, lookup() answers None and the host can type the place.
"""
import ipaddress
import logging
import os
import re
import threading

log = logging.getLogger("algo_rhythm.places")
DEFAULT_DB = "/app/geo/dbip-city-lite.mmdb"

# Old or other spellings people (and the database) use -> the name the MC says
ALIASES = {"bangalore": "Bengaluru", "bengaluru": "Bengaluru", "bombay": "Mumbai", "calcutta": "Kolkata",
           "madras": "Chennai", "new delhi": "Delhi", "gurgaon": "Gurugram", "poona": "Pune", "mysore": "Mysuru",
           "cochin": "Kochi", "trivandrum": "Thiruvananthapuram", "vizag": "Visakhapatnam", "baroda": "Vadodara",
           "panaji": "Goa", "panjim": "Goa", "nyc": "New York", "new york city": "New York", "sf": "San Francisco"}
# Cities most people in the room would recognise by name; anywhere else, the MC says the country
POPULAR = {c.lower() for c in (
    "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Pune", "Ahmedabad", "Jaipur", "Goa",
    "Chandigarh", "Lucknow", "Kochi", "Gurugram", "Noida", "Indore", "Surat", "Nagpur", "Bhopal", "Mysuru",
    "Coimbatore", "Visakhapatnam", "Vadodara", "Udaipur", "Amritsar", "Ludhiana", "Dehradun", "Shimla", "Rishikesh",
    "Varanasi", "Patna", "Ranchi", "Bhubaneswar", "Guwahati", "Shillong", "Thiruvananthapuram", "Mangaluru",
    "Kathmandu", "Dhaka", "Colombo", "Karachi", "Lahore", "Dubai", "Abu Dhabi", "Doha", "Muscat", "Riyadh",
    "Singapore", "Kuala Lumpur", "Bangkok", "Hong Kong", "Tokyo", "Seoul", "Sydney", "Melbourne", "Auckland",
    "London", "Manchester", "Birmingham", "Leicester", "Paris", "Berlin", "Amsterdam", "Dublin", "Madrid", "Rome",
    "New York", "New Jersey", "Chicago", "Houston", "Dallas", "Boston", "Seattle", "San Francisco", "Los Angeles",
    "Atlanta", "Washington", "Toronto", "Vancouver", "Montreal", "Brampton", "Nairobi", "Johannesburg", "Cape Town")}
COUNTRY_ALIASES = {"bharat": "India", "hindustan": "India", "usa": "United States", "us": "United States",
                   "america": "United States", "uk": "United Kingdom", "england": "United Kingdom", "uae": "UAE",
                   "united arab emirates": "UAE"}

_db = {"reader": None, "tried": False, "lock": threading.Lock()}


def _reader():
    with _db["lock"]:
        if not _db["tried"]:
            _db["tried"] = True
            path = os.environ.get("GEOIP_DB", DEFAULT_DB)
            try:
                import maxminddb
                _db["reader"] = maxminddb.open_database(path)
                log.info("IP location database %s loaded", path)
            except Exception as e:  # no database here (a laptop, the tests): the host types the place instead
                log.info("no IP location database (%s): %s", path, e)
        return _db["reader"]


def _title(s):
    return " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() for w in s.split())


def tidy(city=None, country=None):
    """{"city", "country"} with known spellings, or None when there is nothing usable."""
    city = re.sub(r"\s*\([^)]*\)", "", str(city or ""))  # "New Delhi (block N)" -> "New Delhi"
    city = re.sub(r"\s+", " ", city).strip()[:60]
    country = re.sub(r"\s+", " ", str(country or "")).strip()[:60]
    if city:
        city = ALIASES.get(city.lower(), _title(city))
    if country:
        country = COUNTRY_ALIASES.get(country.lower(), _title(country))
    if not city and not country:
        return None
    return {"city": city or None, "country": country or None}


def lookup(ip):
    """City and country for an IP address (offline database), or None: unknown, private or no database."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if not addr.is_global:
        return None
    r = _reader()
    if r is None:
        return None
    try:
        rec = r.get(str(addr)) or {}
    except Exception:
        return None
    name = lambda k: ((rec.get(k) or {}).get("names") or {}).get("en")
    return tidy(name("city"), name("country"))


def parse(text):
    """The host's words, "Bengaluru, India" or just "Goa", as {"city", "country"}; None for an empty text."""
    parts = [p.strip() for p in str(text or "").split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:  # one name: a country if it is one we know, else a city
        one = parts[0]
        if one.lower() in COUNTRY_ALIASES or one.lower() in ("india", "nepal", "sri lanka", "bangladesh", "pakistan",
                                                              "united states", "united kingdom", "canada", "australia"):
            return tidy(None, one)
        return tidy(one, None)
    return tidy(parts[0], parts[-1])


def spoken(loc):
    """(what the MC says, matching key) for a location: the city when it is a well-known one, else the country;
    (None, None) without a location."""
    if not loc:
        return None, None
    city, country = loc.get("city"), loc.get("country")
    say = city if city and city.lower() in POPULAR else (country or city)
    if not say:
        return None, None
    return say, re.sub(r"[^a-z0-9]+", "-", say.lower()).strip("-")


def label(loc):
    """"Bengaluru, India" for showing and editing."""
    return ", ".join(x for x in ((loc or {}).get("city"), (loc or {}).get("country")) if x)
