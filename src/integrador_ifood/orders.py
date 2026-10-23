"""orders.py - os "botoes do balcao".

Cada metodo e um botao: ver o pedido, confirmar, iniciar preparo, avisar
que esta pronto, despachar (so entrega propria) e cancelar.

Uso rapido pelo terminal (para testes e prints da homologacao):
    python orders.py get <orderId>
    python orders.py confirm <orderId>
    python orders.py prepare <orderId>
    python orders.py ready <orderId>
    python orders.py dispatch <orderId>
    python orders.py reasons <orderId>
    python orders.py cancel <orderId> <codigo>
"""
import sys

from client import IFoodClient, checar

ORDERS = "/order/v1.0/orders"


class OrderNotFound(Exception):
    """404: o pedido ainda nao esta disponivel (ou passou de 7 dias)."""


class IFoodOrders:
    def __init__(self, client=None):
        self.client = client or IFoodClient()

    def get(self, order_id):
        resp = self.client.request("GET", f"{ORDERS}/{order_id}")
        if resp.status_code == 404:
            raise OrderNotFound(order_id)
        checar(resp)
        return resp.json()

    def _acao(self, order_id, acao):
        resp = self.client.request("POST", f"{ORDERS}/{order_id}/{acao}")
        checar(resp)  # 202 = pedido aceito, resultado vem em evento
        return resp

    def confirm(self, order_id):
        return self._acao(order_id, "confirm")

    def start_preparation(self, order_id):
        return self._acao(order_id, "startPreparation")

    def ready_to_pickup(self, order_id):
        return self._acao(order_id, "readyToPickup")

    def dispatch(self, order_id):
        # So para entrega propria (deliveredBy = MERCHANT), apos readyToPickup
        return self._acao(order_id, "dispatch")

    def cancellation_reasons(self, order_id):
        resp = self.client.request("GET", f"{ORDERS}/{order_id}/cancellationReasons")
        checar(resp)
        return resp.json()

    def request_cancellation(self, order_id, reason_code):
        """reason_code vem da lista de cancellation_reasons (ex.: "503")."""
        resp = self.client.request(
            "POST",
            f"{ORDERS}/{order_id}/requestCancellation",
            json={"cancellationCode": str(reason_code)},
        )
        checar(resp)  # 202 so significa "pedido aceito"; o
        return resp  # cancelamento real chega no evento CANCELLED


if __name__ == "__main__":
    import json

    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd, oid = sys.argv[1], sys.argv[2]
    api = IFoodOrders()
    if cmd == "get":
        print(json.dumps(api.get(oid), indent=2, ensure_ascii=False))
    elif cmd == "reasons":
        print(json.dumps(api.cancellation_reasons(oid), indent=2, ensure_ascii=False))
    elif cmd == "cancel":
        print(api.request_cancellation(oid, sys.argv[3]).status_code)
    elif cmd in ("confirm", "prepare", "ready", "dispatch"):
        metodo = {
            "confirm": api.confirm,
            "prepare": api.start_preparation,
            "ready": api.ready_to_pickup,
            "dispatch": api.dispatch,
        }[cmd]
        print(metodo(oid).status_code)
    else:
        print(__doc__)
