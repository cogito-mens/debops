# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Transports for the DebOps MCP server.

Two transports are provided: stdio, where the server is spawned by an MCP
client as a subprocess, and a UNIX socket listener, where a long-running
server can serve local clients under its own UNIX account. Both carry
newline-delimited JSON-RPC messages; on the socket this follows the
specification's guidance that custom byte-stream transports should reuse
the stdio framing.

Access to the socket is controlled by filesystem permissions; there is no
protocol-level authentication on this transport.
"""

import logging
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time

from . import rpc

logger = logging.getLogger(__name__)


def get_peer_credentials(connection):
    """Return the (pid, uid, gid) of the process on the other end of a
    connected UNIX socket, using the SO_PEERCRED socket option."""
    credentials = connection.getsockopt(socket.SOL_SOCKET,
                                        socket.SO_PEERCRED,
                                        struct.calcsize('3i'))
    return struct.unpack('3i', credentials)


def _write_response(server, writer, write_lock, data):
    """Handle one message and write its response, if any.

    Each message runs in its own thread, so a long-running request does
    not block cancellations or later requests; writes are serialized to
    keep messages intact. The notify channel lets tools send
    server-initiated messages (progress notifications) mid-request.
    """
    def notify(message):
        try:
            with write_lock:
                rpc.write_message(writer, message)
        except OSError:
            # The client went away while the request was being handled
            pass

    response = server.handle_bytes(data, notify=notify)
    if response is None:
        return
    try:
        with write_lock:
            rpc.write_message(writer, response)
    except OSError:
        # The client went away while the request was being handled
        pass


class StdioTransport(object):
    """Serve MCP requests on stdin/stdout.

    Nothing except protocol messages may be written to stdout; logging
    goes to stderr.
    """

    def serve(self, server):
        logger.notice('MCP server serving on stdio')
        reader = sys.stdin.buffer
        writer = sys.stdout.buffer
        write_lock = threading.Lock()
        workers = []
        try:
            while True:
                try:
                    data = rpc.read_message(reader)
                except rpc.JsonRpcError as errmsg:
                    with write_lock:
                        rpc.write_message(writer, errmsg.to_response())
                    continue
                if data is None:
                    break
                thread = threading.Thread(
                        target=_write_response,
                        args=(server, writer, write_lock, data),
                        daemon=True)
                thread.start()
                workers.append(thread)
        except KeyboardInterrupt:
            # Interrupt: cancel in-flight requests and wind down
            server.cancel_all()
            deadline = time.monotonic() + 5
            for worker in workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                worker.join(timeout=remaining)
            raise
        # End of input (the client went away or is batching): let
        # in-flight requests finish and deliver their responses; every
        # tool is bounded (fast or 'execute_timeout'-limited)
        for worker in workers:
            worker.join()


class UnixSocketTransport(object):
    """Serve MCP requests on a UNIX socket.

    The server accepts multiple concurrent connections; because the
    protocol is stateless, connections share no state and clients can
    reconnect freely.
    """

    def __init__(self, path, mode=0o660, max_connections=32):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.mode = mode
        self._semaphore = threading.BoundedSemaphore(max_connections)
        self._socket = None
        self._serving = False

    def _prepare_socket_path(self):
        """Make sure that the socket path can be bound safely."""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError('refusing to use a symlink as the socket: '
                             + self.path)
        if not os.path.exists(self.path):
            return
        if not stat.S_ISSOCK(os.stat(self.path).st_mode):
            raise FileExistsError('refusing to remove a non-socket file: '
                                  + self.path)
        # Distinguish a stale socket from a live server: a live server
        # accepts connections, a stale one refuses them
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(self.path)
        except ConnectionRefusedError:
            # A leftover socket from a previous run; safe to replace
            logger.notice('Replacing a stale socket: %s', self.path)
            os.unlink(self.path)
        else:
            raise FileExistsError('another MCP server is already '
                                  'listening on ' + self.path)
        finally:
            probe.close()

    def serve(self, server):
        self._prepare_socket_path()
        old_umask = os.umask(0o077)
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(self.path)
        finally:
            os.umask(old_umask)
        os.chmod(self.path, self.mode)
        listener.listen()
        # The timeout lets the accept loop notice a shutdown request
        listener.settimeout(0.5)
        self._socket = listener
        self._serving = True
        # Terminate gracefully so that the socket file is cleaned up;
        # signal handlers can only be installed in the main thread
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: self.shutdown())
        logger.notice('MCP server listening on %s (mode %04o)',
                      self.path, self.mode)
        try:
            while self._serving:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                if not server.accepting():
                    # The kill switch is active: refuse the connection
                    logger.notice('Connection refused: the MCP '
                                  'functionality is disabled')
                    connection.close()
                    continue
                if not self._semaphore.acquire(blocking=False):
                    logger.warning('Connection limit reached, rejecting '
                                   'a connection')
                    connection.close()
                    continue
                thread = threading.Thread(
                        target=self._serve_connection,
                        args=(connection, server), daemon=True)
                thread.start()
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
            listener.close()
            # Shutting down: cancel in-flight requests; their tools
            # terminate promptly
            server.cancel_all()
            try:
                os.unlink(self.path)
            except OSError:
                pass
            else:
                logger.notice('MCP server on %s shut down', self.path)

    def shutdown(self):
        """Ask the accept loop to stop; in-flight connections finish."""
        self._serving = False

    def _serve_connection(self, connection, server):
        try:
            pid, uid, gid = get_peer_credentials(connection)
            logger.notice('Client connected: pid=%s uid=%s gid=%s',
                          pid, uid, gid)
            reader = connection.makefile('rb')
            writer = connection.makefile('wb')
            write_lock = threading.Lock()
            while True:
                try:
                    data = rpc.read_message(reader)
                except rpc.JsonRpcError as errmsg:
                    logger.warning('Rejecting a message from pid=%s: %s',
                                   pid, errmsg.message)
                    with write_lock:
                        rpc.write_message(writer, errmsg.to_response())
                    continue
                except OSError:
                    break
                if data is None:
                    break
                # Requests run concurrently so that long-running tools do
                # not block cancellations and later requests
                thread = threading.Thread(
                        target=_write_response,
                        args=(server, writer, write_lock, data),
                        daemon=True)
                thread.start()
            logger.notice('Client disconnected: pid=%s', pid)
        except OSError as errmsg:
            logger.debug('Connection ended with an error: %s', errmsg)
        finally:
            self._semaphore.release()
            connection.close()


def ping_unix(path, timeout=5):
    """Probe an MCP server on a UNIX socket with 'server/discover'.

    Returns the raw response line. Raises ConnectionError when the
    socket cannot be connected, EOFError when the server closes the
    connection without a response (for example because of the kill
    switch), and TimeoutError when no response arrives in time.
    """
    from .client import request_unix
    return request_unix(path, 'server/discover', timeout=timeout)


def bridge_stdio_to_unix(path):
    """Bridge stdin/stdout to a UNIX socket ('debops mcp connect').

    The bridge is a dumb byte relay; protocol enforcement is entirely the
    server's job. A single-threaded poll loop pumps both directions, so
    there is no thread left blocking on stdin at interpreter shutdown
    (a daemon thread parked in a buffered read would crash it).
    """
    path = os.path.abspath(os.path.expanduser(path))
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(path)
    except OSError as errmsg:
        raise ConnectionError('cannot connect to {}: {}'
                              .format(path, errmsg))
    logger.debug('Bridging stdio to %s', path)

    stdin_fd = sys.stdin.fileno()
    poll = select.poll()
    poll.register(stdin_fd, select.POLLIN | select.POLLHUP)
    poll.register(connection.fileno(), select.POLLIN | select.POLLHUP)
    try:
        while True:
            for fd, _event in poll.poll():
                if fd == stdin_fd:
                    chunk = os.read(stdin_fd, 65536)
                    if chunk:
                        connection.sendall(chunk)
                    else:
                        # No more input; tell the server, keep reading
                        poll.unregister(stdin_fd)
                        connection.shutdown(socket.SHUT_WR)
                else:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
    finally:
        connection.close()
