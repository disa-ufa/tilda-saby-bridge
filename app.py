import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "orders.db")))
PRODUCT_MAP_PATH = Path(os.getenv("PRODUCT_MAP_PATH", str(BASE_DIR / "product_map.json")))

SABY_APP_CLIENT_ID = os.getenv("SABY_APP_CLIENT_ID")
SABY_APP_SECRET = os.getenv("SABY_APP_SECRET")
SABY_SECRET_KEY = os.getenv("SABY_SECRET_KEY")
SABY_POINT_ID = int(os.getenv("SABY_POINT_ID", "277"))
SABY_PRICE_LIST_ID = int(os.getenv("SABY_PRICE_LIST_ID", "6"))

SHOP_URL = os.getenv("SHOP_URL", "https://scrocca-order.tilda.ws/scrocca_order_kalyan")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
REGISTER_PAYMENT_ENABLED = os.getenv("REGISTER_PAYMENT_ENABLED", "false").lower() == "true"

SABY_AUTH_URL = "https://online.sbis.ru/oauth/service/"
SABY_CREATE_ORDER_URL = "https://api.sbis.ru/retail/order/create"


app = FastAPI(title="Tilda → Saby Presto Bridge")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def saby_datetime_now() -> str:
    """
    Saby/Presto ожидает локальное московское время.
    Контейнер на VPS может работать в UTC, поэтому явно прибавляем MSK + небольшой запас,
    чтобы Saby не отклонял заказ как время в прошлом.
    """
    return (datetime.utcnow() + timedelta(hours=3, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def require_env(name: str, value: str | None) -> str:
    if not value:
        raise RuntimeError(f"Не заполнено значение {name} в .env")
    return value


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tilda_order_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                saby_order_id TEXT,
                saby_order_number TEXT,
                tilda_payload TEXT NOT NULL,
                saby_payload TEXT,
                saby_response TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def get_order_by_tilda_id(tilda_order_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM orders WHERE tilda_order_id = ?",
            (tilda_order_id,),
        ).fetchone()

    return dict(row) if row else None


def save_order_started(tilda_order_id: str, tilda_payload: dict[str, Any]) -> None:
    current_time = now_iso()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO orders (
                tilda_order_id,
                status,
                tilda_payload,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                tilda_order_id,
                "STARTED",
                json.dumps(tilda_payload, ensure_ascii=False),
                current_time,
                current_time,
            ),
        )
        conn.commit()


def save_order_success(
    tilda_order_id: str,
    saby_payload: dict[str, Any],
    saby_response: dict[str, Any],
) -> None:
    response_data = saby_response or {}

    saby_order_id = (
        response_data.get("id")
        or response_data.get("saleKey")
        or response_data.get("externalId")
        or response_data.get("key")
    )
    saby_order_number = response_data.get("number") or response_data.get("orderNumber")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE orders
            SET
                status = ?,
                saby_order_id = ?,
                saby_order_number = ?,
                saby_payload = ?,
                saby_response = ?,
                error = NULL,
                updated_at = ?
            WHERE tilda_order_id = ?
            """,
            (
                "DONE",
                saby_order_id,
                saby_order_number,
                json.dumps(saby_payload, ensure_ascii=False),
                json.dumps(saby_response, ensure_ascii=False),
                now_iso(),
                tilda_order_id,
            ),
        )
        conn.commit()


def save_order_failed(
    tilda_order_id: str,
    saby_payload: dict[str, Any] | None,
    error: str,
    response: dict[str, Any] | None = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE orders
            SET
                status = ?,
                saby_payload = ?,
                saby_response = ?,
                error = ?,
                updated_at = ?
            WHERE tilda_order_id = ?
            """,
            (
                "FAILED",
                json.dumps(saby_payload, ensure_ascii=False) if saby_payload else None,
                json.dumps(response, ensure_ascii=False) if response else None,
                error,
                now_iso(),
                tilda_order_id,
            ),
        )
        conn.commit()


def load_product_map() -> dict[str, dict[str, Any]]:
    if not PRODUCT_MAP_PATH.exists():
        raise RuntimeError(f"Не найден файл {PRODUCT_MAP_PATH}")

    return json.loads(PRODUCT_MAP_PATH.read_text(encoding="utf-8"))


def to_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0

    return float(str(value).replace(",", ".").strip())


def normalize_product_name(name: Any) -> str:
    """
    Нормализует название товара для безопасного сопоставления Tilda → Saby.
    Не делает fuzzy-подбор, а только убирает технические различия:
    пробелы, скобки, кавычки, регистр, ё/е.
    """
    text = str(name or "").strip().lower()
    text = text.replace("ё", "е")
    text = text.replace("(", " ").replace(")", " ")
    text = re.sub(r"[\\/|\"'«»“”„.,;:!?\[\]{}<>]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_product_mapping(
    product_map: dict[str, dict[str, Any]],
    tilda_name: str,
) -> dict[str, Any] | None:
    """
    Ищет товар сначала по точному названию, затем по нормализованному названию.
    Если найдено несколько разных товаров — не выбирает автоматически,
    чтобы не отправить в Saby неверную позицию.
    """
    exact = product_map.get(tilda_name)
    if exact:
        return exact

    normalized_tilda_name = normalize_product_name(tilda_name)
    matches: list[tuple[str, dict[str, Any]]] = []

    for map_name, mapped in product_map.items():
        if normalize_product_name(map_name) == normalized_tilda_name:
            matches.append((map_name, mapped))

    if not matches:
        return None

    # Если совпадений несколько, но они указывают на один и тот же nomNumber,
    # это безопасно: это просто aliases одного товара.
    unique_by_nom_number: dict[str, dict[str, Any]] = {}
    for _, mapped in matches:
        nom_number = str(mapped.get("nomNumber") or "")
        unique_by_nom_number[nom_number] = mapped

    if len(unique_by_nom_number) == 1:
        return next(iter(unique_by_nom_number.values()))

    variants = ", ".join(name for name, _ in matches[:10])
    raise HTTPException(
        status_code=400,
        detail=(
            f"Неоднозначное сопоставление товара Tilda с Saby: {tilda_name}. "
            f"Варианты: {variants}"
        ),
    )


def get_tilda_order_id(tilda_payload: dict[str, Any]) -> str:
    payment = tilda_payload.get("payment") or {}
    order_id = str(payment.get("orderid") or "").strip()

    if not order_id:
        raise HTTPException(status_code=400, detail="В webhook нет payment.orderid")

    return order_id


def build_saby_payload(tilda_payload: dict[str, Any]) -> dict[str, Any]:
    product_map = load_product_map()

    payment = tilda_payload.get("payment") or {}
    products = payment.get("products") or []

    if not products:
        raise HTTPException(status_code=400, detail="В webhook нет payment.products")

    nomenclatures = []

    for product in products:
        tilda_name = str(product.get("name") or "").strip()

        if not tilda_name:
            raise HTTPException(status_code=400, detail="В одной из позиций нет name")

        mapped = find_product_mapping(product_map, tilda_name)

        if not mapped:
            raise HTTPException(
                status_code=400,
                detail=f"Нет сопоставления товара Tilda с Saby: {tilda_name}",
            )

        nomenclatures.append(
            {
                "nomNumber": mapped["nomNumber"],
                "priceListId": int(mapped.get("priceListId") or SABY_PRICE_LIST_ID),
                "name": mapped.get("sabyName") or tilda_name,
                "count": to_float(product.get("quantity", 1)),
                "cost": to_float(product.get("price", 0)),
            }
        )

    order_id = get_tilda_order_id(tilda_payload)

    comment = str(tilda_payload.get("Комментарий") or "").strip()
    delivery_info = str(
        tilda_payload.get("Доставка_в_Кальянную")
        or payment.get("delivery")
        or ""
    ).strip()

    return {
        "product": "delivery",
        "pointId": SABY_POINT_ID,
        "comment": (
            f"Tilda #{order_id}. "
            f"Комментарий клиента: {comment}. "
            f"Доставка/столик: {delivery_info}."
        ),
        "customer": {
            "externalId": None,
            "name": tilda_payload.get("Name") or "Клиент Tilda",
            "email": tilda_payload.get("Email") or "",
            "phone": tilda_payload.get("Phone") or "",
        },
        "datetime": saby_datetime_now(),
        "nomenclatures": nomenclatures,
        "delivery": {
            "isPickup": False,
            "addressFull": "г. Москва, ул. Фридриха Энгельса, д. 23с3, кальянная",
            "paymentType": "online",
            "shopURL": SHOP_URL,
            "successURL": SHOP_URL,
            "errorURL": SHOP_URL,
        },
    }


def get_saby_token() -> str:
    payload = {
        "app_client_id": require_env("SABY_APP_CLIENT_ID", SABY_APP_CLIENT_ID),
        "app_secret": require_env("SABY_APP_SECRET", SABY_APP_SECRET),
        "secret_key": require_env("SABY_SECRET_KEY", SABY_SECRET_KEY),
    }

    response = requests.post(SABY_AUTH_URL, json=payload, timeout=30)

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(f"Saby auth не вернул JSON: {response.text}")

    if response.status_code >= 400:
        raise RuntimeError(f"Saby auth error {response.status_code}: {data}")

    token = data.get("token")

    if not token:
        raise RuntimeError(f"Saby auth: в ответе нет token: {data}")

    return token


def create_saby_order(saby_payload: dict[str, Any]) -> dict[str, Any]:
    token = get_saby_token()

    headers = {
        "X-SBISAccessToken": token,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }

    response = requests.post(
        SABY_CREATE_ORDER_URL,
        headers=headers,
        json=saby_payload,
        timeout=60,
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(f"Saby create order не вернул JSON: {response.text}")

    if response.status_code >= 400:
        raise RuntimeError(f"Saby create order error {response.status_code}: {data}")

    if data.get("resultCode") not in (0, None):
        raise RuntimeError(f"Saby resultCode != 0: {data}")

    return data


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "dryRun": DRY_RUN,
        "registerPaymentEnabled": REGISTER_PAYMENT_ENABLED,
        "pointId": SABY_POINT_ID,
        "priceListId": SABY_PRICE_LIST_ID,
    }


@app.post("/webhooks/tilda")
async def tilda_webhook(
    request: Request,
    secret: str | None = Query(default=None),
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if WEBHOOK_SECRET:
        provided_secret = secret or x_webhook_secret

        if provided_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Неверный webhook secret")

    try:
        tilda_payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook должен быть JSON")

    payment = tilda_payload.get("payment") or {}
    tilda_order_id = str(payment.get("orderid") or "").strip()

    # Tilda при сохранении Webhook может отправлять проверочный запрос
    # без payment.orderid. Это не реальный заказ, а проверка доступности URL.
    # На такую проверку надо отвечать 200 OK, иначе Tilda не даст сохранить webhook.
    if not tilda_order_id:
        return {
            "ok": True,
            "probe": True,
            "message": "Webhook доступен. Это проверочный запрос без payment.orderid.",
        }

    existing_order = get_order_by_tilda_id(tilda_order_id)

    if existing_order and existing_order["status"] == "DONE":
        return {
            "ok": True,
            "duplicate": True,
            "message": "Заказ уже был создан ранее, повторная отправка не выполнена",
            "tildaOrderId": tilda_order_id,
            "sabyOrderId": existing_order.get("saby_order_id"),
            "sabyOrderNumber": existing_order.get("saby_order_number"),
        }

    if not existing_order:
        save_order_started(tilda_order_id, tilda_payload)

    saby_payload = None

    try:
        saby_payload = build_saby_payload(tilda_payload)

        if DRY_RUN:
            return {
                "ok": True,
                "dryRun": True,
                "tildaOrderId": tilda_order_id,
                "sabyPayload": saby_payload,
            }

        saby_response = create_saby_order(saby_payload)

        save_order_success(
            tilda_order_id=tilda_order_id,
            saby_payload=saby_payload,
            saby_response=saby_response,
        )

        return {
            "ok": True,
            "tildaOrderId": tilda_order_id,
            "sabyOrderId": saby_response.get("id") or saby_response.get("saleKey"),
            "sabyOrderNumber": saby_response.get("number") or saby_response.get("orderNumber"),
            "registerPaymentEnabled": REGISTER_PAYMENT_ENABLED,
        }

    except HTTPException as exc:
        save_order_failed(
            tilda_order_id=tilda_order_id,
            saby_payload=saby_payload,
            error=str(exc.detail),
        )
        raise
    except Exception as exc:
        save_order_failed(
            tilda_order_id=tilda_order_id,
            saby_payload=saby_payload,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/orders/{tilda_order_id}")
def get_order(tilda_order_id: str) -> dict[str, Any]:
    order = get_order_by_tilda_id(tilda_order_id)

    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    return order