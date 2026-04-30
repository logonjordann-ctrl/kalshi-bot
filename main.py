from flask import Flask, request
import os, time, base64, uuid, requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

app = Flask(__name__)

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

BASE_URL = "https://demo-api.kalshi.co" if KALSHI_ENV == "demo" else "https://api.kalshi.com"

# Current demo BTC 15-minute ticker
MARKET_TICKER = "KXBCTC15M-26APR301530"


def load_private_key():
    return serialization.load_pem_private_key(
        PRIVATE_KEY_TEXT.encode("utf-8"),
        password=None
    )


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


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.data.decode("utf-8")
    print("ALERT RECEIVED:", data)

    try:
        parsed = dict(part.split("=") for part in data.split("|"))

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

        if direction == "above":
            kalshi_side = "yes"
            price_field = "yes_price"
        elif direction == "below":
            kalshi_side = "no"
            price_field = "no_price"
        else:
            return {"error": "SIDE must be ABOVE or BELOW"}

        order = {
            "ticker": MARKET_TICKER,
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
            timeout=15
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
