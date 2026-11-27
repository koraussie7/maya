#!/usr/bin/env python3
"""mac 로컬 whisper STT 프록시 서버 (port 18108).
- POST /transcribe (multipart file OR json {audio_b64})
- GET /health
- faster-whisper base/small 모델 사용
"""
import os, sys, base64, tempfile, json, time
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")

app = FastAPI(title="Whisper STT Proxy")

_model = None
def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        print(f"  loading faster-whisper {MODEL_SIZE} on {DEVICE} ({COMPUTE})...", flush=True)
        _model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE)
    return _model

@app.get("/health")
async def health():
    ok = True
    try:
        _get_model()
    except Exception as e:
        ok = False
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE}

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(None), audio_b64: str = Form(None), language: str = Form("ko")):
    t0 = time.monotonic()
    try:
        m = _get_model()
        if file is not None:
            data = await file.read()
        elif audio_b64:
            data = base64.b64decode(audio_b64)
        else:
            raise HTTPException(400, "file or audio_b64 required")
        # save temp wav
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            segs, info = m.transcribe(tmp, language=language, beam_size=5)
            text = " ".join(s.text.strip() for s in segs).strip()
            dt = time.monotonic() - t0
            return {"text": text, "language": info.language,
                    "duration": info.duration, "time": round(dt, 3)}
        finally:
            try: os.unlink(tmp)
            except: pass
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"transcribe failed: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "18108"))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")
