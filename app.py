import csv
import io
import re
import unicodedata
import zipfile
from collections import Counter
from datetime import date, datetime

import pandas as pd
import streamlit as st
from decimal import Decimal, InvalidOperation

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
.tr-header h1{color:#fff;font-size:1.6rem;margin:0;padding:0;font-weight:600;}
.tr-header p{color:#D8D8D8;margin:2px 0 0 0;font-size:.9rem;}
.stButton>button,.stDownloadButton>button{background:#FA6400;color:#fff;border:0;border-radius:2px;font-weight:600;}
.stButton>button:hover,.stDownloadButton>button:hover{background:#D64000;color:#fff;}
[data-testid="stMetric"]{background:#F5F5F5;border-left:4px solid #FA6400;padding:10px 14px;border-radius:2px;}
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

CRLF = "\r\n"
MAX_BYTES = 10240 * 1024  # limite do manual (10 MB)


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


def latin1_safe(s):
    """Garante que 1 caractere = 1 byte em ISO-8859-1 (necessário p/ largura fixa)."""
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
        return dt.strftime("%Y%m%d" if kind == "D" else "%Y%m%d%H%M%S")
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
    (72, "Campo reservado", 132, "BLANK", None, None),
]
ALIASES = {
    "ISS recolhido": ["ISS pago"],
    "ISS a recolher": ["ISS a pagar"],
    "CSLL": ["CSSL"],
    "Encapsulamento": ["Número de seleção de notas dedutoras"],
    "Data do Fato Gerador": ["Data de Emissão do RPS"],
}


def positions(reservado=132):
    out, start = [], 1
    for ordem, nome, w, *_ in SPEC:
        if ordem == 72:
            w = reservado
        out.append((ordem, nome, start, start + w - 1, w))
        start += w
    return out


# Autoconferência com o manual (posições finais)
_CHECK = {9: 69, 21: 417, 33: 517, 47: 885, 55: 1036, 60: 1111, 68: 1212, 70: 1228, 71: 1240, 72: 1372}
_p = {o: fim for o, _, _, fim, _ in positions()}
assert all(_p[k] == v for k, v in _CHECK.items()), "SPEC não confere com o manual"


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


def nfts_to_nfse(r):
    g = lambda k: clean(r.get(k, ""))
    out = {k: g(k) for k in COMMON}
    serie = g("Série do Documento")
    nsub = digits(g("Nota Substituta"))
    iss, guia = g("Valor ISS"), g("ISS Recolhido por Guia NFS-e")
    out.update({
        "Nº NFS-e": g("Nº NFTS"),
        "Data Hora NFE": g("Data Hora Emissão NFTS"),
        "Série do RPS": "" if serie.strip() in ("-", "--") else serie,
        "Número do RPS": g("Número do Documento"),
        "Data do Fato Gerador": g("Data da Prestação de Serviços"),
        "Opção Pelo Simples": g("Regime de Tributação"),
        "Situação da Nota Fiscal": "C" if g("Data de Cancelamento").strip() else g("Tributação do Serviço"),
        "Data de Quitação da Guia Vinculada a Nota Fiscal": g("Data de Quitação da Guia Vinculada a NFS-e"),
        "Código do Serviço Prestado na Nota Fiscal": g("Código do Serviço Prestado na NFTS"),
        "ISS devido": iss,
        "Valor do Crédito": "0,00",
        "Nº NFS-e Substituta": "" if (not nsub or int(nsub) == 0) else nsub,
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
        if clean(r[0]).strip().lower() == "total":   # totalizador do CSV (será recalculado)
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


# ───────────────────── Geração do TXT ─────────────────────
def build_detail(r, ctx, zero_cancel, reservado):
    r = dict(r)
    sit = clean(get(r, "Situação da Nota Fiscal")).strip().upper()
    if zero_cancel and sit in ("C", "E"):
        for k in ("Valor dos Serviços", "Valor das Deduções", "ISS devido", "Valor do Crédito"):
            r[k] = "0,00"
    parts, vals = [], {}
    for ordem, nome, w, tipo, src, default in SPEC:
        if ordem == 72:
            w = reservado
        s = fmt(tipo, get(r, src) if src else "", w, default, ctx, nome)
        assert len(s) == w, (nome, len(s), w)
        parts.append(s)
        vals[ordem] = s
    d = latin1_safe(clean(get(r, "Discriminação dos Serviços")))
    d = re.sub(r"\r\n|\r|\n", "|", d).replace("\t", " ").strip()   # CR+LF -> '|' (manual)
    if len(d) > 1000:
        ctx.warn("Discriminação truncada em 1000 caracteres")
        d = d[:1000]
    return "".join(parts) + d, vals, d


def convert(rows, im, d1, d2, version, zero_cancel, reservado):
    ctx = Ctx()
    lines = ["1" + version[:3].rjust(3, "0") + im.rjust(8, "0")[:8] + d1.strftime("%Y%m%d") + d2.strftime("%Y%m%d")]
    recs, tot = [], [0, 0, 0, 0]
    for r in rows:
        num = digits(get(r, "Nº NFS-e"))
        ctx.cur = f"Nota {num or '?'}"
        line, vals, disc = build_detail(r, ctx, zero_cancel, reservado)
        if len(line) != 1372 - (132 - reservado) * -1 + len(disc) - 0 and len(line) != (1240 + reservado + len(disc)):
            ctx.warn(f"tamanho inesperado da linha ({len(line)})")
        lines.append(line)
        for i, o in enumerate((27, 28, 31, 32)):
            tot[i] += int(vals[o])
        recs.append({"Nº": num, "Emissão": vals[3][:8], "Situação": vals[23].strip(),
                     "Valor serviços": br(int(vals[27])), "ISS": br(int(vals[31])),
                     "Crédito": br(int(vals[32])), "Tam. linha": len(line), "_vals": vals, "_disc": disc})
    n = len(rows)
    lines.append("9" + str(n).rjust(7, "0") + "".join(str(t).rjust(15, "0") for t in tot))
    txt = CRLF.join(lines) + CRLF
    return {"txt_bytes": txt.encode("latin-1", errors="replace"), "recs": recs, "tot": tot,
            "n": n, "warnings": ctx.warnings, "lines": lines}


def out_name(name):
    base = re.sub(r"\.csv$", "", name, flags=re.I)
    base = re.sub(r"^CSV\s*-\s*", "", base, flags=re.I)
    return f"TXT v004 - {base}.txt"


# ───────────────────── Sidebar ─────────────────────
with st.sidebar:
    st.header("Opções")
    version = st.text_input("Versão no cabeçalho (registro 1)", "004", max_chars=3)
    zero_cancel = st.checkbox("Zerar valores de notas canceladas/extraviadas (C/E)", False,
                              help="O manual permite zeros nos 15 dígitos do valor dos serviços nessas situações.")
    res_opt = st.radio("Campo reservado (ordem 72)",
                       ["132 posições (1241–1372; discriminação em 1373)", "145 posições (tamanho citado no manual)"])
    reservado = 132 if res_opt.startswith("132") else 145
    logo = st.file_uploader("Logo (opcional)", type=["png", "jpg", "jpeg"])
    if logo:
        st.image(logo, use_container_width=True)

files = st.file_uploader("Selecione os CSVs (NFS-e prestados, NFS-e tomados e/ou NFTS)",
                         type=["csv"], accept_multiple_files=True)
if not files:
    st.info("Envie um ou mais arquivos .CSV para iniciar a conversão.")
    st.stop()

results = {}
tabs = st.tabs([f.name for f in files])
for i, (tab, f) in enumerate(zip(tabs, files)):
    with tab:
        try:
            header, rows, merged = read_table(decode(f.getvalue()))
            kind = "NFTS" if "Nº NFTS" in header else "NFS-e"
            canon = [nfts_to_nfse(r) for r in rows] if kind == "NFTS" else rows
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
            with c3:
                p1, p2 = st.columns(2)
                d1 = p1.date_input("Início do período", dmin, key=f"d1_{i}_{role}", format="DD/MM/YYYY")
                d2 = p2.date_input("Fim do período", dmax, key=f"d2_{i}_{role}", format="DD/MM/YYYY")

            if not im.strip():
                st.error("Informe a Inscrição Municipal do contribuinte para o cabeçalho.")
                continue

            res = convert(canon, im, d1, d2, version, zero_cancel, reservado)
            results[f.name] = res
            fname = out_name(f.name)

            m = st.columns(5)
            m[0].metric("Origem", "NFTS → recebidas" if kind == "NFTS" else "NFS-e")
            m[1].metric("Linhas de detalhe", res["n"])
            m[2].metric("Σ Valor dos serviços", br(res["tot"][0]))
            m[3].metric("Σ ISS", br(res["tot"][2]))
            m[4].metric("Σ Crédito", br(res["tot"][3]))

            if len(res["txt_bytes"]) > MAX_BYTES:
                st.error("O arquivo gerado excede 10 MB (limite do manual). Divida por período.")
            if merged:
                st.warning(f"{merged} linha(s) com ';' na discriminação foram reunidas no último campo.")
            if res["warnings"]:
                with st.expander(f"⚠️ {len(res['warnings'])} aviso(s) de conversão"):
                    st.write("\n".join(f"- {w}" for w in res["warnings"]))

            st.download_button(f"⬇️ Baixar {fname}", res["txt_bytes"], file_name=fname,
                               mime="text/plain", key=f"dl_{i}")

            with st.expander("Pré-visualização dos registros"):
                st.dataframe(pd.DataFrame(res["recs"]).drop(columns=["_vals", "_disc"]),
                             hide_index=True, use_container_width=True)
                st.caption("Cabeçalho: `" + res["lines"][0] + "`  |  Rodapé: `" + res["lines"][-1] + "`")

            with st.expander("Inspetor de posições (confira com o manual)"):
                idx = st.selectbox("Registro", range(len(res["recs"])), key=f"insp_{i}",
                                   format_func=lambda k: f"Nota {res['recs'][k]['Nº']}")
                rec, pos, tab_rows = res["recs"][idx], positions(reservado), []
                for ordem, nome, ini, fim, w in pos:
                    tab_rows.append({"Ordem": ordem, "Campo": nome, "Início": ini, "Fim": fim,
                                     "Tam.": w, "Conteúdo": f"[{rec['_vals'][ordem]}]"})
                ini = pos[-1][3] + 1
                tab_rows.append({"Ordem": 73, "Campo": "Discriminação dos serviços", "Início": ini,
                                 "Fim": ini + len(rec["_disc"]) - 1, "Tam.": len(rec["_disc"]),
                                 "Conteúdo": f"[{rec['_disc']}]"})
                st.dataframe(pd.DataFrame(tab_rows), hide_index=True, use_container_width=True, height=380)
        except Exception as e:
            st.error(f"Falha ao converter **{f.name}**: {e}")

if len(results) > 1:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, res in results.items():
            z.writestr(out_name(name), res["txt_bytes"])
    st.divider()
    st.download_button("⬇️ Baixar todos (ZIP)", buf.getvalue(),
                       file_name="NFSe_NFTS_TXT_v004.zip", mime="application/zip")
