# ==============================================================================
# سجل تدريب موظفين الخط الأمامي — Template 1755809
# GitHub Actions / Colab script — builds dashboard PDF and sends via email
# ==============================================================================

import os
import re
import ast
import html
import time
import base64
import smtplib
import asyncio
import concurrent.futures
from io import BytesIO
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# ==============================================================================
# CONFIG  (GitHub Actions injects env vars; Colab uses the fallbacks)
# ==============================================================================
ZENPUT_API_KEY = os.environ.get("ZENPUT_API_KEY", "PASTE_TOKEN_FOR_COLAB")
GMAIL_PASS     = os.environ.get("GMAIL_PASS", "")
SEND_EMAIL     = os.environ.get("SEND_EMAIL", "true").lower() == "true"
INCLUDE_PHOTOS = os.environ.get("INCLUDE_PHOTOS", "true").lower() == "true"

GMAIL_USER = "aof.group.auto@gmail.com"
TO_EMAIL   = "s.alharbi@aofgroup.com"
CC_EMAIL   = ["a.alsalem@aofgroup.com", "o.salahaddin@aofgroup.com"]

TEMPLATE_ID  = 1755809
REPORT_TITLE = "سجل تدريب موظفين الخط الأمامي"
KSA          = ZoneInfo("Asia/Riyadh")
API_URL      = "https://www.zenput.com/api/v3/submissions/"
STORAGE_URL  = "https://www.zenput.com/api/v2/users/current/storage/"
HEADERS      = {"X-API-TOKEN": ZENPUT_API_KEY}

# "created" = session start date (sessions often finish after midnight)
# "submitted" = submission date
DATE_BASIS = os.environ.get("DATE_BASIS", "created")

today       = datetime.now(KSA).date()
_rep_start  = os.environ.get("REPORT_START", "").strip()   # YYYY-MM-DD, optional
_rep_end    = os.environ.get("REPORT_END", "").strip()
START_DATE  = date.fromisoformat(_rep_start) if _rep_start else today - timedelta(days=1)
END_DATE    = date.fromisoformat(_rep_end)   if _rep_end   else today - timedelta(days=1)
DATE_LABEL  = str(END_DATE) if START_DATE == END_DATE else f"{START_DATE} إلى {END_DATE}"

OUTPUT_DIR = "/content" if os.path.isdir("/content") else "."

print(f"Report period: {START_DATE} -> {END_DATE}  (basis: {DATE_BASIS})")

# ==============================================================================
# FIELD MAP (by field_id — titles repeat per slot and 'بنود التدريب' for
# trainees 2/3 sits at the end of the form, so positional parsing is unsafe)
# ==============================================================================
HEADER_FIELDS = {
    "branch": 17529442, "trainer_name": 17529444, "trainer_id": 17529445,
    "declared": 17529447, "notes": 17529491, "support_photos": 17529492,
}
SLOT_FIELDS = {
    1: dict(name=17529449, emp_id=17529450, photo=17529451, status=17529452,
            section=17529453, items=17529454, train_photos=17529455, response=17529456,
            pct=17529457, eff=17529458, passed=17529459, retrain=17529460, schedule=17529461),
    2: dict(name=17529463, emp_id=17529464, photo=17529465, status=17529466,
            section=17529467, items=17529493, train_photos=17529469, response=17529470,
            pct=17529471, eff=17529472, passed=17529473, retrain=17529474, schedule=17529475),
    3: dict(name=17529477, emp_id=17529478, photo=17529479, status=17529480,
            section=17529481, items=17529494, train_photos=17529483, response=17529484,
            pct=17529485, eff=17529486, passed=17529487, retrain=17529488, schedule=17529489),
}
KNOWN_IDS = set(HEADER_FIELDS.values()) | {fid for d in SLOT_FIELDS.values() for fid in d.values()}

CANONICAL_ITEMS = [
    "نظافة المنطقة", "تجهيز المنطقة", "تنظيف منطقة المحلي", "استلام الشفت",
    "تسليم الشفت", "عد العهدة قبل وبعد استلام الشفت", "قراءة الفواتير", "تجميع الطلب",
    "تصوير الطلب", "تغليف الطلب", "تسليم الطلب", "تدريب على اخذ الطلب من العميل",
    "الترحيب والضيافة", "تدريب على الاقتراحات", "تدريب على المنيو", "تدريب على العروض المتاحة",
]

# ==============================================================================
# 1. FETCH (retry on 403/5xx, fall back to one-by-one if a page stays blocked)
# ==============================================================================
def api_get(params, tries=3):
    r = None
    for attempt in range(tries):
        r = requests.get(API_URL, headers=HEADERS,
                         params={"form_template_id": TEMPLATE_ID, **params}, timeout=40)
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429, 500, 502, 503, 504):
            print(f"  HTTP {r.status_code} (attempt {attempt + 1}/{tries}) params={params}")
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
    return r

def sub_dates(s):
    m   = s.get("smetadata") or {}
    sub = (m.get("date_submitted_local") or "")[:10]
    cre = (m.get("date_created_local") or "")[:10] or sub
    return sub, cre

def in_window(s):
    sub, cre = sub_dates(s)
    d = cre if DATE_BASIS == "created" else sub
    return str(START_DATE) <= d <= str(END_DATE)

def fetch_submissions():
    records, seen = [], set()
    offset, page_size, total, blocked = 0, 100, None, []
    while True:
        r = api_get({"limit": page_size, "start": offset})
        if r.status_code != 200:
            if page_size > 1:
                print(f"  page blocked at offset {offset} -> switching to one-by-one")
                page_size = 1
                continue
            blocked.append(offset)
            offset += 1
            if (total is not None and offset >= total) or len(blocked) > 25:
                break
            continue
        payload = r.json()
        if total is None:
            total = (payload.get("meta") or {}).get("count")
        batch = payload.get("data", [])
        if not batch:
            break
        stop = False
        for s in batch:
            if s.get("id") in seen:
                continue
            seen.add(s.get("id"))
            sub, _ = sub_dates(s)
            # created <= submitted, so once submitted < START nothing older can qualify
            if sub and sub < str(START_DATE):
                stop = True
                break
            if in_window(s):
                records.append(s)
        if stop:
            break
        offset += len(batch)
        if total is not None and offset >= total:
            break
        if page_size > 1 and len(batch) < page_size:
            break
    print(f"  Fetched {len(records)} submissions in window"
          + (f" | blocked offsets skipped: {blocked}" if blocked else ""))
    return records

# ==============================================================================
# 2. PARSE
# ==============================================================================
UNMAPPED = Counter()

def clean(s):
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()

def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [clean(x) for x in v if clean(x)]
    s = str(v).strip()
    if s.startswith("["):
        try:
            return [clean(x) for x in ast.literal_eval(s) if clean(x)]
        except Exception:
            pass
    return [clean(s)] if clean(s) else []

def first(v):
    lst = as_list(v)
    return lst[0] if lst else ""

def photo_list(v):
    if not isinstance(v, list):
        return []
    return [p for p in v if isinstance(p, dict) and p.get("s3_key")]

def fmt_duration(minutes):
    if minutes is None:
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}س {m}د" if h else f"{m}د"

def parse_submission(s):
    m     = s.get("smetadata") or {}
    ans   = s.get("answers", [])
    by_id = {a.get("field_id"): a for a in ans}

    for a in ans:
        if (a.get("field_id") not in KNOWN_IDS and a.get("field_type") != "section"
                and a.get("is_answered")):
            UNMAPPED[(a.get("field_id"), a.get("title"))] += 1

    def val(fid):
        a = by_id.get(fid)
        return a.get("value") if a else None

    def note(fid):
        a = by_id.get(fid)
        return clean(a.get("notes")) if a else ""

    branch_ans = by_id.get(HEADER_FIELDS["branch"]) or {}
    branch = clean(branch_ans.get("display_value")) or clean((m.get("location") or {}).get("name"))

    declared_raw = val(HEADER_FIELDS["declared"])
    try:
        declared = int(declared_raw) if declared_raw not in (None, "") else None
    except Exception:
        declared = None

    created  = m.get("date_created_local") or ""
    finished = m.get("date_completed_local") or m.get("date_submitted_local") or ""
    ttc      = m.get("time_to_complete")
    duration = (ttc / 60000.0) if isinstance(ttc, (int, float)) and ttc > 0 else None

    trainees = []
    for n, f in SLOT_FIELDS.items():
        name   = first(val(f["name"]))
        emp_id = first(val(f["emp_id"]))
        if not (name or emp_id):
            continue
        pct_raw = val(f["pct"])
        try:
            eff_pct = float(pct_raw) if pct_raw not in (None, "", []) else None
        except Exception:
            eff_pct = None
        items = as_list(val(f["items"]))
        slot_notes = [note(fid) for fid in f.values() if note(fid)]
        trainees.append({
            "slot": n, "name": name, "emp_id": emp_id,
            "photo_keys": [p["s3_key"] for p in photo_list(val(f["photo"]))],
            "status": first(val(f["status"])),
            "section": first(val(f["section"])), "section_inherited": False,
            "items": items, "items_count": len(set(items)),
            "train_photo_count": len(photo_list(val(f["train_photos"]))),
            "response": first(val(f["response"])),
            "eff_pct": eff_pct, "efficiency": first(val(f["eff"])),
            "passed": first(val(f["passed"])),
            "needs_retrain": str(val(f["retrain"]) or "").lower() == "true",
            "schedule": first(val(f["schedule"])),
            "note": " | ".join(OrderedDict.fromkeys(slot_notes)),
        })

    # section is usually only answered on trainee 1 — inherit and flag
    base_section = next((t["section"] for t in trainees if t["section"]), "")
    for t in trainees:
        if not t["section"] and base_section:
            t["section"], t["section_inherited"] = base_section, True

    support = photo_list(val(HEADER_FIELDS["support_photos"]))
    all_photos = []
    for a in ans:
        if a.get("field_type") == "image":
            all_photos += photo_list(a.get("value"))

    flags = []
    if declared is not None and declared != len(trainees):
        flags.append(f"عدد المتدربين المُدخل {declared} بينما المسجَّل فعلياً {len(trainees)}")
    missing_result = [t["name"] for t in trainees if not t["passed"]]
    if missing_result:
        flags.append("نتيجة الاجتياز غير محددة: " + "، ".join(missing_result))
    if any(t["section_inherited"] for t in trainees):
        flags.append("القسم غير مُدخل لبعض المتدربين (تم اعتماد قسم المتدرب الأول)")
    low_q = sum(1 for p in all_photos if p.get("is_low_quality"))
    if low_q:
        flags.append(f"{low_q} صورة منخفضة الجودة")

    return {
        "id": s.get("id"),
        "date": (created or finished)[:10] if DATE_BASIS == "created" else finished[:10],
        "start_time": created[11:16], "end_time": finished[11:16],
        "end_date": finished[:10], "duration_min": duration,
        "branch": branch,
        "trainer_name": clean(val(HEADER_FIELDS["trainer_name"])),
        "trainer_id": clean(val(HEADER_FIELDS["trainer_id"])),
        "submitter": clean((m.get("created_by") or {}).get("display_name")),
        "declared": declared, "notes": clean(val(HEADER_FIELDS["notes"])),
        "support_photo_count": len(support), "low_quality_photos": low_q,
        "trainees": trainees, "flags": flags,
    }

print("Fetching data...")
raw_subs    = fetch_submissions()
submissions = [parse_submission(s) for s in raw_subs]
submissions = [s for s in submissions if s["branch"]]
print(f"  Parsed {len(submissions)} sessions")
if UNMAPPED:
    print("  WARNING — answered fields not in the field map (template may have changed):")
    for (fid, title), c in UNMAPPED.most_common():
        print(f"    field_id={fid}  title={title}  x{c}")

# ==============================================================================
# 3. PHOTOS (trainee photo thumbnails via storage exchange)
# ==============================================================================
THUMBS = {}

def fetch_thumb(s3_key):
    try:
        from PIL import Image, ImageOps
        r = requests.get(STORAGE_URL, headers=HEADERS, params={"path": s3_key}, timeout=30)
        r.raise_for_status()
        loc = (r.json().get("data") or {}).get("location")
        if not loc:
            return s3_key, None
        img = requests.get(loc, timeout=30)
        img.raise_for_status()
        im = ImageOps.exif_transpose(Image.open(BytesIO(img.content))).convert("RGB")
        im.thumbnail((180, 180))
        buf = BytesIO()
        im.save(buf, "JPEG", quality=72)
        return s3_key, "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"    photo failed: {s3_key[-40:]} ({e})")
        return s3_key, None

if INCLUDE_PHOTOS and submissions:
    keys = list(OrderedDict.fromkeys(
        t["photo_keys"][0] for s in submissions for t in s["trainees"] if t["photo_keys"]))
    print(f"Fetching {len(keys)} trainee photos...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for k, uri in ex.map(fetch_thumb, keys):
            if uri:
                THUMBS[k] = uri
    print(f"  {len(THUMBS)}/{len(keys)} photos ready")

# ==============================================================================
# 4. DATAFRAME + KPIs
# ==============================================================================
rows = []
for sub in submissions:
    for tr in sub["trainees"]:
        rows.append({
            "submission_id": sub["id"], "date": sub["date"], "branch": sub["branch"],
            "trainer_name": sub["trainer_name"], "trainer_id": sub["trainer_id"],
            "duration_min": sub["duration_min"],
            "name": tr["name"], "emp_id": tr["emp_id"], "status": tr["status"],
            "section": tr["section"], "items_count": tr["items_count"],
            "efficiency": tr["efficiency"], "eff_pct": tr["eff_pct"],
            "passed": tr["passed"], "needs_retrain": tr["needs_retrain"],
            "schedule": tr["schedule"], "response": tr["response"],
        })
df = pd.DataFrame(rows)
print(f"  Total trainees: {len(df)}")

all_items = list(CANONICAL_ITEMS)
for sub in submissions:
    for tr in sub["trainees"]:
        for it in tr["items"]:
            if it not in all_items:
                all_items.append(it)
N_ITEMS = len(CANONICAL_ITEMS)

EFFICIENCY_COLOR = {
    "ممتاز": "#28a745", "جيد جداً": "#20c997", "جيد جدا": "#20c997",
    "جيد": "#17a2b8", "مقبول": "#ffc107", "ضعيف": "#fd7e14", "غير مؤهل": "#dc3545",
}

def eff_color(eff):
    return EFFICIENCY_COLOR.get(clean(eff), "#636e72")

def pct_color(pct):
    if pct is None or pd.isna(pct): return "#636e72"
    if pct >= 85: return "#28a745"
    if pct >= 70: return "#20c997"
    if pct >= 60: return "#fd7e14"
    return "#dc3545"

def compute_kpis(d):
    total = len(d)
    if total == 0:
        return {}
    pct     = d["eff_pct"].dropna()
    passed  = int((d["passed"] == "اجتاز").sum())
    failed  = int((d["passed"] == "لم يجتاز").sum())
    pending = total - passed - failed
    retrain = int(d["needs_retrain"].sum())
    full    = int((d["items_count"] >= N_ITEMS).sum())
    return {
        "total": total, "sessions": d["submission_id"].nunique(),
        "branches": d["branch"].nunique(),
        "avg_pct": round(pct.mean(), 1) if not pct.empty else 0,
        "passed": passed, "pct_passed": round(passed / total * 100, 1),
        "failed": failed, "pct_failed": round(failed / total * 100, 1),
        "pending": pending,
        "retrain": retrain, "pct_retrain": round(retrain / total * 100, 1),
        "new": int((d["status"] == "موظف جديد").sum()),
        "old": int((d["status"] == "موظف قديم").sum()),
        "avg_items": round(d["items_count"].mean(), 1),
        "full_items": full, "pct_full_items": round(full / total * 100, 1),
        "eff_counts": d["efficiency"].map(clean).value_counts(),
    }

overall = compute_kpis(df) if not df.empty else {}
sess_durations = [s["duration_min"] for s in submissions if s["duration_min"]]
avg_duration   = sum(sess_durations) / len(sess_durations) if sess_durations else None

def group_table(key):
    out = []
    if df.empty:
        return pd.DataFrame()
    for k, grp in df.groupby(key):
        kp = compute_kpis(grp)
        durs = grp.drop_duplicates("submission_id")["duration_min"].dropna()
        out.append({
            key: k, "dates": "، ".join(sorted(grp["date"].unique())),
            "trainers": "، ".join(sorted(grp["trainer_name"].unique())),
            "branches_list": "، ".join(sorted(grp["branch"].unique())),
            "sessions": kp["sessions"], "total": kp["total"], "avg_pct": kp["avg_pct"],
            "pct_passed": kp["pct_passed"], "pct_retrain": kp["pct_retrain"],
            "avg_items": kp["avg_items"], "branches": kp["branches"],
            "avg_duration": durs.mean() if not durs.empty else None,
        })
    return pd.DataFrame(out).sort_values("avg_pct", ascending=False)

branch_df  = group_table("branch")
trainer_df = group_table("trainer_name")

# ==============================================================================
# 5. HTML
# ==============================================================================
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Cairo', sans-serif; direction: rtl; text-align: right; background: #f0f2f5; color: #2d3436; padding: 20px; }
.container { width: 1300px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #0f6e8c 0%, #0a3d62 100%); color: white; border-radius: 12px; padding: 30px 40px; margin-bottom: 25px; text-align: center; }
.header h1 { font-size: 30px; font-weight: 700; margin-bottom: 6px; }
.header .subtitle { font-size: 15px; opacity: 0.85; }
.section-title { font-size: 19px; font-weight: 700; color: #0a3d62; border-right: 5px solid #0f6e8c; padding-right: 12px; margin: 28px 0 14px; }
.kpi-grid { display: grid; grid-template-columns: repeat(6,1fr); gap: 14px; margin-bottom: 22px; }
.kpi-card { background: white; border-radius: 10px; padding: 18px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border-top: 4px solid #0f6e8c; }
.kpi-card.green  { border-top-color: #28a745; }
.kpi-card.orange { border-top-color: #fd7e14; }
.kpi-card.red    { border-top-color: #dc3545; }
.kpi-card.teal   { border-top-color: #20c997; }
.kpi-card.purple { border-top-color: #6f42c1; }
.kpi-label { font-size: 13px; color: #636e72; margin-bottom: 8px; }
.kpi-value { font-size: 32px; font-weight: 700; color: #2d3436; }
.kpi-sub   { font-size: 12px; color: #8a959b; margin-top: 4px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.eff-grid { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 22px; }
.eff-card { background: white; border-radius: 10px; padding: 14px 20px; text-align: center; box-shadow: 0 2px 6px rgba(0,0,0,0.07); flex: 1; min-width: 130px; }
.eff-label { font-size: 13px; color: #636e72; margin-bottom: 6px; }
.eff-value { font-size: 28px; font-weight: 700; }
.eff-bar   { height: 6px; background: #f0f0f0; border-radius: 3px; margin-top: 8px; overflow: hidden; }
.eff-fill  { height: 100%; border-radius: 3px; }
.table-wrap { background: white; border-radius: 10px; padding: 20px; margin-bottom: 22px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead tr { background: #0a3d62; color: white; }
th { padding: 11px 8px; font-weight: 600; text-align: center; white-space: nowrap; }
td { padding: 9px 7px; border-bottom: 1px solid #f0f0f0; text-align: center; vertical-align: middle; }
.branch-name { text-align: right; font-weight: 600; }
.rank-badge { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 50%; background: #e9ecef; font-weight: 700; font-size: 12px; color: #495057; }
.rank-badge.gold   { background: #ffd700; color: #6d5700; }
.rank-badge.silver { background: #c0c0c0; color: #4a4a4a; }
.rank-badge.bronze { background: #cd7f32; color: white; }
.mini-bar  { height: 7px; background: #e9ecef; border-radius: 4px; overflow: hidden; margin-top: 3px; }
.mini-fill { height: 100%; border-radius: 4px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; color: white; white-space: nowrap; }
.passed-yes { background: #28a745; } .passed-no { background: #dc3545; } .passed-na { background: #b2bec3; color: #444; }
.resp-yes   { background: #17a2b8; } .resp-no   { background: #dc3545; }
.status-new { background: #6f42c1; } .status-old { background: #495057; } .status-re { background: #e83e8c; }
.cov-row { display: grid; grid-template-columns: 1fr 70px 160px; gap: 10px; align-items: center; padding: 6px 0; border-bottom: 1px solid #f3f3f3; font-size: 13px; }
.cov-row:last-child { border-bottom: none; }
.cov-bar { height: 9px; background: #e9ecef; border-radius: 5px; overflow: hidden; }
.cov-fill { height: 100%; border-radius: 5px; }
.branch-section { background: white; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow: hidden; page-break-inside: avoid; }
.branch-header { background: #0a3d62; color: white; padding: 13px 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.branch-header h3 { font-size: 16px; font-weight: 700; }
.branch-header .meta { font-size: 12px; opacity: 0.9; }
.trainer-badge { background: rgba(255,255,255,0.18); border-radius: 8px; padding: 3px 10px; font-size: 12px; }
.emp-table-wrap { padding: 14px 18px; }
.emp-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.emp-table th { background: #e8f3f8; color: #0a3d62; padding: 9px 7px; font-weight: 600; text-align: center; border-bottom: 2px solid #b9dceb; white-space: nowrap; }
.emp-table td { padding: 8px 7px; border-bottom: 1px solid #f0f0f0; text-align: center; vertical-align: middle; }
.emp-name { text-align: right; font-weight: 600; min-width: 110px; }
.avatar { width: 58px; height: 58px; border-radius: 8px; object-fit: cover; border: 1px solid #dfe6e9; }
.avatar-empty { width: 58px; height: 58px; border-radius: 8px; background: #f1f2f6; display: inline-flex; align-items: center; justify-content: center; color: #b2bec3; font-size: 11px; }
.items-missing { font-size: 10.5px; color: #c0392b; margin-top: 3px; max-width: 200px; display: inline-block; }
.trainee-note { font-size: 11px; color: #6c5300; background: #fff8e1; border-radius: 6px; padding: 3px 7px; display: inline-block; max-width: 200px; }
.inherited { font-size: 10px; color: #b2bec3; display: block; }
.notes-row td { background: #f2f8fb; color: #0a3d62; font-size: 12px; text-align: right; padding: 8px 14px; }
.flags-row td { background: #fff4f0; color: #b34700; font-size: 12px; text-align: right; padding: 8px 14px; }
.empty { color: #636e72; padding: 20px; }
"""

DASH = '<span style="color:#b2bec3">—</span>'

def esc(x):
    return html.escape(str(x if x is not None else ""))

def rank_badge_html(i):
    cls = {0: "gold", 1: "silver", 2: "bronze"}.get(i, "")
    return f'<span class="rank-badge {cls}">{i + 1}</span>'

def response_badge(val):
    if val == "مستجيب":     return '<span class="badge resp-yes">مستجيب</span>'
    if val == "غير مستجيب": return '<span class="badge resp-no">غير مستجيب</span>'
    return f'<span class="badge resp-yes">{esc(val)}</span>' if val else DASH

def status_badge(val):
    cls = {"موظف جديد": "status-new", "موظف قديم": "status-old", "إعادة تدريب": "status-re"}.get(val, "passed-na")
    return f'<span class="badge {cls}">{esc(val)}</span>' if val else DASH

def eff_badge(eff, pct):
    label   = esc(clean(eff)) or "—"
    pct_str = f"{pct:.0f}%" if pct is not None else "—"
    return (f'<div style="display:inline-block"><span class="badge" style="background:{eff_color(eff)}">{label}</span>'
            f'<div style="font-size:11px;color:{pct_color(pct)};font-weight:700;margin-top:3px">{pct_str}</div></div>')

def items_cell(items):
    got = set(items)
    cnt = len(got)
    if cnt == 0:
        return DASH
    color   = "#28a745" if cnt >= N_ITEMS else ("#fd7e14" if cnt >= N_ITEMS * 0.75 else "#dc3545")
    missing = [i for i in CANONICAL_ITEMS if i not in got]
    miss    = (f'<div class="items-missing">ناقص: {esc("، ".join(missing))}</div>' if missing else "")
    return f'<span style="font-weight:700;color:{color}">{cnt}/{N_ITEMS}</span>{miss}'

def result_cell(passed, needs_retrain, schedule):
    if passed == "اجتاز":      primary = '<span class="badge passed-yes">اجتاز</span>'
    elif passed == "لم يجتاز": primary = '<span class="badge passed-no">لم يجتاز</span>'
    else:                      primary = '<span class="badge passed-na">غير محدد</span>'
    secondary = ""
    if needs_retrain:
        sched = f" ({esc(schedule)})" if schedule else ""
        secondary = f'<div style="font-size:11px;color:#fd7e14;margin-top:4px;font-weight:600">إعادة تدريب{sched}</div>'
    return f"<div>{primary}{secondary}</div>"

def avatar(tr):
    key = tr["photo_keys"][0] if tr["photo_keys"] else None
    if key and key in THUMBS:
        return f'<img class="avatar" src="{THUMBS[key]}">'
    return f'<span class="avatar-empty">{"لا صورة" if not key else "—"}</span>'

def bar_cell(value, color, cap=100):
    return (f'<span style="color:{color};font-weight:700">{value}%</span>'
            f'<div class="mini-bar"><div class="mini-fill" style="width:{min(value, cap)}%;background:{color}"></div></div>')

def overall_kpi_section():
    if not overall:
        return "<p class='empty'>لا توجد جلسات تدريب مسجلة لهذه الفترة</p>"
    o = overall
    return f"""
    <div class="kpi-grid">
        <div class="kpi-card">
            <div class="kpi-label">إجمالي المتدربين</div>
            <div class="kpi-value">{o['total']}</div>
            <div class="kpi-sub">{o['sessions']} جلسة | {o['branches']} فرع</div>
        </div>
        <div class="kpi-card teal">
            <div class="kpi-label">متوسط نسبة الكفاءة</div>
            <div class="kpi-value" style="color:{pct_color(o['avg_pct'])}">{o['avg_pct']}%</div>
            <div class="kpi-sub">جديد {o['new']} | قديم {o['old']}</div>
        </div>
        <div class="kpi-card green">
            <div class="kpi-label">اجتازوا القسم</div>
            <div class="kpi-value" style="color:#28a745">{o['pct_passed']}%</div>
            <div class="kpi-sub">{o['passed']} متدرب</div>
        </div>
        <div class="kpi-card red">
            <div class="kpi-label">لم يجتازوا</div>
            <div class="kpi-value" style="color:#dc3545">{o['pct_failed']}%</div>
            <div class="kpi-sub">{o['failed']} متدرب | غير محدد {o['pending']}</div>
        </div>
        <div class="kpi-card orange">
            <div class="kpi-label">يحتاجون إعادة تدريب</div>
            <div class="kpi-value" style="color:#fd7e14">{o['pct_retrain']}%</div>
            <div class="kpi-sub">{o['retrain']} متدرب</div>
        </div>
        <div class="kpi-card purple">
            <div class="kpi-label">اكتمال بنود التدريب</div>
            <div class="kpi-value" style="color:#6f42c1">{o['pct_full_items']}%</div>
            <div class="kpi-sub">متوسط {o['avg_items']}/{N_ITEMS} | مدة الجلسة {fmt_duration(avg_duration)}</div>
        </div>
    </div>"""

def eff_breakdown_section():
    if not overall:
        return ""
    counts = overall["eff_counts"]
    total  = overall["total"]
    labels = ["ممتاز", "جيد جداً", "جيد", "ضعيف", "غير مؤهل"]
    labels += [l for l in counts.index if l and l not in labels]
    cards = ""
    for lbl in labels:
        cnt = int(counts.get(lbl, 0))
        pct = round(cnt / total * 100, 1) if total else 0
        clr = eff_color(lbl)
        cards += f"""<div class="eff-card">
            <div class="eff-label">{esc(lbl)}</div>
            <div class="eff-value" style="color:{clr}">{cnt}</div>
            <div style="font-size:12px;color:#8a959b">{pct}%</div>
            <div class="eff-bar"><div class="eff-fill" style="width:{pct}%;background:{clr}"></div></div>
        </div>"""
    return f'<div class="eff-grid">{cards}</div>'

def items_coverage_section():
    if not overall:
        return ""
    total = overall["total"]
    cnt   = Counter(it for s in submissions for t in s["trainees"] for it in set(t["items"]))
    rows_ = []
    for it in all_items:
        c   = cnt.get(it, 0)
        pct = round(c / total * 100) if total else 0
        clr = "#28a745" if pct >= 90 else ("#fd7e14" if pct >= 70 else "#dc3545")
        rows_.append(f"""<div class="cov-row"><div>{esc(it)}{'' if it in CANONICAL_ITEMS else ' <span style="color:#b2bec3">(جديد)</span>'}</div>
            <div style="text-align:center;font-weight:700;color:{clr}">{c}/{total}</div>
            <div class="cov-bar"><div class="cov-fill" style="width:{pct}%;background:{clr}"></div></div></div>""")
    half = (len(rows_) + 1) // 2
    return (f'<div class="two-col"><div class="table-wrap">{"".join(rows_[:half])}</div>'
            f'<div class="table-wrap">{"".join(rows_[half:])}</div></div>')

def branch_ranking_table():
    if branch_df.empty:
        return "<p class='empty'>لا توجد بيانات</p>"
    rows_ = ""
    for i, (_, r) in enumerate(branch_df.iterrows()):
        rows_ += f"""<tr>
            <td>{rank_badge_html(i)}</td>
            <td class="branch-name">{esc(r['branch'])}</td>
            <td>{esc(r['dates'])}</td>
            <td>{esc(r['trainers'])}</td>
            <td>{r['sessions']}</td>
            <td>{r['total']}</td>
            <td>{bar_cell(r['avg_pct'], pct_color(r['avg_pct']))}</td>
            <td>{bar_cell(r['pct_passed'], '#28a745')}</td>
            <td>{bar_cell(r['pct_retrain'], '#fd7e14')}</td>
            <td>{r['avg_items']}/{N_ITEMS}</td>
        </tr>"""
    return f"""<div class="table-wrap"><table>
        <thead><tr><th>#</th><th>الفرع</th><th>التاريخ</th><th>المدرب</th><th>الجلسات</th>
        <th>المتدربين</th><th>متوسط الكفاءة %</th><th>اجتاز %</th><th>إعادة تدريب %</th><th>متوسط البنود</th></tr></thead>
        <tbody>{rows_}</tbody></table></div>"""

def trainer_table():
    if trainer_df.empty:
        return ""
    rows_ = ""
    for _, r in trainer_df.iterrows():
        rows_ += f"""<tr>
            <td class="branch-name">{esc(r['trainer_name'])}</td>
            <td>{r['sessions']}</td>
            <td>{r['branches']}</td>
            <td>{r['total']}</td>
            <td>{bar_cell(r['avg_pct'], pct_color(r['avg_pct']))}</td>
            <td>{bar_cell(r['pct_passed'], '#28a745')}</td>
            <td>{fmt_duration(r['avg_duration'])}</td>
            <td style="text-align:right;font-size:12px">{esc(r['branches_list'])}</td>
        </tr>"""
    return f"""<div class="table-wrap"><table>
        <thead><tr><th>المدرب</th><th>الجلسات</th><th>الفروع</th><th>المتدربين</th>
        <th>متوسط الكفاءة %</th><th>اجتاز %</th><th>متوسط مدة الجلسة</th><th>الفروع المزارة</th></tr></thead>
        <tbody>{rows_}</tbody></table></div>"""

def session_detail_sections():
    out = ""
    for sub in sorted(submissions, key=lambda x: (x["branch"], x["date"], x["start_time"])):
        trainees = sub["trainees"]
        if not trainees:
            continue
        n_passed = sum(1 for t in trainees if t["passed"] == "اجتاز")
        n_fail   = sum(1 for t in trainees if t["passed"] == "لم يجتاز")
        pcts     = [t["eff_pct"] for t in trainees if t["eff_pct"] is not None]
        avg_pct  = round(sum(pcts) / len(pcts), 1) if pcts else 0
        end_lbl  = sub["end_time"] + ("" if sub["end_date"] == sub["date"] else " (+1)")

        emp_rows = ""
        for tr in trainees:
            inh = '<span class="inherited">من المتدرب الأول</span>' if tr["section_inherited"] else ""
            note_html = f'<span class="trainee-note">{esc(tr["note"])}</span>' if tr["note"] else DASH
            emp_rows += f"""<tr>
                <td>{avatar(tr)}</td>
                <td class="emp-name">{esc(tr['name'])}<div style="font-size:11px;color:#8a959b;font-weight:400">{esc(tr['emp_id'])}</div></td>
                <td>{status_badge(tr['status'])}</td>
                <td>{esc(tr['section']) or DASH}{inh}</td>
                <td>{items_cell(tr['items'])}</td>
                <td>{eff_badge(tr['efficiency'], tr['eff_pct'])}</td>
                <td>{response_badge(tr['response'])}</td>
                <td>{result_cell(tr['passed'], tr['needs_retrain'], tr['schedule'])}</td>
                <td>{note_html}</td>
            </tr>"""

        notes = sub["notes"]
        notes_row = (f'<tr class="notes-row"><td colspan="9">ملاحظات عامة: {esc(notes)}</td></tr>'
                     if notes and notes.lower() not in ("none", ".") else "")
        flags_row = (f'<tr class="flags-row"><td colspan="9">تنبيهات البيانات: {esc(" | ".join(sub["flags"]))}</td></tr>'
                     if sub["flags"] else "")

        out += f"""<div class="branch-section">
            <div class="branch-header">
                <h3>{esc(sub['branch'])}</h3>
                <span class="trainer-badge">المدرب: {esc(sub['trainer_name'])} ({esc(sub['trainer_id'])})</span>
                <div class="meta">
                    {sub['date']} &nbsp;|&nbsp; {sub['start_time']} - {end_lbl} ({fmt_duration(sub['duration_min'])}) &nbsp;|&nbsp;
                    {len(trainees)} متدرب &nbsp;|&nbsp; متوسط الكفاءة: <strong>{avg_pct}%</strong> &nbsp;|&nbsp;
                    <span style="color:#a8e6a2">اجتاز: {n_passed}</span> &nbsp;
                    <span style="color:#f5a5a5">لم يجتاز: {n_fail}</span> &nbsp;|&nbsp;
                    صور داعمة: {sub['support_photo_count']}
                </div>
            </div>
            <div class="emp-table-wrap">
                <table class="emp-table">
                    <thead><tr>
                        <th>الصورة</th><th>المتدرب</th><th>حالة الموظف</th><th>القسم</th>
                        <th>بنود التدريب</th><th>الكفاءة</th><th>الاستجابة</th><th>النتيجة</th><th>ملاحظة المدرب</th>
                    </tr></thead>
                    <tbody>{emp_rows}{notes_row}{flags_row}</tbody>
                </table>
            </div>
        </div>"""
    return out

def build_html():
    n_branches = df["branch"].nunique() if not df.empty else 0
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<title>{REPORT_TITLE} — {DATE_LABEL}</title>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head><body><div class="container">
    <div class="header">
        <h1>{REPORT_TITLE}</h1>
        <div class="subtitle">{DATE_LABEL} &nbsp;|&nbsp; {len(submissions)} جلسة &nbsp;|&nbsp; {n_branches} فرع &nbsp;|&nbsp; {len(df)} متدرب</div>
    </div>
    <div class="section-title">مؤشرات الأداء العامة</div>
    {overall_kpi_section()}
    <div class="section-title">توزيع مستويات الكفاءة</div>
    {eff_breakdown_section()}
    <div class="section-title">تغطية بنود التدريب</div>
    {items_coverage_section()}
    <div class="section-title">تصنيف الفروع حسب متوسط الكفاءة</div>
    {branch_ranking_table()}
    <div class="section-title">أداء المدربين</div>
    {trainer_table()}
    <div class="section-title">تفاصيل الجلسات والمتدربين</div>
    {session_detail_sections()}
</div></body></html>"""

# ==============================================================================
# 6. PDF VIA PLAYWRIGHT (works in Actions and inside Colab's running loop)
# ==============================================================================
async def build_pdf(html_content: str) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page(viewport={"width": 1380, "height": 1000})
        await page.set_content(html_content, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        height = await page.evaluate("() => document.body.scrollHeight")
        pdf_bytes = await page.pdf(
            width="1380px", height=f"{height + 100}px",
            print_background=True, scale=1.0,
            margin={"top": "20px", "bottom": "20px", "left": "20px", "right": "20px"},
        )
        await browser.close()
    return pdf_bytes

def run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()

html_content = build_html()
html_path = os.path.join(OUTPUT_DIR, f"frontline_training_{END_DATE}.html")
with open(html_path, "w", encoding="utf-8") as f:
    f.write(html_content)
print(f"  HTML saved: {html_path}")

pdf_bytes, pdf_filename = None, f"frontline_training_{END_DATE}.pdf"
if submissions:
    print("Generating PDF...")
    pdf_bytes = run_async(build_pdf(html_content))
    with open(os.path.join(OUTPUT_DIR, pdf_filename), "wb") as f:
        f.write(pdf_bytes)
    print(f"  PDF size: {len(pdf_bytes):,} bytes -> {os.path.join(OUTPUT_DIR, pdf_filename)}")

# ==============================================================================
# 7. EMAIL (no emoji)
# ==============================================================================
subject = f"{REPORT_TITLE} — {DATE_LABEL}"

if overall:
    o = overall
    summary_lines = [
        f"إجمالي المتدربين: {o['total']} متدرب في {o['branches']} فرع ({o['sessions']} جلسة)",
        f"متوسط الكفاءة: {o['avg_pct']}%",
        f"اجتازوا القسم: {o['pct_passed']}% ({o['passed']} متدرب)",
        f"لم يجتازوا: {o['failed']} متدرب | غير محدد: {o['pending']}",
        f"يحتاجون إعادة تدريب: {o['pct_retrain']}% ({o['retrain']} متدرب)",
        f"اكتمال بنود التدريب ({N_ITEMS} بند): {o['pct_full_items']}%",
    ]
    summary_html = "<br>".join(esc(l) for l in summary_lines)
    attach_line  = "التقرير التفصيلي مرفق بصيغة PDF."
else:
    summary_html = "لا توجد جلسات تدريب مسجلة لهذه الفترة."
    attach_line  = ""

body_html = f"""
<div dir="rtl" style="font-family:Cairo,Arial,sans-serif;font-size:14px;color:#2d3436;line-height:1.8">
  <p>السلام عليكم،</p>
  <p>مرفق تقرير {REPORT_TITLE} للفترة <strong>{DATE_LABEL}</strong>.</p>
  <br>
  <p style="color:#0a3d62;font-weight:700">الملخص:</p>
  <p>{summary_html}</p>
  <br>
  <p style="color:#636e72;font-size:12px">{attach_line}</p>
  <br>
  <p>Business Intelligence<br>AOF Group</p>
</div>"""

if not SEND_EMAIL:
    print("SEND_EMAIL is off — email not sent (files saved above).")
else:
    msg = MIMEMultipart("mixed")
    msg["From"]    = f"Business Intelligence <{GMAIL_USER}>"
    msg["To"]      = TO_EMAIL
    msg["Cc"]      = ", ".join(CC_EMAIL)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    if pdf_bytes:
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{pdf_filename}"')
        msg.attach(part)
    print("Sending email...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, [TO_EMAIL] + CC_EMAIL, msg.as_string())
    print(f"Email sent -> {TO_EMAIL}, CC: {CC_EMAIL}")
