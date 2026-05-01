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

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_ENV = os.getenv("KALSHI_ENV", "prod").lower().strip()

BASE_URL = (
    "https://demo-api.kalshi.co"
    if KALSHI_ENV == "demo"
    else "https://api.elections.kalshi.com"
)

# ✅ FIXED DEFAULT
DEFAULT_MAX_PRICE = 0.45

MAX_WAIT_SECONDS = 90
FRESH_MARKET_SECONDS_LEFT = 600


def load_private_key():
    if not PRIVATE_KEY_TEXT:
        raise Exception("Missing KALSHI_PRIVATE_KEY")

    key_text = PRIVATE_KEY_TEXT.replace("\\n", "\n").strip()

    return serialization.load_pem_private_key(
        key_text.encode("utf-8"),
        password=None,
    )


def sign_request(method, path):
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
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


def request_kalshi(method, path, params=None, body=None):
    headers = sign_request(method, path)

    if method == "GET":
        r = requests.get(BASE_URL + path, headers=headers, params=params)
    else:
        r = requests.post(BASE_URL + path, headers=headers, json=body)

    print("KALSHI:", r.status_code, r.text[:500])
    return r


def parse_alert(raw):
    parsed = {}

    for part in raw.replace("\n", "|").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            parsed[k.strip().upper()] = v.strip()

    if "SIDE" not in parsed and "ACTION" in parsed:
        parsed["SIDE"] = parsed["ACTION"]

    if "SIDE" not in parsed or "STAKE" not in parsed:
        raise Exception("Missing SIDE or STAKE")

    return parsed


def normalize_side(v):
    v = v.lower()

    if v in ["above", "yes", "up"]:
        return "yes"
    if v in ["below", "no", "down"]:
        return "no"

    raise Exception("Invalid SIDE")


def dollars_to_cents(p):
    p = float(p)
    return int(p * 100) if p <= 1 else int(p)


def get_markets():
    r = request_kalshi(
        "GET",
        "/trade-api/v2/markets",
        {
            "series_ticker": "KXBTC15M",
            "status": "open",
            "limit": 1000,
        },
    )
    return r.json().get("markets", [])


def seconds_left(m):
    t = m.get("close_time") or m.get("expiration_time")
    t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (t - datetime.now(timezone.utc)).total_seconds()


def select_market():
    markets = get_markets()

    valid = []
    for m in markets:
        s = seconds_left(m)
        print("CHECK:", m["ticker"], s)

        if s >= FRESH_MARKET_SECONDS_LEFT:
            valid.append(m)

    if not valid:
        raise Exception("No fresh market")

    valid.sort(key=lambda m: m["close_time"])
    return valid[0]


def wait_for_market():
    start = time.time()

    while time.time() - start < MAX_WAIT_SECONDS:
        try:
            return select_market()
        except:
            time.sleep(1)

    raise Exception("Timeout waiting for new market")


def calc_contracts(stake, price_cents):
    stake = float(stake)
    risk = price_cents / 100

    c = int(stake // risk)
    if c < 1:
        raise Exception("Stake too small")

    return c


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw = request.data.decode()
        print("ALERT:", raw)

        p = parse_alert(raw)

        side = normalize_side(p["SIDE"])
        stake = float(p["STAKE"])

        max_price = p.get("MAX_PRICE", DEFAULT_MAX_PRICE)
        max_price_cents = dollars_to_cents(max_price)

        market = wait_for_market()

        contracts = calc_contracts(stake, max_price_cents)

        price_field = "yes_price" if side == "yes" else "no_price"

        # ✅ CORRECT LIMIT ORDER (NO time_in_force)
        order = {
            "ticker": market["ticker"],
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "action": "buy",
            "count": contracts,
            "type": "limit",
            price_field: max_price_cents,
        }

        print("ORDER:", order)

        r = request_kalshi("POST", "/trade-api/v2/portfolio/orders", body=order)

        return jsonify({
            "status": r.status_code,
            "response": r.text
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/")
def home():
    return {"status": "running", "default_max_price": DEFAULT_MAX_PRICE}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
