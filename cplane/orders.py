"""HTTP transport for queued owner orders."""
import json
import os
from pathlib import Path
import urllib.request
from urllib.parse import quote
from urllib.request import Request

ENV_VAR = 'CPLANE_ORDERS_URL'
URL_FILE = 'orders-url'
DEFAULT_TIMEOUT = 10


def orders_url(runs):
    """Return the configured orders service URL, or None when unset."""
    from_env = os.environ.get(ENV_VAR)
    if from_env is not None and from_env.strip():
        return from_env.strip()
    path = Path(runs).parent / URL_FILE
    try:
        from_file = path.read_text().strip()
    except OSError:
        return None
    return from_file or None


class HttpClient:
    """Minimal JSON client for the orders service."""

    def __init__(self, base, timeout=DEFAULT_TIMEOUT):
        self.base = base.rstrip('/')
        self.timeout = timeout

    def fetch(self):
        request = Request(self.base + '/orders')
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode())
        return payload['orders']

    def ack(self, id, outcome, detail):
        segment = quote(str(id), safe='')
        body = json.dumps({'outcome': outcome, 'detail': detail}).encode()
        request = Request(self.base + '/orders/' + segment + '/ack',
                          data=body, method='POST',
                          headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(request, timeout=self.timeout)
