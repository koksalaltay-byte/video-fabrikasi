import warnings
warnings.filterwarnings("ignore")

import os, requests as http_requests, threading, re, asyncio, random, logging, traceback, uuid, hashlib
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pymongo import MongoClient
import certifi
import PIL.Image
import edge_tts

if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

import imageio_ffmpeg
from moviepy.editor import VideoFileClip, AudioFileClip
from moviepy.config import change_settings
change_settings({"FFMPEG_BINARY": imageio_ffmpeg.get_ffmpeg_exe()})

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- CONFIG ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
MONGO_URL = os.environ.get("MONGO_URL", "")
OUTPUT_DIR = "/tmp/videofabrikasi"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# MongoDB
try:
    mongo_client = MongoClient(MONGO_URL, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
    db = mongo_client["VideoFabrikasiDB"]
    users_col = db["Kullanicilar"]
    mongo_client.admin.command('ping')
    logging.info("MongoDB baglantisi basarili.")
except Exception as e:
    logging.error(f"MongoDB baglanti hatasi: {e}")
    users_col = None

jobs: Dict[str, dict] = {}
sessions: Dict[str, str] = {}  # token -> username

app = FastAPI(title="Video Fabrikasi API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --- HELPERS ---
def sifre_hashle(sifre: str) -> str:
    return hashlib.sha256(sifre.encode()).hexdigest()

def dosya_temizle(metin: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", metin).strip()

def youtube_bilgi_uret(baslik, metin, dil, api_key):
    try:
        modeller = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]
        if dil == "tr":
            prompt = (
                f"Asagidaki YouTube Shorts videosu icin Turkce SEO icerigi olustur:\n"
                f"Baslik: {baslik}\nIcerik ozeti: {metin[:200]}\n\n"
                f"1. ETIKETLER: Virgülle ayrilmis 20 adet Turkce etiket\n"
                f"2. ACIKLAMA: SEO'ya uygun, emoji iceren, 150 kelimelik aciklama. Sona hashtag ekle.\n\n"
                f"Format:\n[ETIKETLER]\netiket1, etiket2, ...\n[ACIKLAMA]\naciklama metni"
            )
        else:
            prompt = (
                f"Create English SEO content for this YouTube Shorts video:\n"
                f"Title: {baslik}\nContent summary: {metin[:200]}\n\n"
                f"1. TAGS: 20 English tags separated by commas\n"
                f"2. DESCRIPTION: SEO-friendly description with emojis, 150 words. Add hashtags at end.\n\n"
                f"Format:\n[TAGS]\ntag1, tag2, ...\n[DESCRIPTION]\ndescription text"
            )

        for model in modeller:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                res = http_requests.post(
                    url,
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=30
                ).json()
                if 'candidates' not in res:
                    continue
                raw = res['candidates'][0]['content']['parts'][0]['text']
                etiketler, aciklama = "", ""
                if dil == "tr":
                    if "[ETIKETLER]" in raw and "[ACIKLAMA]" in raw:
                        etiketler = raw.split("[ETIKETLER]")[1].split("[ACIKLAMA]")[0].strip()
                        aciklama = raw.split("[ACIKLAMA]")[1].strip()
                else:
                    if "[TAGS]" in raw and "[DESCRIPTION]" in raw:
                        etiketler = raw.split("[TAGS]")[1].split("[DESCRIPTION]")[0].strip()
                        aciklama = raw.split("[DESCRIPTION]")[1].strip()
                if etiketler and aciklama:
                    return etiketler, aciklama
            except Exception as e:
                logging.error(f"YouTube bilgi uretim hatasi ({model}): {e}")
                continue
    except Exception as e:
        logging.error(f"YouTube bilgi genel hata: {e}")

    if dil == "tr":
        return f"{baslik}, shorts, viral", f"📌 {baslik}\n\n#shorts #viral #kesfet"
    return f"{baslik}, shorts, viral", f"📌 {baslik}\n\n#shorts #viral #facts"


def txt_dosyasi_kaydet(kayit_yolu, dosya_adi, baslik, etiketler, aciklama, dil):
    try:
        txt_adi = dosya_temizle(dosya_adi) + ".txt"
        txt_yolu = os.path.join(kayit_yolu, txt_adi)
        if dil == "tr":
            icerik = (
                f"{'='*60}\n BASLIK\n{'='*60}\n{baslik}\n\n"
                f"{'='*60}\n ETIKETLER\n{'='*60}\n{etiketler}\n\n"
                f"{'='*60}\n ACIKLAMA\n{'='*60}\n{aciklama}\n"
            )
        else:
            icerik = (
                f"{'='*60}\n TITLE\n{'='*60}\n{baslik}\n\n"
                f"{'='*60}\n TAGS\n{'='*60}\n{etiketler}\n\n"
                f"{'='*60}\n DESCRIPTION\n{'='*60}\n{aciklama}\n"
            )
        with open(txt_yolu, "w", encoding="utf-8") as f:
            f.write(icerik)
        return txt_adi
    except Exception as e:
        logging.error(f"TXT kayit hatasi: {e}")
        return None


def yedi_farkli_derin_senaryo(ana_konu, dil="tr"):
    try:
        modeller = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]
        if dil == "tr":
            tts_ses = "tr-TR-AhmetNeural"
            prompt = (
                f"'{ana_konu}' hakkinda birbirinden TAMAMEN FARKLI 7 uzmanlik konusu belirle. "
                f"Her biri icin:\n1. Pexels arama terimi (Ingilizce)\n2. Viral YouTube Shorts basligi (Turkce)\n"
                f"3. TAM OLARAK 120 kelimelik Turkce anlati metni yaz. "
                f"ASLA giris cumlesi kullanma. Direkt bilgiyle basla.\n"
                f"Format: [ARAMA] | [BASLIK] | [METIN]. Her bolum arasina '###' koy."
            )
        else:
            tts_ses = "en-US-GuyNeural"
            prompt = (
                f"Determine 7 completely DIFFERENT expertise topics about '{ana_konu}'. "
                f"For each:\n1. Pexels search term (English)\n2. Viral YouTube Shorts title (English)\n"
                f"3. Write EXACTLY 140 words of English narration. NEVER use an intro sentence.\n"
                f"Format: [SEARCH] | [TITLE] | [TEXT]. Separate each section with '###'."
            )

        for model in modeller:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
                res = http_requests.post(
                    url,
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=60
                ).json()
                if 'candidates' not in res:
                    continue
                raw_text = res['candidates'][0]['content']['parts'][0]['text']
                maddeler = [m.strip() for m in raw_text.split('###') if "|" in m]
                sonuc = []
                for m in maddeler[:7]:
                    parts = m.split('|', 2)
                    if len(parts) == 3:
                        q, b, t = parts
                        kelimeler = t.strip().split()
                        maks = 125 if dil == "tr" else 145
                        if len(kelimeler) > maks:
                            t = " ".join(kelimeler[:maks])
                        sonuc.append({"q": q.strip(), "b": b.strip(), "m": t.strip(), "ses": tts_ses})
                if sonuc:
                    return sonuc
            except Exception as e:
                logging.error(f"{model} hatasi: {e}")
                continue
        raise Exception("Hicbir model calismadi!")
    except Exception as e:
        logging.error(f"Senaryo uretim hatasi: {e}")
        ses = "tr-TR-AhmetNeural" if dil == "tr" else "en-US-GuyNeural"
        return [{"q": ana_konu, "b": f"{ana_konu} #{i+1}", "m": f"{ana_konu} hakkinda bilgi.", "ses": ses} for i in range(7)]


async def seslendir_async(metin, yol, ses_adi):
    await edge_tts.Communicate(metin, ses_adi).save(yol)

def seslendir(metin, yol, ses_adi):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(seslendir_async(metin, yol, ses_adi))
    finally:
        loop.close()


def uretim_dongusu_api(job_id: str, konu: str, dil: str):
    job = jobs[job_id]
    gecici_dosyalar = []
    try:
        job["progress"] = "🧠 AI 7 Senaryo Hazırlıyor..."
        gorevler = yedi_farkli_derin_senaryo(konu, dil)
        headers = {"Authorization": PEXELS_API_KEY}

        for i, veri in enumerate(gorevler):
            job["progress"] = f"🎬 {i+1}/7 Video Hazırlanıyor..."
            try:
                res = http_requests.get(
                    f"https://api.pexels.com/videos/search?query={veri['q']}&per_page=10&orientation=portrait",
                    headers=headers, timeout=15
                ).json()

                if "videos" not in res or not res["videos"]:
                    job["completed"] = i + 1
                    continue

                secilen_video = random.choice(res["videos"])
                video_files = sorted(secilen_video["video_files"], key=lambda x: x.get("width", 9999))
                v_url = video_files[0]["link"]

                temp_v = os.path.join(OUTPUT_DIR, f"{job_id}_v_{i}.mp4")
                temp_s = os.path.join(OUTPUT_DIR, f"{job_id}_s_{i}.mp3")
                gecici_dosyalar.extend([temp_v, temp_s])

                v_res = http_requests.get(v_url, timeout=60)
                if v_res.status_code != 200 or len(v_res.content) < 1000:
                    job["completed"] = i + 1
                    continue

                with open(temp_v, "wb") as f:
                    f.write(v_res.content)

                seslendir(veri['m'], temp_s, veri['ses'])

                if not os.path.exists(temp_s) or os.path.getsize(temp_s) == 0:
                    job["completed"] = i + 1
                    continue

                audio = AudioFileClip(temp_s)
                if audio.duration > 58:
                    audio = audio.subclip(0, 58)

                clip = VideoFileClip(temp_v).loop(duration=audio.duration + 0.5)
                hedef_w = int(clip.h * 9 / 16)
                clip_dik = clip.crop(x_center=clip.w / 2, width=hedef_w).resize((720, 1920))
                final = clip_dik.set_audio(audio)

                f_adi = dosya_temizle(veri['b'])[:50]
                cikis_adi = f"{job_id}_{i+1}_{f_adi}.mp4"
                cikis_yolu = os.path.join(OUTPUT_DIR, cikis_adi)

                final.write_videofile(
                    cikis_yolu, codec="libx264", audio_codec="aac",
                    fps=30, preset="ultrafast", threads=4, logger=None
                )
                clip.close(); audio.close(); final.close()

                job["progress"] = f"🏷️ {i+1}/7 Etiketler Üretiliyor..."
                etiketler, aciklama = youtube_bilgi_uret(veri['b'], veri['m'], dil, GEMINI_API_KEY)

                txt_dosya_adi = f"{job_id}_{i+1}_{f_adi}"
                txt_gercek_adi = txt_dosyasi_kaydet(OUTPUT_DIR, txt_dosya_adi, veri['b'], etiketler, aciklama, dil)

                job["files"].append({
                    "video": cikis_adi,
                    "txt": txt_gercek_adi or "",
                    "baslik": veri['b']
                })
                job["completed"] = i + 1

            except Exception as e:
                logging.error(f"Video {i+1} hatasi: {e}\n{traceback.format_exc()}")
                job["completed"] = i + 1
                continue

        job["status"] = "completed"
        job["progress"] = "✅ Tamamlandı!"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["progress"] = f"❌ Hata: {str(e)}"
    finally:
        for dosya in gecici_dosyalar:
            try:
                if os.path.exists(dosya):
                    os.remove(dosya)
            except:
                pass


# --- API ROUTES ---
class LoginRequest(BaseModel):
    username: str
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str

class GenerateRequest(BaseModel):
    token: str
    konu: str
    dil: str = "tr"


@app.post("/login")
def login(req: LoginRequest):
    if users_col is None:
        raise HTTPException(status_code=503, detail="Veritabanı bağlantısı yok")
    h_sifre = sifre_hashle(req.password)
    kullanici = users_col.find_one({"username": req.username, "password": h_sifre})
    if not kullanici:
        eski = users_col.find_one({"username": req.username, "password": req.password})
        if eski:
            users_col.update_one({"_id": eski["_id"]}, {"$set": {"password": h_sifre}})
            kullanici = eski
    if not kullanici:
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre yanlış!")
    token = str(uuid.uuid4())
    sessions[token] = req.username
    return {"token": token, "username": req.username}


@app.post("/register")
def register(req: RegisterRequest):
    if users_col is None:
        raise HTTPException(status_code=503, detail="Veritabanı bağlantısı yok")
    if users_col.find_one({"username": req.username}):
        raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten alınmış!")
    users_col.insert_one({"username": req.username, "password": sifre_hashle(req.password)})
    return {"message": "Kayıt tamamlandı!"}


@app.post("/generate")
def generate(req: GenerateRequest):
    if req.token not in sessions:
        raise HTTPException(status_code=401, detail="Geçersiz token, lütfen tekrar giriş yapın")
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "progress": "Başlatılıyor...",
        "completed": 0,
        "total": 7,
        "files": [],
        "error": None
    }
    threading.Thread(
        target=uretim_dongusu_api,
        args=(job_id, req.konu, req.dil),
        daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job bulunamadı")
    return jobs[job_id]


@app.get("/download/{filename}")
def download(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Geçersiz dosya adı")
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı (sunucu yeniden başlatıldıysa silinmiş olabilir)")
    return FileResponse(
        filepath,
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
    )


# Static files — MUST be last
app.mount("/", StaticFiles(directory="static", html=True), name="static")
