"""auth.py - a "chave da padaria".

Responsavel por obter e manter um token valido da API do iFood
(app CENTRALIZADO, grant client_credentials).

Regras da homologacao que este arquivo respeita:
- Usar o `expiresIn` devolvido pela API (nunca um tempo fixo).
- Renovar o token SO quando ele expirou (ou ao receber 401), nunca antes.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()  # le o arquivo .env (client id/secret ficam so la)

BASE_URL = os.getenv("IFOOD_BASE_URL", "https://merchant-api.ifood.com.br")
TOKEN_URL = f"{BASE_URL}/authentication/v1.0/oauth/token"


class IFoodAuth:
    """prefixo escolhe QUAL aplicativo usar, lendo do .env:
    IFoodAuth()                  -> IFOOD_CLIENT_ID / IFOOD_CLIENT_SECRET  (pedidos)
    IFoodAuth(prefixo="IFOOD_BI") -> IFOOD_BI_CLIENT_ID / IFOOD_BI_CLIENT_SECRET
    """

    def __init__(self, client_id=None, client_secret=None, session=None,
                 prefixo="IFOOD", margem_renovacao=0):
        # margem_renovacao: segundos ANTES do vencimento em que ja troca o token.
        # 0 = so troca depois de vencer (regra do Order/Events).
        # 600 = troca 10 min antes (exigido pela homologacao do Analytics).
        self.margem = int(margem_renovacao)
        var_id, var_secret = f"{prefixo}_CLIENT_ID", f"{prefixo}_CLIENT_SECRET"
        self.client_id = client_id or os.getenv(var_id)
        self.client_secret = client_secret or os.getenv(var_secret)
        if not self.client_id or not self.client_secret:
            raise RuntimeError(f"Preencha {var_id} e {var_secret} no arquivo .env")
        self._session = session or requests.Session()
        self._token = None
        self._expires_at = 0.0  # instante (time.time) em que o token vence

    def _request_new_token(self):
        resp = self._session.post(
            TOKEN_URL,
            data={  # a API espera form-urlencoded, nao JSON
                "grantType": "client_credentials",
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["accessToken"]
        # usa o tempo que a API informou; so renova depois de vencer
        self._expires_at = time.time() + int(body["expiresIn"])

    def get_token(self):
        """Devolve o token atual; so pede outro se vencido."""
        if self._token is None or time.time() >= self._expires_at - self.margem:
            self._request_new_token()
        return self._token

    def invalidate(self):
        """Chame ao receber 401: forca novo token na proxima chamada."""
        self._token = None
        self._expires_at = 0.0

    def headers(self):
        return {"Authorization": f"Bearer {self.get_token()}"}
