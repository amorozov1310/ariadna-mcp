"""python -m tools.license_desk [--port 9890] [--home ~/.ariadna-license] [--no-browser]"""

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app, default_home


def main() -> None:
    parser = argparse.ArgumentParser(prog='python -m tools.license_desk',
                                     description='Выдача ключей Ариадны (только на этом компьютере)')
    parser.add_argument('--port', type=int, default=9890)
    parser.add_argument('--home', type=Path, default=None,
                        help='каталог с закрытым ключом и реестром (по умолчанию ~/.ariadna-license)')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    home = args.home or default_home()
    url = f'http://127.0.0.1:{args.port}/'
    print(f'Выдача ключей: {url}  (данные: {home}; остановить — Ctrl+C)')
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    # Только 127.0.0.1: реестр клиентов и закрытый ключ — не для сети.
    uvicorn.run(create_app(home), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
