"""Фейковый AutoScout24 для e2e: выдача /lst/<марка>?cy=..&fregfrom..&priceto..&page=.. в HTML с __NEXT_DATA__.

Каталог: {"DE": {"bmw": [{"id", "price", "year", "mileage"?}]}}; 20 объявлений на странице, сортировка по цене.
"""
import json
import threading
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CY = {"D": "DE", "A": "AT", "B": "BE", "E": "ES", "F": "FR", "I": "IT", "L": "LU", "NL": "NL"}
MAKE_NAMES = {"bmw": "BMW", "fiat": "Fiat", "mercedes-benz": "Mercedes-Benz"}


def listing_json(make, country, ad):
    return {"id": ad["id"], "url": f"/angebote/{ad['id']}",
            "vehicle": {"make": MAKE_NAMES.get(make, make.title()), "model": ad.get("model", "Serie 3")},
            "location": {"countryCode": country, "city": "Stadt"}, "seller": {"id": 1},
            "tracking": {"firstRegistration": f"06-{ad['year']}", "price": str(ad["price"]),
                         "mileage": str(ad.get("mileage", 100000))}}


def make_handler(catalog):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parsed = up.urlparse(self.path)
            q = {k: v[0] for k, v in up.parse_qs(parsed.query).items()}
            make = parsed.path.rsplit("/", 1)[-1]
            country = CY[q["cy"]]
            ads = [a for a in catalog.get(country, {}).get(make, [])
                   if int(q.get("fregfrom", 0)) <= a["year"] <= int(q.get("fregto", 9999))
                   and int(q.get("pricefrom", 0)) <= a["price"] <= int(q.get("priceto", 10**9))]
            ads.sort(key=lambda a: (a["price"], a["id"]))
            size, page = int(q.get("size", 20)), int(q.get("page", 1))
            data = {"props": {"pageProps": {
                "listings": [listing_json(make, country, a) for a in ads[(page - 1) * size: page * size]],
                "numberOfResults": len(ads), "numberOfPages": min(20, -(-len(ads) // size))}}}
            body = (f'<html><body><div id="__next"></div><script id="__NEXT_DATA__" type="application/json">'
                    f'{json.dumps(data)}</script></body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
    return H


def serve(catalog):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(catalog))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
