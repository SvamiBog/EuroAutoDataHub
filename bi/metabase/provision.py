"""Настройка Metabase «как код»: администратор, подключение к БД, коллекция, вопросы и дашборды.

Идемпотентно: при повторном запуске вопросы и дашборды обновляются по имени в коллекции.

Запуск: make bi-provision (или python bi/metabase/provision.py)

Переменные окружения:
  MB_URL                  адрес Metabase (по умолчанию http://localhost:3000)
  MB_PUBLIC_URL           адрес Metabase в браузере для ссылок в выводе (по умолчанию MB_URL)
  MB_ADMIN_EMAIL, MB_ADMIN_PASSWORD   администратор (создаётся при первом запуске Metabase)
  MB_DB_HOST, MB_DB_PORT  адрес PostgreSQL, как его видит Metabase (в docker-compose: db_postgres:5432)
  POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD   база данных проекта
"""
import os
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cards import CARDS, DASHBOARDS, VARIABLES  # noqa: E402

COLLECTION = "EuroAutoDataHub"
DATABASE_NAME = "EuroAutoDataHub"
VARIABLE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class Metabase:
    def __init__(self, url: str, public_url: str | None = None):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=60)
        self.public_url = (public_url or url).rstrip("/")

    def request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:500]}")
        return response.json() if response.content else None

    def wait_ready(self, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.client.get("/api/health").json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(3)
        raise RuntimeError("Metabase не запустился")

    def login(self, email: str, password: str, database: dict) -> None:
        properties = self.request("GET", "/api/session/properties")
        if not properties.get("has-user-setup"):
            self.request("POST", "/api/setup", json={
                "token": properties["setup-token"],
                "user": {"email": email, "password": password, "first_name": "Admin", "last_name": "EADH",
                         "site_name": "EuroAutoDataHub"},
                "prefs": {"site_name": "EuroAutoDataHub", "site_locale": "ru", "allow_tracking": False},
                "database": database,
            })
            print("Metabase: первичная настройка выполнена")
        session = self.request("POST", "/api/session", json={"username": email, "password": password})
        self.client.headers["X-Metabase-Session"] = session["id"]


def database_payload() -> dict:
    return {
        "engine": "postgres",
        "name": DATABASE_NAME,
        "details": {
            "host": os.getenv("MB_DB_HOST", "localhost"),
            "port": int(os.getenv("MB_DB_PORT", "5433")),
            "dbname": os.getenv("POSTGRES_DB", "euroautodatahub_db"),
            "user": os.getenv("POSTGRES_USER", "postgres"),
            "password": os.getenv("POSTGRES_PASSWORD", ""),
            "ssl": False,
        },
    }


def ensure_database(mb: Metabase, payload: dict) -> int:
    databases = mb.request("GET", "/api/database")
    databases = databases.get("data", databases) if isinstance(databases, dict) else databases
    for db in databases:
        if db["name"] == DATABASE_NAME:
            mb.request("PUT", f"/api/database/{db['id']}", json={"details": payload["details"]})
            return db["id"]
    return mb.request("POST", "/api/database", json=payload)["id"]


def ensure_collection(mb: Metabase) -> int:
    for collection in mb.request("GET", "/api/collection"):
        if collection.get("name") == COLLECTION and not collection.get("archived"):
            return collection["id"]
    return mb.request("POST", "/api/collection", json={"name": COLLECTION, "color": "#509EE3"})["id"]


def collection_items(mb: Metabase, collection_id: int, model: str) -> dict[str, int]:
    items = mb.request("GET", f"/api/collection/{collection_id}/items", params={"models": model})
    items = items.get("data", items) if isinstance(items, dict) else items
    return {item["name"]: item["id"] for item in items}


def template_tags(sql: str, required: list[str]) -> dict:
    tags = {}
    for name in dict.fromkeys(VARIABLE_RE.findall(sql)):
        display_name, var_type, _ = VARIABLES[name]
        tags[name] = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"eadh/{name}")), "name": name,
                      "display-name": display_name, "type": var_type, "required": name in required}
    return tags


def ensure_cards(mb: Metabase, database_id: int, collection_id: int) -> dict[str, dict]:
    existing = collection_items(mb, collection_id, "card")
    cards = {}
    for key, spec in CARDS.items():
        sql = spec["sql"].strip()
        payload = {
            "name": spec["name"],
            "display": spec["display"],
            "collection_id": collection_id,
            "visualization_settings": spec.get("viz", {}),
            "dataset_query": {
                "type": "native",
                "database": database_id,
                "native": {"query": sql, "template-tags": template_tags(sql, spec.get("required", []))},
            },
        }
        if spec["name"] in existing:
            card = mb.request("PUT", f"/api/card/{existing[spec['name']]}", json=payload)
        else:
            card = mb.request("POST", "/api/card", json=payload)
        cards[key] = {"id": card["id"], "variables": set(payload["dataset_query"]["native"]["template-tags"])}
    return cards


def ensure_dashboards(mb: Metabase, collection_id: int, cards: dict[str, dict]) -> list[int]:
    existing = collection_items(mb, collection_id, "dashboard")
    ids = []
    for spec in DASHBOARDS:
        parameters = [{"id": name, "name": VARIABLES[name][0], "slug": name, "type": VARIABLES[name][2],
                       "sectionId": "date" if VARIABLES[name][1] == "date" else
                       ("number" if VARIABLES[name][1] == "number" else "string")}
                      for name in spec["parameters"]]
        if spec["name"] in existing:
            dashboard_id = existing[spec["name"]]
        else:
            dashboard_id = mb.request("POST", "/api/dashboard", json={
                "name": spec["name"], "description": spec["description"], "collection_id": collection_id})["id"]

        dashcards, row, col, row_height = [], 0, 0, 0
        for index, (key, width, height) in enumerate(spec["cards"]):
            if col + width > 24:
                row, col, row_height = row + row_height, 0, 0
            card = cards[key]
            dashcards.append({
                "id": -(index + 1), "card_id": card["id"], "row": row, "col": col, "size_x": width, "size_y": height,
                "parameter_mappings": [
                    {"parameter_id": name, "card_id": card["id"], "target": ["variable", ["template-tag", name]]}
                    for name in spec["parameters"] if name in card["variables"]],
            })
            col += width
            row_height = max(row_height, height)

        mb.request("PUT", f"/api/dashboard/{dashboard_id}", json={
            "description": spec["description"], "parameters": parameters, "dashcards": dashcards, "tabs": []})
        ids.append(dashboard_id)
        print(f"Metabase: дашборд «{spec['name']}» ({len(dashcards)} карточек) — {mb.public_url}/dashboard/{dashboard_id}")
    return ids


def main() -> None:
    mb = Metabase(os.getenv("MB_URL", "http://localhost:3000"), os.getenv("MB_PUBLIC_URL"))
    mb.wait_ready()
    database = database_payload()
    mb.login(os.getenv("MB_ADMIN_EMAIL", "admin@example.com"), os.environ["MB_ADMIN_PASSWORD"], database)
    database_id = ensure_database(mb, database)
    collection_id = ensure_collection(mb)
    cards = ensure_cards(mb, database_id, collection_id)
    ensure_dashboards(mb, collection_id, cards)
    print(f"Metabase: вопросов {len(cards)}, дашбордов {len(DASHBOARDS)} в коллекции «{COLLECTION}»")


if __name__ == "__main__":
    main()
