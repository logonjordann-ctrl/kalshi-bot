from flask import Flask, request, jsonify
import os
import time
import json
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
# Optional but recommended with Railway volume:
# BOT_STATE_FILE=/data/kalshi_bot_state.json

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_TEXT = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_ENV = os.getenv("KALSHI_ENV", "prod").lower().strip()

BASE_URL = (
    "https://demo-api.kalshi.co"
    if KALSHI_ENV == "demo"
    else "https://api.elections.kalshi.com"
)

DEFAULT_MAX_PRICE = 0.45

MAX_WAIT_SECONDS = 90
FRESH_MARKET_SECONDS_LEFT = 600

LADDER_START_STEP = 1
LADDER_MAX_STEP = 23

STATE_FILE = os.getenv("BOT_STATE_FILE", "kalshi_bot_state.json")


# Exact ladder through Step 23.
# Based on the user's Kalshi Exact Step 1 Up / Step 1 Down ladder.
# Step 1 stake is the implied stake needed to move $29 -> $30 at the 40c model:
# $0.67 risk produces about $1.00 profit at 150% profit-on-risk.
def stake_for_step(step_num):
    stakes = {
        1: 0.67,
        2: 1.00,
        3: 1.50,
        4: 2.25,
        5: 3.37,
        6: 5.05,
        7: 7.58,
        8: 11.37,
        9: 17.05,
        10: 25.58,
        11: 38.37,
        12: 57.56,
        13: 86.34,
        14: 129.51,
        15: 194.26,
        16: 291.39,
        17: 437.08,
        18: 655.62,
        19: 983.43,
        20: 1475.14,
        21: 2212.71,
        22: 3319.06,
        23: 4978.59,
    }

    step = int(step_num)
    step = max(LADDER_START_STEP, min(step, LADDER_MAX_STEP))
    return float(stakes[step])


def default_state():
    return {
        "current_step": LADDER_START_STEP,
        "open_trade": None,
        "last_result": "N/A",
        "total_orders_sent": 0,
        "total_filled_trades": 0,
        "total_no_fills": 0,
        "total_wins": 0,
        "total_losses": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                loaded = json.load(file)

            base = default_state()
            base.update(loaded)

            # Safety clamp in case old state has a bad step.
            base["current_step"] = max(
                LADDER_START_STEP,
                min(int(base.get("current_step", LADDER_START_STEP)), LADDER_MAX_STEP),
            )

            return base

    except Exception as error:
        print("STATE LOAD ERROR:", str(error))

    return default_state()


def save_state(state):
    state["current_step"] = max(
        LADDER_START_STEP,
        min(int(state.get("current_step", LADDER_START_STEP)), LADDER_MAX_STEP),
    )
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp_path = STATE_FILE + ".tmp"

    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)

    os.replace(tmp_path, STATE_FILE)


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
    print("KALSHI RESPONSE:", response.text[:2000])

    return response


def parse_alert(raw_text):
    parsed = {}

    for part in raw_text.replace("\n", "|").split("|"):
        if "=" not in part:
            continue

        key, value = part.split("=", 1)
        parsed[key.strip().upper()] = value.strip()

    if "SIDE" not in parsed and "ACTION" in parsed:
        parsed["SIDE"] = parsed["ACTION"]

    missing = [key for key in ["SIDE"] if key not in parsed]

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


def normalize_market_hint(value):
    hint = str(value or "").strip().upper()

    if hint in ["", "BTC15M", "BTC_15M", "BTC-15M", "BTCUSD15M", "BTCUSD_15M", "BTCUSD-15M"]:
        return "KXBTC15M"

    return hint


def seconds_until_market_close(market):
    close_time = market.get("close_time") or market.get("expiration_time") or ""

    if not close_time:
        return None

    parsed_close_time = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    return (parsed_close_time - now).total_seconds()


def get_market_by_ticker(ticker):
    path = f"/trade-api/v2/markets/{ticker}"
    response = request_kalshi("GET", path)

    if response.status_code == 200:
        return response.json().get("market")

    return None


def get_btc_15m_candidates():
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

    response.raise_for_status()
    return response.json().get("markets", [])


def select_fresh_new_market(candidates):
    valid_markets = []

    for market in candidates:
        try:
            seconds_left = seconds_until_market_close(market)
        except Exception:
            continue

        if seconds_left is None:
            continue

        print(
            "MARKET CHECK:",
            market.get("ticker"),
            "SECONDS LEFT:",
            seconds_left,
            "CLOSE:",
            market.get("close_time") or market.get("expiration_time"),
        )

        if seconds_left >= FRESH_MARKET_SECONDS_LEFT:
            valid_markets.append(market)

    if not valid_markets:
        raise Exception("No fresh BTC 15-minute market available yet")

    valid_markets.sort(
        key=lambda market: datetime.fromisoformat(
            (market.get("close_time") or market.get("expiration_time")).replace("Z", "+00:00")
        )
    )

    return valid_markets[0]


def wait_for_fresh_btc_15m_market(alert_market=None):
    exact = normalize_market_hint(alert_market)

    print("NORMALIZED MARKET HINT:", exact)

    start_time = time.time()
    last_error = None

    while time.time() - start_time < MAX_WAIT_SECONDS:
        try:
            if exact and exact != "KXBTC15M":
                direct_market = get_market_by_ticker(exact)

                if direct_market:
                    selected = select_fresh_new_market([direct_market])
                    print("SELECTED EXACT MARKET:", selected.get("ticker"))
                    return selected

            candidates = get_btc_15m_candidates()

            if candidates:
                selected = select_fresh_new_market(candidates)
                print(
                    "SELECTED FRESH MARKET:",
                    selected.get("ticker"),
                    selected.get("title"),
                    selected.get("close_time"),
                )
                return selected

            last_error = Exception("No BTC 15m candidates returned")

        except Exception as error:
            last_error = error
            print("MARKET NOT READY YET. RETRYING:", str(error))

        time.sleep(1)

    raise Exception(
        f"No fresh BTC 15-minute market found after {MAX_WAIT_SECONDS} seconds. "
        f"Last error: {last_error}"
    )


def calculate_contracts(stake_dollars, max_price_cents):
    stake = float(stake_dollars)
    risk_per_contract = max_price_cents / 100

    if stake <= 0:
        raise Exception("STAKE must be greater than 0")

    if risk_per_contract <= 0:
        raise Exception("MAX_PRICE must be greater than 0")

    contracts = int(stake // risk_per_contract)

    if contracts < 1:
        raise Exception("Stake is too small for at least 1 contract at this max price")

    return contracts


def extract_order(response_json):
    if not isinstance(response_json, dict):
        return {}

    if isinstance(response_json.get("order"), dict):
        return response_json["order"]

    return response_json


def get_order(order_id):
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    response = request_kalshi("GET", path)

    if response.status_code != 200:
        return None

    return extract_order(response.json())


def order_counts(order):
    count = int(order.get("count") or order.get("order_count") or 0)
    remaining = int(order.get("remaining_count") or order.get("remaining") or 0)

    filled = (
        order.get("filled_count")
        or order.get("fill_count")
        or order.get("matched_count")
        or None
    )

    if filled is not None:
        filled = int(filled)
    elif count > 0:
        filled = max(count - remaining, 0)
    else:
        filled = 0

    return count, remaining, filled


def market_result(market):
    if not market:
        return None

    result = (
        market.get("result")
        or market.get("market_result")
        or market.get("settlement_value")
        or ""
    )

    result = str(result).strip().lower()

    if result in ["yes", "y", "1"]:
        return "yes"

    if result in ["no", "n", "0"]:
        return "no"

    return None


def resolve_previous_trade(state, max_wait_seconds=90):
    trade = state.get("open_trade")

    if not trade:
        return {"resolved": True, "message": "No open trade to resolve."}

    print("RESOLVING PREVIOUS TRADE:", trade)

    start_time = time.time()
    last_message = ""

    while time.time() - start_time < max_wait_seconds:
        order_id = trade.get("order_id")
        ticker = trade.get("market_ticker")
        side = trade.get("side")

        order = get_order(order_id) if order_id else None

        if not order:
            last_message = "Could not retrieve previous order yet."
            print(last_message)
            time.sleep(2)
            continue

        status = str(order.get("status", "")).lower()
        count, remaining, filled = order_counts(order)

        print("PREVIOUS ORDER STATUS:", status, "COUNT:", count, "REMAINING:", remaining, "FILLED:", filled)

        # No fill = no real trade. Step stays unchanged.
        if filled <= 0 and status in ["canceled", "cancelled", "expired", "rejected"]:
            state["total_no_fills"] += 1
            state["last_result"] = "NO FILL"
            state["open_trade"] = None
            save_state(state)

            return {
                "resolved": True,
                "message": "Previous order had no fill. Step unchanged.",
                "step": state["current_step"],
                "stake": stake_for_step(state["current_step"]),
            }

        if filled <= 0 and status in ["resting", "open", "pending"]:
            market = get_market_by_ticker(ticker) if ticker else None
            seconds_left = seconds_until_market_close(market) if market else None

            if seconds_left is not None and seconds_left <= 0:
                state["total_no_fills"] += 1
                state["last_result"] = "NO FILL"
                state["open_trade"] = None
                save_state(state)

                return {
                    "resolved": True,
                    "message": "Previous order did not fill before market close. Step unchanged.",
                    "step": state["current_step"],
                    "stake": stake_for_step(state["current_step"]),
                }

            last_message = "Previous order still resting and not filled."
            print(last_message)
            time.sleep(2)
            continue

        # Filled trade: wait for market result, then step up or down.
        if filled > 0:
            market = get_market_by_ticker(ticker) if ticker else None
            result = market_result(market)

            if result is None:
                last_message = "Previous order filled, waiting for market result."
                print(last_message)
                time.sleep(2)
                continue

            won = result == side

            state["total_filled_trades"] += 1

            if won:
                state["total_wins"] += 1
                state["last_result"] = "WIN"

                if state["current_step"] >= LADDER_MAX_STEP:
                    state["current_step"] = LADDER_START_STEP
                else:
                    state["current_step"] += 1
            else:
                state["total_losses"] += 1
                state["last_result"] = "LOSS"
                state["current_step"] = max(state["current_step"] - 1, LADDER_START_STEP)

            state["open_trade"] = None
            save_state(state)

            return {
                "resolved": True,
                "message": "Previous filled trade resolved.",
                "won": won,
                "market_result": result,
                "new_step": state["current_step"],
                "new_stake": stake_for_step(state["current_step"]),
            }

        last_message = f"Previous order not resolved yet. Status={status}"
        print(last_message)
        time.sleep(2)

    save_state(state)

    return {
        "resolved": False,
        "message": f"Previous trade still unresolved after waiting. {last_message}",
        "step": state["current_step"],
        "stake": stake_for_step(state["current_step"]),
    }


@app.route("/", methods=["GET"])
def home():
    state = load_state()

    return jsonify(
        {
            "status": "online",
            "service": "Kalshi TradingView webhook bot - bot-owned exact ladder",
            "environment": KALSHI_ENV,
            "base_url": BASE_URL,
            "current_step": state["current_step"],
            "current_stake": stake_for_step(state["current_step"]),
            "last_result": state["last_result"],
            "open_trade": state["open_trade"],
            "default_max_price": DEFAULT_MAX_PRICE,
            "ladder_start_step": LADDER_START_STEP,
            "ladder_max_step": LADDER_MAX_STEP,
            "max_wait_seconds": MAX_WAIT_SECONDS,
            "fresh_market_seconds_left": FRESH_MARKET_SECONDS_LEFT,
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/state", methods=["GET"])
def state_view():
    state = load_state()
    state["current_stake"] = stake_for_step(state["current_step"])
    return jsonify(state)


@app.route("/reset-step", methods=["POST", "GET"])
def reset_step():
    requested_step = request.args.get("step", LADDER_START_STEP)

    step = int(requested_step)
    step = max(LADDER_START_STEP, min(step, LADDER_MAX_STEP))

    state = load_state()
    state["current_step"] = step
    state["open_trade"] = None
    state["last_result"] = "RESET"
    save_state(state)

    return jsonify(
        {
            "status": "OK",
            "current_step": state["current_step"],
            "current_stake": stake_for_step(state["current_step"]),
        }
    )


@app.route("/resolve", methods=["POST", "GET"])
def resolve_route():
    state = load_state()
    result = resolve_previous_trade(state)
    state = load_state()

    return jsonify(
        {
            "resolve_result": result,
            "state": state,
            "current_stake": stake_for_step(state["current_step"]),
        }
    )


@app.route("/test-market", methods=["GET"])
def test_market():
    try:
        market = wait_for_fresh_btc_15m_market("BTC15M")

        return jsonify(
            {
                "status": "OK",
                "ticker": market.get("ticker"),
                "title": market.get("title"),
                "close_time": market.get("close_time"),
                "market_status": market.get("status"),
                "seconds_left": seconds_until_market_close(market),
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

        state = load_state()

        # Resolve previous trade before placing a new one.
        resolve_result = resolve_previous_trade(state)

        if not resolve_result.get("resolved"):
            return jsonify(
                {
                    "status": "WAITING",
                    "reason": "Previous trade is filled/resting and not resolved yet. No new order placed.",
                    "resolve_result": resolve_result,
                    "state": load_state(),
                }
            ), 200

        state = load_state()

        side = normalize_side(parsed["SIDE"])

        # IMPORTANT:
        # Bot owns step/stake now. TradingView stake/step is ignored.
        step = int(state["current_step"])
        stake = stake_for_step(step)

        max_price = parsed.get("MAX_PRICE", DEFAULT_MAX_PRICE)
        max_price_cents = dollars_to_cents(max_price)

        if max_price_cents > 99:
            raise Exception("MAX_PRICE is too high. Use 0.45 for 45 cents, or 45.")

        market_hint = parsed.get("MARKET")
        market = wait_for_fresh_btc_15m_market(market_hint)
        market_ticker = market["ticker"]

        contracts = calculate_contracts(stake, max_price_cents)
        price_field = "yes_price" if side == "yes" else "no_price"

        # Resting limit order:
        # Do NOT include time_in_force on this legacy Kalshi endpoint.
        # If price is not available immediately, this limit order can remain open.
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

        try:
            response_json = response.json()
        except Exception:
            response_json = {"raw": response.text}

        order_response = extract_order(response_json)
        order_id = order_response.get("order_id") or order_response.get("id")

        if response.status_code not in [200, 201]:
            return jsonify(
                {
                    "status": "ORDER REJECTED",
                    "status_code": response.status_code,
                    "step": step,
                    "stake": stake,
                    "market_ticker": market_ticker,
                    "side": side,
                    "contracts": contracts,
                    "max_price_cents": max_price_cents,
                    "kalshi_response": response.text,
                    "state": load_state(),
                }
            ), 200

        state = load_state()
        state["total_orders_sent"] += 1
        state["open_trade"] = {
            "order_id": order_id,
            "client_order_id": order.get("client_order_id"),
            "market_ticker": market_ticker,
            "side": side,
            "step": step,
            "stake": stake,
            "contracts": contracts,
            "max_price_cents": max_price_cents,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["last_result"] = "ORDER SENT"
        save_state(state)

        return jsonify(
            {
                "status": "ORDER SENT",
                "status_code": response.status_code,
                "step": step,
                "stake": stake,
                "market_ticker": market_ticker,
                "side": side,
                "contracts": contracts,
                "max_price_cents": max_price_cents,
                "seconds_left": seconds_until_market_close(market),
                "kalshi_response": response_json,
                "state": state,
            }
        ), 200

    except Exception as error:
        print("ERROR:", str(error))
        return jsonify({"status": "ERROR", "error": str(error), "state": load_state()}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
