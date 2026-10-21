import csv
import io
import re
import unicodedata
import zipfile
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Conversor NFS-e / NFTS | TXT v004 SP",
                   page_icon="🟠", layout="wide")

# ───────────────────── Tema Thomson Reuters ─────────────────────
st.markdown("""
<style>
html, body, [class*="css"]{font-family:'Knowledge2017','Source Sans Pro',Arial,sans-serif;color:#404040;}
.tr-header{background:#404040;color:#fff;padding:20px 26px;margin-bottom:1.2rem;
  border-bottom:5px solid #FA6400;border-radius:3px;display:flex;align-items:center;gap:18px;}
.tr-dots{display:grid;grid-template-columns:repeat(3,10px);gap:4px;}
.tr-dots span{width:10px;height:10px;border-radius:50%;background:#FA6400;display:block;}
.tr-dots span:nth-child(even){background:#FF8000;}
.tr-header h1{color:#fff !important;font-size:1.6rem;margin:0;padding:0;font-weight:600;}
.tr-header p{color:#D8D8D8 !important;margin:2px 0 0 0;font-size:.9rem;}
.stButton>button,.stDownloadButton>button{background:#FA6400;color:#fff;border:0;border-radius:2px;font-weight:600;}
.stButton>button:hover,.stDownloadButton>button:hover{background:#D64000;color:#fff;}
[data-testid="stMetric"]{background:#F5F5F5;border-left:4px solid #FA6400;padding:10px 14px;border-radius:2px;}
[data-testid="stMetric"] *{color:#404040 !important;}
[data-testid="stMetricLabel"] p{font-size:.8rem;white-space:normal;}
[data-testid="stMetricValue"]{font-size:1.4rem;}
[data-testid="stMetricValue"] > div{white-space:normal;overflow:visible;text-overflow:clip;}
.stTabs [aria-selected="true"]{color:#FA6400 !important;}
.stTabs [data-baseweb="tab-highlight"]{background-color:#FA6400 !important;}
section[data-testid="stSidebar"]{border-right:3px solid #FA6400;}
</style>
<div class="tr-header">
  <div class="tr-dots"><span></span><span></span><span></span><span></span><span></span><span></span></div>
  <div><h1>Conversor NFS-e / NFTS &rarr; TXT leiaute v004 (São Paulo)</h1>
  <p>Prestados &bull; Tomados &bull; NFTS &mdash; largura fixa, ISO-8859-1, CRLF</p></div>
</div>
""", unsafe_allow_html=True)

# ───────────────────── Parâmetros fixos ─────────────────────
CRLF = "\r\n"
MAX_BYTES = 10240 * 1024          # limite do manual (10 MB)
VERSAO = "004"                    # leiaute sempre v004
RESERVADO = 145                   # campo reservado (ordem 72) sempre 145
SOMENTE_DATA = True               # Data Hora NFE: grava só a data (hora = 000000)
TIPO_RPS_NFTS = "RPS"             # Tipo de RPS (ordem 5) gravado nas NFTS
ZERO_SITUACOES = {"C", "E", "S"}  # canceladas, extraviadas, substituídas
# Todos os campos monetários são zerados nessas situações
ZERO_FIELDS = (
    "Valor dos Serviços", "Valor das Deduções", "ISS devido", "Valor do Crédito",
    "ISS recolhido", "ISS a recolher", "Repasse do Plano de Saúde",
    "PIS/PASEP", "COFINS", "INSS", "IR", "CSLL",
    "Carga tributária: Valor", "Valor Total Recebido",
)

# Opções de data para NFTS (ordens 3 e 8 do TXT ficam iguais)
OPC_EMISSAO = "Data de emissão da NFTS"
OPC_FATO = "Data do fato gerador (prestação do serviço)"

# Retenções federais agregadas pela Prefeitura (NFS-e emitidas a partir de 14/05/2026)
DATA_CORTE_FEDERAIS = date(2026, 5, 14)
ALIQ = {"PIS": Decimal("0.0065"), "COFINS": Decimal("0.03"), "CSLL": Decimal("0.01")}
COMBOS = [("PIS",), ("CSLL",), ("PIS", "CSLL"), ("COFINS",),
          ("PIS", "COFINS"), ("COFINS", "CSLL"), ("PIS", "COFINS", "CSLL")]
TOL_CENTS = 2

# Data AAAAMMDD válida (2000-2099) e par de datas AAAAMMDD_AAAAMMDD no nome do arquivo.
# Evita confundir a IM (ex.: 11467690) com uma data.
DATE8 = r"20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
PERIOD_RE = re.compile(rf"(?<!\d)({DATE8})_({DATE8})(?!\d)")


# ───────────────────── Utilidades de formatação ─────────────────────
class Ctx:
    def __init__(self):
        self.warnings, self.cur = [], ""

    def warn(self, msg):
        if len(self.warnings) < 300:
            self.warnings.append(f"{self.cur}: {msg}")


def clean(v):
    if v is None:
        return ""
    s = str(v)
    return "" if s.strip().lower() == "nan" else s


def digits(v):
    s = clean(v).strip()
    if re.fullmatch(r"\d+\.0+", s):          # artefato "889176.0"
        s = s.split(".")[0]
    return re.sub(r"\D", "", s)


def cents(v):
    s = clean(v).replace("R$", "").strip()
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return int((Decimal(s) * 100).to_integral_value())
    except InvalidOperation:
        return None


def parse_dt(v):
    m = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?", clean(v))
    if not m:
        return None
    d, mo, y, hh, mi, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        return None


def period_from_name(name):
    """Período AAAAMMDD_AAAAMMDD no nome do arquivo (ex.: NFTS_11467690_20260901_20260930.csv)."""
    m = PERIOD_RE.search(name)
    if not m:
        return None
    try:
        return (datetime.strptime(m.group(1), "%Y%m%d").date(),
                datetime.strptime(m.group(2), "%Y%m%d").date())
    except ValueError:
        return None


def latin1_safe(s):
    """Garante 1 caractere = 1 byte em ISO-8859-1 (necessário p/ largura fixa)."""
    out = []
    for ch in s:
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode() or "?")
    return "".join(out)


def br(c):
    return f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def br_or_blank(s):
    """Converte o campo de 15 posições do TXT (centavos) em texto monetário; vazio -> ''."""
    return br(int(s)) if s.strip() else ""


def fmt(kind, value, width, default, ctx, nome):
    if kind == "CONST":
        return str(default)
    if kind == "BLANK":
        return " " * width
    if kind in ("T", "TN"):
        s = clean(value)
        if kind == "TN" and re.fullmatch(r"\s*\d+\.0\s*", s):
            s = s.split(".")[0]
        s = re.sub(r"[\t\r\n]+", " ", latin1_safe(s)).strip()
        if len(s) > width:
            ctx.warn(f"'{nome}' truncado ({len(s)}>{width})")
            s = s[:width]
        return s.ljust(width)
    if kind in ("D", "DH"):
        dt = parse_dt(value)
        if not dt:
            return " " * width
        if kind == "D":
            return dt.strftime("%Y%m%d")
        hora = "000000" if SOMENTE_DATA else dt.strftime("%H%M%S")
        return dt.strftime("%Y%m%d") + hora
    if kind == "SN":
        return "S" if clean(value).strip().upper()[:1] in ("S", "1") else "N"
    if kind in ("N", "NS"):
        d = digits(value)
        if not d:
            if default:
                d = str(default)
            else:
                return (" " if kind == "NS" else "0") * width
        if len(d) > width:
            ctx.warn(f"'{nome}' excede {width} posições")
            d = d[-width:]
        return d.rjust(width, "0")
    if kind in ("M", "MS", "A", "PS"):
        c = cents(value)
        if c is None:
            return (" " if kind in ("MS", "PS") else "0") * width
        if c < 0:
            ctx.warn(f"'{nome}' negativo; gravado em módulo")
        d = str(abs(c))
        if len(d) > width:
            ctx.warn(f"'{nome}' excede {width} posições")
            d = d[-width:]
        return d.rjust(width, "0")
    raise ValueError(kind)


# ───────────────────── Layout V.004 – Registro tipo 2 (manual 4.09) ─────────────────────
# (ordem, campo, largura, tipo, coluna_fonte, padrão)
SPEC = [
    (1, "Tipo de registro", 1, "CONST", None, "2"),
    (2, "Nº NFS-e", 8, "N", "Nº NFS-e", None),
    (3, "Data Hora NFE", 14, "DH", "Data Hora NFE", None),
    (4, "Código de verificação", 8, "T", "Código de Verificação da NFS-e", None),
    (5, "Tipo de RPS", 5, "T", "Tipo de RPS", None),
    (6, "Série do RPS", 5, "TN", "Série do RPS", None),
    (7, "Número do RPS", 12, "N", "Número do RPS", None),
    (8, "Data de emissão do RPS", 8, "D", "Data do Fato Gerador", None),
    (9, "IM do prestador", 8, "N", "Inscrição Municipal do Prestador", None),
    (10, "Ind. CPF/CNPJ prestador", 1, "N", "Indicador de CPF/CNPJ do Prestador", None),
    (11, "CPF/CNPJ prestador", 14, "N", "CPF/CNPJ do Prestador", None),
    (12, "Razão social prestador", 75, "T", "Razão Social do Prestador", None),
    (13, "Tipo end. prestador", 3, "T", "Tipo do Endereço do Prestador", None),
    (14, "Endereço prestador", 50, "T", "Endereço do Prestador", None),
    (15, "Nº end. prestador", 10, "TN", "Número do Endereço do Prestador", None),
    (16, "Compl. prestador", 30, "T", "Complemento do Endereço do Prestador", None),
    (17, "Bairro prestador", 30, "T", "Bairro do Prestador", None),
    (18, "Cidade prestador", 50, "T", "Cidade do Prestador", None),
    (19, "UF prestador", 2, "T", "UF do Prestador", None),
    (20, "CEP prestador", 8, "N", "CEP do Prestador", None),
    (21, "E-mail prestador", 75, "T", "Email do Prestador", None),
    (22, "Opção pelo Simples", 1, "N", "Opção Pelo Simples", None),
    (23, "Situação da nota", 1, "T", "Situação da Nota Fiscal", None),
    (24, "Data de cancelamento", 8, "D", "Data de Cancelamento", None),
    (25, "Nº da guia", 12, "N", "Nº da Guia", None),
    (26, "Data quitação da guia", 8, "D", "Data de Quitação da Guia Vinculada a Nota Fiscal", None),
    (27, "Valor dos serviços", 15, "M", "Valor dos Serviços", None),
    (28, "Valor das deduções", 15, "M", "Valor das Deduções", None),
    (29, "Código do serviço", 5, "N", "Código do Serviço Prestado na Nota Fiscal", None),
    (30, "Alíquota", 4, "A", "Alíquota", None),
    (31, "Valor do ISS", 15, "M", "ISS devido", None),
    (32, "Valor do crédito", 15, "M", "Valor do Crédito", None),
    (33, "ISS retido (S/N)", 1, "SN", "ISS Retido", None),
    (34, "Ind. CPF/CNPJ tomador", 1, "N", "Indicador de CPF/CNPJ do Tomador", None),
    (35, "CPF/CNPJ tomador", 14, "N", "CPF/CNPJ do Tomador", None),
    (36, "IM do tomador", 8, "N", "Inscrição Municipal do Tomador", None),
    (37, "IE do tomador", 12, "N", "Inscrição Estadual do Tomador", None),
    (38, "Razão social tomador", 75, "T", "Razão Social do Tomador", None),
    (39, "Tipo end. tomador", 3, "T", "Tipo do Endereço do Tomador", None),
    (40, "Endereço tomador", 50, "T", "Endereço do Tomador", None),
    (41, "Nº end. tomador", 10, "TN", "Número do Endereço do Tomador", None),
    (42, "Compl. tomador", 30, "T", "Complemento do Endereço do Tomador", None),
    (43, "Bairro tomador", 30, "T", "Bairro do Tomador", None),
    (44, "Cidade tomador", 50, "T", "Cidade do Tomador", None),
    (45, "UF tomador", 2, "T", "UF do Tomador", None),
    (46, "CEP tomador", 8, "N", "CEP do Tomador", None),
    (47, "E-mail tomador", 75, "T", "Email do Tomador", None),
    (48, "Nº NFS-e substituta", 8, "NS", "Nº NFS-e Substituta", None),
    (49, "ISS recolhido por guia", 15, "M", "ISS recolhido", None),
    (50, "ISS a recolher", 15, "M", "ISS a recolher", None),
    (51, "Ind. CPF/CNPJ intermediário", 1, "N", "Indicador de CPF/CNPJ do Intermediário", "3"),
    (52, "CPF/CNPJ intermediário", 14, "N", "CPF/CNPJ do Intermediário", None),
    (53, "IM intermediário", 8, "N", "Inscrição Municipal do Intermediário", None),
    (54, "Razão social intermediário", 75, "T", "Razão Social do Intermediário", None),
    (55, "Repasse plano de saúde", 15, "M", "Repasse do Plano de Saúde", None),
    (56, "PIS/PASEP", 15, "MS", "PIS/PASEP", None),
    (57, "COFINS", 15, "MS", "COFINS", None),
    (58, "INSS", 15, "MS", "INSS", None),
    (59, "IR", 15, "MS", "IR", None),
    (60, "CSSL", 15, "MS", "CSLL", None),
    (61, "Carga tributária: valor", 15, "MS", "Carga tributária: Valor", None),
    (62, "Carga tributária: %", 5, "PS", "Carga tributária: Porcentagem", None),
    (63, "Carga tributária: fonte", 10, "T", "Carga tributária: Fonte", None),
    (64, "CEI", 12, "NS", "CEI", None),
    (65, "Matrícula da obra", 12, "NS", "Matrícula da Obra", None),
    (66, "Município prestação (IBGE)", 7, "NS", "Município Prestação - cód. IBGE", None),
    (67, "Situação do aceite", 30, "T", "Situação do Aceite", None),
    (68, "Nº seleção notas dedutoras", 10, "NS", "Encapsulamento", None),
    (69, "Valor total recebido", 15, "MS", "Valor Total Recebido", None),
    (70, "Tipo de consolidação", 1, "NS", "Tipo de Consolidação", None),
    (71, "Nº NFS-e consolidada", 12, "NS", "Nº NFS-e Consolidada", None),
    (72, "Campo reservado", RESERVADO, "BLANK", None, None),
]
ALIASES = {
    "ISS recolhido": ["ISS pago"],
    "ISS a recolher": ["ISS a pagar"],
    "CSLL": ["CSSL"],
    "Encapsulamento": ["Número de seleção de notas dedutoras"],
    "Data do Fato Gerador": ["Data de Emissão do RPS"],
}


def positions():
    out, start = [], 1
    for ordem, nome, w, *_ in SPEC:
        out.append((ordem, nome, start, start + w - 1, w))
        start += w
    return out


# Autoconferência com o manual (posições finais até a ordem 71 + reservado de 145)
_CHECK = {9: 69, 21: 417, 33: 517, 47: 885, 55: 1036, 60: 1111, 68: 1212, 70: 1228,
          71: 1240, 72: 1240 + RESERVADO}
_p = {o: fim for o, _, _, fim, _ in positions()}
assert all(_p[k] == v for k, v in _CHECK.items()), "SPEC não confere com o manual"
BASE_LEN = 1240 + RESERVADO       # tamanho da linha sem a discriminação


def get(row, key):
    for k in [key] + ALIASES.get(key, []):
        if k in row:
            return row[k]
    return ""


# ───────────────────── NFTS → modelo NFS-e recebida ─────────────────────
COMMON = [
    "Indicador de CPF/CNPJ do Prestador", "CPF/CNPJ do Prestador", "Inscrição Municipal do Prestador",
    "Razão Social do Prestador", "Tipo do Endereço do Prestador", "Endereço do Prestador",
    "Número do Endereço do Prestador", "Complemento do Endereço do Prestador", "Bairro do Prestador",
    "Cidade do Prestador", "UF do Prestador", "CEP do Prestador", "Email do Prestador",
    "Indicador de CPF/CNPJ do Tomador", "CPF/CNPJ do Tomador", "Inscrição Municipal do Tomador",
    "Razão Social do Tomador", "Tipo do Endereço do Tomador", "Endereço do Tomador",
    "Número do Endereço do Tomador", "Complemento do Endereço do Tomador", "Bairro do Tomador",
    "Cidade do Tomador", "UF do Tomador", "CEP do Tomador", "Email do Tomador",
    "Data de Cancelamento", "Nº da Guia", "Valor dos Serviços", "Valor das Deduções", "Alíquota",
    "ISS Retido", "Indicador de CPF/CNPJ do Intermediário", "CPF/CNPJ do Intermediário",
    "Inscrição Municipal do Intermediário", "Razão Social do Intermediário",
    "Repasse do Plano de Saúde", "CEI", "Matrícula da Obra", "Discriminação dos Serviços",
]


def nfts_to_nfse(r, base_data="emissao"):
    """Converte linha de NFTS no modelo NFS-e recebida.
    base_data: 'emissao' ou 'fato'. A data escolhida é gravada nas duas datas
    do TXT (ordem 3 - Data Hora NFE e ordem 8 - Data de emissão do RPS).
    O Tipo de RPS (ordem 5) é sempre preenchido (o importador não aceita em branco)."""
    g = lambda k: clean(r.get(k, ""))
    out = {k: g(k) for k in COMMON}
    serie = g("Série do Documento")
    nsub = digits(g("Nota Substituta"))
    tem_sub = bool(nsub) and int(nsub) != 0
    iss, guia = g("Valor ISS"), g("ISS Recolhido por Guia NFS-e")
    if g("Data de Cancelamento").strip():
        situacao = "C"
    elif tem_sub:
        situacao = "S"          # nota substituída (remova este elif se não quiser)
    else:
        situacao = g("Tributação do Serviço")

    data_ref = (g("Data da Prestação de Serviços") if base_data == "fato"
                else g("Data Hora Emissão NFTS"))

    out.update({
        "Nº NFS-e": g("Nº NFTS"),
        "Data Hora NFE": data_ref,                    # ordem 3
        "Tipo de RPS": TIPO_RPS_NFTS,                 # ordem 5
        "Série do RPS": "" if serie.strip() in ("-", "--") else serie,
        "Número do RPS": g("Número do Documento"),
        "Data do Fato Gerador": data_ref,             # ordem 8 (mesma data da ordem 3)
        "Opção Pelo Simples": g("Regime de Tributação"),
        "Situação da Nota Fiscal": situacao,
        "Data de Quitação da Guia Vinculada a Nota Fiscal": g("Data de Quitação da Guia Vinculada a NFS-e"),
        "Código do Serviço Prestado na Nota Fiscal": g("Código do Serviço Prestado na NFTS"),
        "ISS devido": iss,
        "Valor do Crédito": "0,00",
        "Nº NFS-e Substituta": nsub if tem_sub else "",
        "ISS recolhido": guia,
        "ISS a recolher": "0,00" if (cents(guia) or 0) > 0 else iss,
    })
    return out


# ───────────────────── Leitura do CSV ─────────────────────
def decode(raw):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def read_table(text):
    text = text.replace("\r\n", "\n")
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        raise ValueError("Arquivo vazio.")
    delim = max([";", "\t", ","], key=lambda d: lines[0].count(d))
    raw = list(csv.reader(lines, delimiter=",")) if delim == "," else [l.split(delim) for l in lines]
    header = [h.strip() for h in raw[0]]
    n, rows, merged = len(header), [], 0
    for r in raw[1:]:
        if len(r) == n + 1 and r[-1].strip() == "":
            r = r[:-1]
        if len(r) > n:                       # ';' dentro da Discriminação
            merged += 1
            r = r[:n - 1] + [delim.join(r[n - 1:])]
        elif len(r) < n:
            r = r + [""] * (n - len(r))
        if clean(r[0]).strip().lower() == "total":   # totalizador do CSV (recalculado)
            continue
        rows.append(dict(zip(header, r)))
    return header, rows, merged


def role_of(rows, header):
    p = {digits(get(r, "CPF/CNPJ do Prestador")) for r in rows} - {""}
    t = {digits(get(r, "CPF/CNPJ do Tomador")) for r in rows} - {""}
    if len(p) == 1 and len(t) != 1:
        return "Prestados"
    if len(t) == 1 and len(p) != 1:
        return "Tomados"
    return "Prestados" if "ISS pago" in header else "Tomados"


def contrib_im(rows, role):
    key = "Inscrição Municipal do Prestador" if role == "Prestados" else "Inscrição Municipal do Tomador"
    c = Counter(digits(get(r, key)) for r in rows)
    c.pop("", None)
    return c.most_common(1)[0][0].rjust(8, "0")[:8] if c else ""


# ───────────────────── Retenções federais (PIS / COFINS / CSLL) ─────────────────────
def _brl(c):
    return f"{c // 100},{c % 100:02d}"


def split_retencoes(r):
    """NFS-e emitidas a partir de 14/05/2026: a Prefeitura agrega as retenções de PIS, COFINS
    e CSLL num único valor (ordem 60) e usa PIS/COFINS para o débito próprio.
    Aqui o valor agregado é desmembrado, porque o sistema contábil lê as retenções separadas.
    Retorna (linha, status): anterior | sem retenção | desmembrada | sem correspondência."""
    r = dict(r)
    dt = parse_dt(get(r, "Data Hora NFE"))
    if not dt or dt.date() < DATA_CORTE_FEDERAIS:
        return r, "anterior"

    pis_orig, cof_orig = get(r, "PIS/PASEP"), get(r, "COFINS")
    total = cents(get(r, "CSLL"))
    r["PIS/PASEP"] = ""               # débito próprio: não é retenção
    r["COFINS"] = ""
    if not total:
        return r, "sem retenção"

    def sem_correspondencia():
        r["PIS/PASEP"], r["COFINS"] = pis_orig, cof_orig
        return r, "sem correspondência"

    base = (cents(get(r, "Valor dos Serviços")) or 0) - (cents(get(r, "Valor das Deduções")) or 0)
    if base <= 0:
        return sem_correspondencia()

    melhor, melhor_dif = None, None
    for combo in COMBOS:
        vals = {k: int((Decimal(base) * ALIQ[k]).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
                for k in combo}
        dif = abs(sum(vals.values()) - total)
        if dif <= TOL_CENTS and (melhor_dif is None or dif < melhor_dif):
            melhor, melhor_dif = (combo, vals), dif
    if melhor is None:
        return sem_correspondencia()

    combo, vals = melhor
    vals[combo[-1]] += total - sum(vals.values())     # fecha a soma exata com o valor da prefeitura
    r["PIS/PASEP"] = _brl(vals["PIS"]) if "PIS" in vals else ""
    r["COFINS"] = _brl(vals["COFINS"]) if "COFINS" in vals else ""
    r["CSLL"] = _brl(vals["CSLL"]) if "CSLL" in vals else ""
    r["_agreg"] = total                               # valor agregado original (ordem 60), só p/ conferência
    return r, "desmembrada"


# ───────────────────── Filtros ─────────────────────
def filter_period(rows, d1, d2):
    """Mantém só as notas cuja Data Hora NFE (ordem 3) está entre d1 e d2, inclusive.
    Notas sem data válida ficam de fora."""
    dentro, fora = [], []
    for r in rows:
        dt = parse_dt(get(r, "Data Hora NFE"))
        if dt and d1 <= dt.date() <= d2:
            dentro.append(r)
        else:
            fora.append(r)
    return dentro, fora


def is_cancelada(r):
    """Cancelada: situação 'C' ou data de cancelamento preenchida."""
    sit = clean(get(r, "Situação da Nota Fiscal")).strip().upper()
    return sit == "C" or bool(clean(get(r, "Data de Cancelamento")).strip())


# ───────────────────── Geração do TXT ─────────────────────
def must_zero(r):
    """Cancelada (C), extraviada (E) ou substituída (S) -> todos os valores zerados."""
    sit = clean(get(r, "Situação da Nota Fiscal")).strip().upper()
    return sit in ZERO_SITUACOES or bool(clean(get(r, "Data de Cancelamento")).strip())


def build_detail(r, ctx):
    r = dict(r)
    zeroed = must_zero(r)
    if zeroed:
        for k in ZERO_FIELDS:
            r[k] = "0,00"
    parts, vals = [], {}
    for ordem, nome, w, tipo, src, default in SPEC:
        s = fmt(tipo, get(r, src) if src else "", w, default, ctx, nome)
        assert len(s) == w, (nome, len(s), w)
        parts.append(s)
        vals[ordem] = s
    d = latin1_safe(clean(get(r, "Discriminação dos Serviços")))
    d = re.sub(r"\r\n|\r|\n", "|", d).replace("\t", " ").strip()   # CR+LF -> '|' (manual)
    if len(d) > 1000:
        ctx.warn("Discriminação truncada em 1000 caracteres")
        d = d[:1000]
    return "".join(parts) + d, vals, d, zeroed


def convert(rows, im, d1, d2):
    ctx = Ctx()
    lines = ["1" + VERSAO + im.rjust(8, "0")[:8] + d1.strftime("%Y%m%d") + d2.strftime("%Y%m%d")]
    recs, tot, n_zero = [], [0, 0, 0, 0], 0
    ret = {"PIS": 0, "COFINS": 0, "INSS": 0, "IR": 0, "CSSL": 0}
    iss_ret, n_iss_ret = 0, 0
    for r in rows:
        num = digits(get(r, "Nº NFS-e"))
        ctx.cur = f"Nota {num or '?'}"
        line, vals, disc, zeroed = build_detail(r, ctx)
        if len(line) != BASE_LEN + len(disc):
            ctx.warn(f"tamanho inesperado da linha ({len(line)})")
        lines.append(line)
        n_zero += zeroed
        for i, o in enumerate((27, 28, 31, 32)):
            tot[i] += int(vals[o])
        for nome_r, o in (("PIS", 56), ("COFINS", 57), ("INSS", 58), ("IR", 59), ("CSSL", 60)):
            ret[nome_r] += int(vals[o]) if vals[o].strip() else 0
        if vals[33] == "S" and not zeroed:
            iss_ret += int(vals[31])
            n_iss_ret += 1
        recs.append({"Nº": num, "Emissão": vals[3][:8], "Tipo RPS": vals[5].strip(),
                     "Data RPS": vals[8].strip(),
                     "Situação": vals[23].strip(),
                     "Zerada": "Sim" if zeroed else "",
                     "Valor serviços": br(int(vals[27])), "ISS": br(int(vals[31])),
                     "ISS retido": vals[33],
                     "Crédito": br(int(vals[32])),
                     "PIS": br_or_blank(vals[56]), "COFINS": br_or_blank(vals[57]),
                     "CSSL": br_or_blank(vals[60]),
                     "Σ PIS+COFINS+CSSL": br(sum(int(vals[o]) for o in (56, 57, 60) if vals[o].strip())),
                     "Tam. linha": len(line),
                     "_vals": vals, "_disc": disc})
    n = len(rows)
    lines.append("9" + str(n).rjust(7, "0") + "".join(str(t).rjust(15, "0") for t in tot))
    txt = CRLF.join(lines) + CRLF
    return {"txt_bytes": txt.encode("latin-1", errors="replace"), "recs": recs, "tot": tot,
            "ret": ret, "iss_ret": iss_ret, "n_iss_ret": n_iss_ret,
            "n": n, "n_zero": n_zero, "warnings": ctx.warnings, "lines": lines}


def out_name(name, im, d1, d2):
    """TXT v004 - <nome> IM <8 dígitos> <DDMMAAAA_DDMMAAAA>.txt"""
    im8 = digits(im).rjust(8, "0")[:8]
    core = im8.lstrip("0") or im8
    base = re.sub(r"\.csv$", "", name, flags=re.I)
    base = re.sub(r"^(CSV|TXT)\s*-\s*", "", base, flags=re.I)
    base = PERIOD_RE.sub("", base)                                        # período AAAAMMDD_AAAAMMDD
    base = re.sub(rf"[_\s-]*(?<!\d){re.escape(core)}(?!\d)", "", base)    # IM repetida no nome
    base = re.sub(r'[\\/:*?"<>|]+', " ", base).strip(" _-")
    base = base or "NOTAS"
    periodo = f"{d1:%d%m%Y}_{d2:%d%m%Y}"
    return f"TXT v004 - {base} IM {im8} {periodo}.txt"


# ───────────────────── Sidebar ─────────────────────
with st.sidebar:
    st.header("Opções")
    logo = st.file_uploader("Logo (opcional)", type=["png", "jpg", "jpeg"])
    if logo:
        st.image(logo)
    st.caption("Parâmetros fixos: leiaute **v004**, campo reservado de **145** posições, "
               "todos os valores zerados nas notas **C**, **E** e **S**, "
               "data de emissão gravada **sem hora** (000000). "
               "Na **NFTS**, o Tipo de RPS é gravado como **RPS** e as duas datas do TXT "
               "(Data/Hora NFE e Data do RPS) são iguais, conforme a escolha feita na aba. "
               "O TXT contém **somente as notas do período selecionado** "
               "(filtro pela Data/Hora NFE, início e fim inclusive). "
               "Em **Tomados** e em **NFTS**, notas **canceladas não geram linha**. "
               "Nas **NFS-e** emitidas a partir de 14/05/2026, as retenções agregadas de "
               "PIS/COFINS/CSLL podem ser **desmembradas** (opção na aba).")

files = st.file_uploader("Selecione os CSVs (NFS-e prestados, NFS-e tomados e/ou NFTS)",
                         type=["csv"], accept_multiple_files=True)
if not files:
    st.info("Envie um ou mais arquivos .CSV para iniciar a conversão.")
    st.stop()

results = {}
used_names = set()
tabs = st.tabs([f.name for f in files])
for i, (tab, f) in enumerate(zip(tabs, files)):
    with tab:
        try:
            header, rows, merged = read_table(decode(f.getvalue()))
            kind = "NFTS" if "Nº NFTS" in header else "NFS-e"

            # Escolha da data (somente NFTS): as duas datas do TXT ficam iguais
            base_data = "emissao"
            if kind == "NFTS":
                escolha = st.radio(
                    "Data a usar nas duas datas do TXT (Data/Hora NFE e Data de emissão do RPS)",
                    [OPC_EMISSAO, OPC_FATO], index=1, horizontal=True, key=f"data_{i}")
                base_data = "fato" if escolha == OPC_FATO else "emissao"

            canon = [nfts_to_nfse(r, base_data) for r in rows] if kind == "NFTS" else rows

            # Retenções federais agregadas (somente NFS-e)
            split_cnt, sem_corr = None, []
            if kind == "NFS-e" and canon:
                desm = st.checkbox(
                    "Desmembrar PIS/COFINS/CSLL retidos (NFS-e a partir de 14/05/2026)",
                    value=True, key=f"split_{i}",
                    help="A Prefeitura passou a agregar as retenções num só valor (ordem 60). "
                         "O app separa PIS, COFINS e CSLL para o sistema contábil.")
                if desm:
                    res_split = [split_retencoes(r) for r in canon]
                    canon = [x[0] for x in res_split]
                    split_cnt = Counter(x[1] for x in res_split)
                    sem_corr = [digits(get(x[0], "Nº NFS-e"))
                                for x in res_split if x[1] == "sem correspondência"]

            if not canon:
                st.warning("Nenhum registro de detalhe encontrado.")
                continue
            det_role = "Tomados" if kind == "NFTS" else role_of(canon, header)

            c1, c2, c3 = st.columns([1, 1, 2])
            role = c1.selectbox("Papel do contribuinte", ["Prestados", "Tomados"],
                                index=["Prestados", "Tomados"].index(det_role), key=f"role_{i}",
                                help="Define de qual parte sai a Inscrição Municipal do cabeçalho.")
            im = c2.text_input("Inscrição Municipal (8 dígitos)", contrib_im(canon, role), max_chars=8,
                               key=f"im_{i}_{role}")

            dts = [d for d in (parse_dt(get(r, "Data Hora NFE")) for r in canon) if d]
            dmin = min(dts).date() if dts else date.today()
            dmax = max(dts).date() if dts else date.today()
            nome_per = period_from_name(f.name)          # período no nome do arquivo tem prioridade
            if nome_per:
                dmin, dmax = nome_per
            with c3:
                p1, p2 = st.columns(2)
                d1 = p1.date_input("Início do período", dmin,
                                   key=f"d1_{i}_{role}_{base_data}", format="DD/MM/YYYY")
                d2 = p2.date_input("Fim do período", dmax,
                                   key=f"d2_{i}_{role}_{base_data}", format="DD/MM/YYYY")

            if not im.strip():
                st.error("Informe a Inscrição Municipal do contribuinte para o cabeçalho.")
                continue

            if d1 > d2:
                st.error("O início do período não pode ser maior que o fim.")
                continue

            # Somente as notas do período selecionado entram no TXT
            canon_per, fora = filter_period(canon, d1, d2)

            # Tomados e NFTS: notas canceladas não geram linha
            canceladas = []
            if role == "Tomados" or kind == "NFTS":
                canceladas = [r for r in canon_per if is_cancelada(r)]
                canon_per = [r for r in canon_per if not is_cancelada(r)]

            if not canon_per:
                st.warning("Nenhuma nota com data dentro do período selecionado "
                           "(ou todas canceladas). Ajuste o período ou a data escolhida.")
                continue

            res = convert(canon_per, im, d1, d2)

            # nome do arquivo: IM + período (único dentro do ZIP)
            fname = out_name(f.name, im, d1, d2)
            if fname in used_names:
                stem, k = fname[:-4], 2
                while f"{stem} ({k}).txt" in used_names:
                    k += 1
                fname = f"{stem} ({k}).txt"
            used_names.add(fname)
            res["fname"] = fname
            results[f.name] = res

            m1 = st.columns(5)
            m1[0].metric("Origem", "NFTS → recebidas" if kind == "NFTS" else "NFS-e")
            m1[1].metric("Linhas de detalhe", res["n"])
            m1[2].metric("Notas zeradas (C/E/S)", res["n_zero"])
            m1[3].metric("Canceladas (não geradas)", len(canceladas))
            m1[4].metric("Fora do período (excluídas)", len(fora))
            m2 = st.columns(3)
            m2[0].metric("Σ Valor dos serviços", br(res["tot"][0]))
            m2[1].metric("Σ ISS", br(res["tot"][2]))
            m2[2].metric("Σ Crédito", br(res["tot"][3]))

            # ISS retido
            m4 = st.columns(2)
            m4[0].metric("Σ ISS retido (ISS retido = S)", br(res["iss_ret"]))
            m4[1].metric("Notas com ISS retido", res["n_iss_ret"])

            # Somas das retenções federais (PIS, COFINS, CSLL, INSS, IR)
            r1 = res["ret"]
            if kind == "NFS-e" or sum(r1.values()) > 0:
                m3 = st.columns(6)
                m3[0].metric("Σ PIS retido", br(r1["PIS"]))
                m3[1].metric("Σ COFINS retido", br(r1["COFINS"]))
                m3[2].metric("Σ CSLL retido", br(r1["CSSL"]))
                m3[3].metric("Σ INSS", br(r1["INSS"]))
                m3[4].metric("Σ IR", br(r1["IR"]))
                m3[5].metric("Σ PIS+COFINS+CSLL", br(r1["PIS"] + r1["COFINS"] + r1["CSSL"]))

            # Conferência do desmembramento: soma separada x valor agregado da Prefeitura (ordem 60)
            desm_rows = [r for r in canon_per if "_agreg" in r and not must_zero(r)]
            if desm_rows:
                agreg = sum(r["_agreg"] for r in desm_rows)
                separado = sum((cents(get(r, k)) or 0) for r in desm_rows
                               for k in ("PIS/PASEP", "COFINS", "CSLL"))
                if agreg == separado:
                    st.success(f"Conferência OK: {len(desm_rows)} nota(s) desmembrada(s). "
                               f"Agregado da Prefeitura = soma de PIS+COFINS+CSLL = R$ {br(agreg)}")
                else:
                    st.error(f"Divergência: agregado R$ {br(agreg)} x separado R$ {br(separado)}")

            if split_cnt is not None:
                st.caption(f"Retenções federais: {split_cnt.get('desmembrada', 0)} desmembradas, "
                           f"{split_cnt.get('sem retenção', 0)} sem retenção, "
                           f"{split_cnt.get('anterior', 0)} anteriores a 14/05/2026, "
                           f"{split_cnt.get('sem correspondência', 0)} sem correspondência.")
                if sem_corr:
                    st.warning("Notas com valor de retenção que não bate com nenhuma combinação de "
                               "PIS/COFINS/CSLL (mantidas como vieram, conferir): "
                               + ", ".join(sem_corr[:30]) + ("…" if len(sem_corr) > 30 else ""))

            if len(res["txt_bytes"]) > MAX_BYTES:
                st.error("O arquivo gerado excede 10 MB (limite do manual). Divida por período.")
            if merged:
                st.warning(f"{merged} linha(s) com ';' na discriminação foram reunidas no último campo.")
            if res["warnings"]:
                with st.expander(f"⚠️ {len(res['warnings'])} aviso(s) de conversão"):
                    st.write("\n".join(f"- {w}" for w in res["warnings"]))

            st.download_button(f"⬇️ Baixar {fname}", res["txt_bytes"], file_name=fname,
                               mime="text/plain", key=f"dl_{i}")

            if canceladas:
                with st.expander(f"Notas canceladas ({len(canceladas)}) – não incluídas no TXT"):
                    st.dataframe(pd.DataFrame([{
                        "Nº": digits(get(r, "Nº NFS-e")),
                        "Cancelamento": clean(get(r, "Data de Cancelamento")).strip(),
                        "Valor serviços": br(cents(get(r, "Valor dos Serviços")) or 0),
                    } for r in canceladas]), hide_index=True)

            if fora:
                with st.expander(f"Notas fora do período ({len(fora)}) – não incluídas no TXT"):
                    linhas_fora = []
                    for r in fora:
                        dt = parse_dt(get(r, "Data Hora NFE"))
                        linhas_fora.append({
                            "Nº": digits(get(r, "Nº NFS-e")),
                            "Data (ordem 3)": dt.strftime("%d/%m/%Y") if dt else "sem data",
                        })
                    st.dataframe(pd.DataFrame(linhas_fora), hide_index=True)

            with st.expander("Pré-visualização dos registros"):
                st.dataframe(pd.DataFrame(res["recs"]).drop(columns=["_vals", "_disc"]),
                             hide_index=True)
                st.caption("Cabeçalho: `" + res["lines"][0] + "`  |  Rodapé: `" + res["lines"][-1] + "`")

            with st.expander("Inspetor de posições (confira com o manual)"):
                idx = st.selectbox("Registro", range(len(res["recs"])), key=f"insp_{i}",
                                   format_func=lambda k: f"Nota {res['recs'][k]['Nº']}")
                rec, pos, tab_rows = res["recs"][idx], positions(), []
                for ordem, nome, ini, fim, w in pos:
                    tab_rows.append({"Ordem": ordem, "Campo": nome, "Início": ini, "Fim": fim,
                                     "Tam.": w, "Conteúdo": f"[{rec['_vals'][ordem]}]"})
                ini = pos[-1][3] + 1
                tab_rows.append({"Ordem": 73, "Campo": "Discriminação dos serviços", "Início": ini,
                                 "Fim": ini + len(rec["_disc"]) - 1, "Tam.": len(rec["_disc"]),
                                 "Conteúdo": f"[{rec['_disc']}]"})
                st.dataframe(pd.DataFrame(tab_rows), hide_index=True, height=380)
        except Exception as e:
            st.error(f"Falha ao converter **{f.name}**: {e}")

if len(results) > 1:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, res in results.items():
            z.writestr(res["fname"], res["txt_bytes"])
    st.divider()
    st.download_button("⬇️ Baixar todos (ZIP)", buf.getvalue(),
                       file_name="NFSe_NFTS_TXT_v004.zip", mime="application/zip")
