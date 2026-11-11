"""CLI login helper. Talks to the auth service."""
import sys
import urllib.request

AUTH_URL = "http://127.0.0.1:{port}/login"


def login(user: str, password: str, port: int) -> str:
    req = urllib.request.Request(AUTH_URL.format(port=port), data=f"{user}:{password}".encode())
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode()


if __name__ == "__main__":
    print(login(sys.argv[1], sys.argv[2], int(sys.argv[3])))
