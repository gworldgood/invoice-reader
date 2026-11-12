"""設定の読み込みと、実行時フォルダの準備。

APIキーやモデル名はコードに書かず、.env か環境変数から受け取る。
両方にあるときは環境変数を優先する（CIや一時的な切り替えで上書きできるように）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# 作業書「4. 実行時のフォルダ」の5つ
DATA_SUBDIRS = ("inbox", "processing", "done", "review", "error")

_ENV_KEYS = ("ANTHROPIC_API_KEY", "MODEL", "JEV_API_KEY")


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model: str | None
    jev_api_key: str | None
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "invoices.db"

    def folder(self, name: str) -> Path:
        """inbox / processing / done / review / error のいずれかのパスを返す。"""
        if name not in DATA_SUBDIRS:
            raise ValueError(f"不明なフォルダ名です: {name}")
        return self.data_dir / name

    def missing_for_api(self) -> list[str]:
        """AIの読み取りに必要な設定のうち、空のものの名前を返す。"""
        missing = []
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.model:
            missing.append("MODEL")
        return missing


def load_settings(
    env_file: Path | None = None, data_dir: Path | None = None
) -> Settings:
    """.env と環境変数から設定を読む。.env がなくてもエラーにはしない。

    APIを呼ばない処理（検算やテスト）は、キーがなくても動かせるようにするため。
    キーが必要な処理では missing_for_api() で確かめる。
    """
    file_values = dotenv_values(env_file or DEFAULT_ENV_FILE)
    values: dict[str, str | None] = {}
    for key in _ENV_KEYS:
        values[key] = _blank_to_none(os.environ.get(key)) or _blank_to_none(
            file_values.get(key)
        )

    return Settings(
        anthropic_api_key=values["ANTHROPIC_API_KEY"],
        model=values["MODEL"],
        jev_api_key=values["JEV_API_KEY"],
        data_dir=data_dir or DEFAULT_DATA_DIR,
    )


def ensure_data_dirs(settings: Settings) -> None:
    """data/ と、その下の5つのフォルダがなければ作る。あれば何もしない。"""
    for name in DATA_SUBDIRS:
        settings.folder(name).mkdir(parents=True, exist_ok=True)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
