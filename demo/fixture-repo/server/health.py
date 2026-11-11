"""Liveness probe. Not part of auth."""
from http.server import BaseHTTPRequestHandler, HTTPServer

HEALTH_PORT = 9100


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def serve():
    HTTPServer(("127.0.0.1", HEALTH_PORT), HealthHandler).serve_forever()
