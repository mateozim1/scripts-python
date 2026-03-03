#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crawler Lovable (HTML + screenshots) com 2 camadas:
1) Descoberta ampla (links + navegação em SPA via cliques seguros em menu/abas)
2) Execução guiada por Auditoria (PDF/MD/TXT) para abrir estados (tabs/modais/expansores)

Motivo: documentos de auditoria costumam cobrir só parte do sistema.
Este crawler tenta "o melhor dos dois mundos":
- Não depende só do PDF para achar telas (cobre mais módulos).
- Usa o PDF para saber o que clicar (tabs/modais) e capturar variações.

Uso:
  python crawler_hybrid.py --base https://ensemble-finance-hub.lovable.app --audit Auditoria.pdf --out dump

Opções úteis:
  --mode hybrid|discover|audit
  --max-pages 2000
  --click-nav 1  (tenta cliques seguros em botões de menu quando não existe <a href>)
"""

import os
import re
import json
import time
import random
import hashlib
import argparse
import subprocess
from urllib.parse import (
    urlparse, urljoin, urldefrag, unquote,
    parse_qsl, urlencode, urlunparse
)

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PWTimeoutError,
    Error as PWError,
)

# =========================
# DEFAULTS
# =========================
DEFAULT_OUT_DIR = "./dump"
DEFAULT_AUDIT = "./Auditoria ResiCode.pdf"
DEFAULT_BASE = "https://project-whisper-76.lovable.app"

HEADLESS = True

# estabilidade / anti-bot
WAIT_MS = 2600
NETWORKIDLE_TIMEOUT_MS = 25000
NAV_DELAY_RANGE = (2.2, 4.2)
ACTION_DELAY_RANGE = (0.45, 1.05)

# retry de navegação
GOTO_RETRIES = 5
GOTO_TIMEOUT_MS = 65000

# hard cap / performance
HARD_CAP_PAGES = 4000

# capturas por página
CAPTURE_AUTO_TABS = True
MAX_AUTO_TABS_PER_PAGE = 25

CAPTURE_EXPANDERS = True
MAX_EXPANDERS_PER_PAGE = 12

CAPTURE_MODALS = True
MAX_MODALS_PER_PAGE = 18

CAPTURE_AUDIT_TRIGGERS = True
MAX_AUDIT_ACTIONS_PER_PAGE = 60

# descoberta por clique (menu/SPA)
CLICK_NAV_DISCOVERY = True
MAX_NAV_CLICKS_PER_PAGE = 18

VIEWPORT = {"width": 1440, "height": 900}

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:")

TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid"
}

DENY_WORDS = [
    "excluir", "delete", "remover", "apagar",
    "salvar", "confirmar", "concluir", "finalizar",
    "baixar", "exportar", "download", "imprimir",
    "pagar", "receber", "gerar", "enviar", "aprovar",
]

# =========================


def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


def jitter_sleep(rng):
    time.sleep(random.uniform(rng[0], rng[1]))


def log_error(out_dir: str, msg: str):
    ensure_dir(out_dir)
    with open(os.path.join(out_dir, "errors.txt"), "a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")


def same_domain(base_url: str, url: str) -> bool:
    return urlparse(base_url).netloc == urlparse(url).netloc


def canonicalize_url(url: str) -> str:
    p = urlparse(url)
    q = parse_qsl(p.query, keep_blank_values=True)
    q = [(k, v) for (k, v) in q if k not in TRACKING_QUERY_KEYS]
    q.sort(key=lambda kv: kv[0])
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))


def normalize_url(current_url: str, href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if any(href.lower().startswith(s) for s in SKIP_SCHEMES):
        return ""
    absu = urljoin(current_url, href)
    absu, _ = urldefrag(absu)
    return canonicalize_url(absu)


def clean_path(p: str) -> str:
    p = unquote(p or "/")
    p = p.split("?")[0]
    p = p.replace("\\", "/")
    p = re.sub(r"/+", "/", p).strip()
    p = p.lstrip("/")
    p = p.replace("..", "__")
    return p


def slugify(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "index"
    s = s.lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:90] or "index"


def route_to_folder_and_file(url: str):
    parsed = urlparse(url)
    path = clean_path(parsed.path)
    query = (parsed.query or "").strip()

    qhash = ""
    if query:
        qhash = hashlib.sha1(query.encode("utf-8")).hexdigest()[:6]

    if not path:
        base = "index"
        if qhash:
            base = f"{base}__q{qhash}"
        return ("", base, "/" + ("?"+query if query else ""))

    parts = [p for p in path.split("/") if p]
    if len(parts) == 1:
        base = "index"
        if qhash:
            base = f"{base}__q{qhash}"
        return (parts[0], base, "/" + path + ("?"+query if query else ""))
    else:
        folder = parts[0]
        filename = parts[-1] or "index"
        if qhash:
            filename = f"{filename}__q{qhash}"
        return (folder, filename, "/" + path + ("?"+query if query else ""))


def unique_file_base(folder_dir: str, file_base: str, full_path_key: str) -> str:
    html_path = os.path.join(folder_dir, f"{file_base}.html")
    if not os.path.exists(html_path):
        return file_base
    h = hashlib.sha1(full_path_key.encode("utf-8")).hexdigest()[:8]
    return f"{file_base}__{h}"


def load_state(state_file: str):
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state_file: str, state: dict):
    ensure_dir(os.path.dirname(state_file))
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def wait_stable(page):
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(WAIT_MS)


def safe_goto(page, out_dir: str, url: str) -> bool:
    last_err = None
    for attempt in range(1, GOTO_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            return True
        except (PWTimeoutError, PWError) as e:
            last_err = e
            # algumas quedas melhoram com "blank" no meio
            try:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            backoff = min(2 ** attempt, 12)
            jitter_sleep((backoff, backoff + 1.8))
        except Exception as e:
            last_err = e
            backoff = min(2 ** attempt, 12)
            jitter_sleep((backoff, backoff + 1.8))

    log_error(out_dir, f"[GOTO_FAIL] {url}\n{str(last_err)}\n")
    return False


def extract_links(page, base_url: str) -> list:
    hrefs = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(a => a.getAttribute('href')).filter(Boolean)"
    )
    cur = page.url
    out = []
    for h in hrefs or []:
        u = normalize_url(cur, h)
        if u and same_domain(base_url, u):
            out.append(u)

    seen = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def extract_nav_links(page, base_url: str) -> list:
    """
    Puxa links que costumam listar o sistema inteiro (sidebar/nav),
    mesmo que o resto da página tenha poucos <a>.
    """
    selectors = "nav a[href], aside a[href], [role='navigation'] a[href]"
    try:
        hrefs = page.eval_on_selector_all(
            selectors,
            "els => els.map(a => a.getAttribute('href')).filter(Boolean)"
        )
    except Exception:
        hrefs = []
    cur = page.url
    out = []
    for h in hrefs or []:
        u = normalize_url(cur, h)
        if u and same_domain(base_url, u):
            out.append(u)

    # dedupe
    uniq = []
    seen = set()
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def is_danger_text(txt: str) -> bool:
    low = (txt or "").strip().lower()
    if not low:
        return False
    return any(w in low for w in DENY_WORDS)


# =========================
# AUDIT READER + PARSER (PDF/MD/TXT) - mantém simples e "aberto"
# =========================
def read_audit_text(audit_path: str) -> str:
    ext = os.path.splitext(audit_path)[1].lower()

    if ext in (".txt", ".md"):
        return open(audit_path, "r", encoding="utf-8", errors="ignore").read()

    if ext == ".pdf":
        # 1) tenta pypdf
        try:
            from pypdf import PdfReader
            reader = PdfReader(audit_path)
            parts = []
            for pg in reader.pages:
                parts.append(pg.extract_text() or "")
            return "\n".join(parts)
        except Exception:
            pass

        # 2) fallback: pdftotext se existir
        try:
            r = subprocess.run(
                ["pdftotext", audit_path, "-"],
                capture_output=True, text=True, check=False
            )
            if r.stdout and r.returncode == 0:
                return r.stdout
        except Exception:
            pass

        raise RuntimeError(
            "Não consegui extrair texto do PDF. Instale 'pypdf' (pip install pypdf) "
            "ou instale 'pdftotext' no mac."
        )

    return ""


def parse_audit_document(audit_path: str) -> dict:
    """
    Parser robusto para:
      - TELA_* com rota em "( /rota )" (mesma linha ou próxima)
      - TRG_* com labels e possíveis 'Navega para /rota'
    """
    txt = read_audit_text(audit_path)
    lines = [ln.strip() for ln in txt.splitlines() if (ln or "").strip()]

    meta = {}
    for ln in lines[:250]:
        low = ln.lower()
        if low.startswith("sistema:"):
            meta["sistema"] = ln.split(":", 1)[1].strip()
        if low.startswith("versão:") or low.startswith("versao:"):
            meta["versao"] = ln.split(":", 1)[1].strip()
        if low.startswith("data da auditoria:"):
            meta["data"] = ln.split(":", 1)[1].strip()

    full = "\n".join(lines)

    # MODAL_* -> TRG_*
    modal_by_trigger = {}
    for m in re.finditer(r"(MODAL_[A-Z0-9_]+)(.{0,220})(TRG_[A-Z0-9_]+)", full, flags=re.S):
        modal_by_trigger[m.group(3)] = m.group(1)

    # TELA_... ( /rota )
    tela_blocks = []
    for m in re.finditer(r"(TELA_[A-Z0-9_]+)(?:[^\n]{0,160})", full):
        tela_blocks.append((m.start(), m.group(1)))

    routes = []
    screens = {}

    # extrai rota dentro do "pedaço" pós TELA_ (até próximo TELA_ ou 1500 chars)
    for idx, (pos, tela_id) in enumerate(tela_blocks):
        end = tela_blocks[idx+1][0] if idx+1 < len(tela_blocks) else min(len(full), pos + 2000)
        chunk = full[pos:end]

        route = ""
        mm = re.search(r"\(\s*(/[\w\-/]+)\s*\)", chunk)
        if mm:
            route = mm.group(1)
        else:
            # fallback: primeira rota iniciada por / e com segmento "real"
            mm = re.search(r"\b(/(?:dashboard|gestao|importacoes|lancamentos|movimentacoes|projecao|protocolos|recorrencias|relatorios|auth|config|cartoes|contratos|financeiro)[\w\-/{}]*)\b", chunk, flags=re.I)
            if mm:
                route = mm.group(1)

        if route:
            routes.append(route)
            screens.setdefault(route, {"tela_id": tela_id, "triggers": []})

        # TRG dentro do chunk
        trgs = re.findall(r"(TRG_[A-Z0-9_]+)", chunk)
        # muito ruído? limita
        trgs = trgs[:120]

        for trg_id in trgs:
            # pega subchunk a partir do trg_id até o próximo TRG_ ou fim do chunk
            start2 = chunk.find(trg_id)
            if start2 < 0:
                continue
            sub = chunk[start2:start2+900]
            next_trg = re.search(r"\nTRG_[A-Z0-9_]+", sub[5:])
            if next_trg:
                sub = sub[:next_trg.start()+1]

            blob_norm = " ".join(sub.split())

            # tipo best-effort
            tipo = ""
            for t in ("aba", "botão", "navegação", "dropdown", "checkbox", "input", "select", "toggle", "tabela", "linha", "menu"):
                if re.search(r"\b"+re.escape(t)+r"\b", blob_norm, flags=re.I):
                    tipo = t
                    break
            if not tipo:
                tipo = "botão"

            label_m = re.search(r"\"([^\"]+)\"", blob_norm)
            label = label_m.group(1).strip() if label_m else ""

            tab_m = re.search(r'TabsTrigger\s*\[\s*value\s*=\s*\"([^\"]+)\"\s*\]', blob_norm)
            tab_value = tab_m.group(1).strip() if tab_m else ""

            nav_m = re.search(r"Navega\s+para\s+(/[\w\-/{}]+)", blob_norm, flags=re.I)
            target_route = nav_m.group(1) if nav_m else ""

            opens_modal = trg_id in modal_by_trigger or bool(re.search(r"\bMODAL_[A-Z0-9_]+\b", blob_norm))
            is_row_click = bool(re.search(r"clique\s+na\s+linha|na\s+linha", blob_norm, flags=re.I))

            is_danger = is_danger_text(label + " " + blob_norm)

            if route and trg_id and trg_id not in [t["id"] for t in screens[route]["triggers"]]:
                screens[route]["triggers"].append({
                    "id": trg_id,
                    "type": tipo,
                    "label": label,
                    "tab_value": tab_value,
                    "target_route": target_route,
                    "opens_modal": opens_modal,
                    "modal_id": modal_by_trigger.get(trg_id, ""),
                    "is_row_click": is_row_click,
                    "is_danger": is_danger,
                    "raw": blob_norm[:600],
                })

    # rotas extras úteis (navega para)
    extra = set(re.findall(r"Navega\s+para\s+(/[\w\-/{}]+)", full, flags=re.I))
    for r in sorted(extra):
        routes.append(r)

    # limpeza
    routes = [r for r in routes if r.startswith("/") and not re.search(r"\.(css|js|png|jpg|jpeg|svg|webp|ico|map)$", r, re.I)]
    routes = sorted(set(routes))

    return {"meta": meta, "routes": routes, "screens": screens, "modal_by_trigger": modal_by_trigger}


# =========================
# UI ACTIONS (tabs, expanders, modal detect)
# =========================
def capture_state(page, folder_dir: str, fname: str, suffix: str = ""):
    base = f"{fname}{suffix}"
    html_path = os.path.join(folder_dir, f"{base}.html")
    png_path = os.path.join(folder_dir, f"{base}.png")

    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass

    try:
        page.screenshot(path=png_path, full_page=True)
    except Exception:
        pass

    return html_path, png_path


def find_modal_locator(page):
    candidates = [
        "[data-state='open'][data-radix-dialog-content]",
        "[data-state='open'][data-radix-alert-dialog-content]",
        "[data-state='open'][role='dialog']",
        "[role='alertdialog']",
        ".modal.show",
        "dialog[open]",
        "[aria-modal='true']",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, None


def close_modal_or_overlay(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass

    overlays = [
        "[data-state='open'][data-radix-dialog-overlay]",
        "[data-state='open'][data-radix-alert-dialog-overlay]",
    ]
    for sel in overlays:
        try:
            ov = page.locator(sel).first
            if ov.count() > 0 and ov.is_visible():
                ov.click(timeout=1500)
                page.wait_for_timeout(250)
                break
        except Exception:
            continue

    close_selectors = [
        ".modal.show [data-bs-dismiss='modal']",
        ".modal.show [data-dismiss='modal']",
        ".modal.show .btn-close",
        "button[aria-label='Close']",
        "button:has-text('Fechar')",
        "button:has-text('Cancelar')",
    ]
    for sel in close_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=1500)
                page.wait_for_timeout(250)
                break
        except Exception:
            continue


def click_by_label(page, label: str) -> bool:
    label = (label or "").strip()
    if not label:
        return False
    if is_danger_text(label):
        return False

    for fn in (
        lambda: page.get_by_role("button", name=label, exact=True),
        lambda: page.get_by_role("button", name=label),
        lambda: page.get_by_role("link", name=label, exact=True),
        lambda: page.get_by_role("link", name=label),
        lambda: page.get_by_text(label, exact=True),
    ):
        try:
            loc = fn()
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def click_tab_value(page, value: str, label: str = "") -> bool:
    value = (value or "").strip()
    selectors = []
    if value:
        selectors = [
            f'[role="tab"][value="{value}"]',
            f'[role="tab"][data-value="{value}"]',
            f'button[role="tab"][value="{value}"]',
            f'button[value="{value}"]',
            f'button[data-value="{value}"]',
            f'[data-value="{value}"]',
        ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:
            pass
    if label:
        return click_by_label(page, label)
    return False


def click_first_row(page) -> bool:
    candidates = [
        "table tbody tr",
        "[role='row']",
        "[data-row]",
        "[data-testid*='row']",
        "[data-radix-collection-item][role='row']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            cnt = loc.count()
            if cnt <= 0:
                continue
            idx = 1 if cnt > 1 else 0
            el = loc.nth(idx)
            if el.is_visible():
                el.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def collect_auto_tabs(page):
    triggers = []

    def add_unique(loc, name, key):
        for _l, _n, _k in triggers:
            if _k == key:
                return
        triggers.append((loc, name, key))

    role_tabs = page.locator("[role='tab']")
    try:
        cnt = min(role_tabs.count(), 50)
    except Exception:
        cnt = 0

    for i in range(cnt):
        el = role_tabs.nth(i)
        try:
            if not el.is_visible():
                continue
            if el.get_attribute("aria-disabled") == "true" or el.get_attribute("disabled") is not None:
                continue
            name = (el.inner_text() or "").strip()
            if not name:
                name = (el.get_attribute("aria-label") or el.get_attribute("title") or f"tab{i+1}")
            key = el.get_attribute("aria-controls") or el.get_attribute("id") or f"role_tab_{i}"
            if is_danger_text(name):
                continue
            add_unique(el, slugify(name), key)
        except Exception:
            continue

    if triggers:
        return triggers

    fallback = page.locator(".nav-tabs a, .nav-tabs button, [data-bs-toggle='tab'], [data-toggle='tab']")
    try:
        cnt = min(fallback.count(), 50)
    except Exception:
        cnt = 0

    for i in range(cnt):
        el = fallback.nth(i)
        try:
            if not el.is_visible():
                continue
            name = (el.inner_text() or "").strip()
            if not name:
                name = (el.get_attribute("aria-label") or el.get_attribute("title") or f"tab{i+1}")
            if is_danger_text(name):
                continue
            key = el.get_attribute("href") or el.get_attribute("aria-controls") or f"nav_tab_{i}"
            add_unique(el, slugify(name), key)
        except Exception:
            continue

    return triggers


def is_tab_active(el) -> bool:
    try:
        if el.get_attribute("aria-selected") == "true":
            return True
        cls = (el.get_attribute("class") or "")
        if "active" in cls.split():
            return True
        ds = (el.get_attribute("data-state") or "")
        if ds.lower() in ("active", "on", "open"):
            return True
    except Exception:
        pass
    return False


def capture_auto_tabs(page, folder_dir: str, fname: str) -> int:
    triggers = collect_auto_tabs(page)
    if not triggers:
        return 0

    captured = 0
    for (el, name_slug, _key) in triggers:
        if captured >= MAX_AUTO_TABS_PER_PAGE:
            break
        try:
            if is_tab_active(el):
                continue
            jitter_sleep(ACTION_DELAY_RANGE)
            el.click(timeout=3000)
            page.wait_for_timeout(300)
            wait_stable(page)
            captured += 1
            capture_state(page, folder_dir, fname, suffix=f"__auto_tab{captured:02d}__{name_slug}")
        except Exception:
            continue
    return captured


def collect_expanders(page):
    selectors = [
        ".accordion-button",
        "[data-bs-toggle='collapse']",
        "[data-toggle='collapse']",
        "[aria-expanded='false']",
    ]
    loc = page.locator(",".join(selectors))
    try:
        cnt = min(loc.count(), 60)
    except Exception:
        cnt = 0

    out = []
    for i in range(cnt):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            txt = (el.inner_text() or "").strip()
            if is_danger_text(txt):
                continue
            name = slugify(txt or f"expander{i+1}")
            out.append((el, name))
        except Exception:
            continue
    return out


def capture_expanders(page, folder_dir: str, fname: str) -> int:
    exps = collect_expanders(page)
    if not exps:
        return 0

    captured = 0
    for (el, name_slug) in exps:
        if captured >= MAX_EXPANDERS_PER_PAGE:
            break
        try:
            jitter_sleep(ACTION_DELAY_RANGE)
            el.click(timeout=3000)
            page.wait_for_timeout(300)
            wait_stable(page)
            captured += 1
            capture_state(page, folder_dir, fname, suffix=f"__exp{captured:02d}__{name_slug}")
        except Exception:
            continue
    return captured


# =========================
# Discovery via click (menu SPA)
# =========================
def discover_via_nav_clicks(page, base_url: str, out_dir: str, queue: list, seen: set) -> int:
    """
    Clica em botões de navegação (sem href) dentro de nav/aside.
    Se a URL mudar, adiciona na fila. Se não mudar, re-extrai links do nav.
    """
    if not CLICK_NAV_DISCOVERY:
        return 0

    base_before = canonicalize_url(page.url)

    # candidatos mais comuns
    candidates_sel = "nav button, aside button, [role='navigation'] button, nav [role='menuitem'], aside [role='menuitem']"
    loc = page.locator(candidates_sel)

    try:
        cnt = min(loc.count(), 80)
    except Exception:
        cnt = 0

    clicks = 0
    for i in range(cnt):
        if clicks >= MAX_NAV_CLICKS_PER_PAGE:
            break
        try:
            el = loc.nth(i)
            if not el.is_visible():
                continue
            txt = (el.inner_text() or "").strip()
            if len(txt) > 70:
                txt = txt[:70]
            if is_danger_text(txt):
                continue

            # tenta clicar
            jitter_sleep(ACTION_DELAY_RANGE)
            el.click(timeout=2500)
            page.wait_for_timeout(350)
            wait_stable(page)

            after = canonicalize_url(page.url)
            if after != base_before and same_domain(base_url, after):
                if after not in seen and after not in queue:
                    queue.append(after)
                clicks += 1

                # volta para a tela anterior para continuar explorando menu
                safe_goto(page, out_dir, base_before)
                wait_stable(page)
            else:
                # pode ser só expandir seção -> extrai links novos do nav
                for lk in extract_nav_links(page, base_url):
                    if lk not in seen and lk not in queue:
                        queue.append(lk)
                clicks += 1
        except Exception:
            continue

    return clicks


# =========================
# Flow handlers (login / seleção de contexto) - best-effort e seguro
# =========================
def handle_common_flows(page):
    """
    Alguns projetos Lovable colocam um gate inicial:
      - /auth/login
      - /auth/selecao-contexto
    Aqui tentamos avançar sem preencher dados sensíveis.
    """
    path = (urlparse(page.url).path or "").lower()

    # seleção de contexto
    if "selecao" in path and "context" in path:
        # tenta clicar no primeiro card/botão "Continuar" ou 1º item clicável
        for label in ("Continuar", "Acessar", "Entrar"):
            if click_by_label(page, label):
                page.wait_for_timeout(400)
                return True

        # fallback: primeiro botão visível não perigoso
        btns = page.locator("button")
        try:
            cnt = min(btns.count(), 30)
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                b = btns.nth(i)
                if not b.is_visible():
                    continue
                txt = (b.inner_text() or "").strip()
                if not txt or is_danger_text(txt):
                    continue
                b.click(timeout=2500)
                page.wait_for_timeout(400)
                return True
            except Exception:
                continue

    # login (não preenche credenciais; só tenta "Entrar" se for demo)
    if "/auth/login" in path or path.endswith("/login"):
        for label in ("Entrar", "Acessar", "Continuar", "Login"):
            if click_by_label(page, label):
                page.wait_for_timeout(500)
                return True

    return False


# =========================
# MAIN
# =========================
def main():
    global HEADLESS, HARD_CAP_PAGES, CLICK_NAV_DISCOVERY
    parser = argparse.ArgumentParser(description="Crawler Lovable (hybrid discovery + audit)")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--audit", default=DEFAULT_AUDIT)
    parser.add_argument("--out", default=DEFAULT_OUT_DIR)
    parser.add_argument("--headless", default="1", help="1=headless (default), 0=visível")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "discover", "audit"])
    parser.add_argument("--max-pages", default=str(HARD_CAP_PAGES))
    parser.add_argument("--click-nav", default="1", help="1=habilita cliques de menu SPA; 0=desliga")

    args = parser.parse_args()

    base = args.base.strip().rstrip("/")
    base = base if base.startswith("http") else ("https://" + base)
    audit_path = args.audit.strip()
    out_dir = args.out.strip()
    state_file = os.path.join(out_dir, "state.json")

    HEADLESS = (str(args.headless).strip() != "0")
    HARD_CAP_PAGES = int(args.max_pages)
    CLICK_NAV_DISCOVERY = (str(args.click_nav).strip() != "0")

    ensure_dir(out_dir)

    audit_map = {"routes": [], "screens": {}, "modal_by_trigger": {}, "meta": {}}
    if args.mode in ("hybrid", "audit"):
        try:
            audit_map = parse_audit_document(audit_path)
        except Exception as e:
            log_error(out_dir, f"[AUDIT_PARSE_FAIL] {audit_path}\n{e}\n")

    try:
        with open(os.path.join(out_dir, "audit_map.json"), "w", encoding="utf-8") as f:
            json.dump(audit_map, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    audit_routes = audit_map.get("routes", [])
    screens = audit_map.get("screens", {})

    st = load_state(state_file)
    if st and st.get("base") == base and st.get("mode") == args.mode:
        queue = st.get("queue", [])
        seen = set(st.get("seen", []))
        visited = st.get("visited", [])
    else:
        queue, seen, visited = [], set(), []
        queue.append(canonicalize_url(base))

        # seeds de auditoria (quando existirem)
        if args.mode in ("hybrid", "audit"):
            for r in audit_routes:
                if ":" in r:
                    continue
                queue.append(canonicalize_url(urljoin(base + "/", r.lstrip("/"))))

        # seeds "clássicos" (ajuda a desbloquear menu em muitos projetos)
        for seed in ("/auth/login", "/auth/selecao-contexto"):
            queue.append(canonicalize_url(urljoin(base + "/", seed.lstrip("/"))))

        # dedupe
        q2, s2 = [], set()
        for u in queue:
            u = canonicalize_url(u)
            if u not in s2:
                s2.add(u)
                q2.append(u)
        queue = q2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()

        while queue and len(visited) < HARD_CAP_PAGES:
            requested = queue.pop(0)
            requested = canonicalize_url(requested)
            if requested in seen:
                continue
            seen.add(requested)

            idx = len(visited) + 1
            print(f"[{idx}] Visitando: {requested}")

            jitter_sleep(NAV_DELAY_RANGE)

            if not safe_goto(page, out_dir, requested):
                print(f"  !! pulando (falha de conexão): {requested}")
                save_state(state_file, {
                    "base": base, "mode": args.mode,
                    "queue": queue, "seen": sorted(seen), "visited": visited,
                    "last_saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                continue

            wait_stable(page)

            # tenta destravar flows (login/contexto)
            try:
                if handle_common_flows(page):
                    wait_stable(page)
            except Exception:
                pass

            # sempre usa URL final após possíveis redirects/flows
            final_url = canonicalize_url(page.url)
            if same_domain(base, final_url) and final_url not in seen:
                # marca final como visitado "de verdade"
                seen.add(final_url)

            folder, fname_raw, full_path_key = route_to_folder_and_file(final_url)
            fname = slugify(fname_raw)

            folder_dir = os.path.join(out_dir, folder) if folder else out_dir
            ensure_dir(folder_dir)

            fname = unique_file_base(folder_dir, fname, full_path_key)

            # captura base
            capture_state(page, folder_dir, fname)

            # ===== descoberta ampla =====
            try:
                for lk in extract_links(page, base):
                    if lk not in seen and lk not in queue:
                        queue.append(lk)
            except Exception:
                pass

            # links de nav (sidebar)
            try:
                for lk in extract_nav_links(page, base):
                    if lk not in seen and lk not in queue:
                        queue.append(lk)
            except Exception:
                pass

            # cliques de menu para SPA (quando links não são <a>)
            if args.mode in ("hybrid", "discover") and CLICK_NAV_DISCOVERY:
                try:
                    discover_via_nav_clicks(page, base, out_dir, queue, seen)
                except Exception:
                    pass

            # ===== estados automáticos (tabs/expanders) =====
            if CAPTURE_AUTO_TABS:
                capture_auto_tabs(page, folder_dir, fname)

            if CAPTURE_EXPANDERS:
                capture_expanders(page, folder_dir, fname)

            # ===== ações guiadas pelo audit (TRG_*) =====
            if args.mode in ("hybrid", "audit") and CAPTURE_AUDIT_TRIGGERS:
                path_now = urlparse(final_url).path or "/"
                screen = screens.get(path_now, {})
                triggers = screen.get("triggers", []) if screen else []

                actions = 0
                modals = 0

                for t in triggers:
                    if actions >= MAX_AUDIT_ACTIONS_PER_PAGE:
                        break
                    if t.get("is_danger"):
                        continue

                    tid = t.get("id", "")
                    ttype = (t.get("type") or "").lower()
                    label = t.get("label", "")
                    tabv = t.get("tab_value", "")
                    target_route = t.get("target_route", "")
                    opens_modal = bool(t.get("opens_modal"))
                    is_row_click = bool(t.get("is_row_click"))

                    if tabv or ttype == "aba":
                        jitter_sleep(ACTION_DELAY_RANGE)
                        if click_tab_value(page, tabv, label=label):
                            wait_stable(page)
                            actions += 1
                            capture_state(page, folder_dir, fname, suffix=f"__trg_{tid}__tab_{slugify(label or tabv)}")
                        continue

                    if ttype == "navegação" and target_route:
                        full = canonicalize_url(urljoin(base + "/", target_route.lstrip("/")))
                        if full not in seen and full not in queue:
                            queue.append(full)
                        continue

                    if is_row_click or ttype in ("tablerow", "linha", "tabela"):
                        if CAPTURE_MODALS and opens_modal and modals < MAX_MODALS_PER_PAGE:
                            jitter_sleep(ACTION_DELAY_RANGE)
                            if click_first_row(page):
                                wait_stable(page)
                                actions += 1
                                modals += 1
                                capture_state(page, folder_dir, fname, suffix=f"__trg_{tid}__open_full")

                                modal_loc, _ = find_modal_locator(page)
                                if modal_loc:
                                    try:
                                        modal_loc.screenshot(path=os.path.join(folder_dir, f"{fname}__trg_{tid}__only.png"))
                                    except Exception:
                                        pass

                                try:
                                    with open(os.path.join(folder_dir, f"{fname}__trg_{tid}__open.html"), "w", encoding="utf-8") as f:
                                        f.write(page.content())
                                except Exception:
                                    pass

                                close_modal_or_overlay(page)
                        continue

                    if label and ttype in ("botão", "checkbox", "dropdown", "menu", "toggle"):
                        jitter_sleep(ACTION_DELAY_RANGE)
                        if not click_by_label(page, label):
                            continue

                        wait_stable(page)
                        actions += 1
                        capture_state(page, folder_dir, fname, suffix=f"__trg_{tid}__state_{slugify(label)}")

                        if CAPTURE_MODALS and opens_modal and modals < MAX_MODALS_PER_PAGE:
                            modal_loc, _ = find_modal_locator(page)
                            if modal_loc and modal_loc.is_visible():
                                modals += 1
                                capture_state(page, folder_dir, fname, suffix=f"__trg_{tid}__modal_full")
                                try:
                                    modal_loc.screenshot(path=os.path.join(folder_dir, f"{fname}__trg_{tid}__modal_only.png"))
                                except Exception:
                                    pass
                                close_modal_or_overlay(page)

            # registra visitado
            visited.append(final_url)

            # salva estado
            save_state(state_file, {
                "base": base, "mode": args.mode,
                "queue": queue,
                "seen": sorted(seen),
                "visited": visited,
                "last_saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

        ctx.close()
        browser.close()

    # urls.txt
    try:
        with open(os.path.join(out_dir, "urls.txt"), "w", encoding="utf-8") as f:
            for u in visited:
                f.write(u + "\n")
    except Exception:
        pass

    print("\nConcluído.")
    print(f"Páginas visitadas: {len(visited)}")
    print(f"Fila pendente: {len(queue)}")
    print(f"Saída: {os.path.abspath(out_dir)}")
    if len(visited) >= HARD_CAP_PAGES:
        print(f"ATENÇÃO: bateu no HARD_CAP_PAGES={HARD_CAP_PAGES} (anti-loop).")


if __name__ == "__main__":
    main()
