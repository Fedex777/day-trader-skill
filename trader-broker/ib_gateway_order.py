#!/usr/bin/env python3
"""Eigener Zusatz (nicht Teil des Original-Skill-Pakets, siehe README dort):
sendet eine von ticket.py bereits freigegebene Order DIREKT an das lokale
IB-Gateway (ib_insync), statt ueber den offiziellen IBKR-MCP-Connector.
Nimmt genau das JSON, das ticket.py fuer place_stock_order ausgibt (Alpaca-
Feldnamen) und bildet es auf IB-Orders ab.

Alle Orders bekommen orderRef="DAYTRADER_SKILL" (Trennflag) -- damit sind
sie im gemeinsam genutzten Paper-Account/Journal eindeutig von den
Produktiv-Bots (die eigene orderRef-Praefixe wie "IC21DTE_..." nutzen)
unterscheidbar, ohne einen zweiten Account/Gateway zu brauchen.

    python3 ib_gateway_order.py place '<tool_input JSON von ticket.py>'
    python3 ib_gateway_order.py positions
"""
import argparse
import json
import os
import random
import sys

IB_HOST = os.environ.get("IB_HOST", "ib-gateway-paper")
IB_PORT = int(os.environ.get("IB_PORT", "4004"))
ORDER_REF = "DAYTRADER_SKILL"


def _connect():
    from ib_insync import IB
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=random.randint(70000, 79000), timeout=20)
    return ib


def cmd_positions(a):
    ib = _connect()
    try:
        ib.reqPositions()
        ib.sleep(2)
        rows = [{"symbol": p.contract.symbol, "position": p.position, "avgCost": p.avgCost}
                 for p in ib.positions()]
        print(json.dumps(rows, indent=2))
    finally:
        ib.disconnect()


def _ticket_signature(symbol, side, qty, is_bracket, order):
    """Fingerprint der Order-Absicht, nicht der IB-Order-IDs (die sind pro
    Verbindung neu vergeben, siehe Vorfall 2026-11-22: 2 Testorders mit
    beide 'orderId 4', nur permId war wirklich eindeutig)."""
    return json.dumps({
        "symbol": symbol, "side": side, "qty": qty, "bracket": is_bracket,
        "take_profit_limit_price": order.get("take_profit_limit_price"),
        "stop_loss_stop_price": order.get("stop_loss_stop_price"),
    }, sort_keys=True)


def _find_existing_order(ib, signature):
    """Duplicate-Guard (Vorfall 2026-11-22, analog zum Produktiv-Fix in
    ic_send_ib_order.py): schaut in orders_log.jsonl nach der letzten Order
    mit DERSELBEN Absicht und prueft ueber permId (NICHT orderId -- das
    kollidiert ueber Verbindungen hinweg) bei IB, ob sie noch aktiv/gefuellt
    ist. Nur wenn sie schon Cancelled/ApiCancelled ist (oder gar nicht mehr
    gefunden wird), gilt sie als 'frei' fuer eine neue Order."""
    log_path = os.path.join(os.path.dirname(__file__), "..", "trader", "orders_log.jsonl")
    last_match = None
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("signature") == signature:
                    last_match = entry
    except FileNotFoundError:
        return None
    if not last_match:
        return None

    perm_ids = {leg.get("permId") for leg in last_match["legs"] if leg.get("permId")}
    if not perm_ids:
        return None

    ib.reqAllOpenOrders()
    ib.sleep(2)
    by_perm = {t.order.permId: t for t in ib.trades()}
    for pid in perm_ids:
        t = by_perm.get(pid)
        if t and t.orderStatus.status not in ("Cancelled", "ApiCancelled", "Inactive"):
            return last_match
    for t in ib.reqCompletedOrders(apiOnly=False):
        if t.order.permId in perm_ids and t.orderStatus.status == "Filled":
            return last_match
    return None


def cmd_place(a):
    order = json.loads(a.ticket_json)
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, Order

    symbol = order["symbol"]
    side = str(order["side"]).upper()  # alpaca: buy/sell -> IB: BUY/SELL
    qty = int(float(order["qty"]))
    tif = str(order.get("time_in_force", "day")).upper()
    tp = order.get("take_profit_limit_price")
    sl = order.get("stop_loss_stop_price")
    is_bracket = order.get("order_class") == "bracket" or tp is not None or sl is not None
    signature = _ticket_signature(symbol, side, qty, is_bracket, order)

    ib = _connect()
    try:
        existing = _find_existing_order(ib, signature)
        if existing is not None:
            print("Bereits platzierte Order mit identischer Absicht gefunden -- "
                  "keine neue Order gesendet:")
            print(json.dumps(existing, indent=2))
            return

        stock = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(stock)

        if is_bracket:
            if tp is None or sl is None:
                print("FEHLER: Bracket braucht sowohl take_profit_limit_price als auch "
                      "stop_loss_stop_price", file=sys.stderr)
                sys.exit(1)
            if str(order.get("type", "market")) == "limit" and order.get("limit_price"):
                entry_price = float(order["limit_price"])
            else:
                entry_price = None
            parent, take_profit, stop_loss = ib.bracketOrder(
                side, qty, limitPrice=entry_price, takeProfitPrice=float(tp), stopLossPrice=float(sl)
            )
            if entry_price is None:
                # Market-Entry: parent von LimitOrder auf MarketOrder umbauen,
                # bracketOrder() baut sonst immer eine Limit-Entry-Order.
                parent = MarketOrder(side, qty)
            for leg in (parent, take_profit, stop_loss):
                leg.orderRef = ORDER_REF
                leg.tif = tif
            trades = [ib.placeOrder(stock, parent)]
            ib.sleep(1)
            take_profit.parentId = parent.orderId
            stop_loss.parentId = parent.orderId
            trades.append(ib.placeOrder(stock, take_profit))
            trades.append(ib.placeOrder(stock, stop_loss))
        else:
            if str(order.get("type", "market")) == "limit" and order.get("limit_price"):
                o = LimitOrder(side, qty, float(order["limit_price"]))
            else:
                o = MarketOrder(side, qty)
            o.orderRef = ORDER_REF
            o.tif = tif
            trades = [ib.placeOrder(stock, o)]

        for _ in range(10):
            ib.sleep(3)
            if all(t.orderStatus.status in ("Filled", "Submitted", "PreSubmitted") for t in trades):
                break

        result = [{"orderId": t.order.orderId, "permId": t.order.permId, "status": t.orderStatus.status,
                    "avgFillPrice": t.orderStatus.avgFillPrice} for t in trades]
        print(json.dumps({"orderRef": ORDER_REF, "legs": result}, indent=2))
        print("\nZum Protokollieren: ticket.py result --index N --status placed "
              "--broker-id %s --filled-qty %s --filled-price <Fill aus obiger Ausgabe>"
              % (result[0]["orderId"], qty))
        _log_activity(symbol, side, qty, is_bracket, result, signature)
    finally:
        ib.disconnect()


def _log_activity(symbol, side, qty, is_bracket, legs, signature):
    """Append-only Log fuer die Django-Monitor-Seite (range_trader_django,
    liest dieselbe Datei ueber einen geteilten Einzel-Datei-Mount) -- der
    Sandbox-Container hat selbst keinen DB-Zugriff, daher JSONL statt DB.
    'signature' wird vom Duplicate-Guard (_find_existing_order) genutzt."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "symbol": symbol, "side": side, "qty": qty, "bracket": is_bracket,
        "legs": legs, "signature": signature,
    }
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "trader", "orders_log.jsonl"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[Log] Fehler: {e}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("positions")
    pl = sub.add_parser("place")
    pl.add_argument("ticket_json")
    a = p.parse_args()
    {"positions": cmd_positions, "place": cmd_place}[a.cmd](a)


if __name__ == "__main__":
    main()
