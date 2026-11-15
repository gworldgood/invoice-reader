"""1件の請求書を、届いてから登録（または要確認）まで流す。作業書「2. 全体の流れ」の1〜8。

    inbox → processing → 文字か画像を取り出す → AIで読み取る → staging に入れる → 検算
        → 通過：invoices に登録し、原本を done/ に「YYYYMMDD_発行元_請求額」で移す
        → 引っかかった：staging を review にし、理由を保存して review/ に移す
    読めないファイル、AIの返事が使えないもの → error/

APIそのものの失敗（通信エラー、レート制限、キーの誤り）は、ファイルを inbox に戻して例外を上に投げる。
ファイルが悪いわけではないので error/ には移さず、直してから再実行すれば続きから処理できる。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from invoice_reader import db
from invoice_reader.config import Settings
from invoice_reader.extract import SUPPORTED_SUFFIXES, Extracted, ExtractError, extract
from invoice_reader.llm import LLMError, ReadResult
from invoice_reader.validate import validate

Reader = Callable[[Extracted], ReadResult]

AUTO_APPROVER = "auto"
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\s]+')


@dataclass
class Outcome:
    file_name: str
    status: str  # passed / review / error / skipped
    moved_to: Path | None
    staging_id: int | None = None
    problems: list[dict] = field(default_factory=list)  # 引っかかった理由（code, field, message）
    read: ReadResult | None = None  # AIを呼んだときの記録（トークン数、時間）


class IntakeError(Exception):
    """受け付けられないファイル（対応していない種類、空のファイル）。"""


def run_inbox(settings: Settings, conn: sqlite3.Connection, reader: Reader) -> list[Outcome]:
    """inbox/ にあるファイルを名前順にすべて処理する。"""
    return [process_file(p, settings, conn, reader) for p in inbox_files(settings)]


def inbox_files(settings: Settings) -> list[Path]:
    """inbox/ で読み取りを待っているファイル。名前が . で始まるもの（書き込み途中など）は除く。"""
    inbox = settings.folder("inbox")
    return sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))


def save_to_inbox(settings: Settings, name: str, data: bytes) -> Path:
    """画面で受け取ったファイルを inbox/ に置く。同じ名前があれば _2, _3 を付ける。

    書き込みの途中で読み取りが始まらないよう、. で始まる仮の名前で書いてから名前を変える。
    """
    base = Path(name.replace("\\", "/")).name  # フォルダの部分は捨てて、ファイル名だけにする
    if Path(base).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise IntakeError(f"{base} は読み取れない種類のファイルです（PDF、JPG、PNG のみ）")
    if not data:
        raise IntakeError(f"{base} は中身が空のファイルです")
    inbox = settings.folder("inbox")
    temp = unique_path(inbox / f".{base}.part")
    temp.write_bytes(data)
    dest = unique_path(inbox / base)
    temp.rename(dest)
    return dest


def recover_processing(settings: Settings) -> list[Path]:
    """processing/ に残ったファイルを inbox/ に戻す。戻したファイルのパスを返す。

    読み取りの途中で画面やPCが止まると、ファイルが processing/ に残って誰にも見えなくなる。
    読み取りを始める前（ほかに読み取り中のものがないとき）に呼んで、読み取り待ちに戻す。
    """
    folder = settings.folder("processing")
    return [move_to(p, settings.folder("inbox")) for p in sorted(folder.iterdir()) if p.is_file()]


def process_file(path: Path, settings: Settings, conn: sqlite3.Connection, reader: Reader) -> Outcome:
    file_name = path.name
    file_hash = sha256(path)

    # 同じ内容のファイルは二度処理しない（AIを呼ぶ前に止めるので費用もかからない）
    existing = db.find_staging_by_hash(conn, file_hash)
    if existing is not None:
        dest = move_to(path, settings.folder("error"))
        first = db.get_staging(conn, existing)
        problem = {
            "code": "same_file",
            "field": None,
            "message": f"同じ内容のファイル（{first['file_name']}）をすでに読み取っています",
        }
        return Outcome(file_name, "skipped", dest, existing, [problem])

    working = move_to(path, settings.folder("processing"))
    try:
        extracted = extract(working)
        read = reader(extracted)
    except (ExtractError, LLMError) as e:
        dest = move_to(working, settings.folder("error"))
        problem = {"code": "unreadable", "field": None, "message": str(e)}
        with conn:
            staging_id = db.insert_staging(
                conn,
                file_name=file_name,
                file_hash=file_hash,
                stored_path=_rel(dest, settings),
                status="error",
                errors=[problem],
            )
        return Outcome(file_name, "error", dest, staging_id, [problem])
    except Exception:
        move_to(working, settings.folder("inbox"))  # 元に戻して、直してから再実行できるように
        raise

    # AIの読み取り結果は、まず仮テーブルに入れる（本番に直接入れない）
    with conn:
        staging_id = db.insert_staging(
            conn,
            file_name=file_name,
            file_hash=file_hash,
            stored_path=_rel(working, settings),
            source=read.source,
            data=read.data,
        )

    problems = [
        asdict(p)
        for p in validate(read.data, exists=lambda issuer, no: db.invoice_exists(conn, issuer, no))
    ]

    if problems:
        dest = move_to(working, settings.folder("review"))
        with conn:
            db.update_staging(
                conn, staging_id, status="review", stored_path=_rel(dest, settings), errors=problems
            )
        return Outcome(file_name, "review", dest, staging_id, problems, read)

    dest = unique_path(settings.folder("done") / done_name(read.data, working.suffix))
    with conn:  # DBの登録とファイルの移動を一緒に行い、移動に失敗したらDBも取り消す
        db.insert_invoice(
            conn, staging_id=staging_id, original_path=_rel(dest, settings), approved_by=AUTO_APPROVER
        )
        db.update_staging(conn, staging_id, status="passed", stored_path=_rel(dest, settings))
        working.rename(dest)
    return Outcome(file_name, "passed", dest, staging_id, [], read)


# ---------- ファイルの扱い ----------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def done_name(data: dict, suffix: str) -> str:
    """登録済みの原本の名前：YYYYMMDD_発行元_請求額.pdf（作業書4）。"""
    issuer = _UNSAFE_FILENAME.sub("_", data["issuer"]).strip("_")
    return f"{data['issue_date'].replace('-', '')}_{issuer}_{data['total']}{suffix.lower()}"


def move_to(path: Path, folder: Path) -> Path:
    """フォルダへ移す。同じ名前があれば上書きせず、名前の後ろに _2, _3 を付ける。"""
    dest = unique_path(folder / path.name)
    path.rename(dest)
    return dest


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _rel(path: Path, settings: Settings) -> str:
    return path.relative_to(settings.data_dir).as_posix()
