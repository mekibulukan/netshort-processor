#!/usr/bin/env python3
"""
FFmpeg Worker - Download, Merge, Upload ke R2
Dipanggil via API oleh Directus
"""

from flask import Flask, request, jsonify
import os
import subprocess
import boto3
import requests
import threading
import time
import json
import shutil
from pathlib import Path
from datetime import datetime
from botocore.config import Config

app = Flask(__name__)

# ============ CONFIG ============
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET", "netshort-dramas")
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

# NETSORT API
NETSHORT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://netshort.dramabos.online/"
}
NETSHORT_API = "https://netshort.dramabos.online/api"

# R2 Client
s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)

# Jobs tracking
jobs = {}

# ============ FUNCTIONS ============

def download_episode(drama_id, episode, code, output_dir):
    """Download 1 episode dari netshort"""
    url = f"{NETSHORT_API}/watch/{drama_id}/{episode}?lang=in&code={code}"
    
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=NETSHORT_HEADERS, timeout=15)
            data = resp.json()
            
            if data.get("success"):
                video_url = data["data"].get("videoUrl")
                if video_url:
                    filepath = Path(output_dir) / f"ep_{episode:04d}.mp4"
                    
                    # Download
                    vresp = requests.get(video_url, stream=True, timeout=120)
                    total = int(vresp.headers.get('content-length', 0))
                    downloaded = 0
                    
                    with open(filepath, 'wb') as f:
                        for chunk in vresp.iter_content(8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                    
                    return {
                        "success": True,
                        "episode": episode,
                        "path": str(filepath),
                        "size": os.path.getsize(filepath)
                    }
        
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    
    return {"success": False, "episode": episode}

def merge_episodes(input_dir, output_filename, job_id):
    """Merge semua episode jadi 1 file"""
    episodes = sorted(Path(input_dir).glob("ep_*.mp4"))
    
    if not episodes:
        return None
    
    print(f"[{job_id}] Merging {len(episodes)} episodes...")
    
    # Bikin concat list
    concat_file = Path(input_dir) / "concat.txt"
    with open(concat_file, 'w') as f:
        for ep in episodes:
            f.write(f"file '{ep.name}'\n")
    
    output_path = Path(input_dir) / output_filename
    
    # Merge pake ffmpeg
    cmd = [
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        "-y",
        str(output_path)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if output_path.exists():
        return {
            "success": True,
            "path": str(output_path),
            "size": os.path.getsize(output_path)
        }
    
    return {"success": False, "error": result.stderr}

def upload_to_r2(local_path, remote_key, job_id):
    """Upload hasil merge ke R2"""
    print(f"[{job_id}] Uploading to R2: {remote_key}")
    
    try:
        s3.upload_file(
            local_path, R2_BUCKET, remote_key,
            ExtraArgs={'ContentType': 'video/mp4'}
        )
        
        public_url = f"{R2_PUBLIC_URL}/{remote_key}"
        print(f"[{job_id}] Uploaded: {public_url}")
        
        return {
            "success": True,
            "url": public_url
        }
    except Exception as e:
        print(f"[{job_id}] Upload failed: {e}")
        return {"success": False, "error": str(e)}

def send_webhook(job_id, data):
    """Kirim hasil ke webhook (Directus/CMS)"""
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        return
    
    try:
        requests.post(webhook_url, json=data, timeout=10)
    except:
        pass

# ============ API ENDPOINTS ============

@app.route('/')
def index():
    return jsonify({
        "service": "Netshort FFmpeg Worker",
        "status": "running",
        "ffmpeg_version": subprocess.getoutput("ffmpeg -version | head -1"),
        "r2_bucket": R2_BUCKET,
        "active_jobs": len([j for j in jobs.values() if j['status'] == 'processing'])
    })

@app.route('/api/process', methods=['POST'])
def process_drama():
    """Process drama: download + merge + upload"""
    data = request.json
    
    drama_id = data.get("drama_id")
    code = data.get("code")
    title = data.get("title", f"Drama_{drama_id}")
    start_ep = data.get("start_ep", 1)
    end_ep = data.get("end_ep")
    
    if not drama_id or not code:
        return jsonify({"error": "Missing drama_id or code"}), 400
    
    # Cek max episodes
    check_url = f"{NETSHORT_API}/watch/{drama_id}/1?lang=in&code={code}"
    try:
        check_resp = requests.get(check_url, headers=NETSHORT_HEADERS, timeout=10)
        check_data = check_resp.json()
        max_eps = check_data.get("data", {}).get("maxEps", 1)
    except:
        max_eps = 1
    
    if not end_ep:
        end_ep = max_eps
    
    # Sanitize title
    safe_title = title.replace('/', '_').replace(' ', '_').replace(':', '')[:80]
    
    # Bikin job
    job_id = f"{drama_id}_{int(time.time())}"
    output_dir = Path(f"/app/downloads/{job_id}")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    jobs[job_id] = {
        "id": job_id,
        "drama_id": drama_id,
        "title": safe_title,
        "status": "downloading",
        "progress": 0,
        "total": end_ep - start_ep + 1,
        "downloaded": 0,
        "merged": False,
        "r2_url": None,
        "started_at": datetime.now().isoformat()
    }
    
    # Process di background
    def worker():
        job = jobs[job_id]
        
        # 1. Download semua episode
        for ep in range(start_ep, end_ep + 1):
            result = download_episode(drama_id, ep, code, output_dir)
            if result['success']:
                job['downloaded'] += 1
            job['progress'] = ep - start_ep + 1
        
        # 2. Merge
        if job['downloaded'] > 0:
            job['status'] = 'merging'
            merge_result = merge_episodes(output_dir, f"{safe_title}_Full.mp4", job_id)
            
            if merge_result['success']:
                job['merged'] = True
                
                # 3. Upload ke R2
                job['status'] = 'uploading'
                r2_key = f"dramas/{drama_id}/{safe_title}_Full.mp4"
                upload_result = upload_to_r2(merge_result['path'], r2_key, job_id)
                
                if upload_result['success']:
                    job['r2_url'] = upload_result['url']
                    job['status'] = 'completed'
                    
                    # Kirim webhook ke Directus
                    send_webhook(job_id, {
                        "drama_id": drama_id,
                        "title": title,
                        "video_url": upload_result['url'],
                        "size": merge_result['size'],
                        "episodes": f"{start_ep}-{end_ep}",
                        "status": "completed"
                    })
                else:
                    job['status'] = 'upload_failed'
            else:
                job['status'] = 'merge_failed'
        else:
            job['status'] = 'download_failed'
        
        # Cleanup setelah 10 menit
        time.sleep(600)
        shutil.rmtree(output_dir, ignore_errors=True)
        if job_id in jobs:
            del jobs[job_id]
    
    threading.Thread(target=worker).start()
    
    return jsonify(jobs[job_id])

@app.route('/api/job/<job_id>')
def job_status(job_id):
    """Cek status job"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route('/api/jobs')
def list_jobs():
    """List semua jobs"""
    return jsonify(list(jobs.values()))

@app.route('/api/info/<drama_id>')
def drama_info(drama_id):
    """Get info drama dari netshort"""
    code = request.args.get('code', '')
    url = f"{NETSHORT_API}/watch/{drama_id}/1?lang=in&code={code}"
    
    try:
        resp = requests.get(url, headers=NETSHORT_HEADERS, timeout=10)
        data = resp.json()
        
        if data.get("success"):
            drama_data = data["data"]
            return jsonify({
                "success": True,
                "drama_id": drama_id,
                "max_episodes": drama_data.get("maxEps", 0),
                "current_episode": drama_data.get("current", 1),
                "has_subtitle": len(drama_data.get("subtitles", [])) > 0
            })
    except:
        pass
    
    return jsonify({"success": False, "error": "Failed to fetch"})

@app.route('/api/batch', methods=['POST'])
def batch_process():
    """Batch process multiple dramas"""
    data = request.json
    dramas = data.get("dramas", [])
    code = data.get("code", "")
    
    results = []
    
    for drama in dramas:
        drama_id = drama.get("drama_id")
        title = drama.get("title", "")
        
        # Trigger individual process
        resp = requests.post(
            f"http://localhost:5000/api/process",
            json={
                "drama_id": drama_id,
                "code": code,
                "title": title,
                "start_ep": drama.get("start_ep", 1),
                "end_ep": drama.get("end_ep")
            }
        )
        
        results.append(resp.json())
    
    return jsonify({"batch_started": True, "jobs": results})

# ============ HEALTH CHECK ============
@app.route('/health')
def health():
    return jsonify({"status": "ok", "ffmpeg": True})

# ============ MAIN ============
if __name__ == '__main__':
    print("=" * 55)
    print("🎬 NETSHORT FFMPEG WORKER")
    print("=" * 55)
    print(f"R2 Bucket: {R2_BUCKET}")
    print(f"FFmpeg: {subprocess.getoutput('ffmpeg -version | head -1')}")
    print("Ready!")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
