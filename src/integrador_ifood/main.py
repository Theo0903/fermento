"""main.py - o "turno da padaria".

Fica em laco: a cada 30 s olha a caixa de correio (events.py), grava tudo
no caderno (store.py), registra no log e busca os detalhes dos pedidos novos.

Confirmar o pedido e uma decisao do negocio: por padrao o programa SO
AVISA. Para confirmar sozinho (ex.: nos testes da homologacao):
    python main.py --auto-confirm

Lembrete: o pedido precisa ser confirmado em ate 8 minutos, senao o iFood
cancela sozinho.
"""
import argparse
import logging
import os
import time

import store
from client import IFoodClient
from events import EventPoller
from orders import IFoodOrders, OrderNotFound

INTERVALO = 30  # segundos entre consultas (minimo permitido pelo iFood)
BACKOFF_MAX = 600  # 404 em pedido novo: tenta de novo em 2,4,8... ate 10 min

log = logging.getLogger("ifood")


def configurar_log():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.StreamHandler(), logging.FileHandler(os.path.join(store.PASTA, "ifood.log"), encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def resumo_pedido(p):
    """Campos principais (usa .get: nao quebra se algum nao vier)."""
    return {
        "displayId": p.get("displayId"),
        "tipo": p.get("orderType"),
        "entrega_por": (p.get("delivery") or {}).get("deliveredBy"),
        "total": (p.get("total") or {}).get("orderAmount"),
        "itens": len(p.get("items") or []),
    }


def tratar_pedido_novo(api, order_id, auto_confirm):
    p = api.get(order_id)  # pode levantar OrderNotFound
    store.salvar_pedido(p)
    log.info("Pedido %s detalhes: %s", order_id, resumo_pedido(p))
    estado = store.status(order_id)  # vem dos eventos ja gravados
    if estado not in (None, "PLACED"):
        log.info("Pedido %s ja esta %s (nada a fazer)", order_id, estado)
    elif auto_confirm:
        api.confirm(order_id)
        log.info("Pedido %s: confirm enviado", order_id)
    else:
        log.warning("Pedido %s aguardando confirmacao (prazo: 8 min)", order_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-confirm", action="store_true")
    args = ap.parse_args()

    configurar_log()
    client = IFoodClient()
    poller = EventPoller(client)
    api = IFoodOrders(client)
    pendentes = {}  # orderId -> (proxima_tentativa, espera_atual)

    log.info("Integrador iniciado (auto_confirm=%s)", args.auto_confirm)
    log.info("Banco de dados: %s", store.DB)
    while True:
        try:
            for ev in poller.novos_eventos():
                log.info("Evento %s pedido=%s id=%s",
                         ev.get("fullCode") or ev.get("code"), ev.get("orderId"), ev.get("id"))
                store.salvar_evento(ev)  # atualiza o estado do pedido
                oid = ev.get("orderId")
                if oid and oid not in pendentes and not store.tem_detalhes(oid):
                    pendentes[oid] = (0.0, 2)  # ainda nao temos os detalhes
        except Exception as e:  # nao derruba o turno por um erro de rede
            log.error("Erro no polling: %s", e)

        agora = time.time()
        for oid, (quando, espera) in list(pendentes.items()):
            if agora < quando:
                continue
            try:
                tratar_pedido_novo(api, oid, args.auto_confirm)
                del pendentes[oid]
            except OrderNotFound:
                espera = min(espera * 2, BACKOFF_MAX)
                pendentes[oid] = (agora + espera, espera)
                log.warning("Pedido %s ainda 404; nova tentativa em %ss", oid, espera)
            except Exception as e:
                log.error("Erro no pedido %s: %s", oid, e)
                del pendentes[oid]

        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
