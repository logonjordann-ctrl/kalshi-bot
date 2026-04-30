from flask import Flask, request
import os
import requests

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.data.decode("utf-8")
    print("ALERT RECEIVED:", data)

    try:
        parsed = dict(part.split("=") for part in data.split("|"))

        side = parsed["SIDE"].lower()
        stake = float(parsed["STAKE"])
        max_price = float(parsed["MAX_PRICE"])

        # TEMP price (we will replace with real Kalshi price later)
        live_price = 0.50

        # Safety check
        if live_price > max_price:
            return {"status": "SKIPPED - PRICE TOO HIGH"}

        # Calculate contracts
        contracts = int(stake / live_price)

        if contracts < 1:
            return {"status": "SKIPPED - TOO SMALL"}

        order = {
            "ticker": "BTC-15M",
            "client_order_id": "tv-bot-1",
            "side": side.upper(),
            "action": "BUY",
            "count": contracts,
            "type": "market"
        }

        print("ORDER:", order)

        # Choose environment
        if os.getenv("KALSHI_ENV") == "demo":
            url = "https://demo-api.kalshi.co/trade-api/v2/portfolio/orders"
        else:
            url = "https://api.kalshi.co/trade-api/v2/portfolio/orders"

        response = requests.post(url, json=order)

        print("KALSHI RESPONSE:", response.text)

        return {"status": "ORDER SENT", "response": response.text}

    except Exception as e:
        return {"error": str(e)}

# Required for Railway
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
