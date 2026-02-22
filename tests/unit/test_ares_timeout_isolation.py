from __future__ import annotations

import socket
from unittest.mock import patch

from kajovospend.integrations import ares


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "obchodniJmeno": "ACME",
            "dic": "CZ12345678",
            "sidlo": {"nazevObce": "Praha", "psc": "11000"},
        }


def test_fetch_by_ico_nemeni_globalni_socket_timeout() -> None:
    puvodni_timeout = socket.getdefaulttimeout()
    sentinel = 12.5
    socket.setdefaulttimeout(sentinel)

    try:
        with patch("kajovospend.integrations.ares.requests.get", return_value=_Response()) as mocked_get:
            rec = ares.fetch_by_ico("12345678", timeout=3, cache_ttl_seconds=0)

        assert rec.ico == "12345678"
        assert socket.getdefaulttimeout() == sentinel
        mocked_get.assert_called_once()
    finally:
        socket.setdefaulttimeout(puvodni_timeout)
