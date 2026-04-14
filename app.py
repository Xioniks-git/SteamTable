import os
import time
import threading
import requests

from flask import Flask, request, jsonify, abort
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

API_KEY = os.getenv("API_KEY", "")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "21600"))  # 6 часов
STEAM_URL = "https://store.steampowered.com/api/appdetails"

# Простенький in-memory cache
_cache = {}
_cache_lock = threading.Lock()


def get_cache(key):
    now = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < now:
            _cache.pop(key, None)
            return None
        return value


def set_cache(key, value):
    expires_at = time.time() + CACHE_TTL_SECONDS
    with _cache_lock:
        _cache[key] = (expires_at, value)


def create_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://store.steampowered.com/",
        "Origin": "https://store.steampowered.com",
    })
    return session


session = create_session()


def require_api_key():
    if not API_KEY:
        return
    incoming = request.headers.get("X-API-Key", "")
    if incoming != API_KEY:
        abort(401, description="Invalid API key")


def normalize_app_ids(raw_ids):
    result = []
    for value in raw_ids:
        app_id = str(value).strip()
        if app_id.isdigit():
            result.append(app_id)
    return list(dict.fromkeys(result))  # убираем дубли, сохраняя порядок


def extract_price(entry):
    if not entry or not entry.get("success") or not entry.get("data"):
        return ""

    data = entry["data"]

    if data.get("is_free") is True:
        return "Free"

    price_overview = data.get("price_overview") or {}
    if price_overview.get("final_formatted"):
        return price_overview["final_formatted"]

    for group in data.get("package_groups") or []:
        for sub in group.get("subs") or []:
            price = sub.get("price")
            if price:
                return price

    return ""


def fetch_prices_for_country(app_ids, country_code):
    result = {}

    for app_id in app_ids:
        cache_key = f"{app_id}:{country_code}"
        cached = get_cache(cache_key)
        if cached is not None:
            result[app_id] = cached
            continue

        try:
            response = session.get(
                STEAM_URL,
                params={
                    "appids": app_id,
                    "cc": country_code,
                    "l": "en",
                },
                timeout=20,
            )

            if response.status_code == 403:
                print(f"403 Forbidden for cc={country_code}, appid={app_id}")
                result[app_id] = ""
                time.sleep(1.5)
                continue

            if response.status_code != 200:
                print(f"HTTP {response.status_code} for cc={country_code}, appid={app_id}")
                result[app_id] = ""
                time.sleep(1.5)
                continue

            payload = response.json()
            value = extract_price(payload.get(app_id))
            result[app_id] = value
            set_cache(cache_key, value)

            time.sleep(0.35)

        except Exception as e:
            print(f"Request error for cc={country_code}, appid={app_id}: {e}")
            result[app_id] = ""
            time.sleep(1.5)

    return result


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/prices")
def prices():
    require_api_key()

    data = request.get_json(silent=True) or {}
    app_ids = normalize_app_ids(data.get("appIds", []))
    countries = data.get("countries", ["ru", "kz", "us"])

    if not app_ids:
        return jsonify({"prices": {}})

    allowed_countries = {"ru", "kz", "us"}
    countries = [c for c in countries if c in allowed_countries]
    if not countries:
        countries = ["ru", "kz", "us"]

    prices_map = {app_id: {} for app_id in app_ids}

    for country_code in countries:
        country_prices = fetch_prices_for_country(app_ids, country_code)
        for app_id in app_ids:
            prices_map[app_id][country_code] = country_prices.get(app_id, "")

    return jsonify({"prices": prices_map})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)