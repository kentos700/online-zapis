import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def resolve_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def build_sqlite_uri(db_path):
    return f"sqlite:///{db_path.as_posix()}"


def ensure_runtime_environment():
    base_dir = resolve_base_dir()
    instance_dir = base_dir / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("DATABASE_URL", build_sqlite_uri(instance_dir / "clinic.db"))
    os.environ.setdefault("SECRET_KEY", "clinicflow-desktop-launcher")


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_browser(url):
    time.sleep(1.2)
    webbrowser.open(url)


def main():
    ensure_runtime_environment()

    from app import app

    host = "127.0.0.1"
    port = get_free_port()
    url = f"http://{host}:{port}"

    print(f"ClinicFlow доступен по адресу: {url}")
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
