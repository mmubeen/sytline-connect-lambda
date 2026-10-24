"""A TCP pass-through in front of the SQL Server container (specs/008 R6).

It never looks inside the stream, so the TLS session is between the function and the real engine. It exists to give the
`sql.unreachable` and `sql.slow` faults without needing control of the container: `refuse` closes each new connection at
once, `delay` holds it before connecting through.
"""

import socket
import threading
import time


class SqlProxy:
    def __init__(self, listen_host, listen_port, target_host, target_port):
        self._listen = (listen_host, listen_port)
        self._target = (target_host, target_port)
        self._mode, self._delay = None, 0.0
        self._sock = None
        self.port = listen_port

    def set_mode(self, mode, delay_ms=0):
        self._mode, self._delay = mode, delay_ms / 1000.0

    def start(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self._listen)
        self._sock.listen(50)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self):
        if self._sock:
            self._sock.close()

    def _accept(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        mode, delay = self._mode, self._delay
        if mode == "refuse":
            client.close()
            return
        if mode == "delay":
            time.sleep(delay)
        try:
            upstream = socket.create_connection(self._target, timeout=5)
        except OSError:
            client.close()
            return
        upstream.settimeout(None)
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
