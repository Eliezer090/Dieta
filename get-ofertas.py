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
    python get-ofertas.py --refazer    refaz a análise com os preços já coletados hoje (depois de
                                       mudar ALVOS/EXTRAS), sem ir ao site; junte --sem-aviso para testar
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

# Itens da dieta. Cada item compara TODAS as variações que servem para a mesma coisa
# (todo corte de frango, toda carne bovina magra, alho com casca ou descascado...)
# pelo preço do que realmente se aproveita: preço ÷ aproveitamento.
# Ex.: coxa e sobrecoxa a R$ 9,00/kg com 60% de carne = R$ 15,00 o kg limpo; sassami a
# R$ 14,00 (100% carne) ganha.
#
# Campos:
#   nome      : como aparece na mensagem e na aba Preços
#   buscas    : termos pesquisados no site (padrão: o 1º "inicio" da 1ª variante)
#   variantes : V(inicio, aprov, tem). O nome do produto (sem acento) precisa COMEÇAR por uma
#               das expressões de "inicio" e ter todas as palavras de "tem" (começo de palavra).
#               A primeira variante que casar define o aproveitamento (aprov: 1 = sem perda).
#   inicio/incluir/aprov : atalho para item de uma variante só
#   excluir   : começos de palavra que NÃO podem estar no nome
#   ref       : menor preço já pago no Komprão (tabela PRECOS do index.html), por kg/L/ovo
#   ref_aprov : aproveitamento do que você comprou (peito com osso = 0,75)
#   unidade   : "kg"/"L" = por kg/litro (lê "500g", "1L" no nome; sem isso, o preço já é por kg)
#               "ovo" = por ovo (lê "c/30", "20 un", "dz")
#   exibir    : (fator, rótulo) para mostrar em outra medida; pão = pacote de 400g
# A ordem importa: o primeiro item que casar vale (extrato de tomate antes de tomate).


def V(inicio, aprov=1.0, tem=()):
    return {"inicio": list(inicio), "aprov": aprov, "tem": list(tem)}


ALVOS = [
    {"nome": "Ovos", "buscas": ["ovos"], "inicio": ["ovo", "ovos"], "incluir": ["ovo"],
     "excluir": ["pascoa", "chocolate", "macarrao", "massa", "codorna", "po", "talharim"],
     "ref": 12.79 / 30, "unidade": "ovo"},
    # Carne bovina: moída pronta ou peça magra para moer (o açougue mói na hora).
    {"nome": "Carne bovina",
     "buscas": ["carne moida", "patinho", "acem", "musculo bovino", "coxao mole", "coxao duro",
                "paleta bovina", "lagarto bovino"],
     "variantes": [V(["carne moida", "carne bovina moida", "moida bovina"], 1.0),
                   V(["patinho", "coxao mole", "coxao duro", "lagarto", "carne bovina patinho"], 0.95),
                   V(["acem", "paleta", "musculo", "carne bovina acem", "carne bovina musculo"], 0.90)],
     "excluir": ["suin", "porco", "frango", "almondega", "hamburguer", "pure", "arroz", "perdigao",
                 "soja", "temperad", "recheado", "cordeiro", "ovelha", "cozid", "desfiad",
                 "escondidinho", "molho", "defumad", "costela", "osso"],
     "ref": 19.13, "unidade": "kg"},
    # Frango: qualquer corte. Aproveitamento = carne sem osso e sem pele.
    {"nome": "Frango",
     "buscas": ["peito de frango", "file de peito de frango", "sassami", "coxa e sobrecoxa",
                "sobrecoxa", "frango congelado", "frango a passarinho", "coxinha da asa"],
     "variantes": [V(["peito de frango", "peito frango"], 1.0, tem=["sem", "osso"]),
                   V(["file de peito", "file peito", "filezinho", "sassami"], 1.0),
                   V(["peito de frango", "peito frango"], 0.75),
                   V(["sobrecoxa"], 0.65),
                   V(["coxa e sobrecoxa", "coxa de frango", "coxa frango", "coxas"], 0.60),
                   V(["coxinha da asa", "asa de frango", "asinha", "meio da asa", "tulipa"], 0.50),
                   V(["frango"], 0.60)],  # inteiro, a passarinho
     "excluir": ["desfiad", "cubos", "tiras", "grelhad", "empanad", "defumad", "temperad",
                 "tempero", "cozid", "padaria", "chester", "assad", "fatiad", "nuggets",
                 "recheado", "milanesa", "espeto", "linguica", "hamburguer", "almondega",
                 "steak", "crocante", "marinad", "coracao", "figado", "moela", "pizza", "salsicha"],
     "ref": 13.49, "ref_aprov": 0.75, "unidade": "kg"},
    {"nome": "Iogurte natural", "inicio": ["iogurte"], "incluir": ["iogurte", "natural"],
     "excluir": ["protein", "baunilha", "grego", "desnatad", "zero", "polpa", "fruta", "morango",
                 "coco", "mel"],
     "ref": 3.69 / 0.160, "unidade": "kg"},
    {"nome": "Leite integral", "inicio": ["leite"], "incluir": ["leite", "integral"],
     "excluir": ["po", "condensad", "creme", "doce", "fermentad", "coco"],
     "ref": 4.49, "unidade": "L"},
    # Fatiada ou em peça (peça você fatia em casa, sem perda).
    {"nome": "Mussarela", "inicio": ["queijo mussarela", "mussarela", "queijo muss"],
     "incluir": ["mussarela"],
     "excluir": ["stick", "burrata", "vegan", "fit", "ralad", "palito", "bufala", "bufalo"],
     "ref": 14.99 / 0.300, "unidade": "kg"},
    {"nome": "Requeijão", "inicio": ["requeijao"], "incluir": ["requeijao"],
     "excluir": ["cheddar", "sabor", "ervas", "bufal", "azul", "brie", "queijos", "culinari"],
     "ref": 14.99 / 0.400, "unidade": "kg"},
    # Pão de forma de qualquer marca/tipo; comparado no tamanho de 400g que você compra.
    {"nome": "Pão de forma", "inicio": ["pao de forma", "pao forma"], "incluir": ["pao", "forma"],
     "excluir": ["hot", "bisnag", "batatinha"],
     "ref": 6.99 / 0.400, "unidade": "kg", "exibir": (0.4, "400g")},
    {"nome": "Batata-doce", "inicio": ["batata doce", "batata-doce"], "incluir": ["batata", "doce"],
     "excluir": ["chips", "palha", "frita", "congelad"],
     "ref": 5.79, "unidade": "kg"},
    {"nome": "Batata inglesa", "inicio": ["batata"], "incluir": ["batata"],
     "excluir": ["salsa", "lays", "ruffles", "slice", "yokitos", "bem", "doce", "palha", "frita",
                 "chips", "congelad", "pre", "pringles", "ondulada", "smiles", "noisette"],
     "ref": 6.49, "unidade": "kg"},
    {"nome": "Abobrinha", "inicio": ["abobrinha"], "incluir": ["abobrinha"], "excluir": ["conserva"],
     "ref": 6.79, "unidade": "kg"},
    {"nome": "Cebola", "inicio": ["cebola"], "incluir": ["cebola"],
     "excluir": ["granulad", "picada", "crispy", "kitano", "spices", "verde", "frita", "creme",
                 "sopa", "cebolinha", "po", "desidratad"],
     "ref": 9.99, "unidade": "kg"},
    {"nome": "Cenoura", "inicio": ["cenoura"], "incluir": ["cenoura"], "excluir": ["bolo", "ralad"],
     "ref": 4.99, "unidade": "kg"},
    # Alho: a granel (com casca, ~85% aproveitável) ou descascado em pacote (100%).
    {"nome": "Alho", "buscas": ["alho"],
     "variantes": [V(["alho kg", "alho granel", "alho cabeca", "alho nobre", "alho roxo kg",
                      "alho branco kg"], 0.85),
                   V(["alho"], 1.0)],
     "excluir": ["poro", "granulad", "triturad", "oleo", "cebola", "spices", "kitano", "frito",
                 "pasta", "sal", "tempero", "po", "negro"],
     "ref": 24.99, "ref_aprov": 0.85, "unidade": "kg"},
    # Só extrato/concentrado de tomate; molho pronto não entra.
    {"nome": "Extrato de tomate", "buscas": ["extrato de tomate", "tomate concentrado"],
     "variantes": [V(["extrato", "tomate concentrado", "concentrado de tomate"], 1.0, tem=["tomat"])],
     "excluir": ["sabores", "strogonoff", "panela", "cebola", "alho", "pizza", "manjericao", "molho"],
     "ref": 13.79 / 1.02, "unidade": "kg"},
    {"nome": "Tomate", "inicio": ["tomate"], "incluir": ["tomate"],
     "excluir": ["triturad", "molho", "seco", "pelado", "polpa", "ketchup", "extrato", "concentrad",
                 "passata", "conserva"],
     "ref": 22.99, "unidade": "kg"},
    {"nome": "Banana", "inicio": ["banana"], "incluir": ["banana"],
     "excluir": ["chips", "passa", "doce", "bolo", "acucar", "desidratad", "chip"],
     "ref": 4.99, "unidade": "kg"},
    {"nome": "Mamão", "inicio": ["mamao"], "incluir": ["mamao"], "excluir": ["doce", "desidratad"],
     "ref": 11.79, "unidade": "kg"},
    {"nome": "Manga", "inicio": ["manga"], "incluir": ["manga"],
     "excluir": ["suco", "nectar", "desidratad", "doce", "polpa"],
     "ref": 4.99, "unidade": "kg"},
    {"nome": "Beterraba", "inicio": ["beterraba"], "incluir": ["beterraba"], "excluir": ["conserva"],
     "ref": 5.79, "unidade": "kg"},
    {"nome": "Repolho", "inicio": ["repolho"], "incluir": ["repolho"], "excluir": ["roxo"],
     "ref": None, "unidade": "kg"},
    {"nome": "Aveia", "inicio": ["aveia", "flocos de aveia", "farelo de aveia"], "incluir": ["aveia"],
     "excluir": ["barra", "bebida", "biscoito", "cookie"],
     "ref": None, "unidade": "kg"},
    {"nome": "Tapioca", "inicio": ["tapioca", "massa de tapioca", "goma de tapioca", "goma para tapioca"],
     "incluir": ["tapioca"], "excluir": ["biscoito"],
     "ref": None, "unidade": "kg"},
    {"nome": "Café", "inicio": ["cafe"], "incluir": ["cafe"],
     "excluir": ["grao", "espresso", "capsula", "cappuccino", "filtro", "leite", "soluvel", "doce", "bolo"],
     "ref": None, "unidade": "kg"},
]
# ref None = nunca comprado no Komprão (ou só por unidade, caso do repolho): só avisa pelo histórico de 30 dias.

for _a in ALVOS:  # completa os atalhos
    _a.setdefault("variantes", [V(_a.get("inicio", []), _a.get("aprov", 1.0), _a.get("incluir", ()))])
    _a.setdefault("buscas", [_a["variantes"][0]["inicio"][0]])
    _a.setdefault("excluir", [])
    _a.setdefault("ref_aprov", 1.0)
    _a["limpo"] = any(v["aprov"] < 1 for v in _a["variantes"]) or _a["ref_aprov"] < 1
    # referência no mesmo critério da comparação (por kg limpo, quando há perda)
    _a["ref_cmp"] = _a["ref"] / _a["ref_aprov"] if _a["ref"] else None


def fator(a):
    """Multiplicador para mostrar o preço (pão: kg -> pacote de 400g)."""
    return a["exibir"][0] if a.get("exibir") else 1.0


def rotulo(a):
    """Unidade mostrada: 'kg', 'L', 'ovo', '400g' ou 'kg limpo'."""
    if a.get("exibir"):
        return a["exibir"][1]
    return a["unidade"] + (" limpo" if a["limpo"] else "")


# Demais itens da tabela de preços (index.html) que não são da dieta: limpeza, higiene,
# temperos, frutas... Procura o MESMO produto (mesma marca) e converte para o tamanho da
# linha (detergente de 1L -> preço de 500ml). Campos:
#   linha  : nome EXATO da linha na tabela PRECOS do index.html (o preço pago vem de lá)
#   busca  : termo pesquisado no site
#   inicio : (opcional) o nome do produto precisa começar por uma destas expressões
#   tem    : começos de palavra que todos precisam estar no nome
#   excluir: começos de palavra proibidos
#   tam    : tamanho da linha, na unidade (kg, L ou un = unidades/rolos)
#   unidade: "kg", "L" ou "un". Sem tamanho no nome: kg/L = preço já por kg; un = mesma embalagem
EXTRAS = [
    {"linha": "Requeijão Tirol cheddar, 180g", "busca": "requeijao cheddar", "tem": ["requeijao", "tirol", "cheddar"], "tam": 0.18, "unidade": "kg"},
    {"linha": "Cebolinha verde, maço", "busca": "cebolinha", "inicio": ["cebolinha"], "excluir": ["desidratad", "picad", "pote", "granulad", "flocos", "pacal", "kitano", "spices", "santa", "pramesa"], "tam": 1, "unidade": "un"},
    {"linha": "Pimentão vermelho, kg", "busca": "pimentao vermelho", "inicio": ["pimentao vermelho"], "excluir": ["conserva", "po", "assad"], "tam": 1, "unidade": "kg"},
    {"linha": "Repolho verde, unidade", "busca": "repolho", "inicio": ["repolho"], "tem": ["un"], "excluir": ["roxo"], "tam": 1, "unidade": "un"},
    {"linha": "Salsa, maço", "busca": "salsa maco", "inicio": ["salsa", "salsinha", "cheiro verde"], "excluir": ["desidratad", "flocos", "po", "pote", "picad", "kg"], "tam": 1, "unidade": "un"},
    {"linha": "Abacaxi pérola, unidade", "busca": "abacaxi", "inicio": ["abacaxi"], "excluir": ["suco", "polpa", "calda", "desidratad", "doce", "gelatina", "kg", "picad", "fatiad", "nectar"], "tam": 1, "unidade": "un"},
    {"linha": "Coco verde, unidade", "busca": "coco verde", "inicio": ["coco verde"], "excluir": ["agua", "ralad", "kg"], "tam": 1, "unidade": "un"},
    {"linha": "Melão amarelo, kg", "busca": "melao amarelo", "inicio": ["melao amarelo", "melao"], "excluir": ["sapo", "cantaloupe", "orange", "galia", "pele"], "tam": 1, "unidade": "kg"},
    {"linha": "Massa Rap10 integral (wrap), 297g", "busca": "rap10 integral", "tem": ["rap", "integral"], "tam": 0.297, "unidade": "kg"},
    {"linha": "Fermento Royal, 100g", "busca": "fermento royal", "tem": ["fermento", "royal"], "tam": 0.1, "unidade": "kg"},
    {"linha": "Milho verde Predilecta, lata 170g", "busca": "milho verde predilecta", "tem": ["milho", "predilecta"], "excluir": ["pipoca", "creme", "ervilha"], "tam": 0.17, "unidade": "kg"},
    {"linha": "Milho de pipoca Yoki, 400g", "busca": "milho de pipoca yoki", "tem": ["milho", "pipoca", "yoki"], "excluir": ["micro"], "tam": 0.4, "unidade": "kg"},
    {"linha": "Pepino Zilse burger fatiado, 330g", "busca": "pepino zilse", "tem": ["pepino", "zilse"], "tam": 0.33, "unidade": "kg"},
    {"linha": "Canela em pó Pacal, 27g", "busca": "canela pacal", "tem": ["canela", "pacal"], "excluir": ["casca", "pau", "rama"], "tam": 0.027, "unidade": "kg"},
    {"linha": "Chimichurri Pacal, 5g", "busca": "chimichurri pacal", "tem": ["chimichurri", "pacal"], "tam": 0.005, "unidade": "kg"},
    {"linha": "Tempero Kitano mix para frango, 40g", "busca": "kitano frango", "tem": ["kitano", "frango"], "tam": 0.04, "unidade": "kg"},
    {"linha": "Café solúvel L'Or Classique, 40g", "busca": "cafe soluvel l'or", "tem": ["soluvel", "classique"], "tam": 0.04, "unidade": "kg"},
    {"linha": "Chá Leão ervas hortelã, 15g", "busca": "cha leao hortela", "tem": ["cha", "leao", "hortela"], "tam": 0.015, "unidade": "kg"},
    {"linha": "Chá Leão Relaxa camomila, 16g", "busca": "cha leao camomila", "tem": ["cha", "leao", "camomila"], "tam": 0.016, "unidade": "kg"},
    {"linha": "Geleia Linea sem açúcar frutas vermelhas, 230g", "busca": "geleia linea", "tem": ["geleia", "linea", "vermelh"], "tam": 0.23, "unidade": "kg"},
    {"linha": "Biscoito Trakinas recheado, 126g", "busca": "trakinas", "tem": ["trakinas"], "excluir": ["mini", "kit"], "tam": 0.126, "unidade": "kg"},
    {"linha": "Pipoca de micro-ondas Yoki, 160g", "busca": "pipoca micro-ondas yoki", "tem": ["pipoca", "micro", "yoki"], "tam": 0.16, "unidade": "kg"},
    {"linha": "Pudim Dr. Oetker diet chocolate, 25g", "busca": "pudim oetker diet", "tem": ["pudim", "oetker", "diet"], "tam": 0.025, "unidade": "kg"},
    {"linha": "Refresco Clight, 8g", "busca": "clight", "tem": ["clight"], "tam": 0.008, "unidade": "kg"},
    {"linha": "Macarrão instantâneo Nissin galinha caipira, 80g", "busca": "nissin galinha caipira", "tem": ["nissin", "caipira"], "excluir": ["cup", "copo"], "tam": 0.08, "unidade": "kg"},
    {"linha": "Petit gateau Mr. Bey chocolate, 240g", "busca": "petit gateau mr bey", "tem": ["petit", "bey"], "tam": 0.24, "unidade": "kg"},
    {"linha": "Papel higiênico Duetto 20m, 18 rolos", "busca": "papel higienico duetto", "tem": ["papel", "higienico", "duetto"], "tam": 18, "unidade": "un"},
    {"linha": "Lava-roupas Girando Sol, 3L", "busca": "lava roupas girando sol", "tem": ["girando", "sol"], "excluir": ["amaciante", "barra", "po", "pasta", "tira", "limpador", "detergente", "desinfetante", "multiuso"], "tam": 3, "unidade": "L"},
    {"linha": "Detergente Suprema neutro, 500ml", "busca": "detergente suprema", "tem": ["detergente", "suprema"], "excluir": ["kit", "caixa"], "tam": 0.5, "unidade": "L"},
    {"linha": "Esponja Condor multiuso, 4 un", "busca": "esponja condor", "tem": ["esponja", "condor"], "excluir": ["aco", "banho", "inox", "maquiagem"], "tam": 4, "unidade": "un"},
    {"linha": "Kit shampoo + condicionador Tresemmé, 525ml", "busca": "kit tresemme", "tem": ["kit", "tresemme"], "tam": 0.525, "unidade": "L"},
    {"linha": "Desodorante Dove creme, 50g", "busca": "desodorante dove creme", "tem": ["desodorante", "dove", "creme"], "excluir": ["aerosol", "roll", "kit"], "tam": 0.05, "unidade": "kg"},
    {"linha": "Sabonete Protex, 85g", "busca": "sabonete protex", "tem": ["sabonete", "protex"], "excluir": ["liquido", "refil", "intimo", "leve", "kit", "pack", "unidades"], "tam": 0.085, "unidade": "kg"},
    {"linha": "Toalha umedecida Piquitucho, 140 un", "busca": "toalha umedecida piquitucho", "tem": ["piquitucho"], "tam": 140, "unidade": "un"},
    {"linha": "Repelente Raid 45 noites, refil", "busca": "raid 45 noites refil", "tem": ["raid", "45", "refil"], "tam": 1, "unidade": "un"},
]


def _ler_precos_pagos():
    """Último preço pago de cada linha da tabela PRECOS do index.html: {linha: preço}."""
    try:
        html = open(os.path.join(PASTA, "index.html"), encoding="utf-8").read()
    except OSError:
        return {}
    pagos = {}
    for linha, vals in re.findall(r'\["[^"]+",\s*"([^"]+)",\s*\[([^\]]*)\]\]', html):
        nums = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", vals)]
        if nums:
            pagos[linha] = nums[-1]
    return pagos


PAGOS = _ler_precos_pagos()
for _e in EXTRAS:
    _e.setdefault("inicio", [])
    _e.setdefault("excluir", [])
    _e.setdefault("tem", [])
    _e["pago"] = PAGOS.get(_e["linha"])


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
    if unidade == "un":  # rolos, unidades, folhas: '18 Rolos', 'c/4', '140 un'
        m = re.search(r"(?:c/|com|leve)\s*(\d+)|(\d+)\s*(?:un\b|unid|rolos?|folhas|lencos)", n)
        if m:
            return float(m.group(1) or m.group(2))
    return None


def casar_extra(nome):
    """Devolve o item de EXTRAS que casa com o nome do produto, ou None."""
    palavras = re.findall(r"[a-z0-9]+", norm(nome))
    texto = " ".join(palavras) + " "
    tem = lambda pre: any(p.startswith(pre) for p in palavras)
    for e in EXTRAS:
        if e["inicio"] and not any(texto.startswith(i + " ") for i in e["inicio"]):
            continue
        if all(tem(t) for t in e["tem"]) and not any(tem(x) for x in e["excluir"]):
            return e
    return None


def preco_equivalente(nome, atual, e):
    """Preço convertido para o tamanho da linha da tabela (detergente 1L -> preço de 500ml)."""
    qtd = quantidade(nome, e["unidade"])
    if qtd is None:  # sem tamanho no nome: kg/L = preço já por kg; un = mesma embalagem da linha
        return atual if e["unidade"] == "un" else atual * e["tam"]
    return atual / qtd * e["tam"]


def casar(nome):
    """Devolve (alvo, aproveitamento) do item que casa com o nome do produto, ou (None, None)."""
    palavras = re.findall(r"[a-z0-9]+", norm(nome))
    texto = " ".join(palavras) + " "
    tem = lambda pre: any(p.startswith(pre) for p in palavras)
    for a in ALVOS:
        if any(tem(e) for e in a["excluir"]):
            continue
        for v in a["variantes"]:
            comeca = any(texto.startswith(i.replace("-", " ") + " ") for i in v["inicio"])
            if comeca and all(tem(t) for t in v["tem"]):
                return a, v["aprov"]
    return None, None


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
        # Buscas de cada item da dieta (com e sem promoção): pega também o preço normal.
        # Itens da dieta: até 2 páginas (muitas marcas/cortes). Demais itens: 1 página basta,
        # porque a busca já tem marca e tipo e os mais relevantes vêm primeiro.
        termos = {t: PAGINAS_POR_BUSCA for a in ALVOS for t in a["buscas"]}
        for e in EXTRAS:
            termos.setdefault(e["busca"], 1)
        for termo, paginas in termos.items():
            for frm in range(0, paginas * SIZE, SIZE):
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
    # dieta = produtos de "precos" que casam com um item da dieta. É refeita do zero a cada
    # execução (reprocessar), então mudar as regras de ALVOS vale também para o histórico.
    # por_unidade = preço comparável: por kg limpo / L / ovo.
    db.execute("drop table if exists dieta")
    db.execute(
        "create table dieta("
        "dia text, item text, id text, nome text, preco real, cheio real, aprov real, "
        "por_unidade real, primary key(dia, id))"
    )
    # extras = demais itens da tabela de preços (limpeza, higiene...), também refeita do zero.
    # equiv = preço convertido para o tamanho da linha da tabela.
    db.execute("drop table if exists extras")
    db.execute(
        "create table extras("
        "dia text, linha text, id text, nome text, preco real, cheio real, equiv real, "
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


def reprocessar(db):
    """Refaz a tabela dieta a partir de todos os preços já coletados, com as regras atuais."""
    db.execute("delete from dieta")
    db.execute("delete from extras")
    for dia, pid, nome, atual, cheio in db.execute(
            "select dia, id, nome, preco_atual, preco_cheio from precos").fetchall():
        e = casar_extra(nome)
        if e:
            db.execute("insert or replace into extras values(?,?,?,?,?,?,?)",
                       (dia, e["linha"], pid, nome, atual, cheio, preco_equivalente(nome, atual, e)))
        alvo, aprov = casar(nome)
        if alvo is None:
            continue
        qtd = quantidade(nome, alvo["unidade"])
        por_un = (atual / qtd if qtd else atual) / aprov
        db.execute("insert or replace into dieta values(?,?,?,?,?,?,?,?)",
                   (dia, alvo["nome"], pid, nome, atual, cheio, aprov, por_un))
    db.commit()


def preco_bom(db, hoje, alvo, por_un):
    """True se o preço comparável está bom: abaixo do menor já pago, ou perto do menor dos 30 dias."""
    ref = alvo["ref_cmp"]
    if ref and por_un < ref - 0.005:  # mais barato de verdade, não igual
        return True
    # Histórico do item (qualquer marca/corte): o mais barato de cada dia nos últimos 30 dias.
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


def analisar(db, hoje):
    """Resumo por item (na ordem de ALVOS): {alvo, melhor, promo, bom, promo_bom}
    onde melhor/promo = (por_un, preço, preço cheio, nome) ou None."""
    resumo = {a["nome"]: {"alvo": a, "melhor": None, "promo": None} for a in ALVOS}
    for item, nome, atual, cheio, aprov, por_un in db.execute(
            "select item, nome, preco, cheio, aprov, por_unidade from dieta where dia=? order by nome",
            (hoje,)):
        a = resumo[item]["alvo"]
        print(f"  [{item}] {nome}: R$ {brl(atual)} = R$ {brl(por_un * fator(a))}/{rotulo(a)}"
              + (f" (aproveita {aprov:.0%})" if aprov < 1 else ""))
        r, opcao = resumo[item], (por_un, atual, cheio, nome)
        if r["melhor"] is None or por_un < r["melhor"][0]:
            r["melhor"] = opcao
        if atual < cheio - 0.005 and (r["promo"] is None or por_un < r["promo"][0]):
            r["promo"] = opcao
    for r in resumo.values():
        r["bom"] = bool(r["melhor"]) and preco_bom(db, hoje, r["alvo"], r["melhor"][0])
        r["promo_bom"] = bool(r["promo"]) and preco_bom(db, hoje, r["alvo"], r["promo"][0])
    return list(resumo.values())


def analisar_extras(db, hoje):
    """Demais itens da tabela: o mais barato de hoje de cada linha (no tamanho da linha).
    Devolve [{e, produto, preco, cheio, equiv}] na ordem de EXTRAS, só os achados no site."""
    achados = []
    for e in EXTRAS:
        row = db.execute("select nome, preco, cheio, equiv from extras where dia=? and linha=? "
                         "order by equiv limit 1", (hoje, e["linha"])).fetchone()
        if row:
            achados.append({"e": e, "produto": row[0], "preco": row[1], "cheio": row[2], "equiv": row[3]})
            print(f"  [extra] {e['linha']}: {row[0]} R$ {brl(row[1])} = R$ {brl(row[3])} no tamanho da linha"
                  + (f" (pagou {brl(e['pago'])})" if e["pago"] else ""))
        else:
            print(f"  [extra] {e['linha']}: não achado no site")
    return achados


# Nomes curtos para caber na tabela do WhatsApp (celular mostra ~32 letras por linha).
CURTO = {"Extrato de tomate": "Extrato tom.", "Iogurte natural": "Iogurte nat.",
         "Leite integral": "Leite", "Batata inglesa": "Batata", "Carne bovina": "Carne"}
DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _nome_curto(nome, alvo, limite=32):
    """Item de uma variante só: tira do nome as palavras que já estão no item
    ('Iogurte Integral Salubi Natural 750G' -> 'Integral Salubi 750G'). Item com vários
    cortes/tipos (frango, carne, alho): mantém o nome inteiro, que diz qual corte é."""
    if len(alvo["variantes"]) == 1:
        v = alvo["variantes"][0]
        chaves = set(v["tem"]) | {w for i in v["inicio"] for w in i.split()}
        fica = [p for p in nome.split()
                if not any(norm(p).startswith(c) for c in chaves) and norm(p) not in ("de", "do", "da", "com")]
        txt = " ".join(fica) or nome
    else:  # tira só o nome do item: 'Coxa e Sobrecoxa De Frango Congelada' -> 'Coxa e Sobrecoxa Congelada'
        txt = nome
        for w in norm(alvo["nome"]).split():
            txt = re.sub(r"\s*\b(?:d[eoa]\s+)?" + w + r"\b", "", txt, flags=re.IGNORECASE).strip()
        txt = txt or nome
    return txt if len(txt) <= limite else txt[:limite - 1] + "…"


def _linha_produto(r, chave, riscado):
    """Duas linhas: '• *Frango* R$ 8,49 ~9,39~' e '   _Coxa e Sobrecoxa... · 14,15/kg limpo_'."""
    a = r["alvo"]
    por_un, atual, cheio, nome = r[chave]
    topo = f"• *{a['nome']}* R$ {brl(atual)}" + (f" ~{brl(cheio)}~" if riscado else "")
    detalhe = _nome_curto(nome, a)
    detalhe = "" if norm(detalhe).strip() in ("kg", "") else detalhe + " · "
    return [topo, f"   _{detalhe}{brl(por_un * fator(a))}/{rotulo(a)}_"]


def montar_mensagem(resumo, extras, data):
    """Mensagem do WhatsApp: promoções que valem no topo, depois preço bom sem promoção,
    depois uma tabela com o resto. Formatação do WhatsApp: *negrito*, _itálico_, ~riscado~,
    ``` bloco de largura fixa ```."""
    promo = [r for r in resumo if r["promo_bom"]]
    bons = [r for r in resumo if not r["promo_bom"] and r["bom"]]
    resto = [r for r in resumo if not r["promo_bom"] and not r["bom"]]

    linhas = [f"🛒 *Preços Koch · {DIAS_SEMANA[data.weekday()]} {data:%d/%m}*", ""]
    if promo:
        linhas.append(f"🔥 *PROMOÇÃO QUE VALE A PENA* ({len(promo)})")
        for r in promo:
            linhas += _linha_produto(r, "promo", riscado=True)
        linhas.append("")
    if bons:
        linhas.append(f"✅ *PREÇO BOM, SEM PROMOÇÃO* ({len(bons)})")
        for r in bons:
            linhas += _linha_produto(r, "melhor", riscado=False)
        linhas.append("")
    if resto:
        linhas.append("📋 *DEMAIS ITENS*")
        linhas.append("```")
        linhas.append(f"{'ITEM':<13}{'HOJE':>7}{'PAGO':>7} {'POR':<4}")
        for r in resto:
            a = r["alvo"]
            hoje_txt = brl(r["melhor"][0] * fator(a)) if r["melhor"] else "—"
            pago_txt = brl(a["ref_cmp"] * fator(a)) if a["ref_cmp"] else "—"
            nome = CURTO.get(a["nome"], a["nome"])[:12] + ("*" if r["promo"] else "")
            un = a["exibir"][1] if a.get("exibir") else a["unidade"]
            linhas.append(f"{nome:<13}{hoje_txt:>7}{pago_txt:>7} {un:<4}")
        linhas.append("```")
        linhas.append("_* = tem promoção, mas não compensa._")
    # Demais itens da tabela (limpeza, higiene...): só os que estão mais baratos que a última compra.
    baratos = [x for x in extras if x["e"]["pago"] and x["equiv"] < x["e"]["pago"] - 0.005]
    if baratos:
        linhas += ["", f"🧺 *OUTROS DA LISTA MAIS BARATOS* ({len(baratos)})"]
        for x in baratos:
            nome_linha = x["e"]["linha"].split(",")[0]
            promo = " 🔥" if x["preco"] < x["cheio"] - 0.005 else ""
            linhas.append(f"• *{nome_linha}* R$ {brl(x['equiv'])} _(pagou {brl(x['e']['pago'])})_{promo}")
    limpos = [a["nome"].lower() for a in ALVOS if a["limpo"]]
    linhas += ["",
               f"_{', '.join(limpos[:-1]).capitalize()} e {limpos[-1]}: preço por kg limpo (sem osso, pele "
               "ou casca), comparando todos os cortes e tipos. Pão: pacote de 400g._",
               "_Promoção que vale e preço bom = mais barato que o menor que você já pagou._"]
    return "\n".join(linhas).strip()


def gerar_js(db, hoje):
    """Escreve ofertas-koch.js: por item da dieta, a melhor oferta de hoje e a menor em 30 dias.
    porUnidade e ref vêm no critério da comparação (por kg limpo/L/ovo); a página multiplica
    por "fator" para mostrar (pão: pacote de 400g) e por aprovRef para converter ao produto
    que você compra (peito com osso)."""
    linhas = []
    for a in ALVOS:
        hoje_row = db.execute(
            "select nome, preco, por_unidade, aprov from dieta where item=? and dia=? "
            "order by por_unidade limit 1", (a["nome"], hoje)).fetchone()
        min_row = db.execute(
            "select nome, preco, por_unidade, dia from dieta where item=? and dia>=date(?, '-30 day') "
            "order by por_unidade, dia desc limit 1", (a["nome"], hoje)).fetchone()
        linhas.append({
            "item": a["nome"],
            "unidade": a["unidade"],
            "rotulo": rotulo(a),
            "fator": fator(a),
            "limpo": a["limpo"],
            "aprovRef": a["ref_aprov"],
            "ref": round(a["ref_cmp"], 4) if a["ref_cmp"] else None,
            "hoje": {"produto": hoje_row[0], "preco": hoje_row[1], "porUnidade": round(hoje_row[2], 4),
                     "aprov": hoje_row[3]} if hoje_row else None,
            "menor30": {"produto": min_row[0], "preco": min_row[1], "porUnidade": round(min_row[2], 4),
                        "dia": min_row[3]} if min_row else None,
        })
    extras = {}
    for linha, nome, preco, cheio, equiv in db.execute(
            "select linha, nome, preco, cheio, min(equiv) from extras where dia=? group by linha", (hoje,)):
        extras[linha] = {"produto": nome, "preco": preco, "cheio": cheio, "equiv": round(equiv, 2)}
    dados = {"atualizado": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), "itens": linhas,
             "extras": extras}
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


def main(enviar=True, ir_ao_site=True):
    hoje = datetime.date.today().isoformat()
    try:
        db = sqlite3.connect(DB)
        criar_tabelas(db)
        if ir_ao_site:
            itens = coletar()
            if len(itens) < MIN_ITENS:
                raise RuntimeError(f"só {len(itens)} itens coletados, algo mudou no site")
            salvar(db, hoje, itens)
        else:
            itens = db.execute("select id from precos where dia=?", (hoje,)).fetchall()
        reprocessar(db)
        resumo = analisar(db, hoje)
        extras = analisar_extras(db, hoje)
        gerar_js(db, hoje)
        db.close()
        print(f"{len(itens)} produtos dos itens da dieta gravados; ofertas-koch.js atualizado.\n")
        notificar(montar_mensagem(resumo, extras, datetime.date.today()), enviar)
    except Exception as e:
        notificar(f"⚠️ Coleta do Koch falhou: {e}", enviar)
        sys.exit(1)


if __name__ == "__main__":
    if "--login-whatsapp" in sys.argv:
        login_whatsapp()
    else:
        main(enviar="--sem-aviso" not in sys.argv, ir_ao_site="--refazer" not in sys.argv)
