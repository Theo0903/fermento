"""auth_distribuido.py - a "chave de prestador" (app DISTRIBUIDO do iFood).

Diferente do app centralizado (auth.py), aqui o dono da loja autoriza o app UMA vez
no Portal do Parceiro (autorizar_bi.py). Depois, este arquivo so renova o token
sozinho usando o refresh token guardado em bi_tokens.json.

Seguranca: bi_tokens.json e bi_autorizacao_pendente.json contem credenciais.
Mantenha-os FORA do git (.gitignore) e nunca envie para ninguem.
"""
import json
import os
import time

import requests
from dotenv import load_dotenv

from auth import BASE_URL, TOKEN_URL

load_dotenv()

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_TOKENS = os.getenv("IFOOD_BI_TOKENS", os.path.join(PASTA, "bi_tokens.json"))
ARQ_PENDENTE = os.path.join(PASTA, "bi_autorizacao_pendente.json")
USERCODE_URL = f"{BASE_URL}/authentication/v1.0/oauth/userCode"


class AutorizacaoNecessaria(Exception):
    """O app ainda nao foi autorizado (ou a autorizacao caiu): rode autorizar_bi.py."""


def _ler(caminho):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _gravar(caminho, dados):
    """Grava de forma atomica (nao deixa arquivo pela metade se cair a luz)."""
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f)
    try:
        os.chmod(tmp, 0o600)  # so o dono le (no Windows e ignorado)
    except OSError:
        pass
    os.replace(tmp, caminho)


def tokens_existem():
    return bool((_ler(ARQ_TOKENS) or {}).get("refreshToken"))


class IFoodAuthDistribuido:
    """Mesma "interface" do IFoodAuth: get_token(), invalidate(), headers()."""

    def __init__(self, client_id=None, client_secret=None, session=None,
                 prefixo="IFOOD_BI", margem_renovacao=0):
        var_id, var_secret = f"{prefixo}_CLIENT_ID", f"{prefixo}_CLIENT_SECRET"
        self.client_id = client_id or os.getenv(var_id)
        self.client_secret = client_secret or os.getenv(var_secret)
        if not self.client_id or not self.client_secret:
            raise RuntimeError(f"Preencha {var_id} e {var_secret} no arquivo .env")
        self._session = session or requests.Session()
        self.margem = int(margem_renovacao)
        self._token, self._expires_at = None, 0.0

    # --- passo 1 e 2 da autorizacao (usados por autorizar_bi.py)
    def pedir_codigo(self):
        resp = self._session.post(USERCODE_URL, data={"clientId": self.client_id}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def trocar_codigo(self, authorization_code, verifier):
        resp = self._session.post(TOKEN_URL, data={
            "grantType": "authorization_code",
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "authorizationCode": authorization_code,
            "authorizationCodeVerifier": verifier,
        }, timeout=30)
        resp.raise_for_status()
        self._guardar(resp.json())

    # --- uso diario
    def _guardar(self, corpo, refresh_antigo=None):
        self._token = corpo["accessToken"]
        self._expires_at = time.time() + int(corpo["expiresIn"])
        refresh = corpo.get("refreshToken") or refresh_antigo
        _gravar(ARQ_TOKENS, {
            "accessToken": self._token, "expiresAt": self._expires_at, "refreshToken": refresh,
        })

    def _carregar(self):
        d = _ler(ARQ_TOKENS) or {}
        if not d.get("refreshToken"):
            raise AutorizacaoNecessaria(
                "O app APP_BI ainda não foi autorizado neste computador. "
                "Rode: python autorizar_bi.py"
            )
        return d

    def get_token(self):
        if self._token is None:  # primeira vez nesta execucao: usa o que esta no arquivo
            d = self._carregar()
            self._token, self._expires_at = d.get("accessToken"), float(d.get("expiresAt") or 0)
        if self._token and time.time() < self._expires_at - self.margem:
            return self._token
        d = self._carregar()
        resp = self._session.post(TOKEN_URL, data={
            "grantType": "refresh_token",
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "refreshToken": d["refreshToken"],
        }, timeout=30)
        if resp.status_code in (400, 401):
            self._token, self._expires_at = None, 0.0
            raise AutorizacaoNecessaria(
                "O iFood recusou renovar o acesso (a autorização pode ter sido revogada ou ter "
                "vencido). Autorize de novo: python autorizar_bi.py"
            )
        resp.raise_for_status()
        self._guardar(resp.json(), refresh_antigo=d["refreshToken"])
        return self._token

    def invalidate(self):
        """Chame ao receber 401: forca renovar na proxima chamada (mantem o refresh token)."""
        self._token, self._expires_at = "", 0.0

    def headers(self):
        return {"Authorization": f"Bearer {self.get_token()}"}
