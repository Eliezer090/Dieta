"""
Busca no Koch (API sense.osuper.com.br, via Playwright) cada item da dieta, com e
sem promoção, compara (preço de referência = o menor que você já pagou no Komprão,
aba Preços do index.html), grava o histórico em SQLite, gera ofertas-koch.js
(lido pela aba Preços do index.html) e avisa no WhatsApp quando vale comprar.

Preço bom = por kg/L/ovo/un igual ou abaixo da referência do Komprão,
            OU o menor preço do Koch em 30 dias (com 7+ dias de histórico).
Todo dia vai uma mensagem com todos os itens: promoções no topo, depois preço bom
sem promoção, depois uma tabela com o resto.

Instalação (uma vez):
    pip install playwright
    playwright install chromium
    python get-ofertas.py --login-whatsapp   (escaneie o QR: WhatsApp > Aparelhos conectados)

Uso:
    python get-ofertas.py              coleta, atualiza ofertas-koch.js e avisa
    python get-ofertas.py --sem-aviso  coleta e atualiza, sem mandar WhatsApp (teste)
O agendamento diário é feito por agendar-ofertas.ps1.
"""
import datetime
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.parse

from playwright.sync_api import sync_playwright

PASTA = os.path.dirname(os.path.abspath(__file__))
SITE = "https://www.superkoch.com.br/"
API = "https://sense.osuper.com.br/295/1415/v2/search"  # 1415 = filial vista no site
SIZE = 20                  # a API aceita no máximo 20 por página
PAUSA_MS = 400             # pausa entre buscas, para não pesar no servidor
PAGINAS_POR_BUSCA = 2      # até 40 resultados por item (os mais relevantes vêm primeiro)
MIN_ITENS = 50             # abaixo disso, considera que a coleta quebrou
MIN_DIAS_HISTORICO = 7     # dias de histórico antes de usar a regra dos 30 dias
TOLERANCIA = 0.05          # até 5% acima do menor preço dos 30 dias ainda conta como preço bom
HEADLESS = os.getenv("HEADLESS") == "1"   # HEADLESS=1 testa sem janela (pode ser bloqueado)
DB = os.path.join(PASTA, "ofertas.db")
JS_SAIDA = os.path.join(PASTA, "ofertas-koch.js")
WA_PERFIL = os.path.join(PASTA, "wa_perfil")
# Número que recebe os avisos (só dígitos, DDI+DDD+número). Fica no arquivo wa_numero.txt,
# fora do Git, para não publicar o telefone; a variável de ambiente WA_NUMERO também vale.
_arq_numero = os.path.join(PASTA, "wa_numero.txt")
WA_NUMERO = os.getenv("WA_NUMERO") or (
    open(_arq_numero, encoding="utf-8").read().strip() if os.path.exists(_arq_numero) else "")
# Usa o Google Chrome instalado no PC; NAVEGADOR=msedge usa o Edge; NAVEGADOR=chromium usa o do Playwright.
NAVEGADOR = os.getenv("NAVEGADOR", "chrome")


def _opcoes(**kw):
    """Opções de lançamento com o navegador escolhido (o Chromium do Playwright não precisa de channel)."""
    return {**kw, "channel": NAVEGADOR} if NAVEGADOR != "chromium" else kw

# Itens da dieta. Campos:
#   nome      : como aparece na aba Preços
#   busca     : termo pesquisado no site (padrão: o 1º de "inicio")
#   inicio    : o nome do produto (sem acento) precisa COMEÇAR por uma destas expressões
#   incluir   : começos de palavra que TODOS precisam estar em algum lugar do nome
#   excluir   : começos de palavra que NÃO podem estar
#   ref       : menor preço já pago no Komprão (tabela PRECOS do index.html), na unidade abaixo
#   unidade   : "kg"/"L" = preço por kg/litro (lê "500g", "1L" no nome; sem isso, assume que já é por kg)
#               "ovo" = preço por ovo (lê "c/30", "20 un", "dz"); "un" = preço da embalagem
# A ordem importa: o primeiro que casar vale (extrato de tomate antes de tomate).
ALVOS = [
    {"nome": "Ovos",                    "busca": "ovos", "inicio": ["ovo", "ovos"], "incluir": ["ovo"],                "excluir": ["pascoa", "chocolate", "macarrao", "massa", "codorna", "po", "talharim"], "ref": 12.79 / 30, "unidade": "ovo"},
    {"nome": "Carne moída",             "inicio": ["carne moida", "carne bovina moida"], "incluir": ["carne", "moida"],     "excluir": ["almondega", "hamburguer", "pure", "perdigao", "soja", "frango", "suina"],                         "ref": 19.13, "unidade": "kg"},
    {"nome": "Peito de frango c/ osso", "inicio": ["peito de frango", "peito frango"], "incluir": ["peito", "frango"],    "excluir": ["sem", "desfiad", "cubos", "tiras", "grelhad", "file", "empanad", "defumad", "temperad", "sassami"], "ref": 13.49, "unidade": "kg"},
    {"nome": "Iogurte natural",         "inicio": ["iogurte"], "incluir": ["iogurte", "natural"], "excluir": ["protein", "baunilha", "grego", "desnatad", "zero", "polpa", "fruta", "morango", "coco", "mel"],                       "ref": 3.69 / 0.160, "unidade": "kg"},
    {"nome": "Leite integral",          "inicio": ["leite"], "incluir": ["leite", "integral"],  "excluir": ["po", "condensad", "creme", "doce", "fermentad", "coco"], "ref": 4.49, "unidade": "L"},
    {"nome": "Mussarela fatiada",       "inicio": ["queijo mussarela", "mussarela", "queijo muss"], "incluir": ["mussarela"],          "excluir": ["stick", "burrata", "vegan", "fit", "ralad", "palito", "bufala"],                       "ref": 14.99 / 0.300, "unidade": "kg"},
    {"nome": "Requeijão",               "inicio": ["requeijao"], "incluir": ["requeijao"],          "excluir": ["cheddar", "sabor", "ervas", "bufal", "azul", "brie", "queijos", "culinari"],                                        "ref": 14.99 / 0.400, "unidade": "kg"},
    {"nome": "Pão de forma",            "inicio": ["pao de forma", "pao forma"], "incluir": ["pao", "forma"],       "excluir": ["hot", "bisnag"],                                   "ref": 6.99 / 0.400, "unidade": "kg"},
    {"nome": "Batata inglesa",          "inicio": ["batata"], "incluir": ["batata"],             "excluir": ["salsa", "lays", "ruffles", "slice", "yokitos", "bem", "doce", "palha", "frita", "chips", "congelad", "pre", "pringles", "ondulada"], "ref": 6.49, "unidade": "kg"},
    {"nome": "Abobrinha",               "inicio": ["abobrinha"], "incluir": ["abobrinha"],          "excluir": [],                                                  "ref": 6.79, "unidade": "kg"},
    {"nome": "Cebola",                  "inicio": ["cebola"], "incluir": ["cebola"],             "excluir": ["granulad", "picada", "crispy", "kitano", "spices", "verde", "roxa", "frita", "creme", "sopa", "cebolinha"],     "ref": 9.99, "unidade": "kg"},
    {"nome": "Cenoura",                 "inicio": ["cenoura"], "incluir": ["cenoura"],            "excluir": ["bolo", "ralad"],                                            "ref": 4.99, "unidade": "kg"},
    {"nome": "Alho",                    "inicio": ["alho"], "incluir": ["alho"],               "excluir": ["poro", "granulad", "triturad", "oleo", "cebola", "spices", "kitano", "frito", "pasta", "sal", "tempero"],                "ref": 24.99, "unidade": "kg"},
    {"nome": "Extrato de tomate",       "inicio": ["extrato"], "incluir": ["extrato", "tomate"],  "excluir": [],                                                  "ref": 13.79 / 1.02, "unidade": "kg"},
    {"nome": "Tomate",                  "inicio": ["tomate"], "incluir": ["tomate"],             "excluir": ["triturad", "grape", "molho", "seco", "cereja", "pelado", "polpa", "ketchup"], "ref": 22.99, "unidade": "kg"},
    {"nome": "Banana",                  "inicio": ["banana"], "incluir": ["banana"],             "excluir": ["chips", "passa", "doce", "bolo"],                  "ref": 4.99, "unidade": "kg"},
    {"nome": "Mamão",                   "inicio": ["mamao"], "incluir": ["mamao"],              "excluir": [],                                                  "ref": 11.79, "unidade": "kg"},
    {"nome": "Manga",                   "inicio": ["manga"], "incluir": ["manga"],              "excluir": ["suco", "nectar"],                                  "ref": 4.99, "unidade": "kg"},
    {"nome": "Beterraba",               "inicio": ["beterraba"], "incluir": ["beterraba"],          "excluir": ["conserva"],                                                  "ref": 5.79, "unidade": "kg"},
    {"nome": "Batata-doce",             "inicio": ["batata doce", "batata-doce"], "incluir": ["batata", "doce"],     "excluir": ["chips"],                                           "ref": 5.79, "unidade": "kg"},
    {"nome": "Repolho",                 "inicio": ["repolho"], "incluir": ["repolho"],            "excluir": ["roxo"],                                            "ref": None, "unidade": "kg"},
    {"nome": "Aveia",                   "inicio": ["aveia", "flocos de aveia", "farelo de aveia"], "incluir": ["aveia"],              "excluir": ["barra", "bebida", "biscoito", "cookie"],           "ref": None, "unidade": "kg"},
    {"nome": "Tapioca",                 "inicio": ["tapioca", "massa de tapioca", "goma de tapioca", "goma para tapioca"], "incluir": ["tapioca"],            "excluir": ["biscoito"],                                        "ref": None, "unidade": "kg"},
    {"nome": "Café",                    "inicio": ["cafe"], "incluir": ["cafe"],               "excluir": ["grao", "espresso", "capsula", "cappuccino", "filtro", "leite", "soluvel", "doce", "bolo"], "ref": None, "unidade": "kg"},
]
# ref None = nunca comprado no Komprão (ou só por unidade, caso do repolho): só avisa pelo histórico de 30 dias.


def brl(v):
    """4.99 -> '4,99' (formato brasileiro nas mensagens)."""
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def norm(t):
    """minúsculo e sem acento, para 'Café' casar com 'cafe'."""
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def quantidade(nome, unidade):
    """Quanto vem na embalagem: kg ou L (ex.: '500g' -> 0.5) ou nº de ovos. None se não achar."""
    n = norm(nome).replace(",", ".")
    if unidade == "ovo":
        m = re.search(r"(?:c/|com|cx|bandeja)\s*(\d+)|(\d+)\s*(?:un|unid|ovos)", n)
        if m:
            return float(m.group(1) or m.group(2))
        return 12.0 if re.search(r"\bdz\b|duzia", n) else None
    if unidade in ("kg", "L"):
        m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g|gr|ml|l|lt|litros?)\b", n)
        if m:
            v, u = float(m.group(1)), m.group(2)
            return v / 1000 if u in ("g", "gr", "ml") else v
    return None


def casar(nome):
    """Devolve o alvo da lista ALVOS que casa com o nome do produto, ou None."""
    palavras = re.findall(r"[a-z0-9]+", norm(nome))
    texto = " ".join(palavras) + " "
    tem = lambda pre: any(p.startswith(pre) for p in palavras)
    for a in ALVOS:
        comeca = any(texto.startswith(i.replace("-", " ") + " ") for i in a["inicio"])
        if comeca and all(tem(i) for i in a["incluir"]) and not any(tem(e) for e in a["excluir"]):
            return a
    return None


def coletar():
    """Abre o site num navegador de verdade e pagina a API de dentro dele."""
    tokens = {}

    def ao_requisitar(req):
        if "sense.osuper.com.br" in req.url:
            t = req.headers.get("x-osuper-search-token")
            if t:
                tokens["search"] = t

    itens = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(**_opcoes(headless=HEADLESS))
        page = browser.new_context(locale="pt-BR").new_page()
        page.on("request", ao_requisitar)
        # "domcontentloaded": o site nunca para de usar a rede, então não espere "load"
        page.goto(SITE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        headers = {"x-osuper-search-token": tokens["search"]} if "search" in tokens else {}
        # Uma busca por item da dieta (com e sem promoção): pega também o preço normal.
        for a in ALVOS:
            termo = a.get("busca", a["inicio"][0])
            for frm in range(0, PAGINAS_POR_BUSCA * SIZE, SIZE):
                url = f"{API}?size={SIZE}&from={frm}&search={urllib.parse.quote(termo)}"
                res = page.evaluate(
                    """async ({url, headers}) => {
                        const r = await fetch(url, {headers});
                        return {status: r.status, body: await r.text()};
                    }""",
                    {"url": url, "headers": headers},
                )
                if res["status"] != 200:
                    raise RuntimeError(f"API respondeu {res['status']} na busca '{termo}': {res['body'][:150]}")
                hits = json.loads(res["body"]).get("hits", [])
                for h in hits:
                    itens[h["id"]] = h
                page.wait_for_timeout(PAUSA_MS)
                if len(hits) < SIZE:
                    break
        browser.close()
    return list(itens.values())


def preco_atual(h):
    pr = h["pricing"]
    return pr["promotionalPrice"] if pr.get("promotion") else pr["price"]


def criar_tabelas(db):
    db.execute(
        "create table if not exists precos("
        "dia text, id text, nome text, marca text, preco_cheio real, "
        "preco_atual real, desconto real, estoque integer, "
        "primary key(dia, id))"
    )
    # um registro por dia para cada produto que casou com um item da dieta
    db.execute(
        "create table if not exists dieta("
        "dia text, item text, id text, nome text, preco real, por_unidade real, "
        "primary key(dia, id))"
    )
    db.execute("create table if not exists alertas(dia text, id text, por_unidade real)")


def salvar(db, hoje, itens):
    for h in itens:
        pr = h["pricing"]
        db.execute(
            "insert or replace into precos values(?,?,?,?,?,?,?,?)",
            (hoje, h["id"], h["name"], h.get("brandName"), pr["price"],
             preco_atual(h), pr.get("discount"), h.get("quantity", {}).get("inStock")),
        )
    db.commit()


def preco_bom(db, hoje, alvo, por_un):
    """True se o preço por kg/L/ovo está bom: abaixo do menor já pago, ou perto do menor dos 30 dias."""
    ref = alvo["ref"]
    if ref and por_un < ref - 0.005:  # mais barato de verdade, não igual
        return True
    # Histórico do item (qualquer marca): o mais barato de cada dia nos últimos 30 dias.
    dias, menor, media = db.execute(
        "select count(*), min(m), avg(m) from ("
        " select dia, min(por_unidade) m from dieta"
        " where item=? and dia<? and dia>=date(?, '-30 day') group by dia)",
        (alvo["nome"], hoje, hoje),
    ).fetchone()
    # Perto do menor dos 30 dias (até TOLERANCIA acima) E abaixo da média: R$ 9,10 conta
    # mesmo que o recorde tenha sido R$ 9,05; um preço que nunca muda não conta.
    return bool(dias >= MIN_DIAS_HISTORICO and menor
                and por_un <= menor * (1 + TOLERANCIA) and por_un < media - 0.005)


def analisar(db, hoje, itens):
    """Grava os produtos da dieta e devolve um resumo por item (na ordem de ALVOS):
    {alvo, melhor, promo, bom} onde melhor/promo = (por_un, preço, preço cheio, nome) ou None."""
    resumo = {a["nome"]: {"alvo": a, "melhor": None, "promo": None} for a in ALVOS}
    for h in sorted(itens, key=lambda h: h["name"]):
        alvo = casar(h["name"])
        if alvo is None:
            continue
        atual, cheio = preco_atual(h), h["pricing"]["price"]
        qtd = quantidade(h["name"], alvo["unidade"])
        por_un = atual / qtd if qtd else atual
        db.execute("insert or replace into dieta values(?,?,?,?,?,?)",
                   (hoje, alvo["nome"], h["id"], h["name"], atual, por_un))
        print(f"  [{alvo['nome']}] {h['name']}: R$ {brl(atual)} = R$ {brl(por_un)}/{alvo['unidade']}"
              + (f" (menor pago R$ {brl(alvo['ref'])})" if alvo["ref"] else ""))
        r, opcao = resumo[alvo["nome"]], (por_un, atual, cheio, h["name"])
        if r["melhor"] is None or por_un < r["melhor"][0]:
            r["melhor"] = opcao
        if atual < cheio - 0.005 and (r["promo"] is None or por_un < r["promo"][0]):
            r["promo"] = opcao
    for r in resumo.values():
        r["bom"] = bool(r["melhor"]) and preco_bom(db, hoje, r["alvo"], r["melhor"][0])
        r["promo_bom"] = bool(r["promo"]) and preco_bom(db, hoje, r["alvo"], r["promo"][0])
    db.commit()
    return list(resumo.values())


# Nomes curtos para caber na tabela do WhatsApp (celular mostra ~32 letras por linha).
CURTO = {"Peito de frango c/ osso": "Frango c/osso", "Mussarela fatiada": "Mussarela",
         "Extrato de tomate": "Extrato tom.", "Iogurte natural": "Iogurte nat.",
         "Leite integral": "Leite", "Batata inglesa": "Batata", "Pão de forma": "Pão de forma"}
DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _nome_curto(nome, alvo, limite=30):
    """Tira do nome do produto as palavras que já estão no item: 'Iogurte Integral Salubi Natural 750G'
    vira 'Integral Salubi 750G' quando o item é Iogurte natural."""
    chaves = set(alvo["incluir"]) | {w for i in alvo["inicio"] for w in i.split()}
    fica = [p for p in nome.split()
            if not any(norm(p).startswith(c) for c in chaves) and norm(p) not in ("de", "do", "da", "com")]
    txt = " ".join(fica) or nome
    return txt if len(txt) <= limite else txt[:limite - 1] + "…"


def montar_mensagem(resumo, data):
    """Mensagem do WhatsApp: promoções que valem no topo, depois preço bom sem promoção,
    depois uma tabela com o resto. Formatação do WhatsApp: *negrito*, _itálico_, ~riscado~,
    ``` bloco de largura fixa ```."""
    un = lambda a: "ovo" if a["unidade"] == "ovo" else a["unidade"]
    promo = [r for r in resumo if r["promo_bom"]]
    bons = [r for r in resumo if not r["promo_bom"] and r["bom"]]
    resto = [r for r in resumo if not r["promo_bom"] and not r["bom"]]

    linhas = [f"🛒 *Preços Koch · {DIAS_SEMANA[data.weekday()]} {data:%d/%m}*", ""]
    if promo:
        linhas.append(f"🔥 *PROMOÇÃO QUE VALE A PENA* ({len(promo)})")
        for r in promo:
            por_un, atual, cheio, nome = r["promo"]
            a = r["alvo"]
            linhas.append(f"• *{a['nome']}* R$ {brl(atual)} ~{brl(cheio)}~")
            linhas.append(f"   _{_nome_curto(nome, a)} · {brl(por_un)}/{un(a)}_")
        linhas.append("")
    if bons:
        linhas.append(f"✅ *PREÇO BOM, SEM PROMOÇÃO* ({len(bons)})")
        for r in bons:
            por_un, atual, cheio, nome = r["melhor"]
            a = r["alvo"]
            linhas.append(f"• *{a['nome']}* R$ {brl(por_un)}/{un(a)}")
            detalhe = _nome_curto(nome, a)
            if norm(detalhe).strip() not in ("kg", ""):
                linhas.append(f"   _{detalhe}_")
        linhas.append("")
    if resto:
        linhas.append("📋 *DEMAIS ITENS*")
        linhas.append("```")
        linhas.append(f"{'ITEM':<14}{'HOJE':>10}{'PAGO':>7}")
        for r in resto:
            a = r["alvo"]
            hoje_txt = f"{brl(r['melhor'][0])}/{un(a)}" if r["melhor"] else "—"
            nome = CURTO.get(a["nome"], a["nome"])[:13] + ("*" if r["promo"] else "")
            linhas.append(f"{nome:<14}{hoje_txt:>10}{brl(a['ref']) if a['ref'] else '—':>7}")
        linhas.append("```")
        linhas.append("_Preços por kg, litro ou ovo. * = tem promoção, mas não compensa._")
    linhas += ["", "_Promoção que vale e preço bom = mais barato que o menor que você já pagou._"]
    return "\n".join(linhas).strip()


def gerar_js(db, hoje):
    """Escreve ofertas-koch.js: por item da dieta, a melhor oferta de hoje e o menor preço em 30 dias."""
    linhas = []
    for a in ALVOS:
        hoje_row = db.execute(
            "select nome, preco, por_unidade from dieta where item=? and dia=? "
            "order by por_unidade limit 1", (a["nome"], hoje)).fetchone()
        min_row = db.execute(
            "select nome, preco, por_unidade, dia from dieta where item=? and dia>=date(?, '-30 day') "
            "order by por_unidade, dia desc limit 1", (a["nome"], hoje)).fetchone()
        linhas.append({
            "item": a["nome"],
            "unidade": a["unidade"],
            "ref": round(a["ref"], 2) if a["ref"] else None,
            "hoje": {"produto": hoje_row[0], "preco": hoje_row[1], "porUnidade": round(hoje_row[2], 2)} if hoje_row else None,
            "menor30": {"produto": min_row[0], "preco": min_row[1], "porUnidade": round(min_row[2], 2),
                        "dia": min_row[3]} if min_row else None,
        })
    dados = {"atualizado": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), "itens": linhas}
    with open(JS_SAIDA, "w", encoding="utf-8") as f:
        f.write("// Gerado por get-ofertas.py; não edite à mão.\nwindow.OFERTAS_KOCH = "
                + json.dumps(dados, ensure_ascii=False, indent=1) + ";\n")


def _abrir_wa(p):
    # perfil persistente: a sessão do WhatsApp Web fica salva na pasta wa_perfil
    return p.chromium.launch_persistent_context(WA_PERFIL, **_opcoes(headless=False, locale="pt-BR"))


def login_whatsapp():
    """Rode uma vez: abre o WhatsApp Web para você escanear o QR code."""
    with sync_playwright() as p:
        ctx = _abrir_wa(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com/")
        input("Escaneie o QR, espere as conversas carregarem e aperte Enter aqui... ")
        ctx.close()


def enviar_whatsapp(texto):
    """Abre a conversa e digita a mensagem linha a linha (Shift+Enter quebra a linha sem enviar).
    Digitar, em vez de mandar o texto no link, não tem limite de tamanho."""
    with sync_playwright() as p:
        ctx = _abrir_wa(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"https://web.whatsapp.com/send?phone={WA_NUMERO}")
        # se a sessão expirou, aparece o QR e esta espera estoura o tempo
        caixa = page.locator('footer div[contenteditable="true"]')
        caixa.wait_for(timeout=90000)
        page.wait_for_timeout(1500)
        caixa.click()
        for i, linha in enumerate(texto.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if linha:
                page.keyboard.insert_text(linha)
        page.wait_for_timeout(800)
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)
        ctx.close()


def notificar(texto, enviar=True):
    print(texto)
    if not (enviar and WA_NUMERO):
        return
    try:
        enviar_whatsapp(texto)
    except Exception as e:
        print(f"(falha ao enviar no WhatsApp: {e})")


def main(enviar=True):
    hoje = datetime.date.today().isoformat()
    try:
        itens = coletar()
        if len(itens) < MIN_ITENS:
            raise RuntimeError(f"só {len(itens)} itens coletados, algo mudou no site")
        db = sqlite3.connect(DB)
        criar_tabelas(db)
        salvar(db, hoje, itens)
        resumo = analisar(db, hoje, itens)
        gerar_js(db, hoje)
        db.close()
        print(f"{len(itens)} produtos dos itens da dieta gravados; ofertas-koch.js atualizado.\n")
        notificar(montar_mensagem(resumo, datetime.date.today()), enviar)
    except Exception as e:
        notificar(f"⚠️ Coleta do Koch falhou: {e}", enviar)
        sys.exit(1)


if __name__ == "__main__":
    if "--login-whatsapp" in sys.argv:
        login_whatsapp()
    else:
        main(enviar="--sem-aviso" not in sys.argv)
