#!/usr/bin/env python3
"""
Monitor de Indicadores Agropecuários e Climáticos

Cruza dados agrícolas do IBGE (Produção Agrícola Municipal, via API de agregados/SIDRA)
com chuva acumulada. Para a chuva usamos o arquivo histórico do Open-Meteo, porque o
INPE/CPTEC não tem uma API JSON de série histórica de precipitação. O CPTEC entra no
comando "previsao" (previsão dos próximos dias, via BrasilAPI).

Comandos:
  comparar  -> estado + cultura: chuva acumulada x produção/produtividade ao longo dos anos
  mapa      -> mapa de calor por estado com a produtividade média da cultura,
               com a chuva média sobreposta em círculos
  previsao  -> previsão do CPTEC/INPE para a capital de um estado

Exemplos:
  python agro_clima.py comparar --uf MT --cultura soja --anos 2014-2023
  python agro_clima.py mapa --cultura milho --anos 2019-2023
  python agro_clima.py previsao --uf SP
"""
import argparse
import os
import time
import unicodedata
from datetime import date, timedelta

import folium
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

IBGE = "https://servicodados.ibge.gov.br/api/v3"
OPEN_METEO = "https://archive-api.open-meteo.com/v1/archive"
BRASIL_API = "https://brasilapi.com.br/api/cptec/v1"
PASTA_SAIDA = "saida"

# A PAM mudou de tabela em 2018: a 1612 vai até 2017 e a 5457 cobre de 2018 em diante.
# Cada uma tem a sua classificação de produtos (81 e 782).
TABELAS_PAM = [
    {"tabela": "1612", "classif": "81", "ini": 1974, "fim": 2017},
    {"tabela": "5457", "classif": "782", "ini": 2018, "fim": 9999},
]
# ids das variáveis da PAM (iguais nas duas tabelas)
VARIAVEIS = {
    "area_plantada": ("109", "Área plantada (ha)"),
    "area_colhida": ("216", "Área colhida (ha)"),
    "producao": ("214", "Quantidade produzida (t)"),
    "produtividade": ("112", "Rendimento médio (kg/ha)"),
    "valor": ("215", "Valor da produção (mil R$)"),
}

# sigla: (código IBGE, nome, capital, latitude, longitude)
# A chuva é medida no ponto da capital. É uma aproximação: em estados grandes a região
# produtora pode ficar longe dela, por isso o comando "comparar" aceita --lat e --lon.
UFS = {
    "AC": (12, "Acre", "Rio Branco", -9.97, -67.81),
    "AL": (27, "Alagoas", "Maceió", -9.67, -35.74),
    "AP": (16, "Amapá", "Macapá", 0.03, -51.07),
    "AM": (13, "Amazonas", "Manaus", -3.12, -60.02),
    "BA": (29, "Bahia", "Salvador", -12.97, -38.50),
    "CE": (23, "Ceará", "Fortaleza", -3.73, -38.52),
    "DF": (53, "Distrito Federal", "Brasília", -15.78, -47.93),
    "ES": (32, "Espírito Santo", "Vitória", -20.32, -40.34),
    "GO": (52, "Goiás", "Goiânia", -16.68, -49.25),
    "MA": (21, "Maranhão", "São Luís", -2.53, -44.30),
    "MT": (51, "Mato Grosso", "Cuiabá", -15.60, -56.10),
    "MS": (50, "Mato Grosso do Sul", "Campo Grande", -20.47, -54.62),
    "MG": (31, "Minas Gerais", "Belo Horizonte", -19.92, -43.94),
    "PA": (15, "Pará", "Belém", -1.46, -48.50),
    "PB": (25, "Paraíba", "João Pessoa", -7.12, -34.86),
    "PR": (41, "Paraná", "Curitiba", -25.43, -49.27),
    "PE": (26, "Pernambuco", "Recife", -8.05, -34.88),
    "PI": (22, "Piauí", "Teresina", -5.09, -42.80),
    "RJ": (33, "Rio de Janeiro", "Rio de Janeiro", -22.91, -43.17),
    "RN": (24, "Rio Grande do Norte", "Natal", -5.79, -35.21),
    "RS": (43, "Rio Grande do Sul", "Porto Alegre", -30.03, -51.23),
    "RO": (11, "Rondônia", "Porto Velho", -8.76, -63.90),
    "RR": (14, "Roraima", "Boa Vista", 2.82, -60.67),
    "SC": (42, "Santa Catarina", "Florianópolis", -27.59, -48.55),
    "SP": (35, "São Paulo", "São Paulo", -23.55, -46.63),
    "SE": (28, "Sergipe", "Aracaju", -10.91, -37.07),
    "TO": (17, "Tocantins", "Palmas", -10.18, -48.33),
}
COD_PARA_SIGLA = {v[0]: k for k, v in UFS.items()}

ERROS_API = (RuntimeError, requests.RequestException)

sessao = requests.Session()
sessao.headers.update({"Accept": "application/json", "User-Agent": "estudo-agro-clima/1.0"})


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def pegar_json(url, params=None, tentativas=4):
    """GET com novas tentativas (429 e 5xx são comuns nessas APIs públicas)."""
    ultimo_erro = None
    for t in range(tentativas):
        try:
            resp = sessao.get(url, params=params, timeout=40)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (t + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as erro:
            ultimo_erro = erro
            time.sleep(2 ** t)
    raise RuntimeError(f"Falha ao acessar {url}: {ultimo_erro or 'muitas tentativas'}")


def normalizar(texto):
    """Tira acento e põe em minúsculas, para comparar nomes ('Soja' == 'soja')."""
    sem_acento = unicodedata.normalize("NFD", texto)
    return "".join(c for c in sem_acento if unicodedata.category(c) != "Mn").lower().strip()


def ler_intervalo_anos(texto):
    """Aceita '2015-2023' ou só '2020'."""
    partes = texto.split("-")
    ini = int(partes[0])
    fim = int(partes[1]) if len(partes) > 1 else ini
    if fim < ini:
        ini, fim = fim, ini
    return list(range(ini, fim + 1))


def anos_padrao():
    # a PAM costuma sair com mais de um ano de atraso, então paramos em (ano atual - 2)
    fim = date.today().year - 2
    return f"{fim - 9}-{fim}"


def pegar_uf(sigla):
    sigla = sigla.upper()
    if sigla not in UFS:
        raise SystemExit(f"UF inválida: {sigla}. Use a sigla, ex.: MT, SP, RS.")
    return sigla, UFS[sigla]


def salvar_figura(nome, mostrar):
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    caminho = os.path.join(PASTA_SAIDA, nome)
    plt.tight_layout()
    plt.savefig(caminho, dpi=150)
    print(f"Gráfico salvo em {caminho}")
    if mostrar:
        plt.show()
    plt.close()


# --------------------------------------------------------------------------
# IBGE: produção agrícola
# --------------------------------------------------------------------------

_cache_categorias = {}


def categorias_da_tabela(tabela, classif):
    """Lista de produtos (id, nome) da classificação de uma tabela, vinda dos metadados."""
    chave = (tabela, classif)
    if chave not in _cache_categorias:
        meta = pegar_json(f"{IBGE}/agregados/{tabela}/metadados")
        for c in meta.get("classificacoes", []):
            if str(c["id"]) == classif:
                _cache_categorias[chave] = c["categorias"]
                break
        else:
            raise RuntimeError(f"Classificação {classif} não encontrada na tabela {tabela}.")
    return _cache_categorias[chave]


def achar_cultura(termo, tabela, classif):
    """
    Em vez de fixar o código de cada cultura (que muda entre tabelas), procuramos o nome
    nos metadados. 'soja' casa com 'Soja (em grão)'.
    """
    alvo = normalizar(termo)
    cats = [c for c in categorias_da_tabela(tabela, classif) if str(c["id"]) != "0"]
    achados = [c for c in cats if alvo in normalizar(c["nome"])]
    if not achados:
        exemplos = ", ".join(c["nome"] for c in cats[:12])
        raise RuntimeError(f"Cultura '{termo}' não encontrada na tabela {tabela}. Exemplos: {exemplos}...")
    # prefere nomes que começam pelo termo e os mais curtos ('Milho (em grão)' antes de 'Milho forrageiro...')
    achados.sort(key=lambda c: (not normalizar(c["nome"]).startswith(alvo), len(c["nome"])))
    return achados[0]


def buscar_pam(anos, cultura, variavel, siglas=None):
    """
    Devolve um DataFrame (uf, ano, valor) com a variável da PAM para os estados pedidos.
    O JSON do SIDRA é bem aninhado:
      [ {variavel..., resultados: [ {classificacoes, series: [ {localidade, serie: {ano: valor}} ]} ]} ]
    """
    id_var = VARIAVEIS[variavel][0]
    if siglas:
        localidades = "N3[" + ",".join(str(UFS[s][0]) for s in siglas) + "]"
    else:
        localidades = "N3[all]"

    linhas = []
    for t in TABELAS_PAM:
        anos_tabela = [a for a in anos if t["ini"] <= a <= t["fim"]]
        if not anos_tabela:
            continue
        cat = achar_cultura(cultura, t["tabela"], t["classif"])
        print(f"  IBGE tabela {t['tabela']}: {cat['nome']} ({anos_tabela[0]}-{anos_tabela[-1]})")
        url = (f"{IBGE}/agregados/{t['tabela']}/periodos/{'|'.join(map(str, anos_tabela))}"
               f"/variaveis/{id_var}?localidades={localidades}&classificacao={t['classif']}[{cat['id']}]")
        dados = pegar_json(url)
        for item in dados:
            for resultado in item.get("resultados", []):
                for serie in resultado.get("series", []):
                    cod_uf = int(serie["localidade"]["id"])
                    for ano, valor in serie["serie"].items():
                        linhas.append({"uf": COD_PARA_SIGLA.get(cod_uf, str(cod_uf)),
                                       "ano": int(ano), "valor": valor})

    df = pd.DataFrame(linhas, columns=["uf", "ano", "valor"])
    # o IBGE manda "-", "..." ou "X" quando não há dado/sigilo; viram NaN
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    return df


# --------------------------------------------------------------------------
# Chuva (Open-Meteo)
# --------------------------------------------------------------------------

def chuva_por_ano(lat, lon, anos, mes_ini=1, mes_fim=12):
    """
    Precipitação acumulada (mm) por ano, somando só os meses da janela escolhida.
    Anos sem dias suficientes na janela (ex.: ano corrente) viram NaN em vez de
    parecerem um ano seco.
    """
    # Janela que cruza o ano (ex.: outubro a março): a safra pertence ao ano em que termina,
    # então a safra 2023 soma out/2022 a mar/2023 e precisamos começar a buscar um ano antes.
    cruza_ano = mes_ini > mes_fim
    inicio = date(anos[0] - 1 if cruza_ano else anos[0], 1, 1)
    # o arquivo histórico tem alguns dias de atraso
    fim = min(date(anos[-1], 12, 31), date.today() - timedelta(days=6))
    if fim < inicio:
        return pd.Series(dtype=float)

    dados = pegar_json(OPEN_METEO, {
        "latitude": lat, "longitude": lon,
        "start_date": inicio.isoformat(), "end_date": fim.isoformat(),
        "daily": "precipitation_sum", "timezone": "America/Sao_Paulo",
    })
    df = pd.DataFrame({"data": pd.to_datetime(dados["daily"]["time"]),
                       "mm": dados["daily"]["precipitation_sum"]})
    mes = df["data"].dt.month
    if cruza_ano:
        df = df[(mes >= mes_ini) | (mes <= mes_fim)].copy()
        df["ano"] = df["data"].dt.year + (df["data"].dt.month >= mes_ini).astype(int)
    else:
        df = df[(mes >= mes_ini) & (mes <= mes_fim)].copy()
        df["ano"] = df["data"].dt.year

    soma = df.groupby("ano")["mm"].sum(min_count=1)
    dias = df.groupby("ano")["mm"].count()

    resultado = {}
    for ano in soma.index:
        # quantos dias a janela completa tem naquela safra
        primeiro = pd.Timestamp(ano - 1 if cruza_ano else ano, mes_ini, 1)
        ultimo = pd.Timestamp(ano, mes_fim, 1) + pd.offsets.MonthEnd(0)
        dias_da_janela = (ultimo - primeiro).days + 1
        # janelas incompletas (ano corrente, ou ponta do período) viram NaN
        resultado[ano] = soma[ano] if dias[ano] >= dias_da_janela else np.nan
    return pd.Series(resultado).reindex(anos)


# --------------------------------------------------------------------------
# Comando 1: comparar chuva x produção de um estado
# --------------------------------------------------------------------------

def cmd_comparar(args):
    sigla, (cod, nome_uf, capital, lat, lon) = pegar_uf(args.uf)
    if args.lat is not None and args.lon is not None:
        lat, lon, capital = args.lat, args.lon, "ponto informado"
    anos = ler_intervalo_anos(args.anos)
    rotulo_var = VARIAVEIS[args.variavel][1]

    print(f"Buscando {args.cultura} em {nome_uf} ({anos[0]}-{anos[-1]})...")
    prod = buscar_pam(anos, args.cultura, args.variavel, [sigla])
    if prod.empty:
        raise SystemExit("O IBGE não devolveu dados para essa combinação de estado/cultura/anos.")
    prod = prod.set_index("ano")["valor"]

    print(f"Buscando chuva em {capital} ({lat}, {lon})...")
    chuva = chuva_por_ano(lat, lon, anos, args.mes_ini, args.mes_fim)

    tabela = pd.DataFrame({"chuva_mm": chuva, "producao": prod}).dropna()
    tabela.index.name = "ano"
    if tabela.empty:
        raise SystemExit("Sem anos em comum entre chuva e produção.")
    print("\n", tabela.round(1).to_string())
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    tabela.to_csv(os.path.join(PASTA_SAIDA, f"comparar_{sigla}_{args.cultura}.csv"))

    if len(tabela) >= 3:
        r = np.corrcoef(tabela["chuva_mm"], tabela["producao"])[0, 1]
        print(f"\nCorrelação de Pearson (chuva x {rotulo_var}): {r:.2f} com {len(tabela)} anos")
        print("Poucos anos e uma única estação de chuva: use como indício, não como prova.")

    fig, ax1 = plt.subplots(figsize=(10, 5.5))
    ax1.bar(tabela.index, tabela["chuva_mm"], color="#8ecae6", label="Chuva acumulada (mm)")
    ax1.set_ylabel("Chuva acumulada (mm)", color="#219ebc")
    ax2 = ax1.twinx()  # segundo eixo y para a produção, que tem outra escala
    ax2.plot(tabela.index, tabela["producao"], color="#d62828", marker="o", linewidth=2, label=rotulo_var)
    ax2.set_ylabel(rotulo_var, color="#d62828")
    ax1.set_xticks(tabela.index)
    janela = "ano todo" if (args.mes_ini, args.mes_fim) == (1, 12) else f"meses {args.mes_ini} a {args.mes_fim}"
    ax1.set_title(f"{args.cultura.capitalize()} em {nome_uf}: chuva ({capital}, {janela}) x {rotulo_var}",
                  fontsize=12, weight="bold")
    for ax in (ax1, ax2):
        ax.spines["top"].set_visible(False)
    salvar_figura(f"comparar_{sigla}_{args.cultura}.png", args.mostrar)


# --------------------------------------------------------------------------
# Comando 2: mapa de calor por estado
# --------------------------------------------------------------------------

def cmd_mapa(args):
    anos = ler_intervalo_anos(args.anos)
    print(f"Buscando produtividade de {args.cultura} em todos os estados...")
    prod = buscar_pam(anos, args.cultura, "produtividade")
    media = prod.groupby("uf")["valor"].mean().dropna()
    if media.empty:
        raise SystemExit("Nenhum estado com dados para essa cultura/período.")

    # chuva só para os estados que realmente produzem a cultura (poupa chamadas)
    chuva_media = {}
    for i, sigla in enumerate(media.index, 1):
        if sigla not in UFS:
            continue
        print(f"  chuva {sigla} ({i}/{len(media)})")
        _, _, _, lat, lon = UFS[sigla]
        try:
            serie = chuva_por_ano(lat, lon, anos)
            chuva_media[sigla] = serie.mean()
        except ERROS_API as erro:
            print(f"  aviso: sem chuva para {sigla} ({erro})")
        time.sleep(0.6)  # o Open-Meteo gratuito limita chamadas por minuto

    # malha dos estados em GeoJSON (cada feature tem properties.codarea = código IBGE da UF)
    geo = pegar_json(f"{IBGE}/malhas/paises/BR",
                     {"intrarregiao": "UF", "formato": "application/vnd.geo+json", "qualidade": "minima"})

    df = pd.DataFrame({"codigo": [str(UFS[s][0]) for s in media.index if s in UFS],
                       "produtividade": [media[s] for s in media.index if s in UFS]})

    mapa = folium.Map(location=[-14.2, -51.9], zoom_start=4, tiles="CartoDB positron")
    folium.Choropleth(
        geo_data=geo, data=df, columns=["codigo", "produtividade"],
        key_on="feature.properties.codarea", fill_color="YlGn", fill_opacity=0.8,
        line_opacity=0.4, nan_fill_color="#e0e0e0",
        legend_name=f"Produtividade média de {args.cultura} (kg/ha), {anos[0]}-{anos[-1]}",
    ).add_to(mapa)

    # círculos azuis = chuva média anual; o raio cresce com a precipitação
    camada = folium.FeatureGroup(name="Chuva média anual (mm)")
    maior = max(chuva_media.values()) if chuva_media else 1
    for sigla, mm in chuva_media.items():
        if np.isnan(mm):
            continue
        _, nome_uf, capital, lat, lon = UFS[sigla]
        folium.CircleMarker(
            [lat, lon], radius=4 + 14 * mm / maior, color="#1d4e89", weight=1,
            fill=True, fill_color="#3a86ff", fill_opacity=0.55,
            tooltip=(f"{nome_uf}: {media[sigla]:.0f} kg/ha | chuva {mm:.0f} mm/ano "
                     f"(medida em {capital})"),
        ).add_to(camada)
    camada.add_to(mapa)
    folium.LayerControl(collapsed=False).add_to(mapa)

    saida = os.path.join(PASTA_SAIDA, f"mapa_{args.cultura}.html")
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    mapa.save(saida)

    resumo = pd.DataFrame({"produtividade_kg_ha": media, "chuva_mm": pd.Series(chuva_media)}).dropna(how="all")
    print("\n", resumo.sort_values("produtividade_kg_ha", ascending=False).round(0).to_string())
    resumo.to_csv(os.path.join(PASTA_SAIDA, f"mapa_{args.cultura}.csv"))
    print(f"\nMapa salvo em {saida} (abra no navegador)")


# --------------------------------------------------------------------------
# Comando 3: previsão do CPTEC/INPE (via BrasilAPI)
# --------------------------------------------------------------------------

def cmd_previsao(args):
    sigla, (_, nome_uf, capital, _, _) = pegar_uf(args.uf)
    cidades = pegar_json(f"{BRASIL_API}/cidade/{capital}")
    # o nome pode casar com várias cidades de estados diferentes
    cidade = next((c for c in cidades if c.get("estado") == sigla), cidades[0] if cidades else None)
    if not cidade:
        raise SystemExit(f"CPTEC não encontrou a cidade {capital}.")
    dados = pegar_json(f"{BRASIL_API}/clima/previsao/{cidade['id']}/{args.dias}")
    print(f"Previsão CPTEC/INPE para {dados.get('cidade', capital)}-{dados.get('estado', sigla)} "
          f"(atualizado em {dados.get('atualizado_em', '?')})")
    for dia in dados.get("clima", []):
        print(f"  {dia.get('data')}: {dia.get('condicao_desc', dia.get('condicao'))} | "
              f"mín {dia.get('min')}°C, máx {dia.get('max')}°C | UV {dia.get('indice_uv')}")


# --------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------

def montar_parser():
    parser = argparse.ArgumentParser(description="Monitor agroclimático (IBGE + INPE/CPTEC + Open-Meteo)")
    parser.add_argument("--mostrar", action="store_true", help="abre o gráfico numa janela além de salvar")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("comparar", help="chuva acumulada x produção de uma cultura num estado")
    p.add_argument("--uf", required=True, help="sigla do estado, ex.: MT")
    p.add_argument("--cultura", required=True, help='ex.: "soja", "milho", "arroz"')
    p.add_argument("--anos", default=anos_padrao(), help="intervalo, ex.: 2014-2023")
    p.add_argument("--variavel", choices=["producao", "produtividade", "area_colhida", "area_plantada", "valor"],
                   default="producao")
    p.add_argument("--mes-ini", type=int, default=1,
                   help="primeiro mês somado na chuva (1-12); se for maior que --mes-fim, a janela cruza o ano")
    p.add_argument("--mes-fim", type=int, default=12, help="último mês somado na chuva (1-12)")
    p.add_argument("--lat", type=float, help="latitude do ponto de chuva (padrão: capital)")
    p.add_argument("--lon", type=float, help="longitude do ponto de chuva (padrão: capital)")
    p.set_defaults(func=cmd_comparar)

    p = sub.add_parser("mapa", help="mapa de calor da produtividade por estado com chuva sobreposta")
    p.add_argument("--cultura", required=True)
    p.add_argument("--anos", default=anos_padrao())
    p.set_defaults(func=cmd_mapa)

    p = sub.add_parser("previsao", help="previsão do CPTEC para a capital de um estado")
    p.add_argument("--uf", required=True)
    p.add_argument("--dias", type=int, default=4, help="de 1 a 6")
    p.set_defaults(func=cmd_previsao)

    return parser


if __name__ == "__main__":
    argumentos = montar_parser().parse_args()
    argumentos.func(argumentos)