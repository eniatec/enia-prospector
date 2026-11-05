"""Enriquece um CSV do kit com dados do Kipflow (CNPJ, sócios, decisor e e-mail verificado).

Só biblioteca padrão. Lê KIPFLOW_API_KEY do .env desta pasta e nunca imprime a chave.

Fluxo por linha com domínio próprio:
  1. GET  /companies/v1/search?domain=<d>&datasets=basic,partners    -> CNPJ, cadastro, sócios
  2. GET  /contacts/v1/emails?cnpj=<cnpj>&email_limit=N                -> pessoa mais sênior + e-mail + validation
  3. (se 2 não achar ninguém) POST /contacts/v1/emails/generate-by-domain com o nome do sócio-administrador

Uso:
  python kipflow_enrich.py --in leads.csv --out leads-kipflow.csv [--limit N] [--dry-run]
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.kipflow.io"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "kipflow_cache.json")
COMPANY_DATASETS = "basic,partners"

# Hosts que não são domínio próprio da empresa (redes, agregadores, e-mail grátis, construtores de site).
NOT_OWN = (
    "instagram.com", "facebook.com", "fb.com", "linkedin.com", "linktr.ee", "wa.me", "whatsapp.com",
    "api.whatsapp.com", "jusbrasil.com.br", "sites.google.com", "business.site", "g.page", "google.com",
    "google.com.br", "goo.gl", "wixsite.com", "wix.com", "gmail.com", "hotmail.com", "outlook.com",
    "live.com", "yahoo.com", "yahoo.com.br", "uol.com.br", "bol.com.br", "tiktok.com", "youtube.com",
    "x.com", "twitter.com", "doctoralia.com.br", "escavador.com",
)

NEW_COLS = [
    "kipflow_status", "cnpj", "razao_social", "situacao_cadastral", "cnae_principal", "porte",
    "decisor_nome", "decisor_cargo", "decisor_email", "decisor_email_verificado", "decisor_telefone",
    "decisor_linkedin", "socios", "custo_kipflow",
]


class NoCredits(Exception):
    pass


def read_key():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line.startswith("KIPFLOW_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def own_domain(website):
    """Devolve (dominio, None) ou (None, motivo)."""
    w = (website or "").strip()
    if not w:
        return None, "sem_site"
    if "://" not in w:
        w = "http://" + w
    host = (urllib.parse.urlparse(w).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None, "sem_site"
    for s in NOT_OWN:
        if host == s or host.endswith("." + s):
            return None, "sem_dominio_proprio"
    return host, None


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)


SPENT = [0.0]  # o que foi cobrado nesta execução (respostas do cache não contam)


def call(key, cache, method, path, params=None, body=None):
    """Chama a API com cache. Devolve (status_http, json, custo_da_consulta)."""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    ck = hashlib.sha1(f"{method} {url} {json.dumps(body, sort_keys=True)}".encode()).hexdigest()
    if ck in cache:
        hit = cache[ck]
        js = hit["json"]
        return hit["status"], js, float(js.get("cost") or 0) if isinstance(js, dict) else 0.0
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-API-Key": key, "Accept": "application/json", "User-Agent": "kipflow-enrich/1.0",
            **({"Content-Type": "application/json"} if data else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            return 0, {"error": str(e.reason)}, 0.0
        if status == 429 and attempt < 3:
            time.sleep(2 * (attempt + 1) ** 2)
            continue
        break
    time.sleep(0.25)  # limite da API: 5 req/s
    try:
        js = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        js = {"raw": raw[:300].decode("utf-8", "replace")}
    if status == 402:
        raise NoCredits()
    cost = float(js.get("cost") or 0) if isinstance(js, dict) else 0.0
    SPENT[0] += cost
    if status in (200, 404):  # respostas definitivas: guardar para não pagar de novo
        cache[ck] = {"status": status, "json": js}
        save_cache(cache)
    return status, js, cost


def norm(name):
    s = unicodedata.normalize("NFKD", name or "")
    return " ".join("".join(ch for ch in s if not unicodedata.combining(ch)).upper().split())


def is_person(p, razao_social):
    """Sem perfil no LinkedIn, /contacts/v1/emails devolve o e-mail do cadastro (contador, contato@, webmail).
    Só conta como decisor quem tem nome de pessoa e e-mail fora de webmail."""
    nome = norm(p.get("full_name"))
    email = (p.get("email") or "").lower()
    if not nome or any(ch.isdigit() for ch in nome) or nome == norm(razao_social):
        return False
    host = email.rsplit("@", 1)[-1]
    return "@" in email and not any(host == s or host.endswith("." + s) for s in NOT_OWN)


def pick_admin(socios):
    for s in socios or []:
        if "ADMIN" in (s.get("qualificacao_socio") or "").upper():
            return s
    return (socios or [None])[0]


def enrich_row(key, cache, row, email_limit):
    out = {c: "" for c in NEW_COLS}
    custo = 0.0
    domain, why = own_domain(row.get("website"))
    if not domain:
        out["kipflow_status"] = "sem_dominio_proprio" if why == "sem_dominio_proprio" else "nao_consultado"
        return out, 0.0

    st, js, c = call(key, cache, "GET", "/companies/v1/search", {"domain": domain, "datasets": COMPANY_DATASETS})
    custo += c
    if st == 404:
        out["kipflow_status"] = "nao_encontrado"
        out["custo_kipflow"] = f"{custo:.2f}"
        return out, custo
    if st != 200 or not isinstance(js.get("data"), dict):
        out["kipflow_status"] = f"erro_{st}"
        out["custo_kipflow"] = f"{custo:.2f}"
        return out, custo

    d = js["data"]
    socios = d.get("socios") or []
    out.update({
        "kipflow_status": "validado",
        "cnpj": d.get("cnpj", ""),
        "razao_social": d.get("razao_social", ""),
        "situacao_cadastral": d.get("situacao_cadastral", ""),
        "cnae_principal": d.get("cnae_principal_desc_subclasse") or d.get("cnae_principal_desc_classe") or "",
        "porte": d.get("porte", ""),
        "socios": "; ".join(f"{s.get('nome_socio', '')} ({s.get('qualificacao_socio', '')})" for s in socios),
    })

    # Decisor: pessoa mais sênior com e-mail gerado e validado pelo Kipflow.
    person = None
    if out["cnpj"]:
        st, js, c = call(key, cache, "GET", "/contacts/v1/emails", {"cnpj": out["cnpj"], "email_limit": email_limit})
        custo += c
        people = js.get("data") if st == 200 and isinstance(js.get("data"), list) else []
        people = [p for p in people if is_person(p, out["razao_social"])]
        people.sort(key=lambda p: p.get("validation") != "ENTREGAVEL")
        person = people[0] if people else None
        if person:
            out["decisor_cargo"] = " / ".join(x for x in (person.get("seniority"), person.get("area")) if x)

    # Plano B: sócio-administrador + domínio.
    if not person:
        admin = pick_admin(socios)
        if admin and admin.get("nome_socio"):
            st, js, c = call(key, cache, "POST", "/contacts/v1/emails/generate-by-domain",
                             body={"full_name": admin["nome_socio"], "domain": domain})
            custo += c
            people = js.get("data") if st == 200 and isinstance(js.get("data"), list) else []
            person = people[0] if people else {"full_name": admin["nome_socio"]}
            out["decisor_cargo"] = admin.get("qualificacao_socio", "")

    if person:
        # /contacts/v1/emails costuma vir sem seniority/area: o cargo sai do quadro societário.
        if not out["decisor_cargo"]:
            nome = norm(person.get("full_name"))
            match = next((s for s in socios if norm(s.get("nome_socio")) == nome), None)
            out["decisor_cargo"] = match.get("qualificacao_socio", "") if match else ""
        out["decisor_nome"] = (person.get("full_name") or "").title()
        out["decisor_email"] = person.get("email") or ""
        if out["decisor_email"]:
            out["decisor_email_verificado"] = "sim" if person.get("validation") == "ENTREGAVEL" else (person.get("validation") or "nao")
        if person.get("linkedin_id"):
            out["decisor_linkedin"] = "https://www.linkedin.com/in/" + person["linkedin_id"]
    out["custo_kipflow"] = f"{custo:.2f}"
    return out, custo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true", help="mostra o que consultaria, sem gastar créditos")
    ap.add_argument("--email-limit", type=int, default=1, help="perfis por empresa no /contacts/v1/emails (padrão 1)")
    a = ap.parse_args()

    with open(a.inp, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = list(reader.fieldnames or []), list(reader)
    if a.limit:
        rows = rows[: a.limit]

    if a.dry_run:
        for r in rows:
            d, why = own_domain(r.get("website"))
            print(f"- {r.get('title', '')[:45]:45}  " + (f"consultaria domain={d}" if d else why))
        return

    key = read_key()
    if not key:
        sys.exit("✗ KIPFLOW_API_KEY não encontrada no .env desta pasta.")

    cache, total, stopped = load_cache(), 0.0, False
    for i, r in enumerate(rows, 1):
        if stopped:
            r.update({c: "" for c in NEW_COLS}, kipflow_status="nao_consultado")
            continue
        try:
            extra, c = enrich_row(key, cache, r, a.email_limit)
        except NoCredits:
            print("✗ 402: sem créditos no Kipflow. Parei aqui; o resto ficou como nao_consultado.")
            stopped = True
            r.update({c: "" for c in NEW_COLS}, kipflow_status="nao_consultado")
            continue
        r.update(extra)
        total += c
        print(f"[{i}/{len(rows)}] {r.get('title', '')[:40]:40} {extra['kipflow_status']:20} R$ {c:.2f}")

    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols + [c for c in NEW_COLS if c not in cols])
        w.writeheader()
        w.writerows(rows)
    print(f"✓ {a.out}  |  custo das linhas: R$ {total:.2f}  |  cobrado nesta execução: R$ {SPENT[0]:.2f}")
    sys.exit(2 if stopped else 0)


if __name__ == "__main__":
    main()
