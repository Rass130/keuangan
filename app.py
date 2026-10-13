"""Bot keuangan Telegram + dashboard, versi Vercel (webhook + Postgres/Neon)."""
import os, re, json, hashlib, requests, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort, render_template_string

TG = os.environ["TELEGRAM_TOKEN"]
AK = os.environ.get("ANTHROPIC_API_KEY")  # opsional
OWNER = int(os.environ.get("OWNER_ID", "0"))
KEY = os.environ.get("DASH_KEY", "ganti-ini")
DBURL = os.environ["DATABASE_URL"]
API = f"https://api.telegram.org/bot{TG}"
SECRET = hashlib.sha256(TG.encode()).hexdigest()[:32]  # penanda webhook asli dari Telegram
WIB = timezone(timedelta(hours=7))
_con = None


def now():
    return datetime.now(WIB)


def week_start():
    d = now().date()
    return (d - timedelta(days=d.weekday())).isoformat()  # Senin


def month_start():
    return now().strftime("%Y-%m-01")


# ---------- database (Postgres) ----------
def conn():
    global _con
    if _con is None or _con.closed:
        _con = psycopg2.connect(DBURL, connect_timeout=10)
        with _con, _con.cursor() as cur:
            cur.execute("create table if not exists trx(id serial primary key, ts text, "
                        "tipe text, jumlah bigint, kategori text, catatan text)")
            cur.execute("create table if not exists cfg(k text primary key, v text)")
    return _con


def q(sql, args=(), one=False, write=False):
    global _con
    for attempt in (1, 2):
        try:
            with conn() as con, con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, args)
                return None if write else (cur.fetchone() if one else cur.fetchall())
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _con = None
            if attempt == 2:
                raise


def cfg(k, v):
    q("insert into cfg values(%s,%s) on conflict (k) do update set v=excluded.v",
      (k, str(v)), write=True)


def stats():
    r = {x["tipe"]: int(x["j"]) for x in q("select tipe, sum(jumlah) j from trx group by tipe")}
    masuk, keluar, tabung, tarik = (r.get(k, 0) for k in ("masuk", "keluar", "tabung", "tarik"))
    mg = q("select coalesce(sum(jumlah),0) j from trx where tipe='keluar' and ts>=%s",
           (week_start(),), one=True)["j"]
    c = {x["k"]: int(x["v"]) for x in q("select k, v from cfg")}
    jajan, target = c.get("jajan", 0), c.get("target", 0)
    return dict(masuk=masuk, keluar=keluar, tabung=tabung - tarik,
                saldo=masuk - keluar - tabung + tarik, minggu=int(mg),
                jajan=jajan, sisa=jajan - int(mg), target=target)


def per_kategori(since, tipe="keluar"):
    return q("select kategori, sum(jumlah) j from trx where tipe=%s and ts>=%s "
             "group by kategori order by j desc", (tipe, since))


def rp(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")


# ---------- parsing ----------
NUM = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+(?:,\d+)?)\s*(rb|ribu|k|jt|juta)?\b", re.I)
MULT = {"rb": 1000, "ribu": 1000, "k": 1000, "jt": 10**6, "juta": 10**6}

# kata kunci -> tipe (urutan = prioritas)
KW = [
    ("tarik", r"\b(tarik|ambil tabungan)\b"),
    ("tabung", r"\b(nabung|menabung|tabung|tabungan)\b"),
    ("masuk", r"\b(gaji|jual|jualan|terjual|sampingan|freelance|bonus|thr|komisi|untung|"
              r"terima|dapat|pemasukan|masuk)\b"),
    ("keluar", r"\b(beli|belanja|bayar|jajan|makan|bensin|isi|topup|pengeluaran|keluar)\b"),
]
KAT = {
    "makanan": r"bakso|makan|kopi|nasi|ayam|mie|minum|sate|\bes\b|snack|warung|jajan",
    "transport": r"bensin|ojek|grab|gojek|parkir|tol|angkot|servis",
    "tagihan": r"listrik|wifi|pulsa|kuota|token|cicil|kos|sewa",
    "hiburan": r"game|nonton|topup|top up|netflix|voucher|skin",
    "belanja": r"beli|belanja|baju|sepatu|charger",
}


def amount(text):
    """Ambil nominal. Angka dengan satuan (rb/jt) diutamakan: 'beli 2 bakso 20rb' -> 20000."""
    best = None
    for m in NUM.finditer(text):
        n = m.group(1)
        n = n.replace(".", "") if re.fullmatch(r"\d{1,3}(\.\d{3})+", n) else n.replace(",", ".")
        u = (m.group(2) or "").lower()
        v = int(float(n) * MULT.get(u, 1))
        if best is None or (u and not best[1]) or (bool(u) == bool(best[1]) and v >= best[0]):
            best = (v, u)
    return best[0] if best else None


def kategori(s, tipe):
    if tipe == "masuk":
        if re.search(r"gaji", s):
            return "gaji"
        return "usaha" if re.search(r"jual|jualan|terjual|sampingan|freelance|komisi|untung", s) else "lainnya"
    if tipe == "keluar":
        for k in ("makanan", "transport", "tagihan", "hiburan", "belanja"):
            if re.search(KAT[k], s):
                return k
        return "lainnya"
    return "tabungan"


def parse_ai(text):
    p = ('Ubah pesan keuangan bahasa Indonesia ini jadi JSON saja, tanpa teks lain: '
         '{"tipe":"keluar|masuk|tabung|tarik","jumlah":angka rupiah,'
         '"kategori":"makanan|transport|belanja|tagihan|hiburan|gaji|usaha|lainnya","catatan":"singkat"}. '
         'rb/ribu=1000, jt/juta=1000000. Kalau bukan transaksi balas {"tipe":null}. Pesan: ' + text)
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=20,
                      headers={"x-api-key": AK, "anthropic-version": "2023-06-01"},
                      json={"model": "claude-haiku-5-5", "max_tokens": 200,
                            "messages": [{"role": "user", "content": p}]})
    t = r.json()["content"][0]["text"]
    return json.loads(re.search(r"\{.*\}", t, re.S).group())


def parse(text, forced=None):
    s, jml, tipe = text.lower(), amount(text), forced
    if not tipe:
        for t, rx in KW:
            if re.search(rx, s):
                tipe = t
                break
    if tipe and jml:
        return dict(tipe=tipe, jumlah=jml, kategori=kategori(s, tipe), catatan=text.strip()[:60])
    if AK and not forced:
        try:
            d = parse_ai(text)
            if d.get("tipe") in ("keluar", "masuk", "tabung", "tarik") and d.get("jumlah"):
                return d
        except Exception as e:
            print("AI gagal:", e)
    return None


# ---------- logika transaksi ----------
def ringkas():
    s = stats()
    out = f"💰 Saldo: {rp(s['saldo'])}\n🏦 Tabungan: {rp(s['tabung'])}"
    if s["target"]:
        out += f" ({s['tabung'] * 100 // s['target']}% dari {rp(s['target'])})"
    if s["jajan"]:
        out += f"\n🍜 Jajan minggu ini: {rp(s['minggu'])} / {rp(s['jajan'])}\n   Sisa: {rp(s['sisa'])}"
    return out


IC = {"keluar": "🔴 Pengeluaran", "masuk": "🟢 Pemasukan", "tabung": "🏦 Nabung", "tarik": "💸 Tarik tabungan"}


def simpan(d):
    s, j, t = stats(), int(d["jumlah"]), d["tipe"]
    if t in ("keluar", "tabung") and j > s["saldo"]:
        return (f"⚠️ Saldo nggak cukup (sisa {rp(s['saldo'])}).\n"
                "Catat pemasukan dulu, misalnya: gaji 3jt")
    if t == "tarik" and j > s["tabung"]:
        return f"⚠️ Tabungan cuma {rp(s['tabung'])}."
    q("insert into trx(ts,tipe,jumlah,kategori,catatan) values(%s,%s,%s,%s,%s)",
      (now().strftime("%Y-%m-%d %H:%M:%S"), t, j, d.get("kategori", "lainnya"),
       str(d.get("catatan", ""))[:60]), write=True)
    return f"{IC[t]}: {d.get('catatan')} {rp(j)}\n\n{ringkas()}"


def send(chat, text):
    requests.post(API + "/sendMessage", json={"chat_id": chat, "text": text}, timeout=15)


HELP = ("Ketik bebas, kata kunci menentukan jenisnya:\n"
        "🔴 beli / bayar / makan / bensin → pengeluaran\n"
        "   beli bakso 20rb\n"
        "🟢 gaji / jual / sampingan / bonus → pemasukan\n"
        "   jual hp 500rb, gaji 3jt\n"
        "🏦 nabung 100rb → masuk tabungan\n"
        "💸 tarik 50rb → ambil dari tabungan\n\n"
        "Command:\n"
        "/saldo → ringkasan\n/riwayat → 10 transaksi terakhir\n"
        "/laporan → per kategori bulan ini (/laporan minggu)\n"
        "/jajan 200rb → jatah jajan mingguan\n/target 5jt → target tabungan\n"
        "/hapus → hapus transaksi terakhir\n"
        "Atau paksa jenisnya: /masuk /keluar /tabung /tarik + nominal")

CMDS = {"/start", "/help", "/saldo", "/jajan", "/target", "/hapus", "/riwayat", "/laporan",
        "/masuk", "/keluar", "/tabung", "/tarik"}
FORCE = {"/masuk": "masuk", "/keluar": "keluar", "/tabung": "tabung", "/tarik": "tarik"}
RIW = {"masuk": "🟢", "keluar": "🔴", "tabung": "🏦", "tarik": "💸"}


def handle(chat, text):
    parts = text.split(None, 1)
    cmd = parts[0].lower().split("@")[0] if text.startswith("/") else ""
    rest = parts[1] if len(parts) > 1 else ""
    if cmd and cmd not in CMDS:  # "/nabung 3jt" atau "/pemasukan 50k" tetap dibaca biasa
        text, cmd = text.lstrip("/"), ""

    if cmd in ("/start", "/help"):
        send(chat, HELP)
    elif cmd == "/saldo":
        send(chat, ringkas())
    elif cmd in ("/jajan", "/target"):
        n = amount(rest)
        if not n:
            return send(chat, f"Contoh: {cmd} 200rb")
        cfg(cmd[1:], n)
        send(chat, f"✅ {cmd[1:].capitalize()} diset {rp(n)}")
    elif cmd == "/hapus":
        r = q("delete from trx where id=(select max(id) from trx) returning catatan, jumlah", one=True)
        send(chat, f"🗑 Dihapus: {r['catatan']} {rp(r['jumlah'])}" if r else "Belum ada transaksi.")
    elif cmd == "/riwayat":
        rows = q("select ts,tipe,jumlah,catatan from trx order by id desc limit 10")
        send(chat, "\n".join(f"{RIW[r['tipe']]} {r['ts'][5:10]} {r['catatan']} {rp(r['jumlah'])}"
                             for r in rows) or "Belum ada transaksi.")
    elif cmd == "/laporan":
        mg = rest.strip().lower().startswith("minggu")
        out = f"📊 Laporan {'minggu' if mg else 'bulan'} ini\n"
        for tipe, judul in (("keluar", "🔴 Pengeluaran"), ("masuk", "🟢 Pemasukan")):
            rows = per_kategori(week_start() if mg else month_start(), tipe)
            out += f"\n{judul}: {rp(sum(r['j'] for r in rows))}\n"
            out += "".join(f"• {r['kategori']}: {rp(r['j'])}\n" for r in rows)
        send(chat, out)
    elif cmd in FORCE:
        d = parse(rest, FORCE[cmd])
        send(chat, simpan(d) if d else f"Contoh: {cmd} 20rb bakso")
    else:
        d = parse(text)
        send(chat, simpan(d) if d else
             "Aku nggak nangkep 🤔 Pakai kata kunci + nominal, contoh: beli bakso 20rb\n"
             "Ketik /help untuk daftar lengkap.")


# ---------- web (Vercel) ----------
app = Flask(__name__)

PAGE = """<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Keuangan</title>
<style>
:root{--bg:#f6f6f3;--card:#fff;--tx:#1c1c1a;--mu:#77756e;--bd:#e4e2da;--ok:#2f9e63;--no:#d6453d;--sv:#3b7ddd}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--card:#222220;--tx:#eeede8;--mu:#9a988f;--bd:#33332f}}
body{margin:0;padding:16px;background:var(--bg);color:var(--tx);font:15px system-ui,sans-serif;max-width:640px;margin:auto}
h1{font-size:20px}.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;margin:12px 0}
.big{font-size:28px;font-weight:700}.mu{color:var(--mu);font-size:13px}
.bar{height:10px;background:var(--bd);border-radius:6px;overflow:hidden;margin:8px 0}.bar i{display:block;height:100%}
.row{display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid var(--bd)}
.g{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.keluar{color:var(--no)}.masuk{color:var(--ok)}.tabung,.tarik{color:var(--sv)}
</style></head><body>
<h1>💰 Keuangan</h1>
<div class="card"><div class="mu">Saldo</div><div class="big">{{rp(s.saldo)}}</div>
<div class="mu">Masuk {{rp(s.masuk)}} · Keluar {{rp(s.keluar)}}</div></div>
<div class="g">
<div class="card"><div class="mu">🍜 Jajan minggu ini</div>
{% if s.jajan %}<div class="big">{{rp(s.sisa)}}</div><div class="mu">sisa dari {{rp(s.jajan)}}</div>
<div class="bar"><i style="width:{{[100,s.minggu*100//s.jajan]|min}}%;background:{{'var(--no)' if s.sisa<0 else 'var(--ok)'}}"></i></div>
{% else %}<div class="mu">Belum diset. Ketik /jajan 200rb</div>{% endif %}</div>
<div class="card"><div class="mu">🏦 Tabungan</div><div class="big">{{rp(s.tabung)}}</div>
{% if s.target %}<div class="mu">target {{rp(s.target)}}</div>
<div class="bar"><i style="width:{{[100,s.tabung*100//s.target]|min}}%;background:var(--sv)"></i></div>
{% else %}<div class="mu">Set target: /target 5jt</div>{% endif %}</div></div>
<div class="card"><b>Pengeluaran bulan ini</b>
{% for k in kat %}<div class="row"><div>{{k.kategori}}</div><div class="keluar">{{rp(k.j)}}</div></div>
{% else %}<div class="mu">Belum ada.</div>{% endfor %}</div>
<div class="card"><b>Transaksi terakhir</b>
{% for t in rows %}<div class="row"><div>{{t.catatan}}<div class="mu">{{t.ts[:16]}} · {{t.kategori}}</div></div>
<div class="{{t.tipe}}">{{'+' if t.tipe in ('masuk','tarik') else '-'}}{{rp(t.jumlah)}}</div></div>
{% else %}<div class="mu">Belum ada data.</div>{% endfor %}</div>
</body></html>"""


@app.route("/")
def index():
    if request.args.get("key") != KEY:
        abort(403)
    rows = q("select * from trx order by id desc limit 50")
    return render_template_string(PAGE, s=type("S", (), stats()), rows=rows,
                                  kat=per_kategori(month_start()), rp=rp)


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    m = (request.get_json(silent=True) or {}).get("message") or {}
    uid, text = m.get("from", {}).get("id"), m.get("text")
    if text:
        chat = m["chat"]["id"]
        if not OWNER:
            send(chat, f"ID kamu: {uid}\nTambahkan env OWNER_ID={uid} di Vercel lalu Redeploy.")
        elif uid == OWNER:
            try:
                handle(chat, text.strip())
            except Exception as e:
                print("error:", e)
                send(chat, "⚠️ Ada error, coba kirim lagi.")
    return "ok"


@app.route("/setup")
def setup():  # buka sekali setelah deploy: /setup?key=DASH_KEY
    if request.args.get("key") != KEY:
        abort(403)
    r = requests.post(API + "/setWebhook", timeout=15, json={
        "url": f"https://{request.host}/webhook", "secret_token": SECRET,
        "allowed_updates": ["message"]})
    return r.text
