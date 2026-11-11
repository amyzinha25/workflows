#!/usr/bin/env python3
"""
Radar de Lançamentos
====================
Todo dia:
  1. Busca anúncios ativos na Biblioteca de Anúncios da Meta (via Apify)
  2. Agrupa por anunciante e filtra os que anunciam forte há alguns dias
  3. Abre a página de destino e usa o Claude para extrair evento, expert e moeda
  4. Consulta o volume de busca no Google (Keyword Planner) do evento e do expert
  5. Dá uma nota e manda os melhores no Telegram

Uso:
  python radar_lancamentos.py --teste   # só manda uma mensagem de teste no Telegram
  python radar_lancamentos.py           # roda o radar completo
"""

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import requests

# ----------------------------------------------------------------------------
# CONFIGURAÇÃO (tudo pode ser trocado por variáveis de ambiente)
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "apify~facebook-ads-scraper")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

# Google Ads (Keyword Planner). Opcional: sem isso o radar roda sem checar volume.
GADS_DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
GADS_CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
GADS_CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
GADS_REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
GADS_LOGIN_CUSTOMER_ID = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")  # só se usar MCC
GADS_CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "")  # sem traços
GEO_ID = os.getenv("GEO_ID", "2840")  # 2840 = Estados Unidos
LANG_ID = os.getenv("LANG_ID", "1000")  # 1000 = inglês

# Onde procurar
PAISES = [p.strip() for p in os.getenv("PAISES", "US,GB,CA,AU").split(",") if p.strip()]
TERMOS = [
    t.strip()
    for t in os.getenv(
        "TERMOS",
        "free masterclass,free webinar,free challenge,free workshop,live training,register now free",
    ).split(",")
    if t.strip()
]
RESULTADOS_POR_BUSCA = int(os.getenv("RESULTADOS_POR_BUSCA", "60"))

# Filtros e nota
MIN_ANUNCIOS = int(os.getenv("MIN_ANUNCIOS", "3"))  # anúncios ativos do anunciante
MIN_DIAS = int(os.getenv("MIN_DIAS", "3"))  # há quantos dias o anúncio mais antigo roda
VOLUME_MINIMO = int(os.getenv("VOLUME_MINIMO", "100"))  # buscas/mês (evento OU expert)
NOTA_MINIMA = float(os.getenv("NOTA_MINIMA", "40"))
MAX_ENVIO = int(os.getenv("MAX_ENVIO", "10"))
MAX_PAGINAS_ANALISADAS = int(os.getenv("MAX_PAGINAS_ANALISADAS", "40"))  # limita custo de IA
MOEDAS_OK = {m.strip().upper() for m in os.getenv("MOEDAS_OK", "USD,EUR,GBP,CAD,AUD").split(",")}

SEEN_FILE = os.getenv("SEEN_FILE", "seen.json")
DIAS_PARA_REENVIAR = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DOMINIOS_IGNORADOS = (
    "facebook.com", "fb.com", "instagram.com", "fb.me", "messenger.com",
    "wa.me", "whatsapp.com", "youtube.com", "youtu.be", "l.facebook.com",
    "apps.apple.com", "play.google.com", "amazon.com", "linktr.ee",
)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------
def telegram_enviar(texto):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log("Telegram não configurado. Mensagem que seria enviada:\n" + texto)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": texto[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        log(f"Erro Telegram {r.status_code}: {r.text[:200]}")
    return r.ok


# ----------------------------------------------------------------------------
# 1) COLETA NA BIBLIOTECA DE ANÚNCIOS DA META (via Apify)
# ----------------------------------------------------------------------------
def url_biblioteca(termo, pais):
    return (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
        f"&country={pais}&q={quote_plus(termo)}&search_type=keyword_unordered&media_type=all"
    )


def pega(d, *caminhos, default=None):
    """Busca o primeiro valor existente entre vários caminhos ('a.b.c') num dict aninhado."""
    for caminho in caminhos:
        atual = d
        ok = True
        for chave in caminho.split("."):
            if isinstance(atual, dict) and chave in atual and atual[chave] not in (None, ""):
                atual = atual[chave]
            else:
                ok = False
                break
        if ok:
            return atual
    return default


def parse_data(valor):
    if valor is None:
        return None
    try:
        if isinstance(valor, (int, float)):
            if valor > 1e12:  # milissegundos
                valor /= 1000
            return datetime.fromtimestamp(valor, tz=timezone.utc)
        s = str(valor).strip()
        if s.isdigit():
            return parse_data(int(s))
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:25] if "T" in s else s[:10])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def coletar_anuncios():
    if not APIFY_TOKEN:
        log("APIFY_TOKEN não configurado: sem coleta de anúncios.")
        return []
    start_urls = [{"url": url_biblioteca(t, p)} for t in TERMOS for p in PAISES]
    log(f"Buscando anúncios: {len(start_urls)} buscas na Biblioteca da Meta (Apify)...")
    endpoint = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
    # ATENÇÃO: o formato do input depende do actor escolhido. Confira na página do actor
    # no Apify e ajuste os nomes dos campos abaixo se for preciso.
    payload = {"startUrls": start_urls, "resultsLimit": RESULTADOS_POR_BUSCA}
    try:
        r = requests.post(endpoint, params={"token": APIFY_TOKEN}, json=payload, timeout=600)
        r.raise_for_status()
        itens = r.json()
    except Exception as e:
        log(f"Falha na coleta do Apify: {e}")
        return []
    log(f"{len(itens)} anúncios recebidos.")
    return itens


def agrupar_por_anunciante(itens):
    grupos = defaultdict(lambda: {"anuncios": 0, "inicio": None, "links": Counter(), "textos": []})
    agora = datetime.now(timezone.utc)
    for it in itens:
        nome = pega(it, "pageName", "page_name", "snapshot.pageName", "snapshot.page_name")
        if not nome:
            continue
        g = grupos[nome.strip()]
        g["anuncios"] += 1
        inicio = parse_data(
            pega(it, "startDateFormatted", "startDate", "start_date", "ad_delivery_start_time")
        )
        if inicio and (g["inicio"] is None or inicio < g["inicio"]):
            g["inicio"] = inicio
        link = pega(
            it, "snapshot.linkUrl", "linkUrl", "link_url", "snapshot.link_url",
            "snapshot.cards.0.linkUrl",
        )
        if isinstance(link, str) and link.startswith("http"):
            host = urlparse(link).netloc.lower()
            if not any(host.endswith(d) for d in DOMINIOS_IGNORADOS):
                g["links"][link] += 1
        texto = pega(it, "snapshot.body.text", "snapshot.body", "adCreativeBody", "ad_creative_bodies")
        if isinstance(texto, str) and len(g["textos"]) < 2:
            g["textos"].append(texto[:300])

    candidatos = []
    for nome, g in grupos.items():
        dias = (agora - g["inicio"]).days if g["inicio"] else 0
        if g["anuncios"] < MIN_ANUNCIOS or dias < MIN_DIAS or not g["links"]:
            continue
        candidatos.append(
            {
                "anunciante": nome,
                "anuncios": g["anuncios"],
                "dias": dias,
                "landing": g["links"].most_common(1)[0][0],
                "textos": g["textos"],
            }
        )
    candidatos.sort(key=lambda c: (c["anuncios"], c["dias"]), reverse=True)
    log(f"{len(candidatos)} anunciantes passaram no filtro de força dos anúncios.")
    return candidatos[:MAX_PAGINAS_ANALISADAS]


# ----------------------------------------------------------------------------
# 2) LEITURA DA PÁGINA DE DESTINO + EXTRAÇÃO COM CLAUDE
# ----------------------------------------------------------------------------
def texto_da_pagina(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log(f"  não abriu {url[:70]}: {e}")
        return "", ""
    raw = r.text
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    titulo = html.unescape(m.group(1)).strip() if m else ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", raw)
    texto = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    texto = re.sub(r"\s+", " ", texto).strip()
    return titulo, texto[:6000]


PROMPT_EXTRACAO = """Você analisa páginas de destino de anúncios. Responda SOMENTE com um JSON válido, sem texto extra.

Campos:
- "e_lancamento": true se a página divulga um evento/lançamento de infoproduto com captura de leads (webinar, masterclass, desafio, workshop, aula ao vivo) que leva a uma venda de curso/programa; false para loja, app, serviço local, etc.
- "nome_evento": nome exato do evento como aparece na página (ou null)
- "expert": nome da pessoa/marca que ministra (ou null)
- "tipo": "webinar", "masterclass", "challenge", "workshop" ou "outro"
- "moeda": moeda dos preços se aparecer (USD, EUR, GBP, CAD, AUD...), senão null
- "data_evento": data/horário do evento se aparecer, senão null
- "programa_afiliados": true se a página mostrar link para afiliados/parceiros/JV, false se não, null se não der para saber

Título da página: {titulo}
Texto da página:
{texto}
"""


def extrair_com_claude(titulo, texto):
    if not ANTHROPIC_API_KEY:
        return {
            "e_lancamento": True, "nome_evento": titulo or None, "expert": None,
            "tipo": "outro", "moeda": None, "data_evento": None, "programa_afiliados": None,
        }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 400,
                "messages": [
                    {"role": "user", "content": PROMPT_EXTRACAO.format(titulo=titulo, texto=texto)}
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        saida = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", saida, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        log(f"  erro na extração com Claude: {e}")
        return None


PALAVRAS_EVENTO = [
    "webinar", "masterclass", "master class", "workshop", "challenge", "live training",
    "free training", "free class", "register", "save my seat", "reserve your spot",
    "reserve my spot", "watch the free", "free video", "sign up", "claim your spot",
]
PALAVRAS_LOJA = [
    "add to cart", "free shipping", "shop now", "track your order", "download the app",
    "view cart", "your cart",
]


def extrair_por_regras(anunciante, titulo, texto):
    """Modo grátis: identifica lançamento por palavras-chave, sem usar IA."""
    t = texto.lower()
    pontos = sum(1 for p in PALAVRAS_EVENTO if p in t)
    loja = sum(1 for p in PALAVRAS_LOJA if p in t)
    if "webinar" in t:
        tipo = "webinar"
    elif "masterclass" in t or "master class" in t:
        tipo = "masterclass"
    elif "challenge" in t:
        tipo = "challenge"
    elif "workshop" in t:
        tipo = "workshop"
    else:
        tipo = "outro"
    if "€" in texto or re.search(r"\bEUR\b", texto):
        moeda = "EUR"
    elif "£" in texto or re.search(r"\bGBP\b", texto):
        moeda = "GBP"
    elif "$" in texto or re.search(r"\bUSD\b", texto):
        moeda = "USD"
    else:
        moeda = None
    nome = re.split(r"\s[|\-–—•]\s", titulo or "")[0].strip() or None
    return {
        "e_lancamento": pontos >= 2 and loja < 2,
        "nome_evento": nome,
        "expert": anunciante,  # na Meta, o nome da página costuma ser o expert/marca
        "tipo": tipo,
        "moeda": moeda,
        "data_evento": None,
        "programa_afiliados": True if re.search(r"affiliate|jv partner|become a partner", t) else None,
    }


# ----------------------------------------------------------------------------
# 3) VOLUME DE BUSCA NO GOOGLE (Keyword Planner)
# ----------------------------------------------------------------------------
def google_ads_configurado():
    return all(
        [GADS_DEVELOPER_TOKEN, GADS_CLIENT_ID, GADS_CLIENT_SECRET, GADS_REFRESH_TOKEN, GADS_CUSTOMER_ID]
    )


def volumes_google(palavras):
    """Retorna {palavra_minuscula: buscas_mensais}. Vazio se não estiver configurado."""
    palavras = sorted({p.strip() for p in palavras if p and p.strip()})
    if not palavras or not google_ads_configurado():
        return {}
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        log("Biblioteca google-ads não instalada: pulando checagem de volume.")
        return {}
    try:
        config = {
            "developer_token": GADS_DEVELOPER_TOKEN,
            "client_id": GADS_CLIENT_ID,
            "client_secret": GADS_CLIENT_SECRET,
            "refresh_token": GADS_REFRESH_TOKEN,
            "use_proto_plus": True,
        }
        if GADS_LOGIN_CUSTOMER_ID:
            config["login_customer_id"] = GADS_LOGIN_CUSTOMER_ID
        client = GoogleAdsClient.load_from_dict(config)
        service = client.get_service("KeywordPlanIdeaService")
        saida = {}
        for i in range(0, len(palavras), 20):  # lotes pequenos
            lote = palavras[i : i + 20]
            req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
            req.customer_id = GADS_CUSTOMER_ID
            req.keywords.extend(lote)
            req.geo_target_constants.append(f"geoTargetConstants/{GEO_ID}")
            req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
            req.language = f"languageConstants/{LANG_ID}"
            resp = service.generate_keyword_historical_metrics(request=req)
            for res in resp.results:
                saida[res.text.lower()] = int(res.keyword_metrics.avg_monthly_searches or 0)
        return saida
    except Exception as e:
        log(f"Erro no Keyword Planner: {e}")
        return {}


# ----------------------------------------------------------------------------
# 4) NOTA
# ----------------------------------------------------------------------------
def calcular_nota(c):
    nota = min(c["anuncios"], 30)  # até 30 pontos: quantidade de anúncios
    nota += min(c["dias"], 14) * 2  # até 28 pontos: tempo no ar
    vol = c.get("volume_max")
    if vol is not None:
        nota += min(vol / 50, 40)  # até 40 pontos: busca no Google
    if c.get("programa_afiliados") is True:
        nota += 10
    return round(nota, 1)


# ----------------------------------------------------------------------------
# DEDUPLICAÇÃO (para não mandar o mesmo lançamento todo dia)
# ----------------------------------------------------------------------------
def carregar_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    except Exception:
        seen = {}
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_PARA_REENVIAR)
    return {k: v for k, v in seen.items() if (parse_data(v) or limite) >= limite}


def salvar_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# MENSAGEM
# ----------------------------------------------------------------------------
def formatar(c):
    e = html.escape
    evento = c.get("nome_evento") or c["anunciante"]
    expert = c.get("expert") or c["anunciante"]
    linhas = [
        f"🚀 <b>{e(evento)}</b>",
        f"👤 {e(expert)}  |  ⭐ nota {c['nota']}",
        f"📣 {c['anuncios']} anúncios ativos, o mais antigo há {c['dias']} dias",
    ]
    if c.get("volume_evento") is not None or c.get("volume_expert") is not None:
        linhas.append(
            f"🔎 Buscas/mês: evento {c.get('volume_evento', '?')} | expert {c.get('volume_expert', '?')}"
        )
    else:
        linhas.append("🔎 Volume: confira no Keyword Planner com as palavras-chave abaixo")
    extras = []
    if c.get("moeda"):
        extras.append(f"moeda {e(str(c['moeda']))}")
    if c.get("data_evento"):
        extras.append(f"data {e(str(c['data_evento']))}")
    if c.get("programa_afiliados") is True:
        extras.append("tem página de afiliados")
    if extras:
        linhas.append("ℹ️ " + " | ".join(extras))
    kws = []
    if c.get("nome_evento"):
        kws.append(f"[{c['nome_evento']}]")
    if c.get("expert"):
        kws.append(f"[{c['expert']}]")
    if kws:
        linhas.append("🎯 Palavras-chave: " + e(" ".join(kws)))
    linhas.append(f"🔗 {e(c['landing'])}")
    linhas.append(
        "📚 Anúncios: https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
        f"&q={quote_plus(c['anunciante'])}&search_type=keyword_unordered"
    )
    return "\n".join(linhas)


# ----------------------------------------------------------------------------
# PRINCIPAL
# ----------------------------------------------------------------------------
def rodar():
    seen = carregar_seen()
    itens = coletar_anuncios()
    candidatos = [c for c in agrupar_por_anunciante(itens) if c["anunciante"].lower() not in seen]
    log(f"{len(candidatos)} candidatos novos para analisar.")

    analisados = []
    for c in candidatos:
        log(f"Analisando: {c['anunciante']} -> {c['landing'][:80]}")
        titulo, texto = texto_da_pagina(c["landing"])
        if not texto:
            continue
        info = extrair_com_claude(titulo, texto) if ANTHROPIC_API_KEY else None
        if info is None:
            info = extrair_por_regras(c["anunciante"], titulo, texto)
        if not info.get("e_lancamento"):
            continue
        moeda = (info.get("moeda") or "").upper() or None
        if moeda and moeda not in MOEDAS_OK:
            continue
        c.update(
            nome_evento=info.get("nome_evento"),
            expert=info.get("expert"),
            tipo=info.get("tipo"),
            moeda=moeda,
            data_evento=info.get("data_evento"),
            programa_afiliados=info.get("programa_afiliados"),
        )
        analisados.append(c)
        time.sleep(1)

    # volumes de busca (em lote)
    palavras = []
    for c in analisados:
        palavras += [c.get("nome_evento"), c.get("expert")]
    vols = volumes_google(palavras)
    for c in analisados:
        if vols:
            ve = vols.get((c.get("nome_evento") or "").lower())
            vx = vols.get((c.get("expert") or "").lower())
            c["volume_evento"], c["volume_expert"] = ve, vx
            achados = [v for v in (ve, vx) if v is not None]
            c["volume_max"] = max(achados) if achados else None
        else:
            c["volume_evento"] = c["volume_expert"] = c["volume_max"] = None

    # filtro de volume: o ponto central do radar
    finais = []
    for c in analisados:
        if c["volume_max"] is not None and c["volume_max"] < VOLUME_MINIMO:
            continue
        c["nota"] = calcular_nota(c)
        if c["nota"] >= NOTA_MINIMA:
            finais.append(c)
    finais.sort(key=lambda x: x["nota"], reverse=True)
    finais = finais[:MAX_ENVIO]

    hoje = datetime.now().strftime("%d/%m/%Y")
    if not finais:
        telegram_enviar(f"📡 Radar {hoje}: nenhum lançamento novo passou nos filtros hoje.")
        return
    telegram_enviar(f"📡 Radar de lançamentos {hoje}: {len(finais)} oportunidade(s)")
    agora = datetime.now(timezone.utc).isoformat()
    for c in finais:
        telegram_enviar(formatar(c))
        seen[c["anunciante"].lower()] = agora
        time.sleep(0.6)
    salvar_seen(seen)
    log("Concluído.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste", action="store_true", help="envia uma mensagem de teste no Telegram")
    args = ap.parse_args()
    if args.teste:
        ok = telegram_enviar("✅ Radar de lançamentos conectado ao Telegram.")
        sys.exit(0 if ok else 1)
    rodar()


if __name__ == "__main__":
    main()
