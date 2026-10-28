"""Guards that prove the test run cannot touch live data or live services."""
import socket

import pytest
import requests

from app.extensions import db


def test_database_is_a_throwaway_sqlite_file(app):
    assert app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")
    assert db.engine.dialect.name == "sqlite"


def test_outbound_network_is_blocked():
    with pytest.raises(Exception):
        requests.get("https://api.paystack.co", timeout=2)
    with pytest.raises(RuntimeError):
        socket.create_connection(("8.8.8.8", 53), timeout=2)
