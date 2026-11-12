from invoice_reader.config import DATA_SUBDIRS, ensure_data_dirs, load_settings

KEYS = ("ANTHROPIC_API_KEY", "MODEL", "JEV_API_KEY")


def _clear_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_reads_values_from_env_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=test-key\nMODEL=test-model\nJEV_API_KEY=\n", encoding="utf-8")

    s = load_settings(env_file=env, data_dir=tmp_path / "data")

    assert s.anthropic_api_key == "test-key"
    assert s.model == "test-model"
    assert s.jev_api_key is None  # 空欄は「未設定」として扱う
    assert s.missing_for_api() == []


def test_environment_variable_wins_over_env_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MODEL", "from-env")

    assert load_settings(env_file=env).model == "from-env"


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    _clear_env(monkeypatch)

    s = load_settings(env_file=tmp_path / "no-such.env")

    assert s.missing_for_api() == ["ANTHROPIC_API_KEY", "MODEL"]


def test_ensure_data_dirs_creates_all_folders(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    s = load_settings(env_file=tmp_path / "no-such.env", data_dir=tmp_path / "data")

    ensure_data_dirs(s)
    ensure_data_dirs(s)  # 2回目も失敗しない

    for name in DATA_SUBDIRS:
        assert (tmp_path / "data" / name).is_dir()
    assert s.db_path == tmp_path / "data" / "invoices.db"
