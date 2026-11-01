#!/usr/bin/env python3
"""A local HTTP proxy that wants a username and password, for the live check of proxy logins
(.github/workflows/engine-live.yml). Not part of the engine. Standard library only.

    python3 engine/test_proxy.py 8191 user pass      # then scrape with --proxy http://user:pass@127.0.0.1:8191

It answers 407 until a request carries the right Basic credentials, tunnels HTTPS (CONNECT) after
that, and keeps the counts in engine/../.engine/test-proxy.json: {"tunnels": n, "refused": n}.
"""
import base64, json, os, select, socket, socketserver, sys, threading

COUNTS = {"tunnels": 0, "refused": 0}
LOCK = threading.Lock()
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".engine", "test-proxy.json")


def count(key):
    with LOCK:
        COUNTS[key] += 1
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as f:
            json.dump(COUNTS, f)


class Proxy(socketserver.BaseRequestHandler):
    def head(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return None
            data += chunk
        return data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")

    def handle(self):
        want = "Basic " + base64.b64encode(self.server.creds.encode()).decode()
        while True:  # Chrome may send its credentials on the same connection
            lines = self.head()
            if not lines:
                return
            method, target = (lines[0].split(" ") + ["", ""])[:2]
            auth = [l.split(":", 1)[1].strip() for l in lines[1:] if l.lower().startswith("proxy-authorization:")]
            if auth != [want]:
                count("refused")
                self.request.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic '
                                     b'realm="kit"\r\nContent-Length: 0\r\n\r\n')
                continue
            if method != "CONNECT":
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            host, _, port = target.rpartition(":")
            try:
                up = socket.create_connection((host.strip("[]"), int(port)), timeout=15)
            except (OSError, ValueError):
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            count("tunnels")
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            pair = [self.request, up]
            try:
                while True:
                    ready, _, _ = select.select(pair, [], [], 60)
                    if not ready:
                        return
                    for s in ready:
                        data = s.recv(65536)
                        if not data:
                            return
                        (up if s is self.request else self.request).sendall(data)
            except OSError:
                return
            finally:
                up.close()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = allow_reuse_address = True


if __name__ == "__main__":
    port, user, password = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    srv = Server(("127.0.0.1", port), Proxy)
    srv.creds = f"{user}:{password}"
    print(f"test proxy on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()
