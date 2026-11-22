"""Serve persisted batch metrics so they remain scrapeable after Python exits."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from src.config import Settings
from src.logging_store import Store
from src.metrics import prometheus_text


def create_server(store, host="127.0.0.1", port=8001):
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                payload = prometheus_text(store).encode("utf-8")
            except Exception:
                self.send_error(503, "Metrics unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer((host, port), MetricsHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    server = create_server(Store(Settings().db_path), args.host, args.port)
    print(f"Metrics available on port {server.server_port} at /metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
