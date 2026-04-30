from flask import Flask, request
import os

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

        # Safety check (your 55% rule)
        if live_price > max_price:
            return {"status": "SKIPPED - PRICE TOO HIGH"}

        # Calculate contracts
        contracts = int(stake / live_price)

        if contracts < 1:
            return {"status": "SKIPPED - TOO SMALL"}

        order = {
            "ticker": "BTC-15M",
            "action": "buy",
            "side": side,
            "count": contracts
        }

        print("ORDER:", order)

        return {"status": "ORDER SENT", "order": order}

    except Exception as e:
        return {"error": str(e)}

# ✅ CRITICAL FIX FOR RAILWAY (DO NOT REMOVE)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
