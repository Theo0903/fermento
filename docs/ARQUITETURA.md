# Arquitetura

## Camadas do banco

| Camada | Objetos | Regra |
|---|---|---|
| Staging | `stg_*` | Cópia crua da fonte; códigos sempre VARCHAR |
| Dimensões | `dim_loja`, `dim_produto`, `dim_tabela_preco`, `dim_canal`, `dim_turno` | Chave interna (IDENTITY); código da fonte é atributo |
| Pontes | `loja_canal`, `produto_canal`, `preco_produto` | Formato longo: canal novo = INSERT |
| De-para | `depara_item_*`, `categoria_delivery` | `origem = REGRA` (deduzido) × `MANUAL` (decidido por pessoa); regra nunca sobrescreve decisão |
| Fato | `fato_venda` | Uma venda nunca é descartada: `status_mapeamento` explica o que não casou |
| Views | `vw_vendas`, `vw_loja`, `vw_produto`, `vw_validacao_preco`, `vw_pendencias_mapeamento` | São a interface pública: portais e relatórios só leem daqui |

## Granularidade por canal

| Canal | Grão |
|---|---|
| PDV | dia × loja × produto |
| iFood (relatório) | mês × loja × item × turno |
| 99Food (API) | pedido |

Somados por mês, os três são comparáveis. Abaixo do mês, não.

## Arquitetura-alvo

- Azure SQL Database com conexão criptografada e usuário de aplicação com mínimo privilégio.
- Conectores agendados gravando em staging; o integrador do iFood deixa o SQLite e passa a gravar no warehouse.
- `id_rede` em dimensões e fato + segurança por linha (RLS) para atender várias redes.
- Portal web com login quando o banco estiver na nuvem.
