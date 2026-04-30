from flask import Flask, request, jsonify
import os
import time
import base64
import uuid
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

app = Flask(__name__)

# Railway environment variables needed:
# KALSHI_API_KEY_ID
# KALSHI_PRIVATE_KEY
# Optional: KALSHI_ENV=prod or demo. Default is prod.
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_ENV = os.getenv("KALSHI_ENV", "prod").lower().strip()

BASE_URL = (
    "https://demo-api.kalshi.co"
    if KALSHI_ENV == "demo"
    else "https://api.elections.kalshi.com"
)

DEFAULT_MAX_PRICE = 0.55


def load_private_key():
    if not PRIVATE_KEY_TEXT:
        raise Exception("Missing KALSHI_PRIVATE_KEY environment variable")

    key_text = PRIVATE_KEY_TEXT.replace("\\n", "\n").strip()

    return serialization.load_pem_private_key(
        key_text.encode("utf-8"),
        password=None,
    )


def sign_request(method, path):
    if not API_KEY_ID:
        raise Exception("Missing KALSHI_API_KEY_ID environment variable")

    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode("utf-8")

    private_key = load_private_key()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
    }


def request_kalshi(method, path, params=None, body=None, timeout=15):
    headers = sign_request(method, path)

    if method.upper() == "GET":
        response = requests.get(
            BASE_URL + path,
            headers=headers,
            params=params,
            timeout=timeout,
        )
    elif method.upper() == "POST":
        response = requests.post(
            BASE_URL + path,
            headers=headers,
            json=body,
            timeout=timeout,
        )
    else:
        raise Exception(f"Unsupported method: {method}")

    print("KALSHI REQUEST:", method.upper(), path, "PARAMS:", params, "STATUS:", response.status_code)
    print("KALSHI RESPONSE:", response.text[:1500])

    return response


def parse_alert(raw_text):
    parsed = {}

    for part in raw_text.replace("\n", "|").split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if key:
            parsed[key] = value

    required = ["SIDE", "STAKE"]
    missing = [key for key in required if key not in parsed]
    if missing:
        raise Exception(f"Alert missing required field(s): {', '.join(missing)}")

    return parsed


def dollars_to_cents(price):
    price_float = float(price)
    if price_float <= 1:
        return int(round(price_float * 100))
    return int(round(price_float))


def normalize_side(value):
    side = str(value).strip().lower()

    if side in ["above", "yes", "up"]:
        return "yes"

    if side in ["below", "no", "down"]:
        return "no"

    raise Exception(f"Invalid SIDE value: {value}")


def market_is_btc_15m(market):
    text_fields = [
        market.get("ticker", ""),
        market.get("event_ticker", ""),
        market.get("series_ticker", ""),
        market.get("title", ""),
        market.get("subtitle", ""),
    ]

    text = " ".join(str(x) for x in text_fields).lower()

    has_btc = "btc" in text or "bitcoin" in text
    has_15m = (
        "kxbtc15m" in text
        or "15m" in text
        or "15 m" in text
        or "15min" in text
        or "15 min" in text
        or "15-minute" in text
        or "15 minute" in text
        or "fifteen minute" in text
    )

    return has_btc and has_15m


def get_all_open_markets():
    path = "/trade-api/v2/markets"
    cursor = None
    markets = []

    while True:
        params = {
            "status": "open",
            "limit": 1000,
        }

        if cursor:
            params["cursor"] = cursor

        response = request_kalshi("GET", path, params=params)
        response.raise_for_status()

        data = response.json()
        page_markets = data.get("markets", [])
        markets.extend(page_markets)

        cursor = data.get("cursor")
        if not cursor:
            break

    print("TOTAL OPEN MARKETS FOUND:", len(markets))
    return markets


def get_market_by_ticker(ticker):
    if not ticker:
        return None

    path = f"/trade-api/v2/markets/{ticker}"
    response = request_kalshi("GET", path)

    if response.status_code == 200:
        data = response.json()
        return data.get("market")

    return None


def normalize_market_hint(value):
    hint = str(value or "").strip().upper()

    if hint in ["", "BTC15M", "BTC_15M", "BTC-15M", "BTCUSD15M", "BTCUSD_15M", "BTCUSD-15M"]:
        return "KXBTC15M"

    return hint


def select_nearest_open_market(candidates):
    now = datetime.now(timezone.utc)

    def close_sort_key(market):
        close_time = market.get("close_time") or market.get("expiration_time") or ""
        try:
            parsed_time = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            if parsed_time < now:
                return "9999-99-99T99:99:99Z"
            return close_time
        except Exception:
            return close_time or "9999-99-99T99:99:99Z"

    candidates.sort(key=close_sort_key)
    return candidates[0]


def get_current_btc_15m_market(alert_market=None):
    exact = normalize_market_hint(alert_market)

    print("NORMALIZED MARKET HINT:", exact)

    # If TradingView sends a real exact Kalshi market ticker, use it.
    # KXBTC15M is a series ticker, not an exact market ticker.
    if exact and exact != "KXBTC15M":
        direct_market = get_market_by_ticker(exact)
        if direct_market and str(direct_market.get("status", "")).lower() == "open":
            print("USING EXACT ALERT MARKET TICKER:", exact)
            return direct_market

    # Search the Kalshi BTC 15-minute series.
    path = "/trade-api/v2/markets"
    response = request_kalshi(
        "GET",
        path,
        params={
            "series_ticker": "KXBTC15M",
            "status": "open",
            "limit": 1000,
        },
    )

    print("SERIES MARKET STATUS:", response.status_code)
    print("SERIES MARKET RESPONSE:", response.text[:1500])

    response.raise_for_status()
    data = response.json()
    candidates = data.get("markets", [])

    # Fallback: if series lookup returns empty, scan all open markets.
    if not candidates:
        print("SERIES LOOKUP EMPTY. FALLING BACK TO ALL OPEN MARKETS.")
        all_open = get_all_open_markets()
        candidates = [m for m in all_open if market_is_btc_15m(m)]

    if not candidates:
        print("BTC 15M CANDIDATES FOUND: 0")
        raise Exception(
            "No open BTC 15-minute market found. Make sure KALSHI_ENV=prod."
        )

    selected = select_nearest_open_market(candidates)

    print("BTC 15M CANDIDATES FOUND:", len(candidates))
    print("SELECTED MARKET:", selected.get("ticker"), selected.get("title"))

    return selected


def calculate_contracts(stake_dollars, max_price_cents):
    stake = float(stake_dollars)
    risk_per_contract = max_price_cents / 100

    if risk_per_contract <= 0:
        raise Exception("MAX_PRICE must be greater than 0")

    contracts = int(stake // risk_per_contract)

    if contracts < 1:
        raise Exception("Stake is too small for at least 1 contract at this max price")

    return contracts


@app.route("/", methods=["GET"])
def home():
    return jsonify(
        {
            "status": "online",
            "service": "Kalshi TradingView webhook bot",
            "environment": KALSHI_ENV,
            "base_url": BASE_URL,
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/test-market", methods=["GET"])
def test_market():
    try:
        market = get_current_btc_15m_market("BTC15M")
        return jsonify(
            {
                "status": "OK",
                "ticker": market.get("ticker"),
                "title": market.get("title"),
                "close_time": market.get("close_time"),
                "status_market": market.get("status"),
            }
        ), 200
    except Exception as error:
        return jsonify({"status": "ERROR", "error": str(error)}), 400


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw_text = request.data.decode("utf-8").strip()
        print("ALERT RECEIVED:", raw_text)

        parsed = parse_alert(raw_text)
        print("PARSED:", parsed)

        side = normalize_side(parsed["SIDE"])
        stake = float(parsed["STAKE"])

        max_price = parsed.get("MAX_PRICE", DEFAULT_MAX_PRICE)
        max_price_cents = dollars_to_cents(max_price)

        if max_price_cents > 99:
            raise Exception("MAX_PRICE is too high. Use 0.55 for 55 cents, or 55.")

        market_hint = parsed.get("MARKET")
        market = get_current_btc_15m_market(market_hint)
        market_ticker = market["ticker"]

        contracts = calculate_contracts(stake, max_price_cents)

        price_field = "yes_price" if side == "yes" else "no_price"

        order = {
            "ticker": market_ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "action": "buy",
            "count": contracts,
            "type": "limit",
            price_field: max_price_cents,
        }

        print("ORDER:", order)

        path = "/trade-api/v2/portfolio/orders"
        response = request_kalshi("POST", path, body=order)

        result = {
            "status": "ORDER SENT" if response.status_code in [200, 201] else "ORDER REJECTED",
            "status_code": response.status_code,
            "market_ticker": market_ticker,
            "side": side,
            "contracts": contracts,
            "max_price_cents": max_price_cents,
            "kalshi_response": response.text,
        }

        print("RESULT:", result)
        return jsonify(result), 200

    except Exception as error:
        print("ERROR:", str(error))
        return jsonify({"status": "ERROR", "error": str(error)}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
