"""Exercício 9 - Dashboard de Indicadores Econômicos Brasileiros.

Execute com:
    streamlit run 9_indicadores_economicos/indicadores_economicos.py

Fontes:
- Banco Central do Brasil (SGS): https://api.bcb.gov.br/
- IPEADATA (OData v4): https://www.ipeadata.gov.br/api/odata4/
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote, urljoin

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# -----------------------------------------------------------------------------
# Configurações e catálogo inicial
# -----------------------------------------------------------------------------

BCB_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs"
IPEA_URL = "https://www.ipeadata.gov.br/api/odata4/"
TIMEOUT = 30
MAX_PAGES = 100

# Códigos conhecidos do SGS. O usuário também pode pesquisar novas séries no
# catálogo oficial do SGS e incluir os códigos neste dicionário.
BCB_SERIES = {
    "IPCA — variação mensal (%)": "433",
    "IGP-M — variação mensal (%)": "189",
    "Dólar comercial — compra (R$/US$)": "1",
    "Selic — taxa meta (% a.a.)": "432",
    "Selic — acumulada no mês (% a.m.)": "1178",
}

FREQUENCIAS = {
    "Mensal": "M",
    "Trimestral": "Q",
    "Anual": "Y",
}


# -----------------------------------------------------------------------------
# Comunicação HTTP e funções de consulta às APIs
# -----------------------------------------------------------------------------

def criar_sessao() -> requests.Session:
    """Cria uma sessão HTTP com tentativas automáticas para erros temporários."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "dashboard-indicadores-economicos/1.0"})
    return session


@st.cache_data(ttl=1800, show_spinner=False)
def consultar_bcb(codigo: str, inicio: date, fim: date) -> pd.DataFrame:
    """Consulta uma série do SGS. Divide períodos longos em blocos de 9 anos."""
    session = criar_sessao()
    partes: list[pd.DataFrame] = []
    data_inicio = inicio

    while data_inicio <= fim:
        # O SGS limita consultas de algumas séries diárias a janelas de 10 anos.
        # Blocos de 9 anos deixam margem e também funcionam para séries mensais.
        try:
            limite = data_inicio.replace(year=data_inicio.year + 9)
        except ValueError:  # trata 29 de fevereiro
            limite = data_inicio.replace(month=2, day=28, year=data_inicio.year + 9)
        data_fim_bloco = min(fim, limite - timedelta(days=1))

        url = f"{BCB_URL}.{codigo}/dados"
        params = {
            "formato": "json",
            "dataInicial": data_inicio.strftime("%d/%m/%Y"),
            "dataFinal": data_fim_bloco.strftime("%d/%m/%Y"),
        }
        resposta = session.get(url, params=params, timeout=TIMEOUT)
        resposta.raise_for_status()
        registros = resposta.json()

        if registros:
            parte = pd.DataFrame(registros)
            if {"data", "valor"}.issubset(parte.columns):
                parte = parte.rename(columns={"data": "data", "valor": "valor"})
                parte["data"] = pd.to_datetime(parte["data"], format="%d/%m/%Y", errors="coerce")
                parte["valor"] = pd.to_numeric(parte["valor"].astype(str).str.replace(",", "."), errors="coerce")
                partes.append(parte[["data", "valor"]])

        data_inicio = data_fim_bloco + timedelta(days=1)

    if not partes:
        return pd.DataFrame(columns=["data", "valor"])

    dados = pd.concat(partes, ignore_index=True)
    dados = dados.dropna(subset=["data", "valor"]).drop_duplicates(subset="data")
    return dados.sort_values("data").reset_index(drop=True)


def extrair_registros_odata(payload: dict | list) -> tuple[list[dict], str | None]:
    """Extrai registros e link da próxima página em respostas OData."""
    if isinstance(payload, list):
        return payload, None
    registros = payload.get("value")
    if registros is None and isinstance(payload.get("d"), dict):
        registros = payload["d"].get("results", [])
        proximo = payload["d"].get("__next")
    else:
        proximo = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
    return registros or [], proximo


def consultar_paginas_odata(url: str, params: dict | None = None) -> list[dict]:
    """Lê todas as páginas retornadas pelo serviço OData."""
    session = criar_sessao()
    registros: list[dict] = []
    proxima_url: str | None = url
    parametros = params
    paginas = 0

    while proxima_url:
        paginas += 1
        if paginas > MAX_PAGES:
            raise RuntimeError(
                f"A consulta ultrapassou o limite de segurança de {MAX_PAGES} páginas. "
                "Tente reduzir o período ou refinar a busca."
            )
        resposta = session.get(proxima_url, params=parametros, timeout=TIMEOUT)
        resposta.raise_for_status()
        pagina, proxima = extrair_registros_odata(resposta.json())
        registros.extend(pagina)
        proxima_url = urljoin(proxima_url, proxima) if proxima else None
        parametros = None  # o link seguinte já contém os parâmetros OData

    return registros


@st.cache_data(ttl=3600, show_spinner=False)
def buscar_series_ipea(termo: str) -> pd.DataFrame:
    """Pesquisa séries numéricas no catálogo de metadados do IPEADATA."""
    termo = termo.strip().replace("'", "''")
    if len(termo) < 2:
        return pd.DataFrame()

    campos = "SERCODIGO,SERNOME,BASNOME,PERNOME,UNINOME,SERNUMERICA,SERSTATUS"
    filtro = f"contains(tolower(SERNOME),'{termo.lower()}')"
    params = {"$select": campos, "$filter": filtro, "$top": "100"}

    try:
        registros = consultar_paginas_odata(f"{IPEA_URL}Metadados", params=params)
    except requests.RequestException:
        # Alternativa para servidores OData que não aceitem tolower().
        filtro_simples = f"contains(SERNOME,'{termo}')"
        params["$filter"] = filtro_simples
        registros = consultar_paginas_odata(f"{IPEA_URL}Metadados", params=params)

    dados = pd.DataFrame(registros)
    if dados.empty:
        return dados

    # Mantém apenas séries numéricas, adequadas aos cálculos e gráficos.
    if "SERNUMERICA" in dados.columns:
        numerica = dados["SERNUMERICA"].astype(str).str.lower().isin(["1", "true"])
        dados = dados[numerica]
    return dados.drop_duplicates(subset="SERCODIGO").reset_index(drop=True)


@st.cache_data(ttl=1800, show_spinner=False)
def consultar_ipea(codigo: str, inicio: date, fim: date) -> pd.DataFrame:
    """Consulta valores numéricos de uma série do IPEADATA, inclusive regionais."""
    codigo_url = quote(codigo, safe="")
    url = f"{IPEA_URL}Metadados('{codigo_url}')/Valores"

    # OData v4 utiliza literais DateTimeOffset sem o prefixo datetime.
    filtro_data = (
        f"VALDATA ge {inicio.isoformat()}T00:00:00Z and "
        f"VALDATA le {fim.isoformat()}T23:59:59Z"
    )
    params = {"$filter": filtro_data, "$select": "SERCODIGO,VALDATA,VALVALOR,NIVNOME,TERCODIGO"}

    try:
        registros = consultar_paginas_odata(url, params=params)
    except requests.HTTPError:
        # Compatibilidade com instalações que não aceitem filtro de data.
        registros = consultar_paginas_odata(
            url,
            params={"$select": "SERCODIGO,VALDATA,VALVALOR,NIVNOME,TERCODIGO"},
        )

    dados = pd.DataFrame(registros)
    if dados.empty:
        return pd.DataFrame(columns=["data", "valor", "nivel", "territorio"])

    dados = dados.rename(columns={
        "VALDATA": "data",
        "VALVALOR": "valor",
        "NIVNOME": "nivel",
        "TERCODIGO": "territorio",
    })
    dados["data"] = pd.to_datetime(dados["data"], errors="coerce", utc=True).dt.tz_convert(None)
    dados["valor"] = pd.to_numeric(dados["valor"], errors="coerce")
    if "nivel" not in dados.columns:
        dados["nivel"] = ""
    if "territorio" not in dados.columns:
        dados["territorio"] = ""
    dados["nivel"] = dados["nivel"].fillna("").astype(str)
    dados["territorio"] = dados["territorio"].fillna("").astype(str)
    dados = dados.dropna(subset=["data", "valor"])
    dados = dados[(dados["data"].dt.date >= inicio) & (dados["data"].dt.date <= fim)]
    return dados[["data", "valor", "nivel", "territorio"]].sort_values("data").reset_index(drop=True)


# -----------------------------------------------------------------------------
# Tratamento, frequência, normalização e correlação
# -----------------------------------------------------------------------------

def tratar_dados(dados: pd.DataFrame, frequencia: str) -> pd.DataFrame:
    """Agrupa as observações na frequência escolhida, sem interpolar dados."""
    if dados.empty:
        return pd.DataFrame(columns=["data", "valor"])

    serie = dados[["data", "valor"]].copy()
    serie["data"] = pd.to_datetime(serie["data"], errors="coerce")
    serie["valor"] = pd.to_numeric(serie["valor"], errors="coerce")
    serie = serie.dropna(subset=["data", "valor"])
    if serie.empty:
        return pd.DataFrame(columns=["data", "valor"])

    serie["data"] = serie["data"].dt.to_period(frequencia).dt.to_timestamp()
    serie = serie.groupby("data", as_index=False)["valor"].mean()
    return serie.sort_values("data").reset_index(drop=True)


def normalizar_series(series: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Converte cada série para índice base 100 usando sua primeira observação válida."""
    normalizadas: list[pd.DataFrame] = []
    for nome, dados in series.items():
        serie = dados.dropna(subset=["valor"]).sort_values("data").copy()
        if serie.empty:
            continue
        base = float(serie.iloc[0]["valor"])
        if base == 0:
            continue
        serie["indice"] = (serie["valor"] / base) * 100
        normalizadas.append(serie[["data", "indice"]].rename(columns={"indice": nome}))

    if not normalizadas:
        return pd.DataFrame()

    resultado = normalizadas[0]
    for serie in normalizadas[1:]:
        resultado = resultado.merge(serie, on="data", how="outer")
    return resultado.sort_values("data").reset_index(drop=True)


def calcular_correlacao(
    primeira: pd.DataFrame,
    segunda: pd.DataFrame,
    nome_primeira: str,
    nome_segunda: str,
    usar_variacoes: bool = False,
) -> tuple[pd.DataFrame, float, np.ndarray]:
    """Alinha duas séries por data e calcula Pearson e a reta de tendência."""
    a = primeira.set_index("data")["valor"].rename(nome_primeira)
    b = segunda.set_index("data")["valor"].rename(nome_segunda)
    alinhados = pd.concat([a, b], axis=1, join="inner").dropna().sort_index()

    if usar_variacoes:
        alinhados = alinhados.pct_change(fill_method=None) * 100
        alinhados = alinhados.replace([np.inf, -np.inf], np.nan).dropna()

    if len(alinhados) < 3:
        raise ValueError("São necessárias pelo menos 3 datas em comum para calcular a correlação.")
    if alinhados[nome_primeira].nunique() < 2 or alinhados[nome_segunda].nunique() < 2:
        raise ValueError("Não é possível calcular correlação quando uma das séries não varia.")

    correlacao = float(alinhados[nome_primeira].corr(alinhados[nome_segunda], method="pearson"))
    x = alinhados[nome_primeira].to_numpy(dtype=float)
    y = alinhados[nome_segunda].to_numpy(dtype=float)
    reta = np.polyfit(x, y, 1)
    return alinhados.reset_index(), correlacao, reta


# -----------------------------------------------------------------------------
# Interface do dashboard
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="Indicadores Econômicos Brasileiros",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Dashboard de Indicadores Econômicos Brasileiros")
st.caption("Comparação de séries históricas do Banco Central (SGS) e do IPEADATA.")

with st.sidebar:
    st.header("1. Período e frequência")
    hoje = date.today()
    inicio_padrao = date(max(1900, hoje.year - 10), 1, 1)
    periodo = st.date_input(
        "Período da consulta",
        value=(inicio_padrao, hoje),
        min_value=date(1900, 1, 1),
        max_value=hoje,
    )
    frequencia_nome = st.selectbox("Frequência da análise", list(FREQUENCIAS.keys()), index=0)
    frequencia = FREQUENCIAS[frequencia_nome]

    st.header("2. Indicadores do Banco Central")
    bcb_selecionados = st.multiselect(
        "Séries SGS",
        options=list(BCB_SERIES.keys()),
        help="Selecione indicadores do catálogo inicial do exercício.",
    )
    codigos_bcb_texto = st.text_input(
        "Outros códigos SGS (opcional)",
        placeholder="Ex.: 12, 433",
        help="Consulte o catálogo do SGS e informe códigos numéricos separados por vírgula.",
    )
    codigos_bcb_adicionais = list(dict.fromkeys(
        codigo.strip() for codigo in codigos_bcb_texto.split(",")
        if codigo.strip() and codigo.strip().isdigit()
    ))
    codigos_bcb_invalidos = [
        codigo.strip() for codigo in codigos_bcb_texto.split(",")
        if codigo.strip() and not codigo.strip().isdigit()
    ]
    codigos_catalogo = set(BCB_SERIES.values())
    codigos_bcb_adicionais = [
        codigo for codigo in codigos_bcb_adicionais if codigo not in codigos_catalogo
    ]

    st.header("3. Indicadores do IPEADATA")
    termo_ipea = st.text_input("Pesquisar série por nome", placeholder="Ex.: PIB, inflação, desemprego")
    pesquisar_ipea = st.button("Pesquisar no IPEADATA", use_container_width=True)
    if pesquisar_ipea:
        if len(termo_ipea.strip()) < 2:
            st.warning("Digite pelo menos 2 caracteres para pesquisar.")
        else:
            try:
                with st.spinner("Consultando catálogo do IPEADATA..."):
                    st.session_state["catalogo_ipea"] = buscar_series_ipea(termo_ipea)
                st.session_state["termo_catalogo_ipea"] = termo_ipea
            except Exception as erro:
                st.error(f"Não foi possível consultar o catálogo do IPEADATA: {erro}")

    catalogo = st.session_state.get("catalogo_ipea", pd.DataFrame())
    opcoes_ipea: dict[str, str] = {}
    if not catalogo.empty:
        for _, linha in catalogo.iterrows():
            codigo = str(linha.get("SERCODIGO", ""))
            nome = str(linha.get("SERNOME", codigo))
            base = str(linha.get("BASNOME", ""))
            periodicidade = str(linha.get("PERNOME", ""))
            rotulo = f"{nome} [{codigo}] — {base} / {periodicidade}"
            opcoes_ipea[rotulo] = codigo

    ipea_selecionados_rotulos = st.multiselect(
        "Séries encontradas",
        options=list(opcoes_ipea.keys()),
        help="Pesquise primeiro e selecione as séries que deseja comparar.",
    )
    ipea_selecionados = [opcoes_ipea[item] for item in ipea_selecionados_rotulos]

    total_selecionado = len(bcb_selecionados) + len(codigos_bcb_adicionais) + len(ipea_selecionados)
    st.caption(f"Indicadores selecionados: {total_selecionado}/5")
    if codigos_bcb_invalidos:
        st.warning("Códigos SGS ignorados (use apenas números): " + ", ".join(codigos_bcb_invalidos))

    consultar = st.button(
        "Consultar indicadores",
        type="primary",
        use_container_width=True,
        disabled=(total_selecionado == 0 or total_selecionado > 5 or len(periodo) != 2),
    )

if len(periodo) != 2:
    st.info("Selecione uma data inicial e uma data final na barra lateral.")
elif periodo[0] > periodo[1]:
    st.error("A data inicial não pode ser posterior à data final.")
elif total_selecionado > 5:
    st.error("O exercício permite selecionar no máximo 5 indicadores no total.")

if consultar:
    if periodo[0] > periodo[1]:
        st.error("Período inválido: revise as datas.")
    else:
        carregadas: dict[str, dict] = {}
        erros: list[str] = []
        inicio, fim = periodo
        with st.spinner("Consultando as fontes selecionadas..."):
            selecoes_bcb = [(nome, BCB_SERIES[nome]) for nome in bcb_selecionados]
            selecoes_bcb.extend(
                (f"Série SGS {codigo}", codigo) for codigo in codigos_bcb_adicionais
            )
            for nome, codigo in selecoes_bcb:
                try:
                    df = consultar_bcb(codigo, inicio, fim)
                    if df.empty:
                        erros.append(f"BCB — {nome}: a API não retornou observações no período.")
                    else:
                        chave = f"BCB | {nome}"
                        carregadas[chave] = {"fonte": "BCB", "dados": df, "codigo": codigo}
                except Exception as erro:
                    erros.append(f"BCB — {nome}: {erro}")

            for rotulo in ipea_selecionados_rotulos:
                codigo = opcoes_ipea[rotulo]
                nome = rotulo.split(" [", 1)[0]
                try:
                    df = consultar_ipea(codigo, inicio, fim)
                    if df.empty:
                        erros.append(f"IPEADATA — {nome}: a API não retornou observações no período.")
                    else:
                        chave = f"IPEADATA | {nome} [{codigo}]"
                        carregadas[chave] = {"fonte": "IPEADATA", "dados": df, "codigo": codigo}
                except Exception as erro:
                    erros.append(f"IPEADATA — {nome}: {erro}")

        st.session_state["series_carregadas"] = carregadas
        st.session_state["erros_consulta"] = erros
        st.session_state["periodo_carregado"] = (inicio, fim)

# Resultado da consulta fica na sessão para permitir selecionar territórios sem
# repetir a chamada às APIs (as próprias funções também têm cache).
series_carregadas = st.session_state.get("series_carregadas", {})
erros_consulta = st.session_state.get("erros_consulta", [])

for erro in erros_consulta:
    st.warning(erro)

if series_carregadas:
    st.subheader("Indicadores carregados")
    st.caption("Para séries regionais ou sociais do IPEADATA, escolha o território correspondente.")

    series_processadas: dict[str, pd.DataFrame] = {}
    for chave, item in series_carregadas.items():
        dados = item["dados"].copy()
        if item["fonte"] == "IPEADATA" and {"nivel", "territorio"}.issubset(dados.columns):
            dados["nivel"] = dados["nivel"].fillna("").astype(str)
            dados["territorio"] = dados["territorio"].fillna("").astype(str)
            combinacoes = dados[["nivel", "territorio"]].drop_duplicates()
            labels: dict[str, tuple[str, str]] = {}
            for _, linha in combinacoes.iterrows():
                nivel = linha["nivel"].strip()
                territorio = linha["territorio"].strip()
                if not nivel and not territorio:
                    label = "Série macroeconômica / nacional"
                else:
                    label = f"{nivel or 'Território'} — código {territorio or 'não informado'}"
                labels[label] = (nivel, territorio)

            if labels:
                territorio_escolhido = st.selectbox(
                    f"Território — {chave}",
                    options=list(labels.keys()),
                    key=f"territorio_{item['codigo']}",
                )
                nivel, codigo_territorio = labels[territorio_escolhido]
                dados = dados[(dados["nivel"] == nivel) & (dados["territorio"] == codigo_territorio)]

        tratado = tratar_dados(dados, frequencia)
        if not tratado.empty:
            series_processadas[chave] = tratado

    if series_processadas:
        st.success(f"{len(series_processadas)} indicador(es) pronto(s) para análise.")
        dados_normalizados = normalizar_series(series_processadas)

        abas = st.tabs(["Comparação normalizada", "Correlação", "Dados"])

        with abas[0]:
            st.markdown("### Comparação de tendências — base 100")
            st.caption(
                "Cada série começa em 100 na sua primeira observação válida dentro do período. "
                "A agregação usa a média das observações em cada período; não há interpolação de lacunas. "
                "Para taxas (como inflação mensal ou juros), o índice compara a variação relativa da própria taxa, não o nível acumulado de preços."
            )
            if dados_normalizados.empty:
                st.warning("Não foi possível normalizar as séries. Verifique se os valores iniciais são diferentes de zero.")
            else:
                colunas_grafico = [coluna for coluna in dados_normalizados.columns if coluna != "data"]
                figura = px.line(
                    dados_normalizados,
                    x="data",
                    y=colunas_grafico,
                    markers=True,
                    labels={"data": "Data", "value": "Índice (base 100)", "variable": "Indicador"},
                    title="Evolução relativa dos indicadores",
                )
                figura.update_layout(legend_title_text="Indicador", hovermode="x unified")
                st.plotly_chart(figura, use_container_width=True)
                st.download_button(
                    "Baixar séries normalizadas (CSV)",
                    dados_normalizados.to_csv(index=False).encode("utf-8-sig"),
                    file_name="indicadores_normalizados.csv",
                    mime="text/csv",
                )

        with abas[1]:
            st.markdown("### Correlação entre dois indicadores")
            nomes_disponiveis = list(series_processadas.keys())
            if len(nomes_disponiveis) < 2:
                st.info("Carregue pelo menos dois indicadores para calcular a correlação.")
            else:
                col1, col2 = st.columns(2)
                with col1:
                    nome_a = st.selectbox("Primeiro indicador", nomes_disponiveis, index=0)
                with col2:
                    nome_b = st.selectbox("Segundo indicador", nomes_disponiveis, index=1)
                usar_variacoes = st.checkbox(
                    "Calcular correlação sobre as variações percentuais, em vez dos níveis",
                    value=False,
                )

                if nome_a == nome_b:
                    st.warning("Selecione dois indicadores diferentes.")
                else:
                    try:
                        alinhados, correlacao, reta = calcular_correlacao(
                            series_processadas[nome_a],
                            series_processadas[nome_b],
                            nome_a,
                            nome_b,
                            usar_variacoes=usar_variacoes,
                        )
                        rotulo_correlacao = "Correlação de Pearson"
                        m1, m2 = st.columns(2)
                        m1.metric(rotulo_correlacao, f"{correlacao:.3f}")
                        m2.metric("Observações em comum", f"{len(alinhados)}")

                        x = alinhados[nome_a].to_numpy(dtype=float)
                        y = alinhados[nome_b].to_numpy(dtype=float)
                        ordem = np.argsort(x)
                        tendencia = go.Figure()
                        tendencia.add_trace(go.Scatter(
                            x=x,
                            y=y,
                            mode="markers",
                            name="Observações",
                            text=alinhados["data"].dt.strftime("%d/%m/%Y"),
                            hovertemplate="Data: %{text}<br>X: %{x:.4f}<br>Y: %{y:.4f}<extra></extra>",
                        ))
                        tendencia.add_trace(go.Scatter(
                            x=x[ordem],
                            y=np.polyval(reta, x[ordem]),
                            mode="lines",
                            name="Linha de tendência",
                        ))
                        eixo = "Variação (%)" if usar_variacoes else "Valor agregado"
                        tendencia.update_layout(
                            title="Dispersão e linha de tendência",
                            xaxis_title=f"{nome_a} — {eixo}",
                            yaxis_title=f"{nome_b} — {eixo}",
                            hovermode="closest",
                        )
                        st.plotly_chart(tendencia, use_container_width=True)
                        st.caption(
                            "A correlação mede associação linear, não causalidade. "
                            "Correlações calculadas sobre níveis podem ser influenciadas por tendências comuns."
                        )
                    except ValueError as erro:
                        st.warning(str(erro))

        with abas[2]:
            st.markdown("### Dados tratados")
            tabela = None
            for nome, df in series_processadas.items():
                parte = df.rename(columns={"valor": nome}).set_index("data")
                tabela = parte if tabela is None else tabela.join(parte, how="outer")
            if tabela is not None:
                tabela = tabela.sort_index().rename_axis("data").reset_index()
                st.dataframe(tabela, use_container_width=True, hide_index=True)
                st.download_button(
                    "Baixar dados tratados (CSV)",
                    tabela.to_csv(index=False).encode("utf-8-sig"),
                    file_name="indicadores_tratados.csv",
                    mime="text/csv",
                )
    else:
        st.warning("Nenhuma série ficou disponível após o tratamento. Tente outro período ou frequência.")
else:
    st.info(
        "Escolha um período e até cinco indicadores na barra lateral. "
        "Para incluir séries do IPEADATA, pesquise pelo nome e selecione os resultados."
    )

st.divider()
st.caption(
    "Fontes: Banco Central do Brasil — SGS e IPEADATA — OData. "
    "Os resultados dependem da disponibilidade e da periodicidade de cada série."
)
