"""analytics.py - o "telefone" do modulo Analytics (categoria BI).

O Analytics NAO entrega pedido por pedido: entrega totais ja somados
(GMV, quantidade de pedidos...) agrupados como pedirmos. Os dados sao D-1
(ate a meia-noite de ontem; o iFood publica ate as 9h).

Configuracao no .env (chaves do app APP_BI, separadas das do Order):
    IFOOD_BI_CLIENT_ID=...
    IFOOD_BI_CLIENT_SECRET=...
    IFOOD_BI_MODELO=rede          (rede | merchant - o modelo escolhido ao criar o app)
    IFOOD_BI_ALVO_ID=...          (chain_id se rede; merchant_id se merchant)

Regras da homologacao que este arquivo respeita:
- escopo analytics no token + renovacao 10 min ANTES de vencer (auth.py)
- sempre filter.referenceDate e ao menos uma agregacao (validado antes de enviar)
- paginacao (page 1-1000, size 1-10000)
- retry com espera crescente (2s, 4s) para 429/500/502/503/504
- 400/401/403/404 viram mensagens claras em portugues
- 403 NUNCA e repetido sozinho (so adianta ajustar a permissao)
- maximo de 500 consultas por minuto (aqui seguramos em 480)
- log de cada chamada em analytics.log (sem token e sem segredo)
"""
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

from auth import IFoodAuth
from auth_distribuido import AutorizacaoNecessaria, IFoodAuthDistribuido, tokens_existem
from client import IFoodClient

PASTA = os.path.dirname(os.path.abspath(__file__))
BASE = "/analytics/v1.0"
TRANSITORIOS = (429, 500, 502, 503, 504)
MAX_TENTATIVAS = 3  # 1a imediata, 2a apos 2 s, 3a apos 4 s
LIMITE_POR_MIN = 480  # o iFood permite 500; deixamos folga

# nomes aceitos pela API (do dicionario de dados)
FUNCOES_METRICA = {"sum", "avg", "min", "max"}
FUNCOES_TERMO = {"count", "cardinality"}
CAMPOS_GROUPBY = {
    "salesChannel", "deliveredBy", "category", "dayOfWeek", "customerFirstOrderInMerchant",
    "orderCreationHour", "orderStatus", "paymentMethod", "merchantId", "merchantShortId",
    "cancellationCode", "cancellationOrigin", "cancellationStage", "cancellationReason",
}
# metricas que pedimos em toda consulta agrupada (so "sum")
METRICAS = [
    "gmv", "gmvWithoutDelivery", "netDeliveryFee",
    "benefitByTypePartnerTotalValue", "feesGrossValue", "refundedTotalValue",
]

log = logging.getLogger("analytics")
if not log.handlers:
    log.setLevel(logging.INFO)
    try:
        _h = logging.FileHandler(os.path.join(PASTA, "analytics.log"), encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(_h)
    except OSError:  # pasta sem permissao de escrita: segue sem log em arquivo
        log.addHandler(logging.NullHandler())


class AnalyticsError(Exception):
    """Erro ja em portugues claro, pronto para mostrar na tela."""

    def __init__(self, mensagem, status=None, detalhes=None):
        super().__init__(mensagem)
        self.status = status
        self.detalhes = detalhes or []


# ---------------------------------------------------------------- validacao
def _como_data(txt):
    txt = str(txt)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            pass
    raise AnalyticsError(f"Data inválida: {txt!r}. Use AAAA-MM-DD ou AAAA-MM-DD HH:MM:SS.", 400)


def validar_corpo(corpo):
    """Pega os erros que o iFood devolveria como 400 ANTES de enviar."""
    ref = (corpo.get("filter") or {}).get("referenceDate")
    if not ref:
        raise AnalyticsError("Faltou o período (filter.referenceDate).", 400)
    if "gte" in ref and "gt" in ref or "lte" in ref and "lt" in ref:
        raise AnalyticsError("No período, use só um entre gte/gt e só um entre lte/lt.", 400)
    ini = _como_data(ref.get("gte") or ref.get("gt"))
    fim = _como_data(ref.get("lte") or ref.get("lt"))
    if ini > fim:
        raise AnalyticsError("A data inicial é depois da data final.", 400)

    agg = corpo.get("agg") or {}
    if not any(agg.get(k) for k in ("metrics", "terms", "groupBy", "dateIntervals")):
        raise AnalyticsError("A consulta precisa de ao menos uma agregação.", 400)
    for nome, funcoes in (agg.get("metrics") or {}).items():
        if len(set(funcoes)) != len(funcoes) or not set(funcoes) <= FUNCOES_METRICA:
            raise AnalyticsError(f"Funções inválidas ou repetidas na métrica {nome}: {funcoes}", 400)
    for nome, funcoes in (agg.get("terms") or {}).items():
        if len(set(funcoes)) != len(funcoes) or not set(funcoes) <= FUNCOES_TERMO:
            raise AnalyticsError(f"Funções inválidas ou repetidas no termo {nome}: {funcoes}", 400)
    campos = (agg.get("groupBy") or {}).get("fields") or []
    if len(set(campos)) != len(campos) or not set(campos) <= CAMPOS_GROUPBY:
        raise AnalyticsError(f"Campos de agrupamento inválidos ou repetidos: {campos}", 400)
    return corpo


def corpo_periodo(ini, fim, agg, extra_filtro=None, page=1, size=1000, sort=None):
    """Monta o corpo: ini/fim sao datas (date) ou textos AAAA-MM-DD, ambos inclusive."""
    corpo = {
        "agg": agg,
        "filter": {"referenceDate": {"gte": f"{ini} 00:00:00", "lte": f"{fim} 23:59:59"}},
        "page": page,
        "size": size,
    }
    corpo["filter"].update(extra_filtro or {})
    if sort:
        corpo["sort"] = {"groupByKey": sort}
    return corpo


# ---------------------------------------------------------------- cliente
class IFoodAnalytics:
    def __init__(self, client=None, modelo=None, alvo_id=None, homologacao=False,
                 sleep=time.sleep, relogio=time.monotonic):
        bruto = (modelo or os.getenv("IFOOD_BI_MODELO") or "rede").strip().lower()
        if bruto in ("rede", "chain"):
            self.modelo = "rede"
        elif bruto in ("merchant", "loja"):
            self.modelo = "merchant"
        else:
            raise AnalyticsError("IFOOD_BI_MODELO deve ser 'rede' ou 'merchant'.")
        self.alvo_id = (alvo_id or os.getenv("IFOOD_BI_ALVO_ID") or "").strip()
        if not self.alvo_id:
            raise AnalyticsError(
                "Preencha IFOOD_BI_ALVO_ID no arquivo .env (chain_id da rede, "
                "ou merchant_id da loja se o app for por merchant)."
            )
        self.homologacao = homologacao
        self.sleep, self.relogio = sleep, relogio
        self._chamadas = []  # instantes das ultimas chamadas (limite por minuto)
        if client is None:
            # tipo do app: centralizado (client_credentials) ou distribuido (autorizacao do dono)
            tipo = (os.getenv("IFOOD_BI_TIPO") or ("distribuido" if tokens_existem() else "centralizado")).lower()
            try:
                if tipo.startswith("distrib"):
                    auth = IFoodAuthDistribuido(prefixo="IFOOD_BI", margem_renovacao=600)
                else:
                    auth = IFoodAuth(prefixo="IFOOD_BI", margem_renovacao=600)
                client = IFoodClient(auth=auth)
            except RuntimeError as e:
                raise AnalyticsError(str(e))
        self.client = client

    # --- caminho conforme o modelo de permissao
    def _path(self):
        if self.modelo == "rede":
            return f"{BASE}/chain/{self.alvo_id}/orders/kpis"
        return f"{BASE}/merchants/{self.alvo_id}/orders/kpis"

    def _path_log(self):
        """O caminho do jeito que pode ir para o log: do id (chain_id na rede,
        merchantId na loja) ficam so os 4 primeiros caracteres.
        """
        if not self.alvo_id:
            return self._path()
        return self._path().replace(str(self.alvo_id), str(self.alvo_id)[:4] + "...")

    def _respeitar_limite(self):
        agora = self.relogio()
        self._chamadas = [t for t in self._chamadas if agora - t < 60]
        if len(self._chamadas) >= LIMITE_POR_MIN:
            espera = 60 - (agora - self._chamadas[0]) + 0.5
            log.warning("Perto do limite de %s consultas/min; aguardando %.1fs", LIMITE_POR_MIN, espera)
            self.sleep(espera)
        self._chamadas.append(self.relogio())

    def consulta(self, corpo):
        """Uma chamada (uma pagina). Devolve o JSON ou levanta AnalyticsError."""
        validar_corpo(corpo)
        headers = {"Content-Type": "application/json"}
        if self.homologacao:
            headers["x-request-homologation"] = "true"
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            self._respeitar_limite()
            try:
                resp = self.client.request("POST", self._path(), json=corpo, headers=headers)
            except AutorizacaoNecessaria as e:
                raise AnalyticsError(str(e), 401)
            except requests.HTTPError as e:
                # o iFood RESPONDEU com erro ao pedir o token (nao e problema de rede)
                r = e.response
                status = getattr(r, "status_code", None)
                corpo = str(getattr(r, "text", ""))[:300]
                log.error("token recusado: %s %s", status, corpo)
                if status == 403 and "No permissions granted" in corpo:
                    raise AnalyticsError(
                        "O login do app foi aceito, mas ele ainda não tem permissão para nenhuma loja/rede. "
                        "No Portal do Desenvolvedor: Meus aplicativos > APP_BI > Permissões > Pedir "
                        "autorização (informe o merchantId da loja, ou o chain_id se o app for por rede) e "
                        "depois aprove o pedido no Portal do Parceiro.", status)
                if "Unsupported grant type" in corpo:
                    raise AnalyticsError(
                        "Este app é do tipo DISTRIBUÍDO e não aceita login direto. Adicione "
                        "IFOOD_BI_TIPO=distribuido no .env e rode: python autorizar_bi.py", status)
                raise AnalyticsError(
                    f"O iFood recusou o pedido de token do app APP_BI (erro {status}). "
                    f"Resposta: {corpo or '(vazia)'}. Confira IFOOD_BI_CLIENT_ID e IFOOD_BI_CLIENT_SECRET "
                    "no .env (sem espaços e sem aspas) e se o secret não foi regenerado no Portal.",
                    status,
                )
            except requests.RequestException as e:
                log.warning("tentativa %s: falha de rede (%s)", tentativa, type(e).__name__)
                if tentativa == MAX_TENTATIVAS:
                    raise AnalyticsError(
                        f"Não consegui falar com o iFood ({type(e).__name__}). "
                        "Se o painel de pedidos funciona, me mande esta mensagem completa."
                    )
                self.sleep(2 ** tentativa)
                continue
            log.info("POST %s -> %s (tentativa %s)", self._path_log(), resp.status_code, tentativa)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in TRANSITORIOS and tentativa < MAX_TENTATIVAS:
                self.sleep(2 ** tentativa)  # 2 s, depois 4 s
                continue
            raise self._erro(resp)

    def consulta_todas(self, corpo):
        """Segue as paginas ate acabar. Devolve (linhas, ultima_resposta)."""
        corpo = dict(corpo)
        linhas, pagina = [], corpo.get("page", 1)
        while True:
            corpo["page"] = pagina
            resp = self.consulta(corpo)
            linhas.extend(resp.get("data") or [])
            if pagina >= (resp.get("totalPages") or 1) or pagina >= 1000:
                return linhas, resp
            pagina += 1

    # --- traduz erro do iFood para portugues claro
    def _erro(self, resp):
        status = resp.status_code
        try:
            j = resp.json()
        except ValueError:
            j = {}
        detalhes = []
        if isinstance(j, dict):
            detalhes = list(j.get("details") or [])
            interno = j.get("error") if isinstance(j.get("error"), dict) else {}
            if j.get("message") or interno.get("message"):
                detalhes.append(j.get("message") or interno.get("message"))
        extra = f" ({'; '.join(map(str, detalhes))})" if detalhes else ""
        log.error("Erro %s do iFood%s", status, extra)
        if status == 400:
            msg = f"O iFood recusou a consulta por dados inválidos{extra}."
        elif status == 401:
            msg = ("O iFood não aceitou o token (401). Confira IFOOD_BI_CLIENT_ID/SECRET e o "
                   "modelo do app: token sem chain_scope indica app criado por merchant.")
        elif status == 403:
            msg = ("Sem permissão (403). Confira: (1) o app tem o módulo Analytics; (2) o "
                   "IFOOD_BI_MODELO bate com o modelo do app; (3) o ID em IFOOD_BI_ALVO_ID foi "
                   "autorizado (no modelo rede, o responsável da rede precisa aprovar). "
                   "Não vou tentar de novo sozinho." + extra)
        elif status == 404:
            msg = f"Endereço não encontrado (404). Confira o ID em IFOOD_BI_ALVO_ID e o modelo.{extra}"
        elif status == 429:
            msg = "Muitas consultas ao mesmo tempo (429). Esperei e tentei de novo, sem sucesso; aguarde 1 minuto."
        else:
            msg = f"O iFood está com problema (erro {status}). Tentei de novo e não deu; tente mais tarde."
        return AnalyticsError(msg, status, detalhes)

    # ------------------------------------------------------------ extracao
    def extrair(self, ini, fim):
        """Roda todas as consultas do painel para o periodo [ini, fim] (inclusive).

        Devolve {"linhas": [...], "erros": [...]}. Se uma consulta SECUNDARIA falhar,
        as outras seguem e o erro vai para a lista (a tela avisa). Se a consulta
        principal (status) falhar, levanta o erro: sem ela nao ha o que mostrar.
        """
        linhas, erros = [], []
        metricas = {m: ["sum"] for m in METRICAS}

        def agrupado(dim, campos, filtro=None):
            agg = {"groupBy": {"fields": campos}, "metrics": metricas}
            dados, _ = self.consulta_todas(corpo_periodo(ini, fim, agg, filtro))
            return [_linha_grupo(dim, r) for r in dados]

        # 1) principal: totais por status (concluido, cancelado...)
        linhas += agrupado("status", ["orderStatus"])
        # 2) quebras por dimensao, sempre abertas por status
        dims = ["salesChannel", "paymentMethod", "deliveredBy", "dayOfWeek", "orderCreationHour"]
        if self.modelo == "rede":
            dims.append("merchantId")
        for dim in dims:
            try:
                linhas += agrupado(dim, [dim, "orderStatus"])
            except AnalyticsError as e:
                erros.append(f"{dim}: {e}")
        # 3) cancelamentos: motivo e origem (so pedidos cancelados)
        for dim in ("cancellationReason", "cancellationOrigin"):
            try:
                linhas += agrupado(dim, [dim], {"orderStatus": ["CANCELLED"]})
            except AnalyticsError as e:
                erros.append(f"{dim}: {e}")
        # 4) serie diaria de GMV dos concluidos (um intervalo por dia)
        try:
            linhas += self._serie_diaria(ini, fim, metricas)
        except AnalyticsError as e:
            erros.append(f"serie diaria: {e}")
        return {"linhas": linhas, "erros": erros}

    def _serie_diaria(self, ini, fim, metricas):
        d0, d1 = _para_date(ini), _para_date(fim)
        intervalos, d = [], d0
        while d <= d1:
            prox = d + timedelta(days=1)
            intervalos.append({"from": d.isoformat(), "to": prox.isoformat()})
            d = prox
        agg = {"dateIntervals": intervalos, "metrics": {"gmv": ["sum"]}}
        corpo = corpo_periodo(ini, fim, agg, {"orderStatus": ["CONCLUDED"]}, size=max(len(intervalos), 1))
        dados, _ = self.consulta_todas(corpo)
        saida = []
        for r in dados:
            chave = (r.get("dateRangeKey") or {})
            de = ((chave.get("value") or {}).get("from") or "")[:10]
            if not de:
                continue
            saida.append(_nova_linha("dia", de, "CONCLUDED", chave.get("count") or 0, r))
        return saida


# ---------------------------------------------------------------- tradutores
def _para_date(v):
    return v if isinstance(v, date) and not isinstance(v, datetime) else datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def _num(row, nome):
    return float((row.get(nome) or {}).get("sum") or 0)


def _nova_linha(dim, chave, status, pedidos, row):
    return {
        "dimensao": dim,
        "chave": "" if chave is None else str(chave),
        "status": status or "",
        "pedidos": int(pedidos or 0),
        "gmv": _num(row, "gmv"),
        "gmv_sem_entrega": _num(row, "gmvWithoutDelivery"),
        "taxa_entrega": _num(row, "netDeliveryFee"),
        "subsidio_parceiro": _num(row, "benefitByTypePartnerTotalValue"),
        "taxas_loja": _num(row, "feesGrossValue"),
        "reembolso": _num(row, "refundedTotalValue"),
    }


def _linha_grupo(dim, row):
    gk = row.get("groupByKey") or {}
    val = gk.get("value") or {}
    status = val.get("orderStatus")
    if dim == "status":
        chave = status
    elif dim in ("cancellationReason", "cancellationOrigin"):
        chave, status = val.get(dim), "CANCELLED"
    else:
        chave = val.get(dim)
    return _nova_linha(dim, chave if chave is not None else "(não informado)", status, gk.get("count"), row)
