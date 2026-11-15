"""請求書読み取りツールの画面を開く。「請求書ツールを開く.bat」から呼ぶ。

- すでに動いていれば、ブラウザで開くだけ
- 動いていなければ画面のサーバーを起動し、準備ができたらブラウザで開く
- サーバーの記録（英語の起動メッセージやエラー）は data/app.log に書き、この画面には日本語の案内だけを出す
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8501
URL = f"http://localhost:{PORT}"
LOG = ROOT / "data" / "app.log"


def running() -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/_stcore/health", timeout=1) as r:
            return r.read().strip() == b"ok"
    except OSError:
        return False


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # 案内をすぐに画面に出す
    if running():
        webbrowser.open(URL)
        print("請求書読み取りツールはもう開いています。ブラウザで表示しました。")
        return 0

    print("請求書読み取りツールを起動しています。少しお待ちください。")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "streamlit", "run", str(ROOT / "app" / "main.py"),
             "--server.port", str(PORT)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )
        for _ in range(120):  # 最大60秒待つ
            if server.poll() is not None:
                print("起動できませんでした。管理者に data/app.log を見てもらってください。")
                return 1
            if running():
                break
            time.sleep(0.5)
        else:
            print("起動に時間がかかっています。管理者に data/app.log を見てもらってください。")
            server.terminate()
            return 1

        webbrowser.open(URL)
        print(f"ブラウザで {URL} を開きました。")
        print("使っている間は、この黒い画面を閉じないでください。使い終わったら閉じてかまいません。")
        try:
            return server.wait()
        except KeyboardInterrupt:
            server.terminate()
            return 0


if __name__ == "__main__":
    sys.exit(main())
