"""Фейковый GraphQL otomoto для e2e: каталог {make: [{"id", "price", "year"}]}, блокировки по марке,
фильтры марки и диапазона лет, пагинация по 50."""
import json
import threading
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(catalog, blocked_makes):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
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
                                              {"key": "mileage", "value": "100000"}]}}
                     for a in chunk]
            body = json.dumps({"data": {"advertSearch": {"totalCount": len(ads), "edges": edges}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
    return H


def serve(catalog, blocked_makes=()):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(catalog, set(blocked_makes)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
