# Exercício 9 — Dashboard de Indicadores Econômicos Brasileiros

Dashboard em Python que consulta séries históricas do Banco Central (SGS) e do IPEADATA (OData), permite comparar até cinco indicadores com índice base 100 e calcula a correlação entre duas séries.

## Executar

Na raiz do repositório:

```bash
python -m pip install -r requirements.txt
streamlit run 9_indicadores_economicos/indicadores_economicos.py
```

O Streamlit abrirá o dashboard no navegador.

## Utilização

1. Escolha o período e a frequência da análise na barra lateral.
2. Selecione séries do catálogo inicial do Banco Central ou informe códigos SGS adicionais (separados por vírgula).
3. Para o IPEADATA, pesquise pelo nome (por exemplo: `PIB`, `inflação` ou `desemprego`) e selecione as séries desejadas.
4. Clique em **Consultar indicadores**.
5. Nas séries regionais ou sociais do IPEADATA, escolha o território apresentado.
6. Use a aba **Comparação normalizada** para visualizar a evolução em base 100.
7. Use a aba **Correlação** para escolher dois indicadores e ver o coeficiente de Pearson, a dispersão e a linha de tendência.
8. Na aba **Dados**, consulte ou baixe os dados tratados em CSV.

## Observações

- O limite é de cinco indicadores somando BCB e IPEADATA.
- A frequência mensal, trimestral ou anual é definida pelo usuário. As observações são agregadas pela média dentro de cada período; o dashboard não interpola datas ausentes.
- Séries com frequências muito diferentes podem ter poucas datas em comum para a correlação.
- O índice base 100 usa a primeira observação válida de cada série. Séries cujo valor-base seja zero não podem ser normalizadas por essa fórmula.
- A correlação não demonstra causalidade. A correlação entre níveis também pode ser afetada por tendências comuns.
- As funções de consulta usam cache temporário para evitar repetir chamadas às APIs durante a navegação.

## Fontes e documentação

- Banco Central — SGS: <https://www.bcb.gov.br/>
- API SGS: <https://api.bcb.gov.br/dados/serie/bcdata.sgs>
- IPEADATA — API OData: <https://www.ipeadata.gov.br/api/>
- Metadados OData: <https://www.ipeadata.gov.br/api/odata4/$metadata>
