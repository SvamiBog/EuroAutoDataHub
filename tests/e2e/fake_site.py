"""Фейковый GraphQL otomoto для e2e: каталог {make: [{"id", "price", "year", "vin"?, "mileage"?}]},
блокировки по марке, фильтры марки и диапазона лет, пагинация по 50.

broken: "graphql" — ответ на запрос с неизвестным хэшем persisted query (как у настоящего API после его смены),
"http400" — ошибка HTTP 400 на любой запрос, "banned" — 403 на любой запрос (заблокированный IP).

Сервер работает и как HTTP-прокси: запрос через прокси приходит с абсолютным URL в строке запроса.
Число обработанных запросов — в атрибуте requests сервера."""
import json
import threading
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PERSISTED_QUERY_NOT_FOUND = {"errors": [{"message": "PersistedQueryNotFound",
                                         "extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"}}]}


def make_handler(catalog, blocked_makes, broken=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            with self.server.lock:
                self.server.requests += 1
            if broken == "banned":
                return self.reply(403, {"error": "forbidden"})
            if broken == "http400":
                return self.reply(400, {"error": "bad request"})
            if broken == "graphql":
                return self.reply(200, PERSISTED_QUERY_NOT_FOUND)
            q = up.parse_qs(up.urlparse(self.path).query)
            v = json.loads(q["variables"][0])
            f = {x["name"]: x["value"] for x in v["filters"]}
            make = f["filter_enum_make"]
            if make in blocked_makes:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return
            ads = [a for a in catalog.get(make, [])
                   if int(f.get("filter_float_year:from", 0)) <= a["year"] <= int(f.get("filter_float_year:to", 9999))]
            page = v["page"]
            chunk = ads[(page - 1) * 50: page * 50]
            edges = [{"node": {"id": a["id"], "url": f"http://x/{a['id']}", "title": f"{make} {a['id']}",
                               "price": {"amount": {"units": str(a["price"]), "currencyCode": "PLN"}},
                               "parameters": [{"key": "make", "value": make},
                                              {"key": "model", "value": a.get("model", "m1")},
                                              {"key": "year", "value": str(a["year"])},
                                              {"key": "mileage", "value": str(a.get("mileage", 100000))}]
                               + ([{"key": "vin", "value": a["vin"]}] if a.get("vin") else [])}}
                     for a in chunk]
            self.reply(200, {"data": {"advertSearch": {"totalCount": len(ads), "edges": edges}}})
    return H


def serve(catalog, blocked_makes=(), broken=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(catalog, set(blocked_makes), broken))
    srv.requests, srv.lock = 0, threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
