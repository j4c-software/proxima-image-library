from src import __version__
from src.app import app


def test_version_endpoint_and_home_page_display_release_version(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", True)
    with app.test_client() as client:
        version = client.get("/api/version")
        assert version.status_code == 200
        assert version.get_json()["version"] == __version__

        with client.session_transaction() as session:
            session["user"] = {"name": "Test User", "preferred_username": "test@example.org"}
        home = client.get("/")
        assert home.status_code == 200
        assert __version__.encode() in home.data
