"""Loopback-only egress proxy used to confine yt-dlp network access."""

import contextlib
import http.server
import ipaddress
import select
import socket
import threading
from urllib.parse import urlsplit


_resolver = socket.getaddrinfo


class DestinationBlocked(OSError):
    """The requested destination did not resolve exclusively to public IPs."""


def _default_connector(family, address, port, timeout):
    connection = socket.socket(family, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        connection.connect(sockaddr)
        return connection
    except Exception:
        connection.close()
        raise


_connector = _default_connector


def _public_destinations(host, port):
    """Resolve once, reject the whole name if any answer is not globally routable."""
    try:
        answers = _resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise DestinationBlocked("destination could not be resolved") from error

    destinations = []
    for family, socktype, _proto, _canonname, sockaddr in answers:
        if socktype not in (0, socket.SOCK_STREAM) or family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        except ValueError as error:
            raise DestinationBlocked("resolver returned an invalid address") from error
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise DestinationBlocked("destination is not globally routable")
        checked = (socket.AF_INET if address.version == 4 else socket.AF_INET6, str(address), port)
        if checked not in destinations:
            destinations.append(checked)

    if not destinations:
        raise DestinationBlocked("destination has no globally routable address")
    return destinations


def _connect_checked(host, port, timeout=10):
    last_error = None
    for family, address, checked_port in _public_destinations(host, port):
        try:
            return _connector(family, address, checked_port, timeout)
        except OSError as error:
            last_error = error
    raise OSError("could not connect to checked destination") from last_error


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_CONNECT(self):
        try:
            target = urlsplit("//" + self.path)
            if not target.hostname or target.username or target.password:
                raise DestinationBlocked("invalid CONNECT destination")
            port = target.port or 443
            upstream = _connect_checked(target.hostname, port)
        except (DestinationBlocked, ValueError):
            self.send_error(403, "Destination is not allowed")
            return
        except OSError:
            self.send_error(502, "Destination is unavailable")
            return

        try:
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            self._tunnel(upstream)
        finally:
            upstream.close()

    def do_GET(self):
        self._forward()

    def do_HEAD(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_OPTIONS(self):
        self._forward()

    def _forward(self):
        try:
            target = urlsplit(self.path)
            if target.scheme.lower() != "http" or not target.hostname or target.username or target.password:
                raise DestinationBlocked("invalid forward-proxy destination")
            transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
            content_lengths = self.headers.get_all("Content-Length", [])
            if transfer_encoding or len(content_lengths) > 1:
                self.send_error(400, "Unsupported request framing")
                return
            if content_lengths and not content_lengths[0].isdigit():
                self.send_error(400, "Invalid Content-Length")
                return
            content_length = int(content_lengths[0]) if content_lengths else 0
            port = target.port or 80
            upstream = _connect_checked(target.hostname, port)
        except (DestinationBlocked, ValueError):
            self.send_error(403, "Destination is not allowed")
            return
        except OSError:
            self.send_error(502, "Destination is unavailable")
            return

        try:
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            upstream.sendall(f"{self.command} {path} HTTP/1.1\r\n".encode("ascii"))

            excluded = {"connection", "host", "proxy-authorization", "proxy-connection", "keep-alive"}
            for name, value in self.headers.items():
                if name.lower() not in excluded:
                    upstream.sendall(f"{name}: {value}\r\n".encode("latin-1"))
            authority = target.hostname
            if ":" in authority:
                authority = "[" + authority + "]"
            if target.port is not None:
                authority += f":{target.port}"
            upstream.sendall(f"Host: {authority}\r\nConnection: close\r\n\r\n".encode("ascii"))

            if content_length:
                remaining = content_length
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    upstream.sendall(chunk)
                    remaining -= len(chunk)

            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
            self.close_connection = True
        except (OSError, ValueError):
            self.close_connection = True
        finally:
            upstream.close()

    def _tunnel(self, upstream):
        sockets = (self.connection, upstream)
        while True:
            readable, _, _ = select.select(sockets, (), (), 30)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                destination = upstream if source is self.connection else self.connection
                destination.sendall(data)

    def log_message(self, format, *args):
        pass


class _LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


@contextlib.contextmanager
def loopback_proxy():
    """Start a per-download forward/CONNECT proxy on an ephemeral loopback port."""
    server = _LoopbackServer(("127.0.0.1", 0), _ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
