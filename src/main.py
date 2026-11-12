import argparse
import queue
import threading

from discovery import DiscoveryService


def display_events(discovery, stop_event):
    while not stop_event.is_set():
        try:
            event = discovery.events.get(timeout=0.2)
        except queue.Empty:
            continue

        if event["type"] == "DISCOVERY_ERROR":
            print(
                f"\n[Error] Discovery worker stopped: "
                f"{event['message']}",
                flush=True,
            )
            print(
                "[App] Enter 'quit' to close the application",
                flush=True,
            )
            return

        peer = event["peer"]

        print(
            f"\n[{event['type']}] "
            f"{peer['name']} "
            f"({peer['ip']}:{peer['tcp_port']}) "
            f"status={peer['status']}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(
        description="LAN discovery demonstration"
    )
    parser.add_argument("--name", default="Mari")
    parser.add_argument("--tcp-port", type=int, default=5001)
    args = parser.parse_args()

    discovery = DiscoveryService(
        name=args.name,
        tcp_port=args.tcp_port,
    )

    display_stop = threading.Event()
    display_worker = None

    try:
        discovery.start()

        display_worker = threading.Thread(
            target=display_events,
            args=(discovery, display_stop),
            name="event-display-worker",
        )
        display_worker.start()

        print("[App] Discovery started")
        print("[App] Commands: online, away, peers, quit")

        while True:
            command = input("> ").strip().lower()

            if command == "quit":
                break

            if command in ("online", "away"):
                discovery.set_status(command)
                print(f"[App] Status changed to: {command}")

            elif command == "peers":
                peers = discovery.get_peers()

                if not peers:
                    print("[App] No active peers")

                for peer in peers:
                    print(
                        f"- {peer['name']} "
                        f"({peer['ip']}:{peer['tcp_port']}) "
                        f"status={peer['status']}"
                    )

            else:
                print("[App] Unknown command")

    except (KeyboardInterrupt, EOFError):
        print("\n[App] Exit requested")

    except (OSError, ValueError, RuntimeError) as error:
        print(f"[Error] {error}")
        raise

    finally:
        discovery.stop()
        display_stop.set()

        if display_worker is not None:
            display_worker.join()

        print("[App] Stopped")


if __name__ == "__main__":
    main()