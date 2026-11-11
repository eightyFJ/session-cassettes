"""Authentication service: issues and checks session tokens."""
import hashlib
import hmac
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTH_PORT = 8080
_SECRET = secrets.token_bytes(32)
_USERS = {"demo": hashlib.sha256(b"demo-password").hexdigest()}


def make_token(user: str) -> str:
    sig = hmac.new(_SECRET, user.encode(), hashlib.sha256).hexdigest()
    return f"{user}.{sig}"


class AuthHandler(BaseHTTPRequestHandler):
    """Handles POST /login and GET /verify."""

    def do_POST(self):
        if self.path != "/login":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        user, _, password = self.rfile.read(length).decode().partition(":")
        if _USERS.get(user) == hashlib.sha256(password.encode()).hexdigest():
            self.send_response(200)
            self.end_headers()
            self.wfile.write(make_token(user).encode())
        else:
            self.send_error(401)

    def do_GET(self):
        if self.path.startswith("/verify"):
            self.send_response(200)
            self.end_headers()


def serve():
    HTTPServer(("127.0.0.1", AUTH_PORT), AuthHandler).serve_forever()


if __name__ == "__main__":
    serve()
