from flask import Flask, request
import os, time, base64, uuid, requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

app = Flask(__name__)

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

BASE_URL = "https://demo-api.kalshi.co" if KALSHI_ENV == "demo" else "https://api.elections.kalshi.com"
BTC_SERIES = "KXBCTC15M"


def load_private_key():
    return serialization.load_pem_private_key(PRIVATE_KEY_TEXT.encode("utf-8"), password=None)


def sign_request(method, path):
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}".encode("utf-8")

    private_key = load_private_key()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )

    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json"
    }


def get_current_btc_ticker():
    path = "/trade-api/v2/markets"

    response = requests.get(
        BASE_URL + path,
        params={
            "series_ticker": BTC_SERIES,
            "status": "open",
            "limit": 100
        },
        timeout=10
    )

    print("MARKETS STATUS:", response.status_code)
    print("MARKETS RESPONSE:", response.text)

    data = response.json()
    markets = data.get("markets", [])

    active = [
        m for m in markets
        if m.get("ticker", "").startswith(BTC_SERIES)
        and m.get("status") == "open"
    ]

    if not active:
        raise Exception("No active BTC 15m market found")

    active.sort(key=lambda m: m.get("close_time") or m.get("expiration_time") or "")
    ticker = active[0]["ticker"]

    print("ACTIVE MARKET TICKER:", ticker)
    return ticker


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.data.decode("utf-8")
    print("ALERT RECEIVED:", data)

    try:
        parsed = dict(part.split("=") for part in data.split("|"))
        print("PARSED:", parsed)

        direction = parsed["SIDE"].lower()
        stake = float(parsed["STAKE"])
        max_price = float(parsed["MAX_PRICE"])

        live_price = 0.50

        if live_price > max_price:
            print("SKIPPED - PRICE TOO HIGH")
            return {"status": "SKIPPED - PRICE TOO HIGH"}

        contracts = int(stake / live_price)

        if contracts < 1:
            print("SKIPPED - TOO SMALL")
            return {"status": "SKIPPED - TOO SMALL"}

        if direction in ["above", "yes"]:
            kalshi_side = "yes"
            price_field = "yes_price"
        elif direction in ["below", "no"]:
            kalshi_side = "no"
            price_field = "no_price"
        else:
            print("INVALID SIDE:", direction)
            return {"error": f"Invalid SIDE: {direction}"}

        market_ticker = get_current_btc_ticker()

        order = {
            "ticker": market_ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": kalshi_side,
            "action": "buy",
            "count": contracts,
            "type": "limit",
            price_field: int(live_price * 100)
        }

        print("ORDER:", order)

        path = "/trade-api/v2/portfolio/orders"
        headers = sign_request("POST", path)

        response = requests.post(
            BASE_URL + path,
            headers=headers,
            json=order,
            timeout=10
        )

        print("STATUS CODE:", response.status_code)
        print("KALSHI RESPONSE:", response.text)

        return {
            "status": "ORDER SENT",
            "status_code": response.status_code,
            "response": response.text
        }

    except Exception as e:
        print("ERROR:", str(e))
        return {"error": str(e)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
