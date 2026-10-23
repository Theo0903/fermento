"""client.py - o "telefone" da padaria.

Todas as chamadas para o iFood passam por aqui. Ele coloca o token
(auth.py) em cada chamada e, se o iFood responder 401 (token recusado),
pede um token novo UMA vez e repete a chamada.
"""
import requests

from auth import BASE_URL, IFoodAuth


class IFoodError(Exception):
    """Erro devolvido pelo iFood, ja com o status e a mensagem dele."""


def checar(resp, ok=(200, 201, 202, 204)):
    """Como raise_for_status, mas mostrando o que o iFood escreveu."""
    if resp.status_code not in ok:
        raise IFoodError(f"{resp.status_code} {str(resp.text)[:300]}")
    return resp


class IFoodClient:
    def __init__(self, auth=None, session=None):
        self.auth = auth or IFoodAuth()
        self.session = session or requests.Session()

    def request(self, method, path, **kwargs):
        url = f"{BASE_URL}{path}"
        kwargs.setdefault("timeout", 30)
        extra_headers = kwargs.pop("headers", {})
        for tentativa in (1, 2):
            headers = {**self.auth.headers(), **extra_headers}
            resp = self.session.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and tentativa == 1:
                self.auth.invalidate()  # token recusado: pega outro e repete
                continue
            return resp
        return resp
