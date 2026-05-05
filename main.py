import os
import uuid
import tempfile
import time
import subprocess
import traceback
import requests
import json
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from gtts import gTTS

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs = {}

def get_media_duration(file_path):
    """FFprobe ကိုသုံး၍ ဗီဒီယို/အသံ ကြာချိန်ကို တိုင်းတာခြင်း"""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(result.stdout.strip())
    except:
        return 0.0

def upload_to_cloud(file_path, filename):
    """Google Drive အစား Free Cloud များသို့ Video တင်၍ Link ထုတ်ပေးခြင်း (Fallback ပါဝင်သည်)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    safe_filename = "recap_output.mp4"
    
    # ၁။ Catbox သို့ အရင်တင်ကြည့်မည်
    try:
        with open(file_path, 'rb') as f:
            response = requests.post(
                'https://catbox.moe/user/api.php',
                data={'reqtype': 'fileupload'},
                files={'fileToUpload': (safe_filename, f, 'video/mp4')},
                headers=headers,
                timeout=180
            )
        if response.status_code == 200 and "catbox.moe" in response.text:
            return response.text.strip()
    except Exception as e:
        print(f"Catbox Upload Failed: {e}")

    # ၂။ Catbox အဆင်မပြေပါက Tmpfiles သို့ ပြောင်းတင်မည်
    try:
        with open(file_path, 'rb') as f:
            response = requests.post(
                'https://tmpfiles.org/api/v1/upload',
                files={'file': (safe_filename, f, 'video/mp4')},
                headers=headers,
                timeout=180
            )
        if response.status_code == 200:
            data = response.json()
            url = data['data']['url']
            return url.replace('tmpfiles.org/', 'tmpfiles.org/dl/')
    except Exception as e:
        print(f"Tmpfiles Upload Failed: {e}")
        
    return None

def process_video_task(job_id: str, file_path: str, filename: str, voice_name: str, user_api_key: str):
    output_video_path = ""
    tts_audio_path = ""
    video_file = None
    
    try:
        # API Key သန့်စင်ခြင်း
        render_key = os.getenv("GEMINI_API_KEY", "")
        if render_key:
            render_key = render_key.strip().replace('"', '').replace("'", "")
            
        active_key = user_api_key.strip() if user_api_key else render_key
        if not active_key:
            raise Exception("Gemini API Key မရှိပါ။")
            
        genai.configure(api_key=active_key)

        # အဆင့် ၁ - Video ကို Gemini ပေါ်တင်ခြင်း
        jobs[job_id]["status"] = "transcribing"
        video_file = genai.upload_file(path=file_path, mime_type="video/mp4")
        
        while video_file.state.name == "PROCESSING":
            time.sleep(4)
            video_file = genai.get_file(video_file.name)
            
        if video_file.state.name == "FAILED":
            raise Exception("Gemini API က ဗီဒီယိုဖိုင်အား ဖတ်၍မရပါ။")
            
        # အဆင့် ၂ - မြန်မာစာသား ထုတ်ယူခြင်း (Gemini 3 Flash -> Fallback: 2.5 Flash)
        prompt = "You are a professional translator. Listen to this video and provide an accurate Burmese transcript of the speech. Only return the translated Burmese text without any other comments."
        transcript = ""
        
        try:
            model = genai.GenerativeModel("gemini-3-flash-preview")
            response = model.generate_content([prompt, video_file])
            transcript = response.text if response.text else ""
        except:
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content([prompt, video_file])
            transcript = response.text if response.text else ""
                
        if not transcript or not transcript.strip():
            raise Exception("ဗီဒီယိုထဲမှ စကားပြောသံကို ရှာမတွေ့ပါ။")
            
        jobs[job_id]["transcript"] = transcript
        clean_transcript = transcript.strip()
        
        # အဆင့် ၃ - မြန်မာ Voice Over ထုတ်ခြင်း (Gemini 3.1 TTS -> Fallback: gTTS)
        jobs[job_id]["status"] = "generating_audio"
        
        try:
            tts_model = genai.GenerativeModel("gemini-3.1-flash-tts-preview")
            tts_prompt = f"Voice character: {voice_name}. Read this text: {clean_transcript}"
            tts_response = tts_model.generate_content(tts_prompt)
            
            audio_data = None
            if tts_response.candidates:
                for part in tts_response.candidates[0].content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data.data:
                        audio_data = part.inline_data.data
                        break
            
            if not audio_data:
                raise Exception("Gemini TTS No Data")
                
            fd, tts_audio_path = tempfile.mkstemp(suffix=".mp3")
            with os.fdopen(fd, 'wb') as f:
                f.write(audio_data)
        except:
            print("Gemini TTS Failed, using gTTS fallback...")
            fd, tts_audio_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            tts = gTTS(text=clean_transcript, lang='my')
            tts.save(tts_audio_path)
            
        # အဆင့် ၄ - Video နှင့် Audio အား Fit ဖြစ်အောင် ပေါင်းစပ်ခြင်း
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
                v_pts_factor = (audio_dur / 1.1) / video_dur
                
        fd2, output_video_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd2)
        
        # FFmpeg အား ultrafast preset ဖြင့် CPU သက်သာစွာ ပေါင်းစပ်ခိုင်းခြင်း
        cmd = [
            "ffmpeg", "-y",
            "-i", file_path,
            "-i", tts_audio_path,
            "-filter_complex", f"[0:v]setpts={v_pts_factor}*PTS[v];[1:a]atempo={a_tempo_factor}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            output_video_path
        ]
        
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if process.returncode != 0:
            raise Exception(f"FFmpeg Merge Failed: {process.stderr}")
        
        # အဆင့် ၅ - Cloud သို့ တင်ခြင်း
        jobs[job_id]["status"] = "uploading_final_to_drive" 
        final_link = upload_to_cloud(output_video_path, filename)
        
        if not final_link:
            raise Exception("Cloud သို့ တင်ခြင်း မအောင်မြင်ပါ။")
            
        jobs[job_id]["drive_link"] = final_link
        jobs[job_id]["status"] = "completed"
        
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        print(traceback.format_exc())
    finally:
        if video_file:
            try: genai.delete_file(video_file.name)
            except: pass
        for path in [file_path, tts_audio_path, output_video_path]:
            if path and os.path.exists(path): 
                try: os.remove(path)
                except: pass

@app.post("/api/upload")
async def upload_video(
    background_tasks: BackgroundTasks, 
    file: UploadFile = File(...), 
    voice: str = Form("Fenrir (Male)"),
    user_api_key: str = Form(None)
):
    job_id = str(uuid.uuid4())
    fd, temp_file_path = tempfile.mkstemp(suffix=".mp4")
    with os.fdopen(fd, 'wb') as f:
        f.write(await file.read())
        
    jobs[job_id] = {"id": job_id, "status": "pending", "filename": file.filename}
    background_tasks.add_task(process_video_task, job_id, temp_file_path, file.filename, voice, user_api_key)
    return {"success": True, "jobId": job_id}

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    return jobs.get(job_id, {"error": "Job not found"})
