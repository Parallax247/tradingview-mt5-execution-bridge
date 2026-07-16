import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(
    title="TradingView–MT5 Execution Bridge",
    version="0.1.0",
)
DB_PATH = Path(os.getenv("DB_PATH", "database/trades.db"))
MT5_COMMENT = os.getenv("MT5_COMMENT", "TV_EXEC_BRIDGE")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
MIN_RR = float(os.getenv("MIN_RR", "1.8"))
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "0.01"))
SUCCESS_RETCODES = {10008, 10009}
DUPLICATE_STATUSES = {"filled", "sent", "accepted"}


class TestOrderRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD"])
    side: str = Field(..., examples=["buy"])


def to_plain(value):
    if hasattr(value, "_asdict"):
        return {key: to_plain(item) for key, item in value._asdict().items()}

    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]

    return value


def to_float(value, default=0.0):
    if value is None or value == "":
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_payload(payload):
    payload = dict(payload or {})

    if payload.get("symbol"):
        payload["symbol"] = str(payload.get("symbol")).upper()

    if payload.get("side"):
        payload["side"] = str(payload.get("side")).lower()

    payload["order_type"] = str(payload.get("order_type") or "market").lower()
    payload["volume"] = to_float(payload.get("volume"), DEFAULT_VOLUME)
    payload["sl"] = to_float(payload.get("sl"), 0.0)
    payload["tp"] = to_float(payload.get("tp"), 0.0)
    payload["price"] = to_float(payload.get("price"), 0.0)

    return payload


def verify_webhook_secret(provided_secret):
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="WEBHOOK_SECRET is not configured",
        )

    if not provided_secret or not secrets.compare_digest(
        provided_secret, WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


def init_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT,
                alert_time TEXT,
                symbol TEXT,
                side TEXT,
                timeframe TEXT,
                strategy_name TEXT,
                regime TEXT,
                preset TEXT,
                order_type TEXT,
                alert_json TEXT,
                entry_price REAL,
                sl REAL,
                tp REAL,
                volume REAL,
                risk_pct REAL,
                rr REAL,
                rr_actual REAL,
                risk_usd REAL,
                reward_usd REAL,
                retcode INTEGER,
                deal INTEGER,
                "order" INTEGER,
                fill_price REAL,
                comment TEXT,
                status TEXT,
                error TEXT,
                created_at TEXT,
                closed_at TEXT,
                pnl REAL
            )
            """
        )

        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(trade_logs)")
        }
        required_columns = {
            "uid": "TEXT",
            "alert_time": "TEXT",
            "symbol": "TEXT",
            "side": "TEXT",
            "timeframe": "TEXT",
            "strategy_name": "TEXT",
            "regime": "TEXT",
            "preset": "TEXT",
            "order_type": "TEXT",
            "alert_json": "TEXT",
            "entry_price": "REAL",
            "sl": "REAL",
            "tp": "REAL",
            "volume": "REAL",
            "risk_pct": "REAL",
            "rr": "REAL",
            "rr_actual": "REAL",
            "risk_usd": "REAL",
            "reward_usd": "REAL",
            "retcode": "INTEGER",
            "deal": "INTEGER",
            "order": "INTEGER",
            "fill_price": "REAL",
            "comment": "TEXT",
            "status": "TEXT",
            "error": "TEXT",
            "created_at": "TEXT",
            "closed_at": "TEXT",
            "pnl": "REAL",
        }

        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                safe_column_name = f'"{column_name}"' if column_name == "order" else column_name
                connection.execute(
                    f"ALTER TABLE trade_logs ADD COLUMN {safe_column_name} {column_type}"
                )


def uid_already_processed(uid):
    if not uid:
        return False

    init_database()

    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM trade_logs
            WHERE uid = ?
              AND status IN ('filled', 'sent', 'accepted')
            LIMIT 1
            """,
            (uid,),
        ).fetchone()

    return row is not None


def get_deal_info(order_result):
    if order_result is None:
        return None

    deal_ticket = getattr(order_result, "deal", 0)
    if not deal_ticket:
        return None

    deals = mt5.history_deals_get(ticket=deal_ticket)
    if not deals:
        return None

    return to_plain(deals[0])


def save_trade_log(
    payload,
    trade_request=None,
    order_result=None,
    deal_info=None,
    status=None,
    error=None,
):
    try:
        init_database()

        payload = normalize_payload(payload or {})
        retcode = getattr(order_result, "retcode", None)
        order_ticket = getattr(order_result, "order", None)
        deal_ticket = getattr(order_result, "deal", None)
        fill_price = None
        comment = getattr(order_result, "comment", None)

        if deal_info:
            fill_price = deal_info.get("price")

        if status is None:
            if retcode == 10009:
                status = "filled"
            elif retcode == 10008:
                status = "placed"
            elif retcode is not None:
                status = "rejected"
            elif error:
                status = "error"
            else:
                status = "sent"

        with sqlite3.connect(DB_PATH) as connection:
            connection.execute(
                """
                INSERT INTO trade_logs (
                    uid,
                    alert_time,
                    symbol,
                    side,
                    timeframe,
                    strategy_name,
                    regime,
                    preset,
                    order_type,
                    alert_json,
                    entry_price,
                    sl,
                    tp,
                    volume,
                    risk_pct,
                    rr,
                    rr_actual,
                    risk_usd,
                    reward_usd,
                    retcode,
                    deal,
                    "order",
                    fill_price,
                    comment,
                    status,
                    error,
                    created_at,
                    closed_at,
                    pnl
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("uid"),
                    payload.get("time") or payload.get("alert_time"),
                    payload.get("symbol"),
                    payload.get("side"),
                    payload.get("timeframe"),
                    payload.get("strategy") or payload.get("strategy_name"),
                    payload.get("regime"),
                    payload.get("preset"),
                    payload.get("order_type"),
                    json.dumps(payload, ensure_ascii=False),
                    payload.get("price"),
                    payload.get("sl"),
                    payload.get("tp"),
                    payload.get("volume"),
                    to_float(payload.get("risk_pct"), None),
                    to_float(payload.get("rr"), None),
                    to_float(payload.get("rr_actual"), None),
                    to_float(payload.get("risk_usd"), None),
                    to_float(payload.get("reward_usd"), None),
                    retcode,
                    deal_ticket,
                    order_ticket,
                    fill_price,
                    error or comment,
                    status,
                    error,
                    datetime.now(timezone.utc).isoformat(),
                    None,
                    None,
                ),
            )

        print("SQLite trade log saved:", DB_PATH)
    except Exception as db_error:
        print("SQLite trade log failed:", repr(db_error))


def order_send_with_filling_retry(trade_request):
    filling_modes = [
        ("IOC", mt5.ORDER_FILLING_IOC),
        ("FOK", mt5.ORDER_FILLING_FOK),
        ("RETURN", mt5.ORDER_FILLING_RETURN),
    ]

    order_result = None
    final_filling_mode = None
    final_retcode = None

    for filling_name, filling_value in filling_modes:
        trade_request["type_filling"] = filling_value
        order_result = mt5.order_send(trade_request)
        final_filling_mode = filling_name
        final_retcode = getattr(order_result, "retcode", None)

        if final_retcode != 10030:
            break

    print("Final filling mode:", final_filling_mode)
    print("Final retcode:", final_retcode)

    return order_result


def execute_mt5_order(payload):
    mt5_initialized = False
    trade_request = None
    order_result = None
    deal_info = None

    try:
        payload = normalize_payload(payload)
        symbol = payload.get("symbol")
        side = payload.get("side")
        uid = payload.get("uid")
        order_type = payload.get("order_type")

        print("Background order started:", payload)

        if uid_already_processed(uid):
            print("duplicate skipped:", uid)
            save_trade_log(payload, status="skipped", error="duplicate uid")
            return

        if not symbol:
            raise ValueError("Missing symbol")

        if side not in ["buy", "sell"]:
            raise ValueError("side must be buy or sell")

        if order_type != "market":
            save_trade_log(payload, status="skipped", error="order_type is not market")
            return

        if not mt5.initialize():
            error = mt5.last_error()
            print("MT5 initialize failed:", error)
            raise RuntimeError(f"MT5 initialize failed: {error}")

        mt5_initialized = True

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            raise ValueError(f"Symbol not found: {symbol}")

        if not symbol_info.visible and not mt5.symbol_select(symbol, True):
            raise ValueError(f"Failed to select symbol: {symbol}")

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"Failed to get tick for symbol: {symbol}")

        order_type_value = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        market_price = tick.ask if side == "buy" else tick.bid
        sl_value = payload.get("sl") or 0.0
        tp_value = payload.get("tp") or 0.0

        if not sl_value or not tp_value:
            save_trade_log(payload, status="missing_sl_tp", error="missing sl or tp")
            return

        if side == "buy":
            risk_usd = tick.ask - sl_value
            reward_usd = tp_value - tick.ask
            if risk_usd <= 0 or reward_usd <= 0:
                payload["risk_usd"] = risk_usd
                payload["reward_usd"] = reward_usd
                save_trade_log(payload, status="invalid_sl_tp", error="invalid buy sl/tp")
                return
        else:
            risk_usd = sl_value - tick.bid
            reward_usd = tick.bid - tp_value
            if risk_usd <= 0 or reward_usd <= 0:
                payload["risk_usd"] = risk_usd
                payload["reward_usd"] = reward_usd
                save_trade_log(payload, status="invalid_sl_tp", error="invalid sell sl/tp")
                return

        rr_actual = reward_usd / risk_usd
        payload["rr_actual"] = rr_actual
        payload["risk_usd"] = risk_usd
        payload["reward_usd"] = reward_usd

        if rr_actual < MIN_RR:
            save_trade_log(
                payload,
                status="rr_too_low",
                error=f"rr_actual below {MIN_RR}",
            )
            return

        trade_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": payload.get("volume"),
            "type": order_type_value,
            "price": market_price,
            "sl": sl_value,
            "tp": tp_value,
            "deviation": 20,
            "magic": 10001,
            "comment": MT5_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        order_result = order_send_with_filling_retry(trade_request)
        deal_info = get_deal_info(order_result)

        print("Order Result:")
        print(order_result)

        if order_result is None:
            error = mt5.last_error()
            raise RuntimeError(f"order_send failed: {error}")

        retcode = getattr(order_result, "retcode", None)
        if retcode == 10009:
            status = "filled"
            error = None
        elif retcode == 10008:
            status = "placed"
            error = None
        else:
            status = "rejected"
            error = getattr(order_result, "comment", None)

        save_trade_log(payload, trade_request, order_result, deal_info, status, error)
    except Exception as error:
        save_trade_log(
            payload,
            trade_request,
            order_result,
            deal_info,
            "error",
            str(error),
        )
        print("Background order error:", repr(error))
    finally:
        if mt5_initialized:
            mt5.shutdown()


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
):
    start_time = time.perf_counter()

    def log_step(message):
        elapsed = time.perf_counter() - start_time
        print(f"{message} | elapsed={elapsed:.4f}s")

    try:
        verify_webhook_secret(x_webhook_secret)
        log_step("Received webhook")

        raw_body = await request.body()
        raw_text = raw_body.decode("utf-8", errors="replace")

        try:
            payload = json.loads(raw_text) if raw_text else {}
            log_step("JSON parsed")
        except json.JSONDecodeError:
            payload = {"raw_text": raw_text}
            log_step("JSON parsed")

        background_tasks.add_task(execute_mt5_order, payload)

        log_step("Before return response")
        return {"status": "accepted"}
    except HTTPException:
        raise
    except Exception as error:
        print("Webhook error:", repr(error))
        raise HTTPException(status_code=500, detail=str(error))


@app.post("/test-order")
async def test_order(
    order: TestOrderRequest,
    x_webhook_secret: str | None = Header(default=None),
):
    mt5_initialized = False
    payload = None
    trade_request = None
    order_result = None
    deal_info = None

    try:
        verify_webhook_secret(x_webhook_secret)
        payload = normalize_payload(order.model_dump())

        symbol = payload.get("symbol")
        side = payload.get("side")

        if not symbol:
            raise HTTPException(status_code=400, detail="Missing symbol")

        if side not in ["buy", "sell"]:
            raise HTTPException(status_code=400, detail="side must be buy or sell")

        if not mt5.initialize():
            error = mt5.last_error()
            print("MT5 initialize failed:", error)
            raise HTTPException(status_code=500, detail=f"MT5 initialize failed: {error}")

        mt5_initialized = True

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            raise HTTPException(status_code=400, detail=f"Symbol not found: {symbol}")

        if not symbol_info.visible and not mt5.symbol_select(symbol, True):
            raise HTTPException(status_code=400, detail=f"Failed to select symbol: {symbol}")

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise HTTPException(status_code=400, detail=f"Failed to get tick for symbol: {symbol}")

        order_type_value = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        market_price = tick.ask if side == "buy" else tick.bid

        trade_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": payload.get("volume"),
            "type": order_type_value,
            "price": market_price,
            "sl": payload.get("sl"),
            "tp": payload.get("tp"),
            "deviation": 20,
            "magic": 10001,
            "comment": MT5_COMMENT,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        order_result = order_send_with_filling_retry(trade_request)
        deal_info = get_deal_info(order_result)

        print("Order Result:")
        print(order_result)

        if order_result is None:
            error = mt5.last_error()
            raise HTTPException(status_code=500, detail=f"order_send failed: {error}")

        retcode = getattr(order_result, "retcode", None)
        if retcode == 10009:
            status = "filled"
            error = None
        elif retcode == 10008:
            status = "placed"
            error = None
        else:
            status = "rejected"
            error = getattr(order_result, "comment", None)

        save_trade_log(payload, trade_request, order_result, deal_info, status, error)

        return {
            "status": "test_order_sent",
            "payload": payload,
            "order_result": order_result._asdict(),
        }
    except HTTPException as error:
        save_trade_log(
            payload,
            trade_request,
            order_result,
            deal_info,
            "error",
            str(error.detail),
        )
        raise
    except Exception as error:
        save_trade_log(
            payload,
            trade_request,
            order_result,
            deal_info,
            "error",
            str(error),
        )
        print("Test order error:", repr(error))
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        if mt5_initialized:
            mt5.shutdown()
