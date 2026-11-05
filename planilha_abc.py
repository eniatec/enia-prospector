"""Monta a planilha de validação (.xlsx) a partir do CSV final e do CSV do Kipflow. Precisa de openpyxl.

Uso: python planilha_abc.py <slug>-final.csv <slug>-kipflow.csv <slug>-validacao.xlsx ["descrição da busca"]
"""
import csv
import datetime
import sys

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation

final_csv, kipflow_csv, out = sys.argv[1:4]
busca = sys.argv[4] if len(sys.argv) > 4 else "prospecção"
hoje = datetime.date.today().strftime("%d/%m/%Y")
rows = list(csv.DictReader(open(final_csv, encoding="utf-8-sig", newline="")))
extra = {r["title"]: r for r in csv.DictReader(open(kipflow_csv, encoding="utf-8-sig", newline=""))}

F = "Arial"
HEAD = PatternFill("solid", start_color="1F3864")
INPUT = PatternFill("solid", start_color="FFF2CC")
thin = Side(style="thin", color="D9D9D9")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)

COLS = [  # (cabeçalho, chave no CSV, largura)
    ("Validação", None, 13), ("Observação", None, 34),
    ("Nota", "nota", 7), ("Motivo da nota", "motivo_nota", 48), ("Escritório", "title", 34),
    ("Decisor", "decisor_nome", 30), ("Cargo", "decisor_cargo", 26), ("E-mail do decisor", "decisor_email", 34),
    ("E-mail verificado", "decisor_email_verificado", 11), ("Telefone (Maps)", "phone", 17),
    ("Site", "website", 32), ("Endereço", "address", 40), ("Nota Google", "review_rating", 9),
    ("Avaliações", "review_count", 11), ("Intent score", "intent_score", 9), ("CNPJ", "cnpj", 17),
    ("Razão social", "razao_social", 40), ("Situação", "situacao_cadastral", 10), ("Sócios", "socios", 60),
    ("E-mails do site", "emails", 34), ("Instagram", "instagram", 28), ("LinkedIn", "linkedin", 28),
    ("Status Kipflow", "kipflow_status", 15), ("Custo Kipflow (R$)", "custo_kipflow", 11),
]
NUMERIC = {"review_rating": float, "review_count": int, "intent_score": int, "custo_kipflow": float}

wb = Workbook()
ws = wb.active
ws.title = "Leads"
for c, (h, _, w) in enumerate(COLS, 1):
    cell = ws.cell(row=1, column=c, value=h)
    cell.font = Font(name=F, bold=True, color="FFFFFF")
    cell.fill = HEAD
    cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.column_dimensions[cell.column_letter].width = w
ws.row_dimensions[1].height = 32

for i, r in enumerate(rows, 2):
    r["socios"] = extra.get(r["title"], {}).get("socios", "")
    r["custo_kipflow"] = extra.get(r["title"], {}).get("custo_kipflow", "")
    for c, (_, key, _) in enumerate(COLS, 1):
        v = r.get(key, "") if key else None
        if key in NUMERIC and v not in ("", None):
            try:
                v = NUMERIC[key](float(v))
            except ValueError:
                pass
        if key == "cnpj" and v:
            v = f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"
        cell = ws.cell(row=i, column=c, value=v if v != "" else None)
        cell.font = Font(name=F, size=10)
        cell.border = BOX
        cell.alignment = Alignment(vertical="top", wrap_text=key in ("motivo_nota", "socios", "address"))
        if key is None:
            cell.fill = INPUT
        if key == "custo_kipflow":
            cell.number_format = "#,##0.00"
last = len(rows) + 1
ws.cell(row=1, column=1).comment = Comment("Escolha: OK, Ajustar ou Descartar.", "Claude")

dv = DataValidation(type="list", formula1='"OK,Ajustar,Descartar"', allow_blank=True)
ws.add_data_validation(dv)
dv.add(f"A2:A{last}")

for nota, cor in (("A", "C6EFCE"), ("B", "FFEB9C"), ("C", "E7E6E6")):
    ws.conditional_formatting.add(f"C2:C{last}", FormulaRule(formula=[f'$C2="{nota}"'],
                                  fill=PatternFill("solid", start_color=cor), font=Font(name=F, bold=True)))
ws.conditional_formatting.add(f"A2:A{last}", FormulaRule(formula=['$A2="OK"'], fill=PatternFill("solid", start_color="C6EFCE")))
ws.conditional_formatting.add(f"A2:A{last}", FormulaRule(formula=['$A2="Descartar"'], fill=PatternFill("solid", start_color="FFC7CE")))
ws.freeze_panes = "F2"
ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(COLS)).column_letter}{last}"

# ---- Funil (fórmulas sobre a aba Leads) ----
fs = wb.create_sheet("Funil")
col = {key: ws.cell(row=1, column=c).column_letter for c, (_, key, _) in enumerate(COLS, 1) if key}
rng = lambda k: f"Leads!${col[k]}$2:${col[k]}${last}"
val = f"Leads!$A$2:$A${last}"
linhas = [
    ("Leads coletados no Maps", f"=COUNTA({rng('title')})", None),
    ("Com domínio próprio", f'=COUNTA({rng("website")})-COUNTIF({rng("kipflow_status")},"sem_dominio_proprio")', None),
    ("Com CNPJ encontrado", f'=COUNTIF({rng("cnpj")},"?*")', None),
    ("Com CNPJ ativo", f'=COUNTIF({rng("situacao_cadastral")},"ATIVA")', None),
    ("Com decisor identificado", f'=COUNTIF({rng("decisor_nome")},"?*")', None),
    ("Com e-mail do decisor verificado", f'=COUNTIF({rng("decisor_email_verificado")},"sim")', None),
    ("Nota A", f'=COUNTIF({rng("nota")},"A")', None),
    ("Nota B", f'=COUNTIF({rng("nota")},"B")', None),
    ("Nota C", f'=COUNTIF({rng("nota")},"C")', None),
    ("Valor das consultas Kipflow (R$)", f"=SUM({rng('custo_kipflow')})", "#,##0.00"),
    ("Valor das consultas por lead A (R$)", "=IFERROR(B11/B8,0)", "#,##0.00"),
    ("Validados como OK", f'=COUNTIF({val},"OK")', None),
    ("Para ajustar", f'=COUNTIF({val},"Ajustar")', None),
    ("Descartados", f'=COUNTIF({val},"Descartar")', None),
]
fs["A1"], fs["B1"] = "Etapa", "Total"
for c in ("A1", "B1"):
    fs[c].font = Font(name=F, bold=True, color="FFFFFF")
    fs[c].fill = HEAD
for i, (label, formula, fmt) in enumerate(linhas, 2):
    fs.cell(row=i, column=1, value=label).font = Font(name=F)
    cell = fs.cell(row=i, column=2, value=formula)
    cell.font = Font(name=F)
    if fmt:
        cell.number_format = fmt
fs.column_dimensions["A"].width = 34
fs.column_dimensions["B"].width = 12
fs["A17"] = "Valor das consultas: campo cost das respostas do Kipflow. Consultas repetidas vêm do cache e não são cobradas de novo."
fs["A18"] = f"Busca: {busca}, coleta em {hoje}."
for c in ("A17", "A18"):
    fs[c].font = Font(name=F, size=9, italic=True, color="595959")

# ---- Legenda ----
lg = wb.create_sheet("Legenda")
texto = [
    "Como validar",
    "1. Na aba Leads, as colunas amarelas (Validação e Observação) são as únicas para preencher.",
    "2. Validação: OK (pronto para abordar), Ajustar (dado errado ou faltando) ou Descartar.",
    "   Exemplo: Validação = Ajustar | Observação = O contato é associado, não sócio; trocar pelo sócio-administrador.",
    "3. A aba Funil se atualiza sozinha com as contagens.",
    "",
    "Notas",
    "A: CNPJ ativo + sócio decisor + e-mail entregável + nota Google >= 4,5 com 30+ avaliações ou pixel de anúncio.",
    "B: CNPJ ativo + decisor identificado, mas sem e-mail verificado ou com contato que não é sócio.",
    "C: CNPJ não encontrado ou inativo.",
    "",
    f"Dados pessoais (LGPD): origem Google Maps + Kipflow, coletados em {hoje}. Não compartilhar fora do time.",
]
for i, t in enumerate(texto, 1):
    lg.cell(row=i, column=1, value=t).font = Font(name=F, bold=t in ("Como validar", "Notas"))
lg.column_dimensions["A"].width = 110

wb.calculation.fullCalcOnLoad = True
wb.save(out)
print("ok", out, len(rows), "linhas")
