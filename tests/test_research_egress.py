import contextlib
import http.server
import socket
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

import research_egress


class _Upstream(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        type(self).requests.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://private.example/secret")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"public response")

    def log_message(self, format, *args):
        pass


@contextlib.contextmanager
def upstream_server():
    _Upstream.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class EgressProxy(unittest.TestCase):
    def _open(self, proxy_url, url):
        opener = build_opener(ProxyHandler({"http": proxy_url}))
        return opener.open(url, timeout=2)

    def test_forward_proxy_connects_to_the_exact_checked_public_address(self):
        connected = []

        def resolver(host, port, **kwargs):
            self.assertEqual(host, "public.example")
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:8.8.8.8", port, 0, 0))]

        with upstream_server() as upstream:
            def connector(family, address, port, timeout):
                connected.append((family, address, port))
                return socket.create_connection(upstream.server_address, timeout)

            with mock.patch.object(research_egress, "_resolver", resolver, create=True), \
                 mock.patch.object(research_egress, "_connector", connector, create=True), \
                 research_egress.loopback_proxy() as proxy_url:
                with self._open(proxy_url, "http://public.example/ok") as response:
                    self.assertEqual(response.read(), b"public response")

        self.assertEqual(connected, [(socket.AF_INET, "8.8.8.8", 80)])

    def test_connect_proxy_tunnels_only_to_the_checked_public_address(self):
        connected = []
        echo_threads = []

        def resolver(host, port, **kwargs):
            self.assertEqual((host, port), ("video.example", 443))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.4.4", port))]

        def connector(family, address, port, timeout):
            connected.append((family, address, port))
            proxy_side, upstream_side = socket.socketpair()

            def echo():
                with upstream_side:
                    upstream_side.sendall(b"upstream-ready")
                    upstream_side.sendall(upstream_side.recv(4096).upper())

            thread = threading.Thread(target=echo, daemon=True)
            thread.start()
            echo_threads.append(thread)
            return proxy_side

        with mock.patch.object(research_egress, "_resolver", resolver), \
             mock.patch.object(research_egress, "_connector", connector), \
             research_egress.loopback_proxy() as proxy_url:
            port = int(proxy_url.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                client.sendall(b"CONNECT video.example:443 HTTP/1.1\r\nHost: video.example:443\r\n\r\n")
                response = client.recv(4096)
                self.assertTrue(response.startswith(b"HTTP/1.1 200 Connection Established\r\n"))
                tunneled = response.split(b"\r\n\r\n", 1)[1]
                while b"upstream-ready" not in tunneled:
                    tunneled += client.recv(4096)
                client.sendall(b"hello")
                self.assertEqual(client.recv(4096), b"HELLO")

        for thread in echo_threads:
            thread.join()

        self.assertEqual(connected, [(socket.AF_INET, "8.8.4.4", 443)])

    def test_forward_proxy_rejects_unsupported_or_ambiguous_request_framing(self):
        def resolver(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        requests = (
            b"Transfer-Encoding: chunked\r\n\r\n4\r\ntest\r\n0\r\n\r\n",
            b"Transfer-Encoding: chunked\r\nContent-Length: 4\r\n\r\n4\r\ntest\r\n0\r\n\r\n",
            b"Content-Length: -1\r\n\r\n",
            b"Content-Length: nope\r\n\r\n",
        )

        connected = []
        with mock.patch.object(research_egress, "_resolver", resolver), \
             mock.patch.object(research_egress, "_connector", lambda *args: connected.append(args)), \
             research_egress.loopback_proxy() as proxy_url:
            port = int(proxy_url.rsplit(":", 1)[1])
            for headers in requests:
                with self.subTest(headers=headers), socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                    client.sendall(b"POST http://public.example/upload HTTP/1.1\r\nHost: public.example\r\n" + headers)
                    response = client.recv(4096)
                    self.assertTrue(response.startswith(b"HTTP/1.1 400 "), response)

        self.assertEqual(connected, [])

    def test_proxy_refuses_every_non_global_address_class(self):
        addresses = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "fc00::1",
            "fe80::1",
            "224.0.0.1",
            "ff00::1",
            "0.0.0.0",
            "::ffff:127.0.0.1",
        )
        connected = []

        def resolver(host, port, **kwargs):
            address = addresses[int(host.removeprefix("blocked").split(".", 1)[0])]
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

        with mock.patch.object(research_egress, "_resolver", resolver, create=True), \
             mock.patch.object(research_egress, "_connector", lambda *args: connected.append(args), create=True), \
             research_egress.loopback_proxy() as proxy_url:
            for index, address in enumerate(addresses):
                with self.subTest(address=address), self.assertRaises(HTTPError) as caught:
                    self._open(proxy_url, f"http://blocked{index}.example/")
                self.assertEqual(caught.exception.code, 403)

        self.assertEqual(connected, [])

    def test_proxy_rejects_a_name_if_any_resolved_address_is_not_global(self):
        def resolver(host, port, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]

        with mock.patch.object(research_egress, "_resolver", resolver, create=True), \
             mock.patch.object(research_egress, "_connector", self.fail, create=True), \
             research_egress.loopback_proxy() as proxy_url, \
             self.assertRaises(HTTPError) as caught:
            self._open(proxy_url, "http://mixed.example/")
        self.assertEqual(caught.exception.code, 403)

    def test_redirect_to_private_is_refused_before_a_second_connection(self):
        connected = []

        def resolver(host, port, **kwargs):
            address = "8.8.8.8" if host == "public.example" else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        with upstream_server() as upstream:
            def connector(family, address, port, timeout):
                connected.append(address)
                return socket.create_connection(upstream.server_address, timeout)

            with mock.patch.object(research_egress, "_resolver", resolver, create=True), \
                 mock.patch.object(research_egress, "_connector", connector, create=True), \
                 research_egress.loopback_proxy() as proxy_url, \
                 self.assertRaises(HTTPError) as caught:
                self._open(proxy_url, "http://public.example/redirect")

        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(_Upstream.requests, ["/redirect"])
        self.assertEqual(connected, ["8.8.8.8"])


if __name__ == "__main__":
    unittest.main()
