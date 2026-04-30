from flask import Flask, request

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

        live_price = 0.50  # placeholder

        if live_price > max_price:
            return {"status": "SKIPPED - PRICE TOO HIGH"}

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
