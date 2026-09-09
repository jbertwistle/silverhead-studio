import os, json, tempfile, subprocess, base64, mimetypes
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

OPENAI = "https://api.openai.com/v1"

def http_json(url, payload, headers):
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={**headers, "Content-Type":"application/json"}, method="POST")
    with urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

def fetch_bytes(url):
    req = Request(url, headers={"User-Agent":"Silverhead-Studio/0.4"})
    with urlopen(req, timeout=120) as r:
        return r.read(), r.headers.get("content-type","video/webm")

def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def run(cmd):
    if cmd and cmd[0] == "ffmpeg":
        cmd[0] = ffmpeg_exe()
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8","ignore")[-1800:] or "FFmpeg failed")
    return p

def ffprobe_duration(path):
    import imageio_ffmpeg
    try:
        _frames, seconds = imageio_ffmpeg.count_frames_and_secs(path)
        return max(0.1, float(seconds))
    except Exception:
        return 1.0

def extract_media(video_path, workdir, duration):
    # Audio: normalize to a compact MP3 accepted by transcription.
    audio_path = os.path.join(workdir, "audio.mp3")
    run(["ffmpeg","-y","-i",video_path,"-vn","-ac","1","-ar","16000","-b:a","48k",audio_path])

    # 3–8 evenly spaced frames; avoid exact first/last frames.
    count = max(3, min(8, int((duration + 2.99)//3)))
    start = min(0.15, max(0.02, duration*0.03))
    finish = max(start, duration - min(0.20, max(0.05, duration*0.03)))
    times = [start if count == 1 else start + ((finish-start)*i)/(count-1) for i in range(count)]
    frames=[]
    for i,t in enumerate(times):
        frame_path=os.path.join(workdir,f"frame-{i:02d}.jpg")
        try:
            run(["ffmpeg","-y","-ss",f"{t:.3f}","-i",video_path,"-frames:v","1","-vf","scale='min(640,iw)':-2", "-q:v","5",frame_path])
            if os.path.exists(frame_path) and os.path.getsize(frame_path)>0:
                with open(frame_path,"rb") as f:
                    frames.append({"time":round(t,2),"image":"data:image/jpeg;base64,"+base64.b64encode(f.read()).decode()})
        except Exception:
            pass
    if not frames:
        raise RuntimeError("SERVER COULD NOT EXTRACT VIDEO FRAMES FROM THIS RECORDING.")
    return audio_path, frames

def transcribe(audio_path, key):
    boundary="----SilverheadBoundary7MA4YWxk"
    with open(audio_path,"rb") as f:
        audio=f.read()
    parts=[]
    def field(name,value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    field("model","gpt-4o-transcribe")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode()+audio+b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req=Request(
        OPENAI+"/audio/transcriptions",
        data=b"".join(parts),
        headers={"Authorization":f"Bearer {key}","Content-Type":f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode()).get("text","")

def analyze(transcript, frames, session, key):
    legend="\n".join([f"Image {i+1}: approximately {f['time']:.2f} seconds." for i,f in enumerate(frames)])
    prompt=f"""You are analyzing embodied improvisation research footage.

Treat the raw recording as evidence. Do not infer emotions, diagnoses, intentions, identities, or off-screen events from appearance. Do not invent visual events from the transcript.

SESSION
Performers label: {session.get('performers','unspecified')}
Duration: {session.get('duration_seconds','unknown')} seconds
Performer notes: {session.get('notes') or 'none'}

TRANSCRIPT
{transcript or '[no intelligible speech transcribed]'}

VIDEO SAMPLE TIMESTAMPS
{legend}

Return ONLY valid JSON:
{{
  "text_patterns": ["3-8 concise observations about repeated words, phrases, images, questions, verbal structures, or conspicuous absences. Do not invent timestamps."],
  "visual_observations": [{{"time": 0.0, "observation": "Concrete visible description based only on the corresponding sampled frame."}}],
  "possible_connections": ["0-5 explicitly tentative connections between audio/text and visible material."]
}}

For visual observations, use only supplied frame timestamps. Describe concrete body position, orientation, spacing, entrance/exit from frame, repeated posture, visible gesture, or stillness when genuinely visible. If a frame supports no useful observation, omit it. Possible connections must be explicitly tentative."""
    content=[{"type":"input_text","text":prompt}]
    content += [{"type":"input_image","image_url":f["image"]} for f in frames]
    result=http_json(
        OPENAI+"/responses",
        {"model":"gpt-5.6-luna","input":[{"role":"user","content":content}]},
        {"Authorization":f"Bearer {key}"}
    )
    text="\n".join(
        c.get("text","")
        for item in result.get("output",[])
        for c in item.get("content",[])
        if c.get("type")=="output_text"
    ).strip()
    cleaned=text
    if cleaned.startswith("```"):
        cleaned=cleaned.split("\n",1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"): cleaned=cleaned.rstrip()[:-3]
    try:
        return json.loads(cleaned)
    except Exception:
        return {"text_patterns":[],"visual_observations":[],"possible_connections":["Analysis returned, but its structure could not be parsed."],"raw_model_output":text}

class handler(BaseHTTPRequestHandler):
    def send_json(self, status, obj):
        body=json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            key=os.environ.get("OPENAI_API_KEY")
            if not key:
                return self.send_json(500,{"error":"OPENAI_API_KEY is not configured in Vercel."})
            length=int(self.headers.get("Content-Length","0"))
            data=json.loads(self.rfile.read(length) or b"{}")
            recording_url=data.get("recordingUrl")
            session=data.get("session") or {}
            if not recording_url:
                return self.send_json(400,{"error":"Missing recording URL."})

            with tempfile.TemporaryDirectory() as d:
                raw, ctype=fetch_bytes(recording_url)
                ext=".mp4" if "mp4" in ctype else ".webm"
                video_path=os.path.join(d,"raw"+ext)
                with open(video_path,"wb") as f: f.write(raw)
                duration=ffprobe_duration(video_path)
                audio_path,frames=extract_media(video_path,d,duration)
                transcript=transcribe(audio_path,key)
                result=analyze(transcript,frames,session,key)

            return self.send_json(200,{
                "transcript":transcript,
                "text_patterns":result.get("text_patterns",[]),
                "visual_observations":result.get("visual_observations",[]),
                "possible_connections":result.get("possible_connections",[]),
                "raw_model_output":result.get("raw_model_output")
            })
        except HTTPError as e:
            try: detail=e.read().decode()
            except Exception: detail=str(e)
            return self.send_json(500,{"error":"UPSTREAM API ERROR: "+detail[-1200:]})
        except subprocess.TimeoutExpired:
            return self.send_json(500,{"error":"SERVER MEDIA PROCESSING TIMED OUT."})
        except Exception as e:
            return self.send_json(500,{"error":str(e)})
