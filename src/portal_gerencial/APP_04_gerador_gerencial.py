"""
APP_04 — Gerador do PORTAL GERENCIAL (todas as lojas, uso interno do Bruno)

O QUE FAZ
---------
Lê os resultados do APP_03_extracao_rede.sql (blocos G1, G2, G3 — e, quando
existirem, G4 e G5), aplica as regras da rede abaixo e grava UM arquivo HTML
com todos os dados dentro. A página calcula MoM, YoY, ranking e comparação
na hora, conforme os filtros — como uma tabela dinâmica do Excel.

ESTE ARQUIVO TEM O FATURAMENTO DE TODAS AS LOJAS.
Não envie para franqueados. O portal de cada loja é outro gerador (APP_02).

COMO RODAR
----------
    python APP_04_gerador_gerencial.py

Nesta pasta ficam so este script, APP_modelo_gerencial.html e logo_200.png.
Os CSVs do APP_03 e o portal_gerencial.html gerado ficam FORA da pasta do
codigo, porque trazem o faturamento de todas as lojas: o caminho vem da
variavel de ambiente FERMENTO_DADOS, ou do padrao ~/dados-fermento.
Para usar outra pasta de CSVs: --pasta "/caminho/da/pasta"; para gravar o
portal em outro lugar: --saida "/caminho/portal.html".

A pasta pode ter os resultados salvos como .csv (separador ; ou tab) ou .txt
copiado do SSMS. O nome do arquivo não importa: a 1ª coluna de cada linha
(G1, G2...) diz a que bloco ela pertence.
"""

import argparse
import base64
import csv
import io
import json
import os
import unicodedata
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# PASTA DOS DADOS
# ---------------------------------------------------------------------------
# Dado real nunca mora na pasta do codigo: ler_blocos() le TODO .csv/.txt da
# pasta que recebe, entao codigo e dado juntos sao uma copia acidental a
# caminho do repositorio.
DADOS = os.environ.get("FERMENTO_DADOS") or os.path.join(
    os.path.expanduser("~"), "dados-fermento"
)

# ---------------------------------------------------------------------------
# REGRAS DA REDE — edite aqui, não no meio do código
# ---------------------------------------------------------------------------

MES_FECHADO = "2026-08"          # último mês fechado nos 3 canais

EXCLUIR = {56: "loja de teste, sem venda"}

OUTRA_MARCA = {97: "outra marca da franqueadora"}

# Lojas que fecharam mas o cadastro ainda está ativo=1
FECHADAS_MANUAL = {21: "loja encerrada; o cadastro ainda está ativo=1"}

# Mesmo ponto físico com dois id_loja (troca de dono). O id de origem é somado
# no id de destino; "troca" marca o 1º mês da nova gestão.
MESCLAR = {54: 34, 108: 27}
TROCA_GESTAO = {34: "2025-02"}

# Nome curto para exibição, quando o cadastro traz um nome ruim de ler.
# Sem apelido, o nome sai da normalização em nome_curto(). Formato: {id_loja: "Apelido"}
APELIDOS = {}

# Mesmo ponto com conta nova no iFood (sem troca de dono): só soma, sem marcar troca
CONTA_TROCADA = {27: "a conta do iFood mudou e o id de origem foi somado aqui"}

# Hipóteses que dependem de você confirmar (aparecem na aba Alertas)
A_CONFIRMAR = []

# O cadastro do PDV traz o nome em MAIÚSCULAS e sem acento. Estes dois ajustes
# devolvem o acento (ACENTOS) e impedem que uma sigla seja "capitalizada"
# (SIGLAS, para não virar "Cd" ou "Sp").
# LISTA DE EXEMPLO, só para demonstrar o mecanismo: a lista real, com as
# cidades, bairros e siglas do cadastro, fica FORA deste repositório.
ACENTOS = {
    "SAO": "São", "JOSE": "José", "ANTONIO": "Antônio", "CONCEICAO": "Conceição",
    "ESTACAO": "Estação", "PRACA": "Praça", "AVIACAO": "Aviação",
}
SIGLAS = {"CD", "SP", "RJ"}
MINUSCULAS = {"DE", "DA", "DO", "DOS", "DAS", "E"}

# O PDV repete o nome da marca no cadastro da loja ("MARCA - BAIRRO"). Ele é
# retirado para o nome curto não ficar igual em todas as lojas. Em MAIÚSCULAS,
# como vem do cadastro; deixe "" para não retirar nada.
PREFIXO_MARCA = "REDE DEMO"

CANAIS = ["DEGUST", "IFOOD", "99FOOD"]

# Ruptura — mesma regra do portal da loja (APP_02): vendeu em 2 dos 3 meses
# anteriores, com média de pelo menos 5 unidades nesses meses, e zerou no mês.
RUPTURA_MESES_MINIMOS = 2
RUPTURA_MEDIA_MINIMA = 5
RUPTURA_IFOOD_ATIVA = False      # desligada até corrigir o de-para de tamanhos do iFood

# Produto: tamanhos somados por família; abreviações do Degust por extenso
TAMANHOS = {"PQNO", "PQNA", "PQN", "PEQUENO", "PEQUENA", "P", "G", "M", "GRANDE", "MEDIO", "MEDIA"}
TAMANHO_EXTENSO = {"PQNO": "pequeno", "PQNA": "pequena", "PQN": "pequeno", "P": "P", "G": "G", "M": "M"}
ABREV = {"EXAG": "exagero", "BRIG": "brigadeiro", "BR": "branco", "CAST": "castanha",
         "PARABENS": "parabéns", "GLACUCAR": "glaçúcar"}


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------

def ler_texto(caminho):
    """O SSMS salva em UTF-8 ou em ANSI (Windows-1252), conforme a configuração.
    Tenta UTF-8 primeiro; se não der, lê como ANSI, sem perder os acentos."""
    with open(caminho, "rb") as f:
        bruto = f.read()
    try:
        return bruto.decode("utf-8-sig")
    except UnicodeDecodeError:
        return bruto.decode("cp1252")


def ler_blocos(pasta):
    """Junta todas as linhas de todos os arquivos, agrupadas pelo 1º campo."""
    blocos, lidos = {}, []
    for nome in sorted(os.listdir(pasta)):
        if not nome.lower().endswith((".csv", ".txt")):
            continue
        texto = ler_texto(os.path.join(pasta, nome))
        amostra = texto[:2000]
        conta = {s: amostra.count(s) for s in ("\t", ";", ",")}
        sep = max(conta, key=conta.get)                 # tab, ponto e vírgula ou vírgula
        achados = set()
        for linha in csv.reader(io.StringIO(texto), delimiter=sep):
            if not linha or not linha[0].strip():
                continue
            chave = linha[0].strip().lstrip("\ufeff")
            if chave.startswith("G") and chave[1:].isdigit():
                blocos.setdefault(chave, []).append([c.strip() for c in linha[1:]])
                achados.add(chave)
        lidos.append("%s -> %s" % (nome, ", ".join(sorted(achados)) or "nenhum bloco G"))
    print("Pasta lida: %s" % os.path.abspath(pasta))
    for l in lidos or ["(nenhum .csv ou .txt nesta pasta)"]:
        print("   " + l)
    return blocos


def num(s):
    if s is None or s == "" or s.upper() == "NULL":
        return 0.0
    s = s.replace("R$", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def mes_menos(ym, n):
    a, m = int(ym[:4]), int(ym[5:7])
    t = a * 12 + (m - 1) - n
    return "%04d-%02d" % (t // 12, t % 12 + 1)


MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def rotulo(ym):
    return MESES_PT[int(ym[5:7]) - 1] + "/" + ym[:4]


def nome_curto(id_loja, original):
    if id_loja in APELIDOS:
        return APELIDOS[id_loja]
    n = original.upper()
    if PREFIXO_MARCA:
        n = n.replace(PREFIXO_MARCA, "")
    n = n.replace(" - ", " ").replace("-", " ")
    palavras = [p for p in n.split() if p != "PDV"]
    saida = []
    for i, p in enumerate(palavras):
        if p in SIGLAS:
            saida.append(p)
        elif p in ACENTOS:
            saida.append(ACENTOS[p])
        elif p in MINUSCULAS and i > 0:
            saida.append(p.lower())
        elif p == "JD":
            saida.append("Jd.")
        else:
            saida.append(p.capitalize())
    return " ".join(saida) or original


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------

def montar(blocos):
    if "G1" not in blocos or "G2" not in blocos:
        raise SystemExit("PARE: faltam os blocos G1 e/ou G2 na pasta acima. Confira se os CSVs do APP_03 estão nela "
                         "(ou indique a pasta com --pasta \"C:\\caminho\\da\\pasta\").")

    meses = [mes_menos(MES_FECHADO, n) for n in range(24, -1, -1)]   # 25 meses
    pos = {m: i for i, m in enumerate(meses)}

    def destino(i):
        return MESCLAR.get(i, i)

    # Lojas (G1): id, nome, ativo, primeiro/último por canal
    lojas = {}
    for r in blocos["G1"]:
        if not r[0].isdigit():
            continue                                   # cabeçalho
        i = int(r[0])
        if i in EXCLUIR or i in MESCLAR:
            continue
        ativo = r[2] in ("1", "True", "true")
        if i in OUTRA_MARCA:
            status = "outra_marca"
        elif not ativo or i in FECHADAS_MANUAL:
            status = "encerrada"
        else:
            status = "ativa"
        lojas[i] = {"id": i, "nome": nome_curto(i, r[1]), "cadastro": r[1], "status": status,
                    "ids": [i] + [o for o, d in MESCLAR.items() if d == i],
                    "troca": TROCA_GESTAO.get(i)}

    # Faturamento (G2): v[id][canal][mês]
    v = {i: [[0.0] * len(meses) for _ in CANAIS] for i in lojas}
    descartadas = 0
    for r in blocos["G2"]:
        if not r[0].isdigit():
            continue
        i = destino(int(r[0]))
        if i not in lojas or r[1] not in pos or r[2] not in CANAIS:
            descartadas += 1
            continue
        v[i][CANAIS.index(r[2])][pos[r[1]]] += num(r[3])

    # 99Food por pedido (G3)
    f99 = {}
    for r in blocos.get("G3", []):
        if not r[0].isdigit():
            continue
        i = destino(int(r[0]))
        if i not in lojas:
            continue
        d = f99.setdefault(i, {}).setdefault(r[1], [0, 0, 0.0, 0.0])
        d[0] += int(num(r[2])); d[1] += int(num(r[3])); d[2] += num(r[4]); d[3] += num(r[5])

    # Lojas sem nenhuma venda na janela de 25 meses ficam de fora
    for i in [i for i in lojas if not any(any(c) for c in v[i])]:
        del lojas[i]; del v[i]

    # Primeiro mês com venda (para marcar loja nova)
    for i, l in lojas.items():
        idx = [k for k in range(len(meses)) if any(v[i][c][k] for c in range(3))]
        l["inicio"] = meses[idx[0]] if idx else None
        l["fim"] = meses[idx[-1]] if idx else None

    # Notas fixas da rede (as que dependem do mês são calculadas na página)
    notas = []
    for i, troca in TROCA_GESTAO.items():
        if i in lojas:
            notas.append({"tipo": "info", "lojas": [i], "titulo": lojas[i]["nome"] + ": troca de gestão em " + rotulo(troca),
                          "texto": "Os ids " + ", ".join(str(x) for x in lojas[i]["ids"]) + " foram somados num só ponto. "
                                   "Antes de " + rotulo(troca) + " é a gestão anterior; a partir dele, a atual.",
                          "acao": "Na comparação, a linha dessa loja mostra a troca marcada no gráfico."})
    for i, txt in CONTA_TROCADA.items():
        if i in lojas:
            notas.append({"tipo": "info", "lojas": [i], "titulo": lojas[i]["nome"] + ": histórico contínuo",
                          "texto": "No " + lojas[i]["nome"] + ", " + txt + ". Assim a comparação com o ano anterior usa a loja inteira.",
                          "acao": ""})
    for i, txt in FECHADAS_MANUAL.items():
        if i in lojas:
            notas.append({"tipo": "dado", "lojas": [i], "titulo": lojas[i]["nome"] + ": fechada, mas ativa no cadastro",
                          "texto": txt + ". Aqui ela já é tratada como encerrada.",
                          "acao": "Pedir ao Téo: UPDATE da dim_loja para ativo = 0."})
    for i, txt in OUTRA_MARCA.items():
        if i in lojas:
            notas.append({"tipo": "info", "lojas": [i], "titulo": txt,
                          "texto": "Fica fora do ranking, mas entra no total da rede, para bater com o relatório do Fred.",
                          "acao": ""})
    for n in A_CONFIRMAR:
        if any(i in lojas for i in n["lojas"]):
            notas.append(dict(n, tipo="confirmar"))

    pmeses, produtos = montar_produtos(blocos, meses)
    rupturas = montar_rupturas(blocos, lojas, destino, MES_FECHADO) if "G5" in blocos else None

    return {
        "pmeses": pmeses,
        "produtos": produtos,
        "rupturas": rupturas,
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "mes_fechado": MES_FECHADO,
        "meses": meses,
        "lojas": sorted(lojas.values(), key=lambda l: l["nome"]),
        "v": {str(i): [[round(x, 2) for x in c] for c in v[i]] for i in v},
        "f99": {str(i): f99[i] for i in f99 if i in lojas},
        "notas": notas,
        "produtos_disponiveis": "G4" in blocos and "G5" in blocos,
        "descartadas": descartadas,
    }


# ---------------------------------------------------------------------------
# Produtos (G4 rede) e rupturas (G5 por loja)
# ---------------------------------------------------------------------------

def sem_acento(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


def familia(nome):
    return " ".join(p for p in sem_acento(nome.upper()).split() if p not in TAMANHOS)


def nome_legivel(nome, com_tamanho=False):
    palavras = []
    for p in nome.split():
        k = sem_acento(p.upper())
        if k in TAMANHOS:
            if com_tamanho:
                palavras.append(TAMANHO_EXTENSO.get(k, p.lower()))
            continue
        palavras.append(ABREV.get(k, p.lower()))
    t = " ".join(palavras)
    return t[:1].upper() + t[1:]


def sazonal(cat):
    return "ARRAI" in sem_acento((cat or "").upper())


def montar_produtos(blocos, meses):
    """G4 -> famílias com unidades e R$ por canal nos 13 meses."""
    pmeses = meses[-13:]
    pos = {m: i for i, m in enumerate(pmeses)}
    fam = {}
    for r in blocos.get("G4", []):
        if len(r) < 7 or r[0] not in pos or r[1] not in CANAIS:
            continue
        f = familia(r[3])
        d = fam.get(f)
        if d is None:
            d = fam[f] = {"nome": nome_legivel(r[3]), "cat": r[4].strip().capitalize(),
                          "u": [[0] * 13 for _ in CANAIS], "v": [[0.0] * 13 for _ in CANAIS]}
        k, i = CANAIS.index(r[1]), pos[r[0]]
        d["u"][k][i] += int(num(r[5]))
        d["v"][k][i] += num(r[6])
    lista = []
    for f, d in fam.items():
        d["v"] = [[round(x) for x in c] for c in d["v"]]
        if sum(map(sum, d["v"])) > 0:
            lista.append(d)
    lista.sort(key=lambda d: -sum(map(sum, d["v"])))
    return pmeses, lista


def montar_rupturas(blocos, lojas, destino, mes_ref):
    """G5 -> uma linha por loja x produto que zerou no mês (regra acima)."""
    ant = [mes_menos(mes_ref, 3), mes_menos(mes_ref, 2), mes_menos(mes_ref, 1)]
    und = defaultdict(lambda: defaultdict(float))      # (loja, canal, chave) -> {mês: unidades}
    fam_ref = defaultdict(float)                       # (loja, canal, família) -> unidades no mês
    info = {}
    for r in blocos.get("G5", []):
        if len(r) < 7 or not r[0].isdigit():
            continue
        loja = destino(int(r[0]))
        if loja not in lojas or lojas[loja]["status"] != "ativa":
            continue
        m, canal, pid, nome, cat, u = r[1], r[2], r[3], r[4], r[5], num(r[6])
        f = familia(nome)
        chave = pid if canal == "DEGUST" else f       # balcão: código exato; apps: família
        und[(loja, canal, chave)][m] += u
        info.setdefault((canal, chave), (nome_legivel(nome, com_tamanho=(canal == "DEGUST")), cat, f))
        if m == mes_ref:
            fam_ref[(loja, canal, f)] += u
    saida = []
    for (loja, canal, chave), ms in und.items():
        if canal == "IFOOD" and not RUPTURA_IFOOD_ATIVA:
            continue
        if ms.get(mes_ref, 0) > 0:
            continue
        vend = [ms.get(m, 0) for m in ant]
        com = [x for x in vend if x > 0]
        if len(com) < RUPTURA_MESES_MINIMOS or sum(com) / len(com) < RUPTURA_MEDIA_MINIMA:
            continue
        nome, cat, f = info[(canal, chave)]
        outros = [o for o in CANAIS if o != canal and fam_ref.get((loja, o, f), 0) > 0]
        saida.append({"loja": loja, "canal": canal, "chave": str(chave), "nome": nome,
                      "cat": cat.strip().capitalize(), "ant": [int(x) for x in vend],
                      "sazonal": sazonal(cat), "outros": outros})
    return {"mes": mes_ref, "anteriores": ant, "lista": saida, "ifood_ativa": RUPTURA_IFOOD_ATIVA}


def gravar(dados, modelo, logo, saida):
    with open(modelo, encoding="utf-8") as f:
        html = f.read()
    if "rupturasDoMes" not in html:
        print("Atenção: o %s é de uma versão antiga (sem a aba Produtos). Troque pelo mais recente." % os.path.basename(modelo))
    if os.path.exists(logo):
        with open(logo, "rb") as f:
            logo_b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    else:
        print("Atenção: não achei o %s; o portal sai sem o logo." % os.path.basename(logo))
        logo_b64 = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
    js = json.dumps(dados, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("__DADOS_JSON__", js).replace("__LOGO__", logo_b64)
    with open(saida, "w", encoding="utf-8") as f:
        f.write(html)
    print("Gravado: %s (%d KB) — %d lojas" % (os.path.abspath(saida), os.path.getsize(saida) // 1024, len(dados["lojas"])))
    if dados["descartadas"]:
        print("Atenção: %d linhas do G2 ficaram fora da janela ou de loja excluída." % dados["descartadas"])


if __name__ == "__main__":
    # O script e o modelo ficam na pasta do codigo; os CSVs e o portal gerado
    # ficam em DADOS, fora dela (ver "PASTA DOS DADOS" acima).
    aqui = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--pasta", default=DADOS, help="pasta dos CSVs do APP_03 (padrão: %s)" % DADOS)
    ap.add_argument("--modelo", default=os.path.join(aqui, "APP_modelo_gerencial.html"))
    ap.add_argument("--logo", default=os.path.join(aqui, "logo_200.png"))
    ap.add_argument("--saida", default=os.path.join(DADOS, "portal_gerencial.html"))
    a = ap.parse_args()
    gravar(montar(ler_blocos(a.pasta)), a.modelo, a.logo, a.saida)
