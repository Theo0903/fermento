# integrador_bdm — integrador oficial da API do iFood

Integração com a **API de parceiros do iFood** para a franqueadora: recebe pedidos,
acompanha eventos, puxa KPIs de Analytics e faz a conciliação financeira.
Dono da frente: **João**. Presidente que assina as decisões: **Bruno**.

Python 3.15 em `c:\python315`, Windows, terminal `cmd`. Painel em Flask.

---

## Antes de mudar qualquer coisa

1. `trail ask "<o que você precisa entender>" --source` **antes** de abrir arquivo.
   Os aprendizados de sessões passadas vêm acima do código.
2. Leia `docs/ARQUITETURA.md` para saber qual módulo faz o quê.
3. `trail callers <símbolo> --depth 2` antes de renomear, apagar ou mudar assinatura.
4. `trail check` antes de commitar.

## Como rodar

```
python painel.py          # painel Flask: pedidos, /analytics e /financeiro (127.0.0.1:5000)
python fin_testar.py      # descobre rotas e testa as APIs do Financial
python fin_extrator.py --recentes          # liga/desliga, sempre 35 dias
python fin_extrator.py --ini 2026-01-01 --fim 2026-03-31
python homologar_analytics.py
```

**O painel Flask não recarrega sozinho.** Depois de mudar um `.py`: `Ctrl+C` e
`python painel.py` de novo. Sintoma clássico de esquecer isso: o painel responde
404 de um endpoint que você já corrigiu.

## Regras que não se negociam

### Segredos
- **Nunca** escrever credencial em código, em valor padrão de `os.environ.get`,
  em `env.example`, em nome de arquivo ou em log.
- Credencial vive só no `.env`, que é ignorado pelo Git. `env.example` tem as
  variáveis com **valor vazio**.
- Segredo que apareceu em chat, log ou arquivo de exemplo está **exposto**:
  a correção é **regenerar a chave no Portal**, não apagar o arquivo.
- Ao mostrar um valor sensível em tela ou log, mascare: só os 4 primeiros
  caracteres.

### Dados que não podem ir para repositório público
`merchantId` e códigos de loja das plataformas, CNPJ, razão social, faturamento
real, caminhos `C:\Users\...`. Use caminho relativo ou `os.path.expanduser("~")`.

### Banco
- Este integrador grava em **SQLite local** (`pedidos.db`, `financeiro.db`).
- Tabela no SQL Server corporativo **não se cria aqui**: é decisão
  conjunta com o **Téo**. A ponte combinada é gravar em *staging*
  (`stg_ifood_financeiro`, `stg_ifood_analytics`), desenhada por ele.
- `TRUNCATE`/`DELETE`/`DROP` só com trava, transação e conferência independente.

### Números
Repasse = soma dos lançamentos com `hasTransferImpact` verdadeiro, tolerância
0,01% (`fin_calculos.conferir_repasse`). Lançamento idêntico em semanas
diferentes conta **uma vez** (`sem_repeticao`, em `fin_web.py`). Métrica de
homologação do Analytics sai **só da página 1**.

## Estilo

Código e mensagens em português. Erro de API vira **mensagem que explica o que
fazer**, não despejo de stack trace — veja `analytics.py:consulta` como modelo:
cada status conhecido tem uma frase dizendo o passo no Portal.

Comentário explica **por que**, não o que a linha faz.

## O que não fazer aqui

- Não chamar a API do iFood de dentro do portal do Igor: ele lê só do banco.
- Não mexer em `vw_vendas` nem em view usada por Fred/Igor.
- Não criar script com prefixo de outro dono: aqui o prefixo é `API_`
  (`DB_` é do Téo, `BI_` do Fred, `APP_` do Igor).
- `auth_distribuido.py` e `autorizar_bi.py` são **referência**, fluxo distribuído
  fora de uso. Não ligar em produção sem decisão do Bruno. Cuidado: `analytics.py`
  entra nesse fluxo sozinho se `IFOOD_BI_TIPO=distribuido` ou se houver
  `bi_tokens.json` com refresh token e a variável estiver vazia.

## Documentos

| Arquivo | Para quê |
|---|---|
| `docs/ARQUITETURA.md` | que módulo faz o quê e como se ligam |
| `docs/AMBIENTE.md` | `.env`, grupos de credencial, teste × produção |
| `docs/FINANCIAL.md` | as 4 APIs financeiras, limites e conciliação |
| `docs/ANALYTICS.md` | KPIs, homologação e os limites de chamada |
| `docs/ERROS.md` | sintoma → causa → solução |
