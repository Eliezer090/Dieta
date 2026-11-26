"""
Busca no Koch (API sense.osuper.com.br, via Playwright) cada item da dieta, com e
sem promoção, compara (preço de referência = o menor que você já pagou no Komprão,
aba Preços do index.html), grava o histórico em SQLite, gera ofertas-koch.js (aba Preços)
e promos-koch.js (aba Promoções: todas as promoções do site) e avisa no WhatsApp quando vale comprar.

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
MAX_EMBALAGEM = 6         # genérico: embalagem até 6x o tamanho que você compra
TOLERANCIA = 0.05          # até 5% acima do menor preço dos 30 dias ainda conta como preço bom
# Ofertas fortes do site inteiro (fora da lista de vocês): todas as promoções do dia
CLUBE_K = 690              # "persona" do Clube K: preço com CPF cadastrado
DESC_FORTE = 0.40          # desconto real mínimo para entrar na lista
MIN_CAMPANHA = 5           # campanha ("Fraldas 50% OFF") vira uma linha só se tiver 5+ itens fortes
MAX_DESTAQUES = 15         # itens avulsos (fora de campanha) na mensagem
PROMO_EXCLUIR = ["Bebidas Alcoólicas", "Eletro"]   # categorias que não interessam
SITE_PROMOS = "https://eliezer090.github.io/Dieta/#promos"   # aba Promoções publicada (link na mensagem)
# Departamentos na mensagem, nesta ordem (os que não estão aqui vão no fim, em "Outros")
DEPTOS = [("Mercearia", "🛒"), ("Hortifruti", "🥦"), ("Açougue", "🥩"), ("Frios & Laticínios", "🧀"),
          ("Padaria & Confeitaria", "🍞"), ("Congelados", "🧊"), ("Alimentação Especial", "🌾"),
          ("Bebidas", "🥤"), ("Limpeza", "🧽"), ("Higiene & Beleza", "🧴"), ("Mundo Bebê", "👶"),
          ("Casa & Utilidades", "🏠"), ("Pet Care", "🐾")]
DEPTO_IGNORAR = ("BLACK FRIDAY", "Grandes Marcas")   # "departamentos" que são vitrines, não seções
HEADLESS = os.getenv("HEADLESS") == "1"   # HEADLESS=1 testa sem janela (pode ser bloqueado)
DB = os.path.join(PASTA, "ofertas.db")
JS_SAIDA = os.path.join(PASTA, "ofertas-koch.js")
JS_PROMOS = os.path.join(PASTA, "promos-koch.js")   # aba Promoções do index.html
DESC_VALE = DESC_FORTE     # aba Promoções: "vale a pena" = desconto real de DESC_FORTE+ ou menor preço já visto
WA_PERFIL = os.path.join(PASTA, "wa_perfil")
# Número que recebe os avisos (só dígitos, DDI+DDD+número). Fica no arquivo wa_numero.txt,
# fora do Git, para não publicar o telefone; a variável de ambiente WA_NUMERO também vale.
_arq_numero = os.path.join(PASTA, "wa_numero.txt")
WA_NUMERO = os.getenv("WA_NUMERO") or (
    open(_arq_numero, encoding="utf-8").read().strip() if os.path.exists(_arq_numero) else "")
# Usa o Google Chrome instalado no PC; NAVEGADOR=msedge usa o Edge; NAVEGADOR=chromium usa o do Playwright.
NAVEGADOR = os.getenv("NAVEGADOR", "chrome")


def _opcoes(visivel=False, **kw):
    """Opções de lançamento com o navegador escolhido (o Chromium do Playwright não precisa de channel).
    O navegador abre com janela de verdade (o site e o WhatsApp Web bloqueiam o modo sem janela com
    mais facilidade), mas fora da tela, para não aparecer. VISIVEL=1 mostra a janela (para depurar)."""
    if not kw.get("headless") and not visivel and os.getenv("VISIVEL") != "1":
        kw["args"] = ["--window-position=-32000,-32000"]
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
     "ref": 21.99 / 0.4, "unidade": "kg"},  # cupom 07/11: moída dianteiro Komprão, 400g por R$ 21,99
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
# temperos, frutas... Genéricos: qualquer marca; específicos: a mesma marca/produto.
# O preço é convertido para o tamanho da linha (detergente de 1L -> preço de 500ml).
# Todas as opções do dia ficam gravadas e aparecem ao clicar na linha da aba Preços. Campos:
#   linha  : nome EXATO da linha na tabela PRECOS do index.html (o preço pago vem de lá)
#   buscas : termos pesquisados no site
#   inicio : (opcional) o nome do produto precisa começar por uma destas expressões
#   tem    : começos de palavra que todos precisam estar no nome
#   qualquer: (opcional) pelo menos uma destas expressões no nome ("sem acucar", "diet"...)
#   excluir: começos de palavra proibidos
#   tam    : tamanho da linha, na unidade (kg, L, un = unidades, m = metros de papel)
#   unidade: "kg", "L", "un" ou "m" (rolos x metros)
#   granel : hortifrúti vendido por kg/unidade (sem tamanho no nome = preço do kg/unidade)
#   multi  : pacote com várias unidades de peso ("leve 6 pague 5 85g" = 6 x 85g)
#   especifico: mesma marca. Genérico sem tamanho no nome fica de fora (não dá para comparar);
#               específico sem tamanho = mesma embalagem da linha
EXTRAS = [
    # --- genéricos: qualquer marca, comparados pelo tamanho/medida ---
    {"linha": "Requeijão Tirol cheddar, 180g", "buscas": ["requeijao cheddar"], "tem": ["requeijao", "cheddar"], "tam": 0.18, "unidade": "kg"},
    {"linha": "Cebolinha verde, maço", "buscas": ["cebolinha"], "inicio": ["cebolinha"], "excluir": ["desidratad", "picad", "pote", "granulad", "flocos", "pacal", "kitano", "spices", "santa", "pramesa"], "tam": 1, "unidade": "un", "granel": True},
    {"linha": "Pimentão vermelho, kg", "buscas": ["pimentao vermelho"], "inicio": ["pimentao vermelho"], "excluir": ["conserva", "po", "assad"], "tam": 1, "unidade": "kg", "granel": True},
    {"linha": "Repolho verde, unidade", "buscas": ["repolho"], "inicio": ["repolho"], "tem": ["un"], "excluir": ["roxo"], "tam": 1, "unidade": "un", "granel": True},
    {"linha": "Salsa, maço", "buscas": ["salsa maco", "cheiro verde"], "inicio": ["salsa", "salsinha", "cheiro verde"], "excluir": ["desidratad", "flocos", "po", "pote", "picad", "kg", "pacal", "kitano"], "tam": 1, "unidade": "un", "granel": True},
    {"linha": "Abacaxi pérola, unidade", "buscas": ["abacaxi"], "inicio": ["abacaxi"], "excluir": ["suco", "polpa", "calda", "desidratad", "doce", "gelatina", "kg", "picad", "fatiad", "nectar"], "tam": 1, "unidade": "un", "granel": True},
    {"linha": "Coco verde, unidade", "buscas": ["coco verde"], "inicio": ["coco verde"], "excluir": ["agua", "ralad", "kg"], "tam": 1, "unidade": "un", "granel": True},
    {"linha": "Melão amarelo, kg", "buscas": ["melao amarelo"], "inicio": ["melao amarelo", "melao"], "excluir": ["sapo", "cantaloupe", "orange", "galia", "pele"], "tam": 1, "unidade": "kg", "granel": True},
    {"linha": "Fermento Royal, 100g", "buscas": ["fermento quimico"], "inicio": ["fermento"], "excluir": ["biolog", "levedura", "fresco", "seco"], "tam": 0.1, "unidade": "kg"},
    {"linha": "Milho verde Predilecta, lata 170g", "buscas": ["milho verde"], "inicio": ["milho verde", "milho em conserva", "milho"], "tem": ["milho"], "excluir": ["pipoca", "creme", "ervilha", "espiga", "congelad", "farinha", "flocos", "canjica", "cozid"], "tam": 0.17, "unidade": "kg"},
    {"linha": "Milho de pipoca Yoki, 400g", "buscas": ["milho de pipoca"], "tem": ["milho", "pipoca"], "excluir": ["micro", "doce", "caramel", "pronta"], "tam": 0.4, "unidade": "kg"},
    {"linha": "Pepino Zilse burger fatiado, 330g", "buscas": ["pepino conserva"], "tem": ["pepino", "conserva"], "tam": 0.33, "unidade": "kg"},
    {"linha": "Canela em pó Pacal, 27g", "buscas": ["canela em po"], "tem": ["canela", "po"], "excluir": ["casca", "pau", "rama", "acucar", "cha"], "tam": 0.027, "unidade": "kg"},
    {"linha": "Chimichurri Pacal, 5g", "buscas": ["chimichurri"], "inicio": ["chimichurri", "tempero chimichurri"], "excluir": ["molho", "oleo", "azeite", "pasta"], "tam": 0.005, "unidade": "kg"},
    {"linha": "Chá Leão ervas hortelã, 15g", "buscas": ["cha hortela"], "inicio": ["cha"], "tem": ["hortela"], "excluir": ["gelado", "mate", "pronto", "limao", "ml", "zero"], "tam": 0.015, "unidade": "kg"},
    {"linha": "Chá Leão Relaxa camomila, 16g", "buscas": ["cha camomila"], "inicio": ["cha"], "tem": ["camomila"], "excluir": ["gelado", "pronto", "ml", "zero", "limao"], "tam": 0.016, "unidade": "kg"},
    {"linha": "Geleia Linea sem açúcar frutas vermelhas, 230g", "buscas": ["geleia sem acucar", "geleia zero"], "tem": ["geleia"], "qualquer": ["sem acucar", "zero acucar", "diet", "zero"], "tam": 0.23, "unidade": "kg"},
    # papel: só folha dupla, por metro (rolos x metros); 18 rolos de 20m = 360m
    {"linha": "Papel higiênico Duetto 20m, 18 rolos", "buscas": ["papel higienico folha dupla"], "tem": ["papel", "higienico", "dupla"], "excluir": ["umedecid"], "tam": 360, "unidade": "m"},
    # lava-roupas: só líquido e não concentrado (concentrado rende mais por litro)
    {"linha": "Lava-roupas Girando Sol, 3L", "buscas": ["lava roupas liquido", "sabao liquido"], "inicio": ["lava roupa", "lava roupas", "sabao liquido"], "excluir": ["em po", "po", "barra", "amaciante", "tira", "limpador", "concentrad", "louca", "loucas"], "tam": 3, "unidade": "L"},
    {"linha": "Lava-roupas concentrado, 1L", "buscas": ["lava roupas concentrado", "sabao liquido concentrado"], "inicio": ["lava roupa", "lava roupas", "sabao liquido", "sabao"], "tem": ["concentrad"], "excluir": ["po", "barra", "amaciante", "tira", "limpador", "louca", "loucas"], "tam": 1, "unidade": "L"},
    {"linha": "Detergente Suprema neutro, 500ml", "buscas": ["detergente liquido"], "inicio": ["detergente", "lava loucas"], "excluir": ["maquina", "pastilha", "po", "roupa", "automotiv", "carro", "piso", "kit", "caixa", "lava roupa", "concentrad"], "tam": 0.5, "unidade": "L"},
    {"linha": "Esponja Condor multiuso, 4 un", "buscas": ["esponja multiuso"], "inicio": ["esponja"], "excluir": ["aco", "banho", "inox", "maquiagem", "bucha", "vidro", "corpo", "infantil"], "tam": 4, "unidade": "un"},
    {"linha": "Desodorante aerosol (Rexona e similares), 150ml", "buscas": ["desodorante aerosol"], "tem": ["desodorante", "aerosol"], "excluir": ["kit", "pes", "intimo", "creme", "roll"], "tam": 0.15, "unidade": "L"},
    {"linha": "Desodorante Dove creme, 50g", "buscas": ["desodorante creme"], "tem": ["desodorante", "creme"], "excluir": ["aerosol", "roll", "spray", "kit", "pes", "intimo"], "tam": 0.05, "unidade": "kg"},
    # sabonete: só em barra; "leve 6 pague 5 85g" = 6 x 85g
    {"linha": "Sabonete Protex, 85g", "buscas": ["sabonete barra"], "inicio": ["sabonete"], "excluir": ["liquido", "refil", "intimo", "facial", "infantil", "kit"], "tam": 0.085, "unidade": "kg", "multi": True},
    {"linha": "Toalha umedecida Piquitucho, 140 un", "buscas": ["toalha umedecida", "lenco umedecido"], "inicio": ["toalha umedecida", "toalhas umedecidas", "lenco umedecido", "lencos umedecidos"], "excluir": ["demaquilante", "intima", "limpeza", "multiuso"], "tam": 140, "unidade": "un"},
    # --- específicos: mesma marca/produto ---
    {"linha": "Massa Rap10 integral (wrap), 297g", "buscas": ["rap10 integral"], "tem": ["rap", "integral"], "tam": 0.297, "unidade": "kg", "especifico": True},
    {"linha": "Pipoca de micro-ondas Yoki, 160g", "buscas": ["pipoca micro-ondas yoki"], "tem": ["pipoca", "micro", "yoki"], "tam": 0.16, "unidade": "kg", "especifico": True},
    {"linha": "Tempero Kitano mix para frango, 40g", "buscas": ["kitano frango"], "tem": ["kitano", "frango"], "tam": 0.04, "unidade": "kg", "especifico": True},
    {"linha": "Café solúvel L'Or Classique, 40g", "buscas": ["cafe soluvel l'or"], "tem": ["soluvel", "classique"], "tam": 0.04, "unidade": "kg", "especifico": True},
    {"linha": "Biscoito Trakinas recheado, 126g", "buscas": ["trakinas"], "tem": ["trakinas"], "excluir": ["mini", "kit"], "tam": 0.126, "unidade": "kg", "especifico": True},
    {"linha": "Pudim Dr. Oetker diet chocolate, 25g", "buscas": ["pudim oetker diet"], "tem": ["pudim", "oetker", "diet"], "tam": 0.025, "unidade": "kg", "especifico": True},
    {"linha": "Refresco Clight, 8g", "buscas": ["clight"], "tem": ["clight"], "tam": 0.008, "unidade": "kg", "especifico": True},
    {"linha": "Macarrão instantâneo Nissin galinha caipira, 80g", "buscas": ["nissin galinha caipira"], "tem": ["nissin", "caipira"], "excluir": ["cup", "copo"], "tam": 0.08, "unidade": "kg", "especifico": True},
    {"linha": "Petit gateau Mr. Bey chocolate, 240g", "buscas": ["petit gateau mr bey"], "tem": ["petit", "bey"], "tam": 0.24, "unidade": "kg", "especifico": True},
    {"linha": "Kit shampoo + condicionador Tresemmé, 525ml", "buscas": ["kit tresemme"], "tem": ["kit", "tresemme"], "tam": 0.525, "unidade": "L", "especifico": True},
    # refil precisa servir no aparelho Raid de vocês
    {"linha": "Repelente Raid 45 noites, refil", "buscas": ["raid 45 noites refil"], "tem": ["raid", "45", "refil"], "tam": 1, "unidade": "un", "especifico": True},
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
    _e.setdefault("qualquer", [])
    _e["pago"] = PAGOS.get(_e["linha"])


def brl(v):
    """4.99 -> '4,99' (formato brasileiro nas mensagens)."""
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def norm(t):
    """minúsculo e sem acento, para 'Café' casar com 'cafe'."""
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def quantidade(nome, unidade, multi=False):
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
            v = v / 1000 if u in ("g", "gr", "ml") else v
            if multi:  # 'Leve 6 Pague 5 85g' -> 6 x 0,085
                c = re.search(r"(?:leve|c/|com)\s*(\d+)(?![\d.]|\s*(?:g|gr|kg|ml|l|lt|m)\b)|(\d+)\s*(?:un\b|unid)", n)
                if c:
                    v *= float(c.group(1) or c.group(2))
            return v
    if unidade == "m":  # papel higiênico: rolos x metros ('30m Leve 24 Pague 22' -> 720)
        r = re.search(r"(?:leve|c/|com)\s*(\d+)(?![\d.]|\s*(?:g|gr|kg|ml|l|lt|m)\b)|(\d+)\s*rolos?", n)
        mt = re.search(r"(\d+(?:\.\d+)?)\s*m(?:etros)?\b", n)
        return float(r.group(1) or r.group(2)) * float(mt.group(1)) if r and mt else None
    if unidade == "un":  # rolos, unidades, folhas: '18 Rolos', 'c/4', '140 un'
        m = re.search(r"(?:leve|c/|com)\s*(\d+)(?![\d.]|\s*(?:g|gr|kg|ml|l|lt|m)\b)|(\d+)\s*(?:un\b|unid|rolos?|folhas|lencos)", n)
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
        if e["qualquer"] and not any(" " + q + " " in " " + texto for q in e["qualquer"]):
            continue
        if all(tem(t) for t in e["tem"]) and not any(tem(x) for x in e["excluir"]):
            return e
    return None


def preco_equivalente(nome, atual, e):
    """Preço convertido para o tamanho da linha da tabela (detergente 1L -> preço de 500ml).
    None quando não dá para comparar (genérico sem tamanho no nome)."""
    qtd = quantidade(nome, e["unidade"], e.get("multi", False))
    if qtd:
        if not e.get("especifico") and qtd > e["tam"] * MAX_EMBALAGEM:
            return None
        return atual / qtd * e["tam"]
    if e.get("granel"):  # hortifrúti: sem tamanho = preço do kg ou da unidade
        return atual * e["tam"] if e["unidade"] in ("kg", "L") else atual
    if e.get("especifico"):  # mesma marca: mesma embalagem da linha
        return atual if e["unidade"] in ("un", "m") else None
    return None


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
            for t in e["buscas"]:
                termos.setdefault(t, 1)
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
        # Todas as promoções do site (a lista da página "Promoções"), do maior desconto para o menor.
        # Cada página dessa lista leva ~3 s no servidor, então busca 4 por vez (~3 min no total).
        frm, fim = 0, False
        while not fim and frm < 10000:
            urls = [f"{API}?promotion=true&size={SIZE}&from={f}&search=&sortField=discount&sortOrder=desc"
                    for f in range(frm, frm + 4 * SIZE, SIZE)]
            res = page.evaluate(
                """async ({urls, headers}) => Promise.all(urls.map(async url => {
                    const r = await fetch(url, {headers});
                    return {status: r.status, body: await r.text()};
                }))""",
                {"urls": urls, "headers": headers},
            )
            for r in res:
                if r["status"] != 200:
                    print(f"(lista de promoções parou em {frm}: API respondeu {r['status']})")
                    fim = True
                    break
                hits = json.loads(r["body"]).get("hits", [])
                for h in hits:
                    itens.setdefault(h["id"], h)
                if len(hits) < SIZE:
                    fim = True
            frm += 4 * SIZE
            page.wait_for_timeout(PAUSA_MS)
        browser.close()
    return list(itens.values())


def _promo_clube(h):
    """Promoção do Clube K ativa hoje (a de menor preço), ou None."""
    agora = datetime.datetime.now(datetime.timezone.utc).isoformat()
    melhor = None
    for pp in h.get("personaPromotions") or []:
        if (pp.get("personaId") == CLUBE_K and pp.get("active") and pp.get("promotionPrice")
                and (pp.get("startDate") or "") <= agora <= (pp.get("endDate") or "9")):
            if melhor is None or pp["promotionPrice"] < melhor["promotionPrice"]:
                melhor = pp
    return melhor


def preco_atual(h):
    """Menor preço de hoje: promoção comum ou do Clube K (com CPF)."""
    pr = h["pricing"]
    atual = pr["promotionalPrice"] if pr.get("promotion") else pr["price"]
    clube = _promo_clube(h)
    return min(atual, clube["promotionPrice"]) if clube else atual


def _campanha(categorias):
    """Nome da campanha ("Fraldas ... com 50% OFF", "Leve 3 Pague 2") nas categorias do produto."""
    for c in categorias:
        ultimo = c.split(":", 1)[-1].split(" > ")[-1].strip()
        if re.search(r"(?i)%|\boff\b|leve \d|pague \d", ultimo):
            return ultimo
    return None


def criar_tabelas(db):
    db.execute(
        "create table if not exists precos("
        "dia text, id text, nome text, marca text, preco_cheio real, "
        "preco_atual real, desconto real, estoque integer, "
        "primary key(dia, id))"
    )
    db.execute("create index if not exists precos_id on precos(id, dia)")
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
    # promos = dados extras de cada produto em promoção no dia (o preço fica em "precos")
    db.execute(
        "create table if not exists promos(dia text, id text, campanha text, categoria text, "
        "clube integer, fim text, primary key(dia, id))"
    )


def salvar(db, hoje, itens):
    for h in itens:
        pr = h["pricing"]
        atual = preco_atual(h)
        db.execute(
            "insert or replace into precos values(?,?,?,?,?,?,?,?)",
            (hoje, h["id"], h["name"], h.get("brandName"), pr["price"],
             atual, pr.get("discount"), h.get("quantity", {}).get("inStock")),
        )
        if atual < pr["price"] - 0.005:
            cats = h.get("categories") or []
            clube = _promo_clube(h)
            normal = pr["promotionalPrice"] if pr.get("promotion") else pr["price"]
            fim = None
            if clube and clube.get("endDate"):
                # endDate vem em UTC; o fim da promoção no horário de Brasília (UTC-3)
                fim = (datetime.datetime.fromisoformat(clube["endDate"].replace("Z", "+00:00"))
                       - datetime.timedelta(hours=3)).date().isoformat()
            # departamentos do produto ("Mercearia|Mundo Bebê"), para filtrar o que não interessa
            principal = "|".join(dict.fromkeys(c.split(":", 1)[-1].split(" > ")[0] for c in cats))
            db.execute("insert or replace into promos values(?,?,?,?,?,?)",
                       (hoje, h["id"], _campanha(cats), principal,
                        1 if clube and clube["promotionPrice"] < normal - 0.005 else 0, fim))
    db.commit()


def reprocessar(db):
    """Refaz a tabela dieta a partir de todos os preços já coletados, com as regras atuais."""
    db.execute("delete from dieta")
    db.execute("delete from extras")
    for dia, pid, nome, atual, cheio in db.execute(
            "select dia, id, nome, preco_atual, preco_cheio from precos").fetchall():
        e = casar_extra(nome)
        equiv = preco_equivalente(nome, atual, e) if e else None
        if equiv:
            db.execute("insert or replace into extras values(?,?,?,?,?,?,?)",
                       (dia, e["linha"], pid, nome, atual, cheio, equiv))
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


def depto_principal(categoria):
    """'BLACK FRIDAY|Limpeza' -> 'Limpeza' (o primeiro departamento que não é vitrine)."""
    for d in (categoria or "").split("|"):
        if d and d not in DEPTO_IGNORAR:
            return d
    return "Outros"


def desconto_real(db, pid, hoje, cheio, atual):
    """(preço cheio de referência, desconto real). Com 7+ dias de histórico, o cheio de referência
    é o mais comum dos últimos 60 dias (mediana), se for menor que o de hoje: pega o truque de
    subir o preço antes de dar o desconto."""
    hist = sorted(c for (c,) in db.execute(
        "select preco_cheio from precos where id=? and dia<? and dia>=date(?, '-60 day')",
        (pid, hoje, hoje)))
    ref = cheio
    if len(hist) >= MIN_DIAS_HISTORICO:
        ref = min(cheio, hist[len(hist) // 2])
    return ref, (1 - atual / ref if ref else 0)


def analisar_promos(db, hoje):
    """Ofertas fortes do site inteiro: desconto real >= DESC_FORTE.
    "Real": se o produto já tem histórico, o preço cheio de referência é o mais comum dos
    últimos 60 dias (mediana), não o de hoje — pega o truque de subir o preço antes do desconto.
    Devolve (campanhas, destaques); campanha = {nome, n, desc, fim, clube, depto, exemplo}."""
    rows = db.execute(
        "select p.id, p.nome, p.preco_cheio, p.preco_atual, m.campanha, m.categoria, m.clube, m.fim, "
        "p.estoque from promos m join precos p on p.dia=m.dia and p.id=m.id where m.dia=?", (hoje,)).fetchall()
    fortes = []
    for pid, nome, cheio, atual, campanha, categoria, clube, fim, estoque in rows:
        if any(x in (categoria or "") for x in PROMO_EXCLUIR):
            continue
        ref, desc = desconto_real(db, pid, hoje, cheio, atual)
        if desc >= DESC_FORTE - 0.005:
            fortes.append({"nome": nome, "cheio": ref, "atual": atual, "desc": desc, "estoque": estoque or 0,
                           "campanha": campanha, "clube": clube, "fim": fim, "depto": depto_principal(categoria)})
    grupos = {}
    for f in fortes:
        if f["campanha"]:
            grupos.setdefault(f["campanha"], []).append(f)
    campanhas = []
    for nome, itens in grupos.items():
        if len(itens) >= MIN_CAMPANHA:
            fins = [i["fim"] for i in itens if i["fim"]]
            deptos = [i["depto"] for i in itens]
            campanhas.append({"nome": nome, "n": len(itens), "desc": max(i["desc"] for i in itens),
                              "fim": max(fins) if fins else None,
                              "clube": sum(i["clube"] for i in itens) > len(itens) / 2,
                              "depto": max(set(deptos), key=deptos.count),
                              # exemplo de preço: o de maior estoque (em geral, o mais vendido)
                              "exemplo": max(itens, key=lambda i: i["estoque"])})
    campanhas.sort(key=lambda c: (-c["desc"], -c["n"]))
    nomes_camp = {c["nome"] for c in campanhas}
    destaques = sorted((f for f in fortes if f["campanha"] not in nomes_camp),
                       key=lambda f: -f["desc"])[:MAX_DESTAQUES]
    print(f"  [promos] {len(rows)} em promoção, {len(fortes)} com {DESC_FORTE:.0%}+ de desconto real")
    return campanhas, destaques


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


def _corta(txt, limite):
    return txt if len(txt) <= limite else txt[:limite - 1].rstrip() + "…"


def _ate(fim):
    return f" · até {fim[8:10]}/{fim[5:7]}" if fim else ""


def montar_mensagem(resumo, extras, data, promos=((), ())):
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
    # Ofertas fortes do site inteiro, por departamento: campanhas (com um exemplo de preço) e avulsos
    campanhas, destaques = promos
    if campanhas or destaques:
        linhas += ["", "━━━━━━━━━━━━━━━",
                   "🏷️ *PROMOÇÕES QUE VALEM A PENA NO SITE*",
                   ]
        # Fim e Clube K mais comuns vão no cabeçalho; a linha só mostra quando for diferente.
        todos = campanhas + destaques
        fins = [x["fim"] for x in todos]
        fim_comum = max(set(fins), key=fins.count)
        clube_comum = sum(1 for x in todos if x["clube"]) > len(todos) / 2
        linhas.append(f"_{DESC_FORTE:.0%} ou mais abaixo do preço de costume"
                      + (f", valem {_ate(fim_comum)[3:]}" if fim_comum else "")
                      + (". Preço com CPF no Clube K (passe o CPF no caixa)" if clube_comum else "") + "._")

        def obs(x):
            t = _ate(x["fim"]) if x["fim"] != fim_comum else ""
            if clube_comum and not x["clube"]:
                t += " · sem CPF"
            elif not clube_comum and x["clube"]:
                t += " · com CPF 🅺"
            return t

        # Avulsos quase iguais (mesma 1ª palavra e mesmos preços: sabores, cores) viram uma linha só.
        juntos = {}
        for d in destaques:
            juntos.setdefault((d["depto"], d["nome"].split()[0].lower(), d["atual"], d["cheio"]), []).append(d)
        destaques = [dict(g[0], mais=len(g) - 1) for g in juntos.values()]
        ordem = [d for d, _ in DEPTOS]
        icone = dict(DEPTOS)
        grupos = {}
        for c in campanhas:
            grupos.setdefault(c["depto"], ([], []))[0].append(c)
        for d in destaques:
            grupos.setdefault(d["depto"], ([], []))[1].append(d)
        for depto in sorted(grupos, key=lambda x: ordem.index(x) if x in ordem else len(ordem)):
            camps, avulsos = grupos[depto]
            linhas += ["", f"{icone.get(depto, '🏷️')} *{depto.upper()}*"]
            for c in camps:
                ex = c["exemplo"]
                linhas.append(f"• *{c['nome']}* · {c['n']} itens{obs(c)}")
                linhas.append(f"   _ex.: {_corta(ex['nome'], 38)}_ *R$ {brl(ex['atual'])}* ~{brl(ex['cheio'])}~")
            for d in avulsos:
                mais = f" _(+{d['mais']} parecido{'s' if d['mais'] > 1 else ''})_" if d["mais"] else ""
                linhas.append(f"• {_corta(d['nome'], 45)}{mais} *R$ {brl(d['atual'])}* ~{brl(d['cheio'])}~ "
                              f"-{d['desc']:.0%}{obs(d)}")
        linhas += ["", f"📱 *Todas as promoções, com busca:* {SITE_PROMOS}", "━━━━━━━━━━━━━━━"]
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
            # todas as marcas/cortes de hoje, do mais barato ao mais caro (lista que abre ao clicar)
            # o mais barato de cada dia nos últimos 30 dias (comparativo dia a dia)
            "hist": [[d, round(v, 4)] for d, v in db.execute(
                "select dia, min(por_unidade) from dieta where item=? and dia>=date(?, '-30 day') "
                "group by dia order by dia", (a["nome"], hoje))],
            "opcoes": [{"produto": n, "preco": pr, "cheio": ch, "porUnidade": round(pu, 4), "aprov": ap}
                       for n, pr, ch, pu, ap in db.execute(
                           "select nome, preco, cheio, por_unidade, aprov from dieta where item=? and dia=? "
                           "order by por_unidade", (a["nome"], hoje))],
        })
    # Demais itens da tabela: por linha, todas as opções de hoje (a 1ª é a mais barata).
    extras = {}
    for linha, nome, preco, cheio, equiv in db.execute(
            "select linha, nome, preco, cheio, equiv from extras where dia=? order by linha, equiv", (hoje,)):
        op = {"produto": nome, "preco": preco, "cheio": cheio, "equiv": round(equiv, 2)}
        if linha not in extras:
            extras[linha] = dict(op, opcoes=[])
        extras[linha]["opcoes"].append(op)
    dados = {"atualizado": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), "itens": linhas,
             "extras": extras}
    with open(JS_SAIDA, "w", encoding="utf-8") as f:
        f.write("// Gerado por get-ofertas.py; não edite à mão.\nwindow.OFERTAS_KOCH = "
                + json.dumps(dados, ensure_ascii=False, indent=1) + ";\n")


def gerar_promos_js(db, hoje):
    """Escreve promos-koch.js (aba Promoções): todas as promoções de hoje, com o desconto real e
    o histórico de preço de cada produto (só os dias em que o preço mudou, para o arquivo ficar leve).
    Cada item é uma lista: [nome, preço, cheio anunciado, cheio de referência, desconto real,
    nº da campanha, nº do departamento, Clube K (0/1), fim "dd/mm", menor preço antes de hoje,
    nº de dias com preço antes de hoje, histórico [[nº do dia, preço], ...]]."""
    rows = db.execute(
        "select p.id, p.nome, p.preco_cheio, p.preco_atual, m.campanha, m.categoria, m.clube, m.fim "
        "from promos m join precos p on p.dia=m.dia and p.id=m.id where m.dia=?", (hoje,)).fetchall()
    dias = [d for (d,) in db.execute(
        "select distinct dia from precos where dia>=date(?, '-60 day') order by dia", (hoje,))]
    idx_dia = {d: i for i, d in enumerate(dias)}
    campanhas, deptos, itens = [], [], []

    def indice(lista, valor):
        if not valor:
            return -1
        if valor not in lista:
            lista.append(valor)
        return lista.index(valor)

    for pid, nome, cheio, atual, campanha, categoria, clube, fim in rows:
        ref, desc = desconto_real(db, pid, hoje, cheio, atual)
        serie = db.execute("select dia, preco_atual from precos where id=? and dia>=date(?, '-60 day') "
                           "order by dia", (pid, hoje)).fetchall()
        antes = [v for d, v in serie if d < hoje]
        hist, ultimo = [], None
        for d, v in serie:
            if ultimo is None or abs(v - ultimo) > 0.005:
                hist.append([idx_dia[d], v])
                ultimo = v
        itens.append([nome, atual, cheio, round(ref, 2), round(desc, 3), indice(campanhas, campanha),
                      indice(deptos, depto_principal(categoria)), clube or 0,
                      f"{fim[8:10]}/{fim[5:7]}" if fim else "", min(antes) if antes else None,
                      len(antes), hist])
    itens.sort(key=lambda i: -i[4])
    dados = {"atualizado": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
             "dias": [f"{d[8:10]}/{d[5:7]}" for d in dias], "campanhas": campanhas, "deptos": deptos,
             "descVale": DESC_VALE, "itens": itens}
    with open(JS_PROMOS, "w", encoding="utf-8") as f:
        f.write("// Gerado por get-ofertas.py; não edite à mão.\nwindow.PROMOS_KOCH = "
                + json.dumps(dados, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print(f"  [promos] {len(itens)} promoções em promos-koch.js")


def _abrir_wa(p, visivel=False):
    # perfil persistente: a sessão do WhatsApp Web fica salva na pasta wa_perfil
    return p.chromium.launch_persistent_context(WA_PERFIL, **_opcoes(visivel, headless=False, locale="pt-BR"))


def login_whatsapp():
    """Rode uma vez: abre o WhatsApp Web para você escanear o QR code."""
    with sync_playwright() as p:
        ctx = _abrir_wa(p, visivel=True)  # precisa ver o QR
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
        promos = analisar_promos(db, hoje)
        gerar_js(db, hoje)
        gerar_promos_js(db, hoje)
        db.close()
        print(f"{len(itens)} produtos gravados; ofertas-koch.js atualizado.\n")
        notificar(montar_mensagem(resumo, extras, datetime.date.today(), promos), enviar)
    except Exception as e:
        notificar(f"⚠️ Coleta do Koch falhou: {e}", enviar)
        sys.exit(1)


if __name__ == "__main__":
    if "--login-whatsapp" in sys.argv:
        login_whatsapp()
    else:
        main(enviar="--sem-aviso" not in sys.argv, ir_ao_site="--refazer" not in sys.argv)
