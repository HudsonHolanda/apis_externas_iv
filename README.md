# Integrações com dados mais complexos

Três mini-projetos de análise e visualização de dados públicos.

| Pasta | Projeto | APIs | Autenticação |
|---|---|---|---|
| `7_agro_clima/` | Monitor agropecuário e climático | IBGE (SIDRA), Open-Meteo, CPTEC/INPE via BrasilAPI | Nenhuma |
| `8_compras_publicas/` | Painel de compras públicas federais | Dados Abertos do Compras.gov.br, malhas do IBGE | Nenhuma |

## Instalação

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 7. Monitor Agropecuário e Climático

Script: `7_agro_clima/agro_clima.py`. Gráficos e tabelas saem na pasta `saida/`.

```bash
cd 7_agro_clima

# Desafio 1: chuva acumulada x produção de uma cultura num estado
python agro_clima.py comparar --uf MT --cultura soja --anos 2014-2023

# Desafio 2: mapa de calor da produtividade por estado, com chuva sobreposta
python agro_clima.py mapa --cultura milho --anos 2019-2023

# Extra: previsão dos próximos dias (CPTEC/INPE) para a capital
python agro_clima.py previsao --uf SP
```

**Como funciona**

- **Produção:** vem da Produção Agrícola Municipal (PAM) do IBGE, pela API de agregados. A PAM mudou de tabela em 2018 (a 1612 vai até 2017 e a 5457 de 2018 em diante), e o script escolhe a tabela certa para cada ano e junta o resultado.
- **Cultura por nome:** o código do produto é procurado nos metadados da tabela, então `--cultura soja` funciona sem decorar códigos. Se o nome não existir, o erro lista exemplos.
- **JSON aninhado:** a resposta do SIDRA é `variável → resultados → séries → localidade → {ano: valor}`. Valores como `"-"` e `"..."` (sem dado ou sigilo) viram `NaN`.
- **Chuva:** vem do arquivo histórico do Open-Meteo (precipitação diária), somada por ano. Dá para restringir a janela com `--mes-ini` e `--mes-fim`. Se o início for maior que o fim (`--mes-ini 10 --mes-fim 3`), a janela cruza o ano e cada safra soma de outubro do ano anterior a março do ano indicado. Anos sem a janela completa ficam de fora em vez de parecerem anos secos.
- **Mapa:** a camada colorida é a produtividade média (kg/ha) por estado, desenhada sobre o GeoJSON de estados do IBGE. Os círculos azuis são a chuva média anual, e o botão de camadas liga e desliga cada um.
- **Datas:** o intervalo padrão termina em (ano atual − 2), porque a PAM sai com atraso.

---

## 8. Painel de Compras Públicas Federais

Script: `8_compras_publicas/compras_publicas.py`.

```bash
cd 8_compras_publicas

# Desafio 1: mapa de calor do valor de contratos por UF
python compras_publicas.py mapa --inicio 2025-01-01 --fim 2025-12-31

# Desafio 2: top 10 fornecedores por valor, com filtro de ramo (CNAE)
python compras_publicas.py ranking --inicio 2025-01-01 --fim 2025-06-30 --top 10
python compras_publicas.py ranking --inicio 2025-01-01 --fim 2025-06-30 --cnae 6201501

# Extras
python compras_publicas.py ranking --por orgao --categoria servicos
python compras_publicas.py fornecedor --cnpj 00000000000191 --inicio 2024-01-01 --fim 2025-12-31
```

Mapas e gráficos HTML ficam em `saida/` (abra no navegador), com CSV ao lado.

**Como funciona**

- **Paginação:** a API atual usa `pagina` e `tamanhoPagina` (máximo 500) e devolve `totalPaginas` na resposta. A paginação por `offset` era da API antiga, que foi descontinuada. O script avisa quando o resultado ficou parcial por causa do limite `--max-paginas`.
- **UF dos contratos:** o contrato não tem campo de UF, só o código da unidade gestora. O script baixa o cadastro de UASGs (código → UF) uma vez e guarda em `cache/uasg_uf.json`; use `--atualizar-cache` para renovar. Contratos sem UF identificada são contados à parte e ficam fora do mapa.
- **Ramo de negócio:** o contrato também não tem CNAE. Com `--cnae`, o script busca os fornecedores ativos daquele CNAE e filtra os contratos pelo CNPJ. Com `--categoria`, filtra pelo texto da categoria do contrato (compras, serviços, obras...).
- **Hierarquia órgão → contrato → fornecedor:** o comando `fornecedor` mostra o cadastro de um CNPJ e os contratos agrupados por órgão.
- **Fornecedores por UF:** o cadastro traz só o município, sem UF. O script extrai a UF quando o texto termina em `CIDADE/UF`; se não der, mostra os 20 municípios com mais fornecedores.

**Cuidados**

- Os contratos do período podem passar de 100 mil registros. O limite padrão é 40 páginas (20 mil contratos). Aumente com calma, porque cada página é uma chamada.
- O valor usado é o `valorGlobal` de cada contrato. Se houver termos aditivos ou valores extremos cadastrados, os rankings podem ser distorcidos por poucos registros.
- O filtro por CNAE considera o CNAE **principal** do fornecedor.

## Estrutura

```
.
├── README.md
├── requirements.txt
├── 7_agro_clima/agro_clima.py
├── 8_compras_publicas/compras_publicas.py
```