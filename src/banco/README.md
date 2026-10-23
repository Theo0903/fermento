# Scripts do banco (SQL Server)

A versão sanitizada dos scripts de criação, carga e views entra aqui depois da revisão de segurança: alguns scripts originais contêm identificadores reais de lojas nas plataformas e precisam ser reescritos com dados fictícios antes de publicar.

Convenção de nomes (proposta): `DB_` banco e cargas · `BI_` relatórios · `APP_` portais · `API_` integradores.

Todo script segue o mesmo cabeçalho: o que faz, o que **não** faz, se pode rodar mais de uma vez, trava antes de apagar, transação e conferências numeradas.
