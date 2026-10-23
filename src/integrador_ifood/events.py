"""events.py - a "caixa de correio" da padaria.

A cada ciclo:
1. pergunta ao iFood se ha eventos novos (GET /events:polling);
2. descarta eventos repetidos (deduplicacao por id) e separa os novos;
3. confirma o recebimento de TODOS (POST /events/acknowledgment), senao o
   iFood reenvia os mesmos eventos na proxima consulta.

Se a confirmacao falhar, os eventos novos AINDA sao entregues ao programa
(a memoria de ids evita tratar o mesmo evento duas vezes) e o erro vai
para o log.

Regra do iFood: consultar no maximo a cada 30 segundos.
"""
import json
import logging
import os

from client import IFoodError, checar

log = logging.getLogger("ifood")

POLLING_PATH = "/events/v1.0/events:polling"
ACK_PATH = "/events/v1.0/events/acknowledgment"
ACK_LOTE = 2000  # maximo de IDs por chamada de acknowledgment
PASTA = os.path.dirname(os.path.abspath(__file__))
MAX_VISTOS = 5000  # quantos ids lembrar (evita o arquivo crescer sem fim)

# Testado em 16/10/2026 na loja de teste: o iFood ACEITA o formato B (lista
# de objetos). O formato A (mostrado em alguns textos da documentacao) fica
# so como reserva, caso o iFood mude.
FORMATOS_ACK = (
    lambda ids: [{"id": i} for i in ids],  # B (lista de objetos) - o que funciona
    lambda ids: {"acknowledgedEventIds": ids},  # A (reserva)
)


class EventPoller:
    def __init__(self, client, arquivo_vistos=None):
        self.client = client
        self.arquivo_vistos = arquivo_vistos or os.path.join(PASTA, "eventos_vistos.json")
        self._vistos = self._carregar()
        self._formato_ack = None  # indice do formato que funcionou

    # -- memoria de eventos ja tratados (sobrevive a reinicio do programa)
    def _carregar(self):
        if os.path.exists(self.arquivo_vistos):
            try:
                with open(self.arquivo_vistos, encoding="utf-8") as f:
                    return list(json.load(f))
            except (ValueError, OSError):
                return []
        return []

    def _salvar(self):
        self._vistos = self._vistos[-MAX_VISTOS:]
        with open(self.arquivo_vistos, "w", encoding="utf-8") as f:
            json.dump(self._vistos, f)

    # -- chamadas ao iFood
    def buscar(self):
        """Devolve a lista bruta de eventos (vazia se nao ha nada: 204)."""
        resp = self.client.request("GET", POLLING_PATH)
        if resp.status_code == 204:
            return []
        checar(resp)
        return resp.json() or []

    def _enviar_ack(self, lote):
        ordem = (
            [self._formato_ack]
            if self._formato_ack is not None
            else range(len(FORMATOS_ACK))
        )
        ultimo_erro = None
        for i in ordem:
            resp = self.client.request("POST", ACK_PATH, json=FORMATOS_ACK[i](lote))
            if resp.status_code in (200, 202, 204):
                if self._formato_ack is None:
                    self._formato_ack = i
                    if i != 0:
                        log.warning("Acknowledgment: o formato principal foi recusado; usando o reserva")
                return
            ultimo_erro = IFoodError(f"{resp.status_code} {str(resp.text)[:300]}")
            if resp.status_code != 400:
                break  # so vale tentar o outro formato quando for 400
        raise ultimo_erro

    def confirmar_recebimento(self, ids):
        for i in range(0, len(ids), ACK_LOTE):
            self._enviar_ack(ids[i:i + ACK_LOTE])

    def novos_eventos(self):
        """Um ciclo completo: busca, separa os ineditos, confirma tudo."""
        eventos = self.buscar()
        if not eventos:
            return []

        vistos = set(self._vistos)
        novos = []
        for e in eventos:
            if e["id"] not in vistos:
                vistos.add(e["id"])
                self._vistos.append(e["id"])
                novos.append(e)
        self._salvar()

        try:
            self.confirmar_recebimento([e["id"] for e in eventos])  # todos
        except Exception as erro:
            log.error("Falha no acknowledgment (eventos serao reenviados): %s", erro)
        return novos
