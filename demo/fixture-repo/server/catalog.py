"""Product catalog service."""
from http.server import BaseHTTPRequestHandler, HTTPServer

from config.settings import CATALOG_PORT

PRODUCTS = [{"id": 1, "name": "mug"}, {"id": 2, "name": "sticker"}]


class CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(repr(PRODUCTS).encode())


def serve():
    HTTPServer(("127.0.0.1", CATALOG_PORT), CatalogHandler).serve_forever()
