import db


def fresh_db(tmp_path, monkeypatch):
    """Point db.py at a throwaway sqlite file and initialise it.

    A plain helper rather than a fixture so call sites can name the path
    explicitly in assertions when useful (e.g. checking migrations twice).
    """
    path = tmp_path / "test.db"
    monkeypatch.setattr("config.settings.db_path", path)
    db.init_db(path)
    return path
