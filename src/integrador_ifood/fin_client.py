"""fin_client.py - o "telefone" do modulo Financial do iFood.

Configuracao no .env (grupo proprio, separado do Order e do BI):
    IFOOD_FIN_CLIENT_ID=...        (agora: app de TESTE; depois: app de producao)
    IFOOD_FIN_CLIENT_SECRET=...
    IFOOD_FIN_LOJAS=<merchantId>[,<merchantId>...]   (agora: a loja de teste)
    IFOOD_FIN_HOMOLOGACAO=1        (1 = ambiente de teste; 0 = producao)
    IFOOD_FIN_ROTA=...             (endereco das APIs; descubra com: python fin_testar.py)
    IFOOD_FIN_ROTA_CONC=...        (endereco da conciliacao, se for diferente; o fin_testar.py descobre)

Regras da documentacao que este arquivo respeita:
- token renovado sozinho, 10 min antes de vencer (auth.py)
- 429: respeita o retry-after; senao espera 2 s, 4 s, 8 s
- 500/502/503/504 e queda de rede: tenta de novo com a mesma espera crescente
- Sales: no maximo 90 dias por consulta, paginas comecam em 0, tamanho fixo
- Financial Events: no maximo 33 dias, paginas comecam em 1, size 100, hasNextPage
- Settlement: 404 = "nenhuma liquidacao no periodo" (nao e erro)
- Reconciliation: 202 = processando; 409 = reutiliza o requestId anterior
- maximo de 1000 consultas/min (seguramos em 900)
"""
import logging
import os
import time

import requests
from dotenv import load_dotenv

from auth import IFoodAuth
from client import IFoodClient

PASTA = os.path.dirname(os.path.abspath(__file__))
ROTA_PADRAO = "/financial/v3.0/merchants/{merchantId}/{recurso}"  # confirmado no teste de 22/10/2026
LIMITE_POR_MIN = 900
TENTATIVAS = 4  # 1a imediata, depois 2 s, 4 s, 8 s
TRANSITORIOS = (429, 500, 502, 503, 504)
MAX_DIAS = {"sales": 90, "financial-events": 33}

log = logging.getLogger("financeiro")
if not log.handlers:
    log.setLevel(logging.INFO)
    try:
        _h = logging.FileHandler(os.path.join(PASTA, "financeiro.log"), encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(_h)
    except OSError:
        log.addHandler(logging.NullHandler())


class FinError(Exception):
    """Erro ja em portugues claro, pronto para a tela."""

    def __init__(self, mensagem, status=None):
        super().__init__(mensagem)
        self.status = status


def lojas_configuradas():
    return [x.strip() for x in (os.getenv("IFOOD_FIN_LOJAS") or "").split(",") if x.strip()]


def em_homologacao():
    return (os.getenv("IFOOD_FIN_HOMOLOGACAO", "1").strip() not in ("0", "nao", "não", "false"))


def lista_da_resposta(corpo, preferidas=("financialEvents", "events", "data", "content", "items")):
    """A documentacao nao mostra o nome da lista de lancamentos; aceita os nomes usuais."""
    if isinstance(corpo, list):
        return corpo
    if not isinstance(corpo, dict):
        return []
    for k in preferidas:
        if isinstance(corpo.get(k), list):
            return corpo[k]
    for v in corpo.values():  # qualquer lista de objetos
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return v
    return []


class FinClient:
    def __init__(self, client=None, rota=None, homologacao=None, sleep=time.sleep, relogio=time.monotonic):
        if client is None:  # rele o .env: mudancas valem sem reiniciar o painel
            load_dotenv(os.path.join(PASTA, ".env"), override=True)
        self.rota = _limpar_rota(rota or os.getenv("IFOOD_FIN_ROTA") or ROTA_PADRAO, "IFOOD_FIN_ROTA")
        self.rota_conc = _limpar_rota(os.getenv("IFOOD_FIN_ROTA_CONC"), "IFOOD_FIN_ROTA_CONC")
        self.homologacao = em_homologacao() if homologacao is None else homologacao
        self.sleep, self.relogio = sleep, relogio
        self._chamadas = []
        if client is None:
            try:
                client = IFoodClient(auth=IFoodAuth(prefixo="IFOOD_FIN", margem_renovacao=600))
            except RuntimeError as e:
                raise FinError(str(e))
        self.client = client

    # ---------------------------------------------------------- endereco
    def _endereco(self, recurso, merchant_id, params, rota=None):
        """A rota pode trazer {merchantId} no caminho; senao ele vai como parametro."""
        params = dict(params or {})
        rota = rota or self.rota
        if "{merchantId}" in rota:
            path = rota.replace("{merchantId}", merchant_id)
        else:
            path = rota
            params["merchantId"] = merchant_id
        return path.replace("{recurso}", recurso), params

    def _respeitar_limite(self):
        agora = self.relogio()
        self._chamadas = [t for t in self._chamadas if agora - t < 60]
        if len(self._chamadas) >= LIMITE_POR_MIN:
            self.sleep(60 - (agora - self._chamadas[0]) + 0.5)
        self._chamadas.append(self.relogio())

    # ---------------------------------------------------------- chamada base
    def chamar(self, metodo, recurso, merchant_id, params=None, json=None, aceitos=(200,), rota=None):
        """Devolve a resposta (requests.Response) se o status estiver em `aceitos`."""
        path, params = self._endereco(recurso, merchant_id, params, rota)
        headers = {"x-request-homologation": "true"} if self.homologacao else {}
        if json is not None:
            headers["Content-Type"] = "application/json"
        espera = 2
        for tentativa in range(1, TENTATIVAS + 1):
            self._respeitar_limite()
            try:
                resp = self.client.request(metodo, path, params=params, json=json, headers=headers)
            except requests.HTTPError as e:  # o pedido de TOKEN foi recusado
                raise self._erro_token(e)
            except requests.RequestException as e:
                log.warning("%s %s: falha de rede (%s)", metodo, path, type(e).__name__)
                if tentativa == TENTATIVAS:
                    raise FinError(f"Não consegui falar com o iFood ({type(e).__name__}). Tente de novo em instantes.")
                self.sleep(espera)
                espera *= 2
                continue
            log.info("%s %s %s -> %s", metodo, path, {k: v for k, v in params.items()}, resp.status_code)
            if resp.status_code in aceitos:
                return resp
            if resp.status_code in TRANSITORIOS and tentativa < TENTATIVAS:
                self.sleep(self._retry_after(resp) or espera)
                espera *= 2
                continue
            raise self._erro(resp, recurso, merchant_id)

    @staticmethod
    def _retry_after(resp):
        try:
            v = resp.headers.get("retry-after") or (resp.json() or {}).get("retryAfter")
            return min(float(v), 120) if v else None
        except Exception:
            return None

    def _erro_token(self, e):
        r = e.response
        status = getattr(r, "status_code", None)
        corpo = str(getattr(r, "text", ""))[:300]
        log.error("token recusado: %s %s", status, corpo)
        if status == 403 and "No permissions granted" in corpo:
            return FinError("O login foi aceito, mas o app ainda não tem permissão para nenhuma loja. "
                            "Peça a autorização da loja em Meus aplicativos > Permissões e aprove no Portal do Parceiro.", status)
        if "Unsupported grant type" in corpo:
            return FinError("Este app é DISTRIBUÍDO e não aceita login direto. Use um app CENTRALIZADO.", status)
        return FinError(f"O iFood recusou o login do app (erro {status}): {corpo or '(vazio)'}. "
                        "Confira IFOOD_FIN_CLIENT_ID e IFOOD_FIN_CLIENT_SECRET no .env.", status)

    def _erro(self, resp, recurso, merchant_id):
        status = resp.status_code
        texto = str(resp.text)[:300]
        log.error("%s erro %s: %s", recurso, status, texto)
        if status == 400:
            return FinError(f"O iFood recusou os parâmetros da consulta ({recurso}): {texto}", status)
        if status == 401:
            return FinError("O iFood não aceitou o token (401), mesmo após renovar. Confira as credenciais IFOOD_FIN_ no .env.", status)
        if status == 403:
            return FinError(f"Sem permissão para a loja {merchant_id} (403). O app precisa ter o módulo Financial "
                            "e a loja precisa estar autorizada.", status)
        if status == 404 and "Route" in texto:
            return FinError("Endereço da API financeira não encontrado (404). Rode: python fin_testar.py "
                            "para descobrir o endereço certo e ajuste IFOOD_FIN_ROTA no .env.", status)
        if status == 404:
            return FinError(f"Nada encontrado ({recurso}, 404): {texto}", status)
        if status == 429:
            return FinError("Muitas consultas seguidas (429). Esperei e tentei de novo, sem sucesso; aguarde 1 minuto.", status)
        return FinError(f"O iFood está com problema (erro {status}). Tentei de novo e não deu; tente mais tarde.", status)

    # ---------------------------------------------------------- APIs
    def vendas(self, merchant_id, ini, fim):
        """API Sales para [ini, fim] (no maximo 90 dias). Segue todas as paginas (comecam em 0)."""
        _checar_janela("sales", ini, fim)
        todas, pagina = [], 0
        while pagina < 1000:
            resp = self.chamar("GET", "sales", merchant_id, {
                "beginSalesDate": str(ini), "endSalesDate": str(fim), "page": pagina})
            corpo = resp.json() or {}
            lote = corpo.get("sales") or []
            todas.extend(lote)
            total_pag = corpo.get("pageCount") or corpo.get("totalPages")
            if not lote or (total_pag is not None and pagina + 1 >= int(total_pag)):
                break
            pagina += 1
        return todas

    def eventos(self, merchant_id, ini, fim):
        """API Financial Events para [ini, fim] (no maximo 33 dias). Paginas comecam em 1."""
        _checar_janela("financial-events", ini, fim)
        todos, pagina = [], 1
        while pagina <= 1000:
            resp = self.chamar("GET", "financial-events", merchant_id, {
                "beginDate": str(ini), "endDate": str(fim), "page": pagina, "size": 100})
            corpo = resp.json() or {}
            lote = lista_da_resposta(corpo)
            todos.extend(lote)
            if not lote or not corpo.get("hasNextPage"):
                break
            pagina += 1
        return todos

    def liquidacoes(self, merchant_id, ini, fim):
        """API Settlement pela SEMANA DE APURACAO (a documentacao fala em beginDate/endDate, mas
        o iFood respondeu que aceita beginCalculationDate/endCalculationDate ou datas de pagamento).
        404 = nenhuma liquidacao no periodo -> resposta vazia."""
        resp = self.chamar("GET", "settlements", merchant_id,
                           {"beginCalculationDate": str(ini), "endCalculationDate": str(fim)}, aceitos=(200, 404))
        if resp.status_code == 404:
            if "Route" in str(resp.text):
                raise self._erro(resp, "settlements", merchant_id)
            return {"balance": 0, "settlements": [], "vazio": True}
        return resp.json() or {}

    def _rota_conc(self):
        """Endereco da conciliacao + "{recurso}" (que vira "" no pedido e "/<requestId>" no status)."""
        base = self.rota_conc or self.rota.replace("{recurso}", "reconciliation-on-demand")
        return base.rstrip("/") + "{recurso}"

    def pedir_conciliacao(self, merchant_id, competencia):
        """POST do arquivo do mes (AAAA-MM). Devolve (request_id, reutilizado?)."""
        # A documentacao manda "competencia", mas o iFood respondeu com "competence" e ignorou o
        # nosso mes (22/10/2026). Mandamos os dois nomes e conferimos o mes devolvido.
        resp = self.chamar("POST", "", merchant_id, json={"competence": competencia, "competencia": competencia},
                           aceitos=(200, 201, 202, 409), rota=self._rota_conc())
        corpo = resp.json() if resp.content else {}
        devolvido = (corpo or {}).get("competence") or (corpo or {}).get("competencia")
        if devolvido and str(devolvido)[:7] != competencia:
            raise FinError(f"Pedi o arquivo de {competencia}, mas o iFood registrou o pedido para {devolvido}. "
                           "O nome do campo do mês pode estar diferente; me envie esta mensagem.", 400)
        if resp.status_code == 409:
            rid = corpo.get("lastRequestId") or corpo.get("requestId")
            if not rid:
                raise FinError("O iFood disse que já existe um pedido recente deste mês (409), mas não informou o "
                               "requestId. Tente de novo em 6 horas.", 409)
            return rid, True
        rid = corpo.get("requestId") or corpo.get("id")
        if not rid:
            raise FinError(f"O iFood aceitou o pedido do arquivo, mas não devolveu o requestId: {str(corpo)[:200]}")
        return rid, False

    def status_conciliacao(self, merchant_id, request_id):
        """Devolve ("processando", None) ou ("pronto", url_de_download)."""
        resp = self.chamar("GET", f"/{request_id}", merchant_id, aceitos=(200, 202), rota=self._rota_conc())
        if resp.status_code == 202:
            return "processando", None
        corpo = resp.json() if resp.content else {}
        url = corpo.get("downloadUrl") or corpo.get("url") or corpo.get("fileUrl")
        if not url:
            return "processando", None
        return "pronto", url

    def baixar(self, url, destino):
        """Baixa o .csv.gz do link temporario (o link ja vem assinado; nao leva token)."""
        r = requests.get(url, timeout=120, stream=True)
        if r.status_code != 200:
            raise FinError(f"Não consegui baixar o arquivo (erro {r.status_code}). O link expira; peça o status de novo.")
        tmp = destino + ".tmp"
        with open(tmp, "wb") as f:
            for parte in r.iter_content(65536):
                f.write(parte)
        os.replace(tmp, destino)
        return destino


def _limpar_rota(valor, nome):
    """Aceita erros comuns de digitacao no .env:
    - nome repetido:  IFOOD_FIN_ROTA_CONC=IFOOD_FIN_ROTA_CONC=/financial/...
    - endereco completo: https://merchant-api.ifood.com.br/financial/...
    - aspas ou espacos.
    Se ainda assim nao comecar com "/", explica o problema em vez de falhar na conexao."""
    if not valor or not str(valor).strip():
        return None
    v = str(valor).strip().strip('"').strip("'").strip()
    if "=" in v:
        v = v.split("=")[-1].strip()
    if v.startswith("http"):
        v = "/" + v.split("://", 1)[-1].split("/", 1)[-1]
    if not v.startswith("/"):
        raise FinError(f"A linha {nome} do .env está estranha: {valor!r}. Ela deve começar com /financial/... "
                       f"(exemplo: {nome}=/financial/v3.0/merchants/{{merchantId}}/...)")
    return v


def _checar_janela(recurso, ini, fim):
    from datetime import date, datetime
    a = ini if isinstance(ini, date) else datetime.strptime(str(ini), "%Y-%m-%d").date()
    b = fim if isinstance(fim, date) else datetime.strptime(str(fim), "%Y-%m-%d").date()
    if a > b:
        raise FinError("A data inicial é depois da final.", 400)
    if (b - a).days + 1 > MAX_DIAS[recurso]:
        raise FinError(f"{recurso}: no máximo {MAX_DIAS[recurso]} dias por consulta.", 400)
