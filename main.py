import os
import uuid
import asyncio
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

app = FastAPI( )

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration (Render/Railway Environment Variables မှ ဖတ်မည်)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_INFO = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") # JSON string တစ်ခုလုံး ထည့်ရန်
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

genai.configure(api_key=GEMINI_API_KEY)

# Database အစား ရိုးရိုး Dictionary သုံးထားသည် (Railway/Render က Restart ဖြစ်လျှင် ပျောက်မည်)
# တကယ်သုံးလျှင် SQLite သို့မဟုတ် PostgreSQL ချိတ်ရန် အကြံပြုပါသည်
jobs = {}

def get_drive_service():
    import json
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_INFO)
    creds = service_account.Credentials.from_service_account_info(info)
    return build('drive', 'v3', credentials=creds)

async def process_video(job_id: str, file_content: bytes, filename: str):
    try:
        jobs[job_id]["status"] = "uploading_to_drive"
        service = get_drive_service()
        
        # 1. Google Drive သို့ တင်ခြင်း
        file_metadata = {'name': filename, 'parents': [DRIVE_FOLDER_ID]}
        media = MediaIoBaseUpload(io.BytesIO(file_content), mimetype='video/mp4', resumable=True)
        drive_file = service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink').execute()
        video_url = drive_file.get('webContentLink')

        # 2. Gemini ဖြင့် Transcription လုပ်ခြင်း
        jobs[job_id]["status"] = "transcribing"
        model = genai.GenerativeModel("gemini-1.5-flash") # Gemini-3 မရသေးမီ flash သုံးထားသည်
        # မှတ်ချက် - Gemini API သို့ Video တိုက်ရိုက်ပို့ရန် လိုအပ်နိုင်သည်
        response = model.generate_content([
            "Translate this video to Burmese transcript accurately.",
            {"mime_type": "video/mp4", "data": file_content}
        ])
        
        jobs[job_id]["transcript"] = response.text
        jobs[job_id]["status"] = "completed"
        
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)

@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    content = await file.read()
    
    jobs[job_id] = {"id": job_id, "status": "pending", "filename": file.filename}
    
    # Background မှာ အလုပ်လုပ်ခိုင်းခြင်း
    background_tasks.add_task(process_video, job_id, content, file.filename)
    
    return {"success": True, "jobId": job_id}

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    return jobs.get(job_id, {"error": "Job not found"})

@app.get("/")
def home():
    return {"message": "Auto Recap AI Python API is running"}
