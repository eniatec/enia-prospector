"""EN.IA Prospector: interface local em português para o pipeline Maps + Kipflow + nota A/B/C.

Só biblioteca padrão (a planilha .xlsx usa openpyxl quando ele estiver disponível).
Escuta apenas em 127.0.0.1 e aceita só pedidos da própria página. A chave do Kipflow fica no .env
do kit e nunca vai para o navegador.

Uso (na pasta do kit):  python prospector/server.py  [--port 8092]
"""
import argparse
import collections
import csv
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
PY = sys.executable
CUSTO_REF = 0.20  # R$ por empresa encontrada no piloto (empresa 0,07 + e-mail 0,13)
UA = "ENIA-Prospector/1.0 (uso local, kit google-maps-scraper)"
COBRADO_RE = re.compile(r"cobrado nesta execução: R\$ ([\d.]+)")
DOWNLOAD_RE = re.compile(r"^[a-z0-9-]+-(maps\.csv|kipflow\.csv|final\.csv|validacao\.xlsx)$")

sys.path.insert(0, KIT)
from kipflow_enrich import own_domain, read_key  # noqa: E402

RUNS = {}
RUNS_FILE = os.path.join(KIT, "prospector-execucoes.json")
LOCK = threading.RLock()
GEO_LOCK = threading.Lock()
GEO_CACHE = {}
PORT = 8092


def slugify(*parts):
    s = unicodedata.normalize("NFKD", " ".join(parts))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:50] or "busca"


def clean(text, limit=120):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()
    return text[:limit]


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- execução

class Run:
    def __init__(self, params):
        self.id = uuid.uuid4().hex[:10]
        self.params = params
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        self.slug = f"{slugify(params['nicho'], params['local_curto'])}-{stamp}"
        self.etapa = "coleta"          # coleta -> aguardando -> validacao -> concluido | erro
        self.ativo = True
        self.log = collections.deque(maxlen=400)
        self.linha = ""
        self.mensagem = ""
        self.resumo = {}
        self.arquivos = {}
        self.criado = time.time()

    def path(self, kind):
        return os.path.join(KIT, f"{self.slug}-{kind}")

    def to_json(self):
        return {
            "id": self.id, "slug": self.slug, "etapa": self.etapa, "ativo": self.ativo,
            "linha": self.linha, "log": list(self.log)[-12:], "mensagem": self.mensagem,
            "resumo": self.resumo, "arquivos": self.arquivos, "params": self.params,
        }

    @classmethod
    def from_disk(cls, d):
        run = cls(d["params"])
        run.id, run.slug, run.etapa, run.mensagem = d["id"], d["slug"], d["etapa"], d.get("mensagem", "")
        run.resumo, run.arquivos, run.criado = d.get("resumo", {}), d.get("arquivos", {}), d.get("criado", time.time())
        run.log.extend(d.get("log", []))
        run.ativo = False
        if run.etapa == "concluido" and "cobrado" not in run.resumo:  # execuções salvas antes deste campo
            run.resumo.update(cobranca(run))
        if run.etapa in ("coleta", "validacao"):  # o servidor caiu no meio
            run.etapa, run.mensagem = "erro", "A execução foi interrompida porque o Prospector foi fechado."
        return run


def save_runs():
    """Guarda as execuções em disco para sobreviverem a um reinício (o arquivo fica fora do Git: /*.json)."""
    with LOCK:
        data = [dict(r.to_json(), criado=r.criado) for r in sorted(RUNS.values(), key=lambda r: r.criado)[-50:]]
    tmp = RUNS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, RUNS_FILE)


def load_runs():
    if os.path.exists(RUNS_FILE):
        with open(RUNS_FILE, encoding="utf-8") as f:
            for d in json.load(f):
                run = Run.from_disk(d)
                RUNS[run.id] = run


def run_proc(run, args, env_extra=None):
    """Roda um script do kit e transmite a saída linha a linha para o run. Devolve o código de saída."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", **(env_extra or {}))
    p = subprocess.Popen([PY, *args], cwd=KIT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    buf = b""
    while True:
        chunk = p.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            line = part.decode("utf-8", "replace").strip()
            if line:
                run.linha = line
                run.log.append(line)
        if buf:
            run.linha = buf.decode("utf-8", "replace").strip() or run.linha
    if buf.strip():
        run.log.append(buf.decode("utf-8", "replace").strip())
    return p.wait()


def coleta(run):
    p = run.params
    maps = run.path("maps.csv")
    qtd = p["quantidade"]
    depth = 2 if qtd <= 30 else 5
    keyword = f"{p['nicho']} em {p['local_curto']}"
    args = ["scripts/scrape.py", keyword]
    if p["modo"] == "grade":
        args += ["--city", p["local_busca"], "--grid", str(p["grade"])]
    else:
        args += [p["lat"], p["lon"]]
    args += ["--lang", "pt", "--country", "br", "--depth", str(depth), "--limit", str(qtd),
             "--tech", "--verify-emails", "--out", os.path.basename(maps)]
    if p["so_telefone"]:
        args.append("--has-phone")
    if p["min_avaliacoes"]:
        args += ["--min-reviews", str(p["min_avaliacoes"])]
    try:
        code = run_proc(run, args)
        if code != 0 or not os.path.exists(maps):
            run.etapa, run.mensagem = "erro", "A coleta no Google Maps falhou. Veja o registro abaixo."
            return
        rows = read_csv(maps)
        proprio = sum(1 for r in rows if own_domain(r.get("website"))[0])
        run.arquivos["maps"] = os.path.basename(maps)
        run.resumo = {
            "coletados": len(rows),
            "com_site": sum(1 for r in rows if r.get("website")),
            "com_email": sum(1 for r in rows if r.get("emails")),
            "dominio_proprio": proprio,
            "estimativa": round(proprio * CUSTO_REF, 2),
        }
        run.etapa = "aguardando" if rows else "concluido"
        run.mensagem = "" if rows else "Nenhum lugar encontrado com esses filtros."
    except Exception as e:  # noqa: BLE001
        run.etapa, run.mensagem = "erro", f"Erro inesperado na coleta: {e}"
    finally:
        run.ativo = False
        save_runs()


def validacao(run):
    maps, kip, final, xlsx = (run.path(k) for k in ("maps.csv", "kipflow.csv", "final.csv", "validacao.xlsx"))
    try:
        code = run_proc(run, ["kipflow_enrich.py", "--in", maps, "--out", kip])
        if code == 2:
            run.mensagem = "Os créditos do Kipflow acabaram no meio do lote: as linhas restantes ficaram sem consulta."
        elif code != 0 or not os.path.exists(kip):
            run.etapa, run.mensagem = "erro", "A validação no Kipflow falhou. Veja o registro abaixo."
            return
        if run_proc(run, ["nota_abc.py", kip, final]) != 0:
            run.etapa, run.mensagem = "erro", "Não consegui dar as notas A/B/C."
            return
        run.arquivos.update(kipflow=os.path.basename(kip), final=os.path.basename(final))
        busca = f"{run.params['nicho']}, {run.params['local_curto']}"
        if run_proc(run, ["planilha_abc.py", final, kip, xlsx, busca]) == 0 and os.path.exists(xlsx):
            run.arquivos["xlsx"] = os.path.basename(xlsx)
        run.resumo.update(funil(final, kip))
        run.resumo.update(cobranca(run))
        run.etapa = "concluido"
    except Exception as e:  # noqa: BLE001
        run.etapa, run.mensagem = "erro", f"Erro inesperado na validação: {e}"
    finally:
        run.ativo = False
        save_runs()


def funil(final, kip):
    rows, krows = read_csv(final), read_csv(kip)
    custo = sum(float(r.get("custo_kipflow") or 0) for r in krows)
    notas = collections.Counter(r["nota"] for r in rows)
    return {
        "cnpj_encontrado": sum(1 for r in rows if r.get("cnpj")),
        "cnpj_ativo": sum(1 for r in rows if r.get("situacao_cadastral") == "ATIVA"),
        "com_decisor": sum(1 for r in rows if r.get("decisor_nome")),
        "email_verificado": sum(1 for r in rows if r.get("decisor_email_verificado") == "sim"),
        "A": notas.get("A", 0), "B": notas.get("B", 0), "C": notas.get("C", 0),
        "custo_total": round(custo, 2),
        "custo_por_a": round(custo / notas["A"], 2) if notas.get("A") else None,
    }


def cobranca(run):
    """O kipflow_enrich.py informa o que foi cobrado na rodada; respostas do cache não entram nesse valor."""
    m = next((m for m in map(COBRADO_RE.search, reversed(run.log)) if m), None)
    if not m:
        return {}
    cobrado = float(m.group(1))
    notas_a = run.resumo.get("A") or 0
    return {"cobrado": cobrado, "cobrado_por_a": round(cobrado / notas_a, 2) if notas_a else None}


LEAD_COLS = {
    "maps": ["title", "phone", "website", "emails", "review_rating", "review_count", "address"],
    "final": ["nota", "motivo_nota", "title", "decisor_nome", "decisor_cargo", "decisor_email",
              "decisor_email_verificado", "phone", "website", "review_rating", "review_count", "cnpj",
              "razao_social", "situacao_cadastral"],
}


def leads(run):
    kind = "final" if "final" in run.arquivos else "maps"
    if kind not in run.arquivos:
        return {"tipo": None, "linhas": []}
    rows = read_csv(os.path.join(KIT, run.arquivos[kind]))
    return {"tipo": kind, "linhas": [{c: r.get(c, "") for c in LEAD_COLS[kind]} for r in rows]}


# ---------------------------------------------------------------- serviços

def engine_status():
    try:
        out = subprocess.run([PY, "scripts/engine.py", "status"], cwd=KIT, capture_output=True, timeout=30,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stdout.decode("utf-8", "replace")
        m = re.search(r"Running on (http://\S+)", text)
        return {"ok": out.returncode == 0 and bool(m), "url": m.group(1) if m else None}
    except Exception:  # noqa: BLE001
        return {"ok": False, "url": None}


def engine_start():
    subprocess.run([PY, "scripts/engine.py", "start"], cwd=KIT, capture_output=True, timeout=180,
                   env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return engine_status()


TIPOS_PT = {"suburb": "bairro", "neighbourhood": "bairro", "quarter": "bairro", "city_district": "distrito",
            "city": "cidade", "town": "cidade", "municipality": "município", "village": "vila", "state": "estado",
            "county": "região", "road": "rua", "railway": "estação", "amenity": "ponto de interesse"}
TIPOS_ORDEM = {t: i for i, t in enumerate(("suburb", "neighbourhood", "quarter", "city_district", "city", "town",
                                           "municipality", "village", "county", "state"))}


def geocode(q):
    """Nominatim (OpenStreetMap), no máximo 1 pedido por segundo, com contorno do lugar."""
    key = q.lower()
    if key in GEO_CACHE:
        return GEO_CACHE[key]
    params = urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 6, "countrycodes": "br",
                                     "addressdetails": 1, "polygon_geojson": 1, "polygon_threshold": 0.0008,
                                     "accept-language": "pt-BR"})
    with GEO_LOCK:
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{params}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            hits = json.loads(r.read())
        time.sleep(1)
    out, vistos = [], set()
    # Bairros e cidades primeiro; estações, ruas e pontos de interesse depois.
    hits.sort(key=lambda h: TIPOS_ORDEM.get(h.get("addresstype") or h.get("type"), 99))
    for h in hits:
        a = h.get("address") or {}
        cidade = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or ""
        uf = (a.get("ISO3166-2-lvl4") or "").replace("BR-", "")
        nome = h.get("name") or cidade or h.get("display_name", "").split(",")[0]
        curto = ", ".join(x for x in (nome, cidade if cidade != nome else "", uf) if x)
        tipo = h.get("addresstype") or h.get("type")
        if (curto, tipo) in vistos:
            continue
        vistos.add((curto, tipo))
        out.append({
            "nome": nome, "curto": curto, "completo": h.get("display_name", ""), "tipo": TIPOS_PT.get(tipo, tipo),
            "lat": h["lat"], "lon": h["lon"], "bbox": [float(x) for x in h.get("boundingbox", [])],
            "geojson": h.get("geojson") if (h.get("geojson") or {}).get("type") in ("Polygon", "MultiPolygon") else None,
        })
    GEO_CACHE[key] = out
    return out


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NUMERICAS = {"review_rating", "review_count", "intent_score", "custo_kipflow"}


def csv_to_xlsx(path):
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    rows = read_csv(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    cols = list(rows[0].keys()) if rows else []
    ws.append(cols)
    for r in rows:
        linha = []
        for c in cols:
            v = r.get(c, "")
            if c in NUMERICAS and v:
                try:
                    v = float(v) if "." in v else int(v)
                except ValueError:
                    pass
            linha.append(v)
        ws.append(linha)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", start_color="1F3864")
    for i, c in enumerate(cols, 1):
        largura = max([len(c)] + [len(str(r.get(c, ""))) for r in rows[:200]])
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(50, max(10, largura + 2))
    ws.freeze_panes = "B2"
    if cols:
        ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ENIA-Prospector"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"localhost:{PORT}", f"127.0.0.1:{PORT}")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, msg):
        self._send(code, {"erro": msg})

    def _excel(self, nome, path):
        """CSV -> .xlsx. O CSV final usa a planilha de validação (com abas Funil e Legenda) quando ela existe."""
        validacao = path.replace("-final.csv", "-validacao.xlsx")
        if nome.endswith("-final.csv") and os.path.isfile(validacao):
            with open(validacao, "rb") as f:
                data, saida = f.read(), os.path.basename(validacao)
        else:
            try:
                data = csv_to_xlsx(path)
            except ImportError:
                return self._err(501, "Excel indisponível: abra o Prospector pelo iniciar-prospector.cmd")
            saida = nome[:-4] + ".xlsx"
        return self._send(200, data, XLSX, {"Content-Disposition": f'attachment; filename="{saida}"'})

    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "host não permitido")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            with open(UI, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/estado":
            with LOCK:
                atual = next((r for r in RUNS.values() if r.ativo), None)
            return self._send(200, {"engine": engine_status(), "kipflow": bool(read_key()),
                                    "execucao_ativa": atual.id if atual else None})
        if u.path == "/api/local":
            termo = clean((q.get("q") or [""])[0])
            if len(termo) < 2:
                return self._err(400, "digite um local")
            try:
                return self._send(200, {"resultados": geocode(termo)})
            except Exception as e:  # noqa: BLE001
                return self._err(502, f"Busca de local indisponível: {e}")
        m = re.fullmatch(r"/api/execucao/([0-9a-f]{10})(/leads)?", u.path)
        if m:
            run = RUNS.get(m.group(1))
            if not run:
                return self._err(404, "execução não encontrada")
            return self._send(200, leads(run) if m.group(2) else run.to_json())
        if u.path == "/api/arquivos":
            nomes = [n for n in os.listdir(KIT) if DOWNLOAD_RE.match(n) and n.endswith(("final.csv", "maps.csv"))]
            nomes.sort(key=lambda n: os.path.getmtime(os.path.join(KIT, n)), reverse=True)
            return self._send(200, {"arquivos": [{
                "nome": n, "tipo": "Leads validados" if n.endswith("final.csv") else "Lugares do Google Maps",
                "data": datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(KIT, n))).strftime("%d/%m/%Y %H:%M"),
            } for n in nomes[:30]]})
        m = re.fullmatch(r"/baixar/([^/]+)", u.path)
        if m:
            nome = urllib.parse.unquote(m.group(1))
            path = os.path.join(KIT, nome)
            if not DOWNLOAD_RE.match(nome) or not os.path.isfile(path):
                return self._err(404, "arquivo não encontrado")
            if (q.get("formato") or [""])[0] == "excel" and nome.endswith(".csv"):
                return self._excel(nome, path)
            ctype = XLSX if nome.endswith(".xlsx") else "text/csv; charset=utf-8"
            with open(path, "rb") as f:
                return self._send(200, f.read(), ctype, {"Content-Disposition": f'attachment; filename="{nome}"'})
        return self._err(404, "rota não encontrada")

    def do_POST(self):
        # Só a própria página: bloqueia outro site aberto no navegador de disparar coleta ou gastar créditos.
        if not self._host_ok() or self.headers.get("Origin") not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self._err(403, "origem não permitida")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._err(415, "envie JSON")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
        except ValueError:
            return self._err(400, "JSON inválido")
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/engine/iniciar":
            return self._send(200, engine_start())

        if path == "/api/coleta":
            try:
                params = {
                    "nicho": clean(body.get("nicho"), 80),
                    "local_curto": clean(body.get("local_curto"), 120),
                    "local_busca": clean(body.get("local_busca"), 200),
                    "modo": "grade" if body.get("modo") == "grade" else "ponto",
                    "grade": max(2, min(5, int(body.get("grade") or 3))),
                    "lat": f"{float(body['lat']):.6f}", "lon": f"{float(body['lon']):.6f}",
                    "quantidade": max(1, min(300, int(body.get("quantidade") or 10))),
                    "so_telefone": bool(body.get("so_telefone", True)),
                    "min_avaliacoes": max(0, min(100000, int(body.get("min_avaliacoes") or 0))),
                }
            except (KeyError, TypeError, ValueError):
                return self._err(400, "preencha nicho, local e quantidade")
            if not params["nicho"] or not params["local_curto"]:
                return self._err(400, "preencha o nicho e escolha um local no mapa")
            if params["modo"] == "grade" and not params["local_busca"]:
                return self._err(400, "a cobertura por grade precisa de uma cidade ou bairro escolhido na busca")
            with LOCK:
                if any(r.ativo for r in RUNS.values()):
                    return self._err(409, "já existe uma execução em andamento: espere ela terminar")
                if not engine_status()["ok"]:
                    return self._err(503, "o motor de coleta está desligado: clique em Ligar motor")
                run = Run(params)
                RUNS[run.id] = run
                save_runs()
            threading.Thread(target=coleta, args=(run,), daemon=True).start()
            return self._send(200, run.to_json())

        m = re.fullmatch(r"/api/execucao/([0-9a-f]{10})/validar", path)
        if m:
            run = RUNS.get(m.group(1))
            if not run:
                return self._err(404, "execução não encontrada")
            if not read_key():
                return self._err(400, "falta a KIPFLOW_API_KEY no .env do kit")
            with LOCK:
                if run.etapa != "aguardando" or any(r.ativo for r in RUNS.values()):
                    return self._err(409, "esta execução não está aguardando validação")
                run.etapa, run.ativo, run.mensagem = "validacao", True, ""
                save_runs()
            threading.Thread(target=validacao, args=(run,), daemon=True).start()
            return self._send(200, run.to_json())

        return self._err(404, "rota não encontrada")


def main():
    global PORT
    ap = argparse.ArgumentParser(description="EN.IA Prospector")
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    PORT = a.port
    load_runs()
    if not engine_status()["ok"]:
        print("Ligando o motor de coleta do kit...")
        engine_start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"EN.IA Prospector rodando em {url}  (Ctrl+C para parar)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
