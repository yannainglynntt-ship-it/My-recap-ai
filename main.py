import os
import uuid
import tempfile
import time
import subprocess
import json
import traceback  # အသစ် ထပ်တိုးထားသော Library (Error အတိအကျသိရန်)
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

jobs = {}

def get_media_duration(file_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(result.stdout.strip())
    except:
        return 0.0

def upload_to_drive(file_path, filename):
    info_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not info_str: return None
    
    info = json.loads(info_str)
    creds = service_account.Credentials.from_service_account_info(info)
    service = build('drive', 'v3', credentials=creds)
    
    file_metadata = {'name': f"Recap_{filename}", 'parents': [os.getenv("GOOGLE_DRIVE_FOLDER_ID")]}
    media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id, webContentLink').execute()
    return file.get('webContentLink')

def process_video_task(job_id: str, file_path: str, filename: str, voice_name: str):
    output_video_path = ""
    tts_audio_path = ""
    
    try:
        # Step 1: Transcribing
        jobs[job_id]["status"] = "transcribing"
        video_file = genai.upload_file(path=file_path)
        
        while video_file.state.name == "PROCESSING":
            time.sleep(2)
            video_file = genai.get_file(video_file.name)
            
        if video_file.state.name == "FAILED":
            raise Exception("Gemini API failed to process the uploaded video file.")
            
        prompt = "You are a professional translator. Listen to this video and provide an accurate Burmese transcript of the speech. Only return the translated Burmese text without any other comments."
        transcript = ""
        
        try:
            model = genai.GenerativeModel("gemini-3-flash-preview")
            response = model.generate_content([prompt, video_file])
            transcript = response.text
        except Exception as e_inner:
            print(f"3 Flash Failed: {e_inner}. Falling back to 2.5 Flash...")
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content([prompt, video_file])
            transcript = response.text
            
        jobs[job_id]["transcript"] = transcript
        genai.delete_file(video_file.name)
        
        # Step 2: Generating Audio
        jobs[job_id]["status"] = "generating_audio"
        tts_model = genai.GenerativeModel("gemini-3.1-flash-tts-preview")
        tts_prompt = f"Voice Profile: {voice_name}\nRead the following text naturally and fluently:\n{transcript}"
        tts_response = tts_model.generate_content(tts_prompt)
        
        audio_data = None
        if tts_response.candidates:
            for part in tts_response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data.data:
                    audio_data = part.inline_data.data
                    break
                    
        if not audio_data:
            raise Exception("No audio data returned from TTS model. Response might be blocked or empty.")
            
        fd, tts_audio_path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, 'wb') as f:
            f.write(audio_data)
            
        # Step 3 & 4: Merging Video
        jobs[job_id]["status"] = "merging_video"
        video_dur = get_media_duration(file_path)
        audio_dur = get_media_duration(tts_audio_path)
        
        v_pts_factor = 1.0
        a_tempo_factor = 1.0
        
        if audio_dur > video_dur and video_dur > 0:
            required_a_speed = audio_dur / video_dur
            if required_a_speed <= 1.1:
                a_tempo_factor = required_a_speed
            else:
                a_tempo_factor = 1.1
                new_audio_dur = audio_dur / 1.1
                v_pts_factor = new_audio_dur / video_dur
                
        fd2, output_video_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd2)
        
        cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            "-i", tts_audio_path,
            "-filter_complex", f"[0:v]setpts={v_pts_factor}*PTS[v];[1:a]atempo={a_tempo_factor}[a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-c:a", "aac",
            output_video_path
        ]
        
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if process.returncode != 0:
            raise Exception(f"FFmpeg Error: {process.stderr}")
        
        # Step 5: Uploading Final to Drive
        jobs[job_id]["status"] = "uploading_final_to_drive"
        drive_link = upload_to_drive(output_video_path, filename)
        jobs[job_id]["drive_link"] = drive_link
        
        jobs[job_id]["status"] = "completed"
        
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        # 🚨 Error အတိအကျကို UI ဆီ ပို့ပေးမည့် အပိုင်း 🚨
        error_name = type(e).__name__
        error_msg = str(e) if str(e).strip() else repr(e)
        jobs[job_id]["error"] = f"[{error_name}] {error_msg}"
        
        print(f"--- SERVER ERROR LOG (Job: {job_id}) ---")
        print(traceback.format_exc())
        print("----------------------------------------")
    finally:
        for path in [file_path, tts_audio_path, output_video_path]:
            if path and os.path.exists(path): os.remove(path)

@app.post("/api/upload")
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), voice: str = Form("Fenrir (Male)")):
    job_id = str(uuid.uuid4())
    
    fd, temp_file_path = tempfile.mkstemp(suffix=".mp4")
    with os.fdopen(fd, 'wb') as f:
        f.write(await file.read())
        
    jobs[job_id] = {"id": job_id, "status": "pending", "filename": file.filename}
    
    background_tasks.add_task(process_video_task, job_id, temp_file_path, file.filename, voice)
    
    return {"success": True, "jobId": job_id}

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    return jobs.get(job_id, {"error": "Job not found"})
