#!/usr/bin/env python3
"""
Painel de Compras Públicas Federais (API de Dados Abertos do Compras.gov.br)

Comandos:
  mapa          -> mapa de calor do Brasil com o valor total de contratos por UF
                   (a UF vem da unidade gestora que firmou o contrato)
  ranking       -> top fornecedores (ou órgãos) por valor contratado num período,
                   com filtro opcional por ramo de atividade (CNAE) ou categoria
  fornecedores  -> distribuição dos fornecedores cadastrados (SICAF) por UF/município e porte
  fornecedor    -> detalhe de um CNPJ: cadastro + contratos agrupados por órgão

  orgaos        -> lista os órgãos (código e nome), para usar no filtro --orgao

Exemplos:
  python compras_publicas.py orgaos --busca saude
  python compras_publicas.py mapa --inicio 2025-01-01 --fim 2025-12-31 --max-orgaos 0
  python compras_publicas.py ranking --inicio 2025-01-01 --fim 2025-06-30 --top 10
  python compras_publicas.py ranking --inicio 2025-01-01 --fim 2025-06-30 --orgao 36000 26000
  python compras_publicas.py ranking --inicio 2025-01-01 --fim 2025-06-30 --cnae 6201501
  python compras_publicas.py ranking --por orgao --categoria servicos
  python compras_publicas.py fornecedores --cnae 6201501
  python compras_publicas.py fornecedor --cnpj 00000000000191 --inicio 2024-01-01 --fim 2025-12-31

Observações:
  - Esta API pagina com "pagina" + "tamanhoPagina" (máximo 500). A versão antiga
    (compras.dados.gov.br), que usava "offset", foi descontinuada. A API não pede chave.
  - O endpoint de contratos EXIGE o código do órgão (sem ele a API responde 404). Por isso
    o script consulta os órgãos um a um. Por padrão usa uma amostra de 30 órgãos; use
    --max-orgaos 0 para varrer todos (demora) ou --orgao para escolher quais.
"""
import argparse
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import folium
import pandas as pd
import plotly.express as px
import requests

BASE = "https://dadosabertos.compras.gov.br"
IBGE_MALHAS = "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
PASTA_SAIDA = "saida"
PASTA_CACHE = "cache"

# O manual escreve alguns caminhos sem hífen, outros com. Tentamos as duas grafias e
# guardamos a que funcionar para as próximas chamadas.
PREFIXOS = {
    "contratos": ["modulo-contratos", "modulocontratos", "modulo-contrato"],
    "fornecedor": ["modulo-fornecedor", "modulofornecedor"],
    "uasg": ["modulo-uasg", "modulouasg"],
}

# código IBGE de cada UF (usado para casar com o GeoJSON do mapa)
UF_CODIGOS = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17, "MA": 21, "PI": 22,
    "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29, "MG": 31, "ES": 32,
    "RJ": 33, "SP": 35, "PR": 41, "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}

sessao = requests.Session()
sessao.headers.update({"Accept": "application/json", "User-Agent": "estudo-compras-publicas/1.0"})


# --------------------------------------------------------------------------
# Acesso à API (paginação por pagina/tamanhoPagina)
# --------------------------------------------------------------------------

def brl(valor):
    """Formata como moeda brasileira: 1234567.8 -> R$ 1.234.567,80"""
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def normalizar(texto):
    sem_acento = unicodedata.normalize("NFD", str(texto))
    return "".join(c for c in sem_acento if unicodedata.category(c) != "Mn").lower().strip()


def so_digitos(texto):
    return re.sub(r"\D", "", str(texto or ""))


_rotas_api = None
_rota_ok = {}
_rotas_anunciadas = set()


def rotas_da_api():
    """
    Lê a lista de caminhos publicada na documentação (OpenAPI) da própria API. O manual em PDF
    escreve os nomes dos módulos de jeitos diferentes, então a lista oficial é mais confiável.
    """
    global _rotas_api
    if _rotas_api is None:
        _rotas_api = []
        for doc in ("/v3/api-docs", "/v2/api-docs"):
            try:
                resp = sessao.get(BASE + doc, timeout=30)
                if resp.status_code == 200:
                    _rotas_api = list(resp.json().get("paths", {}))
                    break
            except (requests.RequestException, ValueError):
                continue
    return _rotas_api


def caminhos_possiveis(modulo, endpoint):
    """Ordem de tentativa: rota já validada, rota da documentação oficial, grafias do manual."""
    if (modulo, endpoint) in _rota_ok:
        return [_rota_ok[(modulo, endpoint)]]
    caminhos = [p for p in rotas_da_api() if p.endswith("/" + endpoint)]
    if caminhos:
        if endpoint not in _rotas_anunciadas:
            _rotas_anunciadas.add(endpoint)
            print(f"  rota encontrada na documentação da API: {caminhos[0]}")
        return caminhos[:1]  # a documentação oficial manda; não adianta testar outras grafias
    return [f"/{prefixo}/{endpoint}" for prefixo in PREFIXOS[modulo]]


class SemRegistros(RuntimeError):
    """A API respondeu 404. Nesse serviço isso acontece quando não há dados para os filtros."""


def pegar_pagina(modulo, endpoint, params):
    """Uma chamada à API, com nova tentativa em 429/5xx e teste das possíveis rotas."""
    candidatos = caminhos_possiveis(modulo, endpoint)
    ultimo = None
    falhas = []  # URLs que deram 404, para mostrar tudo no erro final
    for caminho in candidatos:
        url = f"{BASE}{caminho}"
        for t in range(4):
            try:
                resp = sessao.get(url, params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout) as erro:
                ultimo = erro
                time.sleep(2 ** t)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (t + 1))
                continue
            break
        else:
            raise RuntimeError(f"A API não respondeu em {url}: {ultimo or 'muitas tentativas'}")

        if resp.status_code == 404:
            falhas.append(f"{resp.url} -> {resp.text[:150]}")
            continue  # pode ser a grafia do caminho; tenta a outra
        if resp.status_code >= 400:
            raise RuntimeError(f"Erro {resp.status_code} em {resp.url}: {resp.text[:300]}")
        _rota_ok[(modulo, endpoint)] = caminho
        return resp.json()
    raise SemRegistros("A API respondeu 404:\n  " + "\n  ".join(falhas))


def paginar(modulo, endpoint, params, max_paginas=40, tamanho=500, silencioso=False):
    """
    Percorre todas as páginas. A resposta traz "resultado" (a lista) e "totalPaginas".
    Devolve (registros, total_de_paginas) para o chamador avisar se o resultado ficou parcial.
    """
    registros = []
    total = 1
    pagina = 1
    while pagina <= min(total, max_paginas):
        dados = pegar_pagina(modulo, endpoint, {**params, "pagina": pagina, "tamanhoPagina": tamanho})
        registros.extend(dados.get("resultado") or [])
        total = dados.get("totalPaginas") or 1
        if not silencioso and (pagina % 10 == 0 or pagina == total):
            print(f"  página {pagina}/{total} ({len(registros)} registros)")
        pagina += 1
    if total > max_paginas and not silencioso:
        print(f"  AVISO: só {max_paginas} das {total} páginas foram lidas (resultado parcial). "
              "Aumente --max-paginas ou reduza o período.")
    return registros, total


# --------------------------------------------------------------------------
# Carga de dados
# --------------------------------------------------------------------------

def listar_orgaos(atualizar=False):
    """
    O endpoint de contratos EXIGE o código do órgão (a API devolve 404 sem ele), então precisamos
    da lista de órgãos para varrer um por um. Guardamos em cache/orgaos.json.
    Devolve {codigoOrgao (texto): nomeOrgao}.
    """
    caminho = os.path.join(PASTA_CACHE, "orgaos.json")
    if os.path.exists(caminho) and not atualizar:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)

    print("Baixando lista de órgãos (só na primeira vez)...")
    registros, _ = paginar("uasg", "2_consultarOrgao", {"statusOrgao": "true"}, max_paginas=100)
    orgaos = {str(r["codigoOrgao"]): r.get("nomeOrgao", "") for r in registros if r.get("codigoOrgao") is not None}
    if not orgaos:
        chaves = list(registros[0].keys()) if registros else []
        raise SystemExit(f"Não encontrei 'codigoOrgao' na lista de órgãos. Campos recebidos: {chaves}. "
                         "Ajuste os nomes em listar_orgaos().")
    os.makedirs(PASTA_CACHE, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(orgaos, f, ensure_ascii=False)
    return orgaos


def contratos_do_orgao(codigo, params, max_paginas):
    """Contratos de um órgão. Devolve (registros, total_de_paginas); 404 vira lista vazia."""
    try:
        return paginar("contratos", "1_consultarContratos", {**params, "codigoOrgao": codigo},
                       max_paginas, silencioso=True)
    except SemRegistros:
        return [], 0


def carregar_contratos(inicio, fim, max_paginas, extra=None, orgaos=None, max_orgaos=0):
    """
    Contratos com vigência iniciada no período, como DataFrame com valor numérico.
    Como a API só consulta um órgão por vez, varremos os órgãos (em paralelo, poucos de cada vez).
      orgaos     -> lista de códigos escolhidos pelo usuário (None = todos os órgãos ativos)
      max_orgaos -> limita a quantidade de órgãos (0 = sem limite); o resultado vira uma amostra
      max_paginas-> páginas de 500 contratos lidas POR órgão
    """
    print(f"Buscando contratos com vigência iniciada entre {inicio} e {fim}...")
    params = {"dataVigenciaInicialMin": inicio, "dataVigenciaInicialMax": fim, **(extra or {})}

    if orgaos:
        codigos = [str(c) for c in orgaos]
    else:
        codigos = sorted(listar_orgaos())
        if max_orgaos and len(codigos) > max_orgaos:
            print(f"  AVISO: usando {max_orgaos} de {len(codigos)} órgãos (amostra, em ordem de código). "
                  "Use --max-orgaos 0 para varrer todos (demora) ou --orgao para escolher.")
            codigos = codigos[:max_orgaos]

    registros, truncados, com_dados = [], 0, 0
    with ThreadPoolExecutor(max_workers=4) as pool:  # poucas threads para não levar 429
        futuros = {pool.submit(contratos_do_orgao, c, params, max_paginas): c for c in codigos}
        for i, futuro in enumerate(as_completed(futuros), 1):
            regs, total = futuro.result()
            registros.extend(regs)
            com_dados += bool(regs)
            truncados += total > max_paginas
            if i % 10 == 0 or i == len(codigos):
                print(f"  {i}/{len(codigos)} órgãos consultados, {len(registros)} contratos até agora")
    if truncados:
        print(f"  AVISO: {truncados} órgãos tinham mais páginas que o limite (--max-paginas {max_paginas}).")

    df = pd.DataFrame(registros)
    if df.empty:
        raise SystemExit("Nenhum contrato retornado. Se TODOS os órgãos deram 404, confira o período e os "
                         "filtros; teste um órgão com curl, passando codigoOrgao, dataVigenciaInicialMin e "
                         "dataVigenciaInicialMax (AAAA-MM-DD).")
    if "contratoExcluido" in df:
        df = df[df["contratoExcluido"] != True]  # noqa: E712 - ignora contratos marcados como excluídos
    df["valorGlobal"] = pd.to_numeric(df["valorGlobal"], errors="coerce").fillna(0)
    df["cnpj"] = df["niFornecedor"].map(so_digitos)
    return df


def carregar_mapa_uasg(atualizar=False):
    """
    O contrato não traz UF, só o código da unidade gestora. Por isso baixamos o cadastro de
    UASGs (código -> UF) uma vez e guardamos em cache/uasg_uf.json, já que não muda muito.
    """
    caminho = os.path.join(PASTA_CACHE, "uasg_uf.json")
    if os.path.exists(caminho) and not atualizar:
        with open(caminho, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}

    print("Baixando cadastro de UASGs (só na primeira vez)...")
    # statusUasg é obrigatório nesse endpoint; pegamos as ativas
    registros, _ = paginar("uasg", "1_consultarUasg", {"statusUasg": "true"}, max_paginas=200)
    mapa = {}
    for r in registros:
        codigo = r.get("codigoUasg", r.get("codigoUnidadeGestora"))
        uf = r.get("siglaUf", r.get("uf"))
        if codigo is not None and uf:
            mapa[int(codigo)] = uf
    if not mapa:
        chaves = list(registros[0].keys()) if registros else []
        raise SystemExit("Não encontrei código/UF no cadastro de UASGs. Campos recebidos: "
                         f"{chaves}. Ajuste os nomes em carregar_mapa_uasg().")
    os.makedirs(PASTA_CACHE, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(mapa, f)
    return mapa


def cnpjs_do_cnae(cnae, max_paginas):
    """Conjunto de CNPJs (só dígitos) dos fornecedores ativos de um CNAE principal."""
    print(f"Buscando fornecedores do CNAE {cnae}...")
    regs, _ = paginar("fornecedor", "1_consultarFornecedor",
                      {"codigoCnae": cnae, "ativo": "true"}, max_paginas)
    return {so_digitos(r.get("cnpj")) for r in regs}


# --------------------------------------------------------------------------
# Gráficos
# --------------------------------------------------------------------------

def barras_horizontais(df, x, y, titulo, arquivo, rotulo_x):
    # nomes longos de órgão/empresa são cortados para não estourar o gráfico
    df = df.assign(**{y: df[y].astype(str).str.slice(0, 45)})
    fig = px.bar(df.sort_values(x), x=x, y=y, orientation="h", text=x, title=titulo,
                 labels={x: rotulo_x, y: ""})
    fig.update_traces(texttemplate="R$ %{x:,.3s}", marker_color="#2a6f97")
    fig.update_layout(height=max(400, 45 * len(df) + 150), margin=dict(l=10, r=10))
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    caminho = os.path.join(PASTA_SAIDA, arquivo)
    fig.write_html(caminho)
    print(f"Gráfico salvo em {caminho}")


# --------------------------------------------------------------------------
# Comando 1: mapa de calor por UF
# --------------------------------------------------------------------------

def cmd_mapa(args):
    contratos = carregar_contratos(args.inicio, args.fim, args.max_paginas,
                                   orgaos=args.orgao, max_orgaos=args.max_orgaos)
    uasg_uf = carregar_mapa_uasg(args.atualizar_cache)

    contratos["uf"] = contratos["codigoUnidadeGestora"].map(lambda c: uasg_uf.get(int(c)) if pd.notna(c) else None)
    sem_uf = contratos[contratos["uf"].isna()]
    if len(sem_uf):
        print(f"{len(sem_uf)} contratos ({brl(sem_uf['valorGlobal'].sum())}) ficaram sem UF e fora do mapa.")

    por_uf = (contratos.dropna(subset=["uf"]).groupby("uf")
              .agg(valor_total=("valorGlobal", "sum"), contratos=("valorGlobal", "size"))
              .sort_values("valor_total", ascending=False))
    print("\n", por_uf.assign(valor_total=por_uf["valor_total"].map(brl)).to_string())
    os.makedirs(PASTA_SAIDA, exist_ok=True)
    por_uf.to_csv(os.path.join(PASTA_SAIDA, "contratos_por_uf.csv"))

    geo = requests.get(IBGE_MALHAS, params={"intrarregiao": "UF", "formato": "application/vnd.geo+json",
                                            "qualidade": "minima"}, timeout=60).json()
    df = por_uf.reset_index()
    df["codigo"] = df["uf"].map(lambda s: str(UF_CODIGOS.get(s, "")))
    # escala em bilhões fica mais legível na legenda do mapa
    df["bilhoes"] = df["valor_total"] / 1e9

    mapa = folium.Map(location=[-14.2, -51.9], zoom_start=4, tiles="CartoDB positron")
    folium.Choropleth(
        geo_data=geo, data=df, columns=["codigo", "bilhoes"], key_on="feature.properties.codarea",
        fill_color="YlOrRd", fill_opacity=0.8, line_opacity=0.4, nan_fill_color="#e0e0e0",
        legend_name=f"Valor total de contratos (R$ bilhões), {args.inicio} a {args.fim}",
    ).add_to(mapa)
    saida = os.path.join(PASTA_SAIDA, "mapa_contratos_uf.html")
    mapa.save(saida)
    print(f"\nMapa salvo em {saida} (abra no navegador)")


# --------------------------------------------------------------------------
# Comando 2: ranking de fornecedores/órgãos
# --------------------------------------------------------------------------

def cmd_ranking(args):
    contratos = carregar_contratos(args.inicio, args.fim, args.max_paginas,
                                   orgaos=args.orgao, max_orgaos=args.max_orgaos)

    # filtro por ramo de atividade: o contrato não tem CNAE, então cruzamos com o cadastro de fornecedores
    if args.cnae:
        cnpjs = cnpjs_do_cnae(args.cnae, 100)
        contratos = contratos[contratos["cnpj"].isin(cnpjs)]
        print(f"{len(cnpjs)} fornecedores no CNAE; {len(contratos)} contratos no período.")
    # filtro por categoria do objeto (compras, serviços, obras...) vem direto no contrato
    if args.categoria:
        alvo = normalizar(args.categoria)
        contratos = contratos[contratos["nomeCategoria"].map(normalizar).str.contains(alvo, na=False)]
        print(f"{len(contratos)} contratos na categoria '{args.categoria}'.")
    if contratos.empty:
        raise SystemExit("Nenhum contrato depois dos filtros.")

    if args.por == "fornecedor":
        chaves, rotulo = ["cnpj", "nomeRazaoSocialFornecedor"], "Fornecedores"
        nome = "nomeRazaoSocialFornecedor"
    else:
        chaves, rotulo = ["codigoOrgao", "nomeOrgao"], "Órgãos"
        nome = "nomeOrgao"

    ranking = (contratos.groupby(chaves)
               .agg(valor_total=("valorGlobal", "sum"), contratos=("valorGlobal", "size"))
               .reset_index().sort_values("valor_total", ascending=False).head(args.top))
    ranking["valor"] = ranking["valor_total"].map(brl)
    print(f"\nTop {args.top} - {rotulo} por valor contratado\n")
    print(ranking[[nome, "contratos", "valor"]].to_string(index=False))

    os.makedirs(PASTA_SAIDA, exist_ok=True)
    ranking.to_csv(os.path.join(PASTA_SAIDA, f"ranking_{args.por}.csv"), index=False)
    filtro = f" (CNAE {args.cnae})" if args.cnae else ""
    barras_horizontais(ranking, "valor_total", nome,
                       f"Top {args.top} {rotulo.lower()} por valor contratado{filtro}, {args.inicio} a {args.fim}",
                       f"ranking_{args.por}.html", "Valor total (R$)")


# --------------------------------------------------------------------------
# Comando 3: distribuição de fornecedores
# --------------------------------------------------------------------------

def extrair_uf(municipio):
    """
    O cadastro devolve 'municipio' como texto e não tem campo de UF. Quando o texto termina
    em 'CIDADE/UF' ou 'CIDADE - UF' dá para extrair; senão devolvemos None.
    """
    achado = re.search(r"[/\-\s]([A-Z]{2})\s*$", str(municipio or "").strip())
    return achado.group(1) if achado and achado.group(1) in UF_CODIGOS else None


def cmd_fornecedores(args):
    params = {"ativo": "true"}
    if args.cnae:
        params["codigoCnae"] = args.cnae
    print("Buscando fornecedores cadastrados no SICAF...")
    regs, total = paginar("fornecedor", "1_consultarFornecedor", params, args.max_paginas)
    df = pd.DataFrame(regs)
    if df.empty:
        raise SystemExit("Nenhum fornecedor retornado.")
    print(f"{len(df)} fornecedores carregados.")

    df["uf"] = df["municipio"].map(extrair_uf)
    os.makedirs(PASTA_SAIDA, exist_ok=True)

    if df["uf"].notna().mean() > 0.5:
        contagem = df["uf"].value_counts().reset_index()
        contagem.columns = ["uf", "fornecedores"]
        fig = px.bar(contagem, x="uf", y="fornecedores", title="Fornecedores ativos por UF",
                     labels={"uf": "UF", "fornecedores": "Fornecedores"}, color_discrete_sequence=["#2a6f97"])
        arquivo = "fornecedores_por_uf.html"
    else:
        print("O campo 'municipio' não traz a UF; mostrando os 20 municípios com mais fornecedores.")
        contagem = df["municipio"].value_counts().head(20).reset_index()
        contagem.columns = ["municipio", "fornecedores"]
        fig = px.bar(contagem.sort_values("fornecedores"), x="fornecedores", y="municipio", orientation="h",
                     title="Fornecedores ativos por município (top 20)", color_discrete_sequence=["#2a6f97"])
        arquivo = "fornecedores_por_municipio.html"
    fig.write_html(os.path.join(PASTA_SAIDA, arquivo))
    contagem.to_csv(os.path.join(PASTA_SAIDA, arquivo.replace(".html", ".csv")), index=False)
    print(contagem.head(15).to_string(index=False))

    if "porteEmpresaNome" in df:
        print("\nPor porte:")
        print(df["porteEmpresaNome"].value_counts().to_string())
    print(f"\nGráfico salvo em {os.path.join(PASTA_SAIDA, arquivo)}")


# --------------------------------------------------------------------------
# Comando 4: detalhe de um fornecedor (órgão -> contrato -> fornecedor)
# --------------------------------------------------------------------------

def cmd_fornecedor(args):
    cnpj = so_digitos(args.cnpj)
    cadastro, _ = paginar("fornecedor", "1_consultarFornecedor", {"cnpj": cnpj, "ativo": "true"}, max_paginas=1)
    if cadastro:
        c = cadastro[0]
        print(f"{c.get('nomeRazaoSocialFornecedor')} | CNAE {c.get('codigoCnae')} | "
              f"{c.get('municipio')} | {c.get('porteEmpresaNome')}")
    else:
        print("CNPJ não encontrado entre os fornecedores ativos do SICAF.")

    contratos = carregar_contratos(args.inicio, args.fim, args.max_paginas, {"niFornecedor": cnpj},
                                   orgaos=args.orgao, max_orgaos=args.max_orgaos)
    print(f"\n{len(contratos)} contratos, total {brl(contratos['valorGlobal'].sum())}\n")
    # hierarquia: órgão -> contratos
    for (_, orgao), grupo in contratos.groupby(["codigoOrgao", "nomeOrgao"]):
        print(f"{orgao}: {len(grupo)} contratos, {brl(grupo['valorGlobal'].sum())}")
        for _, k in grupo.sort_values("valorGlobal", ascending=False).head(5).iterrows():
            print(f"    contrato {k.get('numeroContrato')} | {brl(k['valorGlobal'])} | "
                  f"vigência {str(k.get('dataVigenciaInicial'))[:10]} a {str(k.get('dataVigenciaFinal'))[:10]}")


# --------------------------------------------------------------------------
# Comando 5: lista de órgãos
# --------------------------------------------------------------------------

def cmd_orgaos(args):
    orgaos = listar_orgaos(args.atualizar)
    alvo = normalizar(args.busca) if args.busca else None
    achados = {c: n for c, n in orgaos.items() if not alvo or alvo in normalizar(n)}
    for codigo, nome in sorted(achados.items(), key=lambda x: x[1]):
        print(f"{codigo:<8} {nome}")
    print(f"\n{len(achados)} de {len(orgaos)} órgãos.")


# --------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------

def montar_parser():
    ano_passado = date.today().year - 1
    parser = argparse.ArgumentParser(description="Painel de compras públicas federais (Compras.gov.br)")
    sub = parser.add_subparsers(dest="comando", required=True)

    def periodo(p):
        p.add_argument("--inicio", default=f"{ano_passado}-01-01", help="início da vigência (AAAA-MM-DD)")
        p.add_argument("--fim", default=f"{ano_passado}-12-31", help="fim da vigência (AAAA-MM-DD)")
        p.add_argument("--orgao", nargs="+", metavar="CODIGO",
                       help="códigos dos órgãos a consultar (veja o comando 'orgaos')")
        p.add_argument("--max-orgaos", type=int, default=30,
                       help="quantos órgãos varrer quando --orgao não é usado (0 = todos, demora)")
        p.add_argument("--max-paginas", type=int, default=5,
                       help="páginas de 500 contratos lidas por órgão")

    p = sub.add_parser("orgaos", help="lista os órgãos (código e nome) para usar em --orgao")
    p.add_argument("--busca", help="filtra pelo nome, ex.: saude")
    p.add_argument("--atualizar", action="store_true", help="baixa a lista de novo")
    p.set_defaults(func=cmd_orgaos)

    p = sub.add_parser("mapa", help="mapa de calor do valor de contratos por UF")
    periodo(p)
    p.add_argument("--atualizar-cache", action="store_true", help="baixa de novo o cadastro de UASGs")
    p.set_defaults(func=cmd_mapa)

    p = sub.add_parser("ranking", help="ranking de fornecedores ou órgãos por valor contratado")
    periodo(p)
    p.add_argument("--por", choices=["fornecedor", "orgao"], default="fornecedor")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--cnae", type=int, help="ramo de atividade (CNAE principal do fornecedor)")
    p.add_argument("--categoria", help='categoria do contrato, ex.: "servicos", "obras", "compras"')
    p.set_defaults(func=cmd_ranking)

    p = sub.add_parser("fornecedores", help="distribuição dos fornecedores cadastrados")
    p.add_argument("--cnae", type=int)
    p.add_argument("--max-paginas", type=int, default=20)
    p.set_defaults(func=cmd_fornecedores)

    p = sub.add_parser("fornecedor", help="cadastro e contratos de um CNPJ")
    p.add_argument("--cnpj", required=True)
    periodo(p)
    p.set_defaults(func=cmd_fornecedor, max_orgaos=0)  # busca específica: varre todos os órgãos

    return parser


if __name__ == "__main__":
    argumentos = montar_parser().parse_args()
    argumentos.func(argumentos)