"""autorizar_bi.py - liga o app APP_BI (distribuido) a sua loja/rede.

Voce faz isso UMA vez (e de novo so se a autorizacao for revogada).

  Passo 1:  python autorizar_bi.py
            -> mostra um CODIGO curto e o endereco do Portal do Parceiro.
  Passo 2:  no Portal do Parceiro (logado como dono/administrador), digite o codigo e
            autorize. O Portal mostra um CODIGO DE AUTORIZACAO.
  Passo 3:  python autorizar_bi.py --codigo <codigo-de-autorizacao>
            -> troca pelo token e guarda em bi_tokens.json.

  python autorizar_bi.py --status   -> diz se ja esta autorizado (sem mostrar segredos)

O codigo do passo 1 vale 10 minutos. Se passar disso, rode o passo 1 de novo.
"""
import argparse
import sys
import time

import requests

import auth_distribuido as ad


def _erro_http(e):
    r = e.response
    return f"erro {getattr(r, 'status_code', '?')}: {str(getattr(r, 'text', ''))[:300]}"


def main(argv=None, auth=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--codigo", help="codigo de autorizacao mostrado pelo Portal do Parceiro")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)

    if args.status:
        d = ad._ler(ad.ARQ_TOKENS) or {}
        if d.get("refreshToken"):
            restante = int(float(d.get("expiresAt") or 0) - time.time())
            print("AUTORIZADO: ha um refresh token guardado.")
            print("O token atual", "vence em %d min." % (restante // 60) if restante > 0
                  else "ja venceu; sera renovado sozinho na proxima consulta.")
        else:
            print("NAO autorizado ainda. Rode: python autorizar_bi.py")
        return 0

    try:
        a = auth or ad.IFoodAuthDistribuido(prefixo="IFOOD_BI")
    except RuntimeError as e:
        print("ERRO:", e)
        return 1

    if not args.codigo:  # ---- passo 1
        try:
            d = a.pedir_codigo()
        except requests.HTTPError as e:
            print("O iFood recusou o pedido de codigo (", _erro_http(e), ")")
            print("Confira IFOOD_BI_CLIENT_ID no .env e se o app APP_BI e do tipo distribuido.")
            return 1
        ad._gravar(ad.ARQ_PENDENTE, {"verifier": d["authorizationCodeVerifier"],
                                     "userCode": d["userCode"], "criadoEm": time.time()})
        print("\n=== PASSO 1 PRONTO ===")
        print("Seu codigo:", d["userCode"], f"(vale {int(d.get('expiresIn', 600)) // 60} minutos)")
        print("Abra este endereco, logado no Portal do Parceiro como dono/administrador:")
        print("   ", d.get("verificationUrlComplete") or d.get("verificationUrl"))
        print("Autorize o aplicativo. O Portal vai mostrar um CODIGO DE AUTORIZACAO.")
        print("Depois rode:  python autorizar_bi.py --codigo SEU_CODIGO_DE_AUTORIZACAO")
        return 0

    pendente = ad._ler(ad.ARQ_PENDENTE)  # ---- passo 3
    if not pendente:
        print("Nao achei o passo 1. Rode primeiro:  python autorizar_bi.py")
        return 1
    try:
        a.trocar_codigo(args.codigo.strip(), pendente["verifier"])
    except requests.HTTPError as e:
        print("O iFood recusou o codigo de autorizacao (", _erro_http(e), ")")
        print("Confira se copiou o codigo inteiro. Se o tempo passou, refaca o passo 1.")
        return 1
    try:
        import os
        os.remove(ad.ARQ_PENDENTE)
    except OSError:
        pass
    print("\nAUTORIZADO! O token foi guardado em bi_tokens.json e sera renovado sozinho.")
    print("Agora rode:  python homologar_analytics.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
