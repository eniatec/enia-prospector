"""Dá nota A/B/C aos leads enriquecidos pelo kipflow_enrich.py e escreve o CSV final.

Uso: python nota_abc.py <slug>-kipflow.csv <slug>-final.csv
"""
import csv
import re
import sys

COLS = ["nota", "motivo_nota", "title", "phone", "website", "address", "review_rating", "review_count",
        "intent_score", "cnpj", "razao_social", "situacao_cadastral", "porte", "decisor_nome", "decisor_cargo",
        "decisor_email", "decisor_email_verificado", "decisor_telefone", "decisor_linkedin", "emails",
        "instagram", "linkedin", "kipflow_status"]
AD_TAGS = ("fb_pixel", "google_ads_tag", "bing_ads", "tiktok_pixel", "linkedin_insight")


def grade(r):
    # Empresário individual / MEI: a razão social é "<raiz do CNPJ> <NOME DO TITULAR>".
    m = re.match(r"^[\d.]+\s+(.+)$", r.get("razao_social", ""))
    if m and not r["decisor_nome"]:
        r["decisor_nome"] = m.group(1).title()
        r["decisor_cargo"] = "TITULAR (EMPRESARIO INDIVIDUAL/MEI)"

    ativo = r.get("situacao_cadastral") == "ATIVA"
    decisor, socio = bool(r["decisor_nome"]), bool(r["decisor_cargo"])
    email_ok = r.get("decisor_email_verificado") == "sim"
    try:
        rating, reviews = float(r.get("review_rating") or 0), int(float(r.get("review_count") or 0))
    except ValueError:
        rating, reviews = 0.0, 0
    bem_avaliado = rating >= 4.5 and reviews >= 30
    anuncio = any(r.get(t) == "yes" for t in AD_TAGS)

    if not ativo:
        return "C", ("CNPJ não encontrado pelo domínio do site" if r["kipflow_status"] == "nao_encontrado"
                     else "sem CNPJ ativo")
    if decisor and socio and email_ok and (bem_avaliado or anuncio):
        sinal = f"nota {rating} com {reviews} avaliações" if bem_avaliado else "pixel de anúncio ativo no site"
        return "A", f"CNPJ ativo, {r['decisor_cargo'].lower()} com e-mail entregável e {sinal}"
    if decisor and email_ok and not socio:
        return "B", "e-mail entregável de pessoa do escritório, mas ela não é sócia: confirmar cargo no LinkedIn"
    if decisor:
        return "B", "CNPJ ativo e decisor no quadro societário, sem e-mail verificado"
    return "C", "CNPJ ativo, mas sem decisor identificado"


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["nota"], r["motivo_nota"] = grade(r)
    rows.sort(key=lambda r: r["nota"])
    with open(dst, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['nota']} | {r['title'][:32]:32} | {r['decisor_nome'][:30]:30} | {r['decisor_cargo'][:22]:22} | "
              f"{'sim' if r['decisor_email_verificado'] == 'sim' else 'não'} | {r['phone']}")


if __name__ == "__main__":
    main()
