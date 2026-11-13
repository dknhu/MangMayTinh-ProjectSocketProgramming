"""Integration entry point; ChatService from Member 2 is still required.

Run as python -m src.main. Only the GUI consumes backend events.
"""

import argparse
import queue
import threading
import time

from .discovery import DiscoveryService, validate_ip, validate_name
from .protocol import peer_id


def off_ui_thread(operation):
    """Run blocking startup/cleanup before Tk starts or after its loop exits."""
    errors = []

    def run():
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, name="app-lifecycle")
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


class Application:
    def __init__(self, local_peer, chat_factory, file_factory):
        self.events = queue.Queue()
        self.local_peer = local_peer
        self._shutdown_lock = threading.Lock()
        self.discovery = DiscoveryService(local_peer, self.events)
        self.files = file_factory(local_peer, self.events)
        try:
            self.chat = chat_factory(
                local_peer, self.events, self.resolve_peer,
                self.files.receive_file, bind_host=local_peer["ip"],
            )
        except Exception as construction_error:
            try:
                self.files.stop_transfers()
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"Chat construction failed: {construction_error}; "
                    f"file cleanup failed: {cleanup_error}"
                ) from construction_error
            raise

    def resolve_peer(self, pid):
        try:
            return self.discovery.get_peers()[pid]
        except KeyError as error:
            raise ValueError(f"Peer is no longer present: {pid}") from error

    def start(self):
        try:
            # The fixed nonzero port and concrete bind IP are the advertised endpoint.
            # start_listener must raise synchronously when bind/listen fails.
            self.chat.start_listener()
            self.discovery.start_discovery()
        except Exception as startup_error:
            try:
                self.shutdown()
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"Startup failed: {startup_error}; cleanup failed: {cleanup_error}"
                ) from startup_error
            raise

    def shutdown(self):
        with self._shutdown_lock:
            errors = []
            for label, stop in (
                ("discovery", self.discovery.stop_discovery),
                ("chat", self.chat.stop_listener),
                ("files", self.files.stop_transfers),
            ):
                try:
                    stop()
                except Exception as error:
                    errors.append(f"{label}: {error}")
            if errors:
                raise RuntimeError("; ".join(errors))

    def actions(self):
        from .gui import UIActions
        return UIActions(
            select_peer=self.chat.select_peer,
            send_message=self.chat.send_message,
            send_file=lambda pid, path: self.files.send_file(self.resolve_peer(pid), path),
            set_status=self.discovery.set_status,
            leave_chat=self.chat.leave_chat,
            cancel_transfer=self.files.cancel_transfer,
            shutdown=self.shutdown,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="LAN Chat Protocol v1 application")
    parser.add_argument("--name", required=True)
    parser.add_argument("--ip", required=True, help="Local LAN IPv4 address, not 0.0.0.0")
    parser.add_argument("--tcp-port", type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        ip = validate_ip(args.ip)
        name = validate_name(args.name)
        pid = peer_id(ip, args.tcp_port)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    # No missing-class import at module load, and no fake TCP implementation.
    from . import chat as chat_module
    chat_factory = getattr(chat_module, "ChatService", None)
    if chat_factory is None:
        parser.exit(2, "ChatService is not available yet. Integration is pending Member 2.\n")

    from .file_transfer import FileTransferManager
    from .gui import run_ui
    local_peer = {"peer_id": pid, "name": name, "ip": ip,
                  "tcp_port": args.tcp_port, "status": "online",
                  "last_seen": time.monotonic()}
    app = Application(local_peer, chat_factory, FileTransferManager)
    try:
        off_ui_thread(app.start)
        # Tk owns the main thread; its command worker invokes app.shutdown.
        run_ui(name, app.events, app.actions())
    finally:
        off_ui_thread(app.shutdown)


if __name__ == "__main__":
    main()
