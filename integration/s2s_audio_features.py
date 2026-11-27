#!/usr/bin/env python3
"""integration/s2s_audio_features.py

마야 음성대화 시뮬레이션 결과(WAV / 시뮬레이션 산출 오디오)의 음향 특징을
추출해 보고서(JSON) 로 내보낸다.

추출 항목
  - 재생 시간, 샘플레이트, RMS 에너지
  - F0 (기본 주파수): 중앙값/평균/최소/최대, 발성 비율(voiced ratio)
  - 스펙트럼 중심(centroid), 롤오프(rolloff), 대역폭(bandwidth)
  - 영교차율(zero crossing rate)
  - MFCC 13 계수 평균, 스펙트럼 대비(contrast) 평균
  - 피크 주파수

비교 모드(--reference)
  기준 클립(참조 음성)과 합성 출력 간의 F0/스펙트럼 차이, MFCC 코사인 유사도,
  스펙트럼 상관도를 산출해 "목소리 유지(클로닝 품질)" 여부를 정량화한다.
  (기존 split_speakers.py 와 동일하게 librosa.pyin 기반 F0, 16kHz mono 전제)

왜 별도 스크립트인가
  maya_voice_simulation_runner.py 는 지연(latency)·라우트 적중만 측정한다.
  도입 효과(Qwen3-TTS vs VoxCPM vs piper 의 음색 일치)를 판정하려면
  출력 오디오의 F0·스펙트럼을 참조 클립과 비교해야 하므로 이 스크립트를 쓴다.

실행 (기준 클립 단일 분석)
  .venv-voicemem/bin/python integration/s2s_audio_features.py \
      --wav maya/models/tts/reference/clip_ref_speaker_a.wav \
      --out /tmp/ref_a_features.json

실행 (참조 vs 합성 출력 비교)
  .venv-voicemem/bin/python integration/s2s_audio_features.py \
      --wav /tmp/s2s_output.wav \
      --reference maya/models/tts/reference/clip_ref_speaker_a.wav \
      --out /tmp/compare_s2s_vs_ref_a.json

디렉터리 일괄 분석 (시뮬레이션 산출 WAV 모음)
  .venv-voicemem/bin/python integration/s2s_audio_features.py \
      --wav /tmp/sim_outputs/ --out /tmp/sim_features_report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# 특징 추출
# ─────────────────────────────────────────────────────────────────────

def _safe_median(vals: np.ndarray) -> float:
    v = vals[np.isfinite(vals)]
    return float(np.median(v)) if len(v) else 0.0


def _safe_mean(vals: np.ndarray) -> float:
    v = vals[np.isfinite(vals)]
    return float(np.mean(v)) if len(v) else 0.0


def extract_features(y: np.ndarray, sr: int) -> dict:
    """오디오 신호 y 의 음향 특징 딕셔너리를 반환한다."""
    duration = float(len(y) / sr)
    rms = float(np.sqrt(np.mean(y ** 2))) if len(y) else 0.0

    # ── F0 (기본 주파수) ──
    f0: Optional[np.ndarray] = None
    try:
        f0, voiced_flag, _voiced_prob = librosa.pyin(
            y, fmin=F0_MIN, fmax=F0_MAX, sr=sr, frame_length=2048
        )
        f0 = np.asarray(f0, dtype=np.float32)
        voiced_flag = np.asarray(voiced_flag, dtype=bool)
    except Exception as exc:  # noqa: BLE001
        log.warning("pyin F0 추출 실패 (0 으로 대체): %s", exc)
        f0 = np.zeros(1, dtype=np.float32)
        voiced_flag = np.zeros(1, dtype=bool)

    f0_valid = f0[(f0 > 0) & np.isfinite(f0)] if f0 is not None else np.array([])
    voiced_ratio = float(voiced_flag.mean()) if len(voiced_flag) else 0.0

    # ── 스펙트럼 특징 ──
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr)

    centroid = librosa.feature.spectral_centroid(S=S, sr=sr)[0]
    rolloff = librosa.feature.spectral_rolloff(S=S, sr=sr, roll_percent=0.85)[0]
    bandwidth = librosa.feature.spectral_bandwidth(S=S, sr=sr)[0]
    zcr = librosa.feature.zero_crossing_rate(y)[0]

    # MFCC 13 (델타/델타-델타는 제외, 단순 평균만)
    mfcc = librosa.feature.mfcc(
        y=y, sr=sr, n_mfcc=13, n_fft=2048, hop_length=512
    )
    mfcc_mean = mfcc.mean(axis=1)

    # 스펙트럼 대비 (7 대역) — 16kHz 에서 나퀴스트 초과 오류 방지
    try:
        contrast = librosa.feature.spectral_contrast(S=S, sr=sr, n_bands=7)
        contrast_mean = contrast.mean(axis=1)
    except Exception as exc:  # noqa: BLE001
        log.warning("spectral_contrast 실패 (0 으로 대체): %s", exc)
        contrast_mean = np.zeros(7, dtype=np.float32)

    # 피크 주파수 (에너지 최대 빈도)
    if S.size:
        frame_energy = S.sum(axis=0)
        peak_frame = int(np.argmax(frame_energy))
        peak_freq = float(
            freqs[int(np.argmax(S[:, peak_frame]))] if S.shape[0] else 0.0
        )
    else:
        peak_freq = 0.0

    return {
        "duration_seconds": round(duration, 4),
        "sample_rate": int(sr),
        "rms": round(rms, 6),
        "f0": {
            "median_hz": round(_safe_median(f0_valid), 2),
            "mean_hz": round(_safe_mean(f0_valid), 2),
            "min_hz": round(float(f0_valid.min()), 2) if len(f0_valid) else 0.0,
            "max_hz": round(float(f0_valid.max()), 2) if len(f0_valid) else 0.0,
            "voiced_ratio": round(voiced_ratio, 4),
        },
        "spectral_centroid": {
            "median_hz": round(_safe_median(centroid), 2),
            "mean_hz": round(_safe_mean(centroid), 2),
        },
        "spectral_rolloff": {
            "median_hz": round(_safe_median(rolloff), 2),
            "mean_hz": round(_safe_mean(rolloff), 2),
        },
        "spectral_bandwidth": {
            "median_hz": round(_safe_median(bandwidth), 2),
            "mean_hz": round(_safe_mean(bandwidth), 2),
        },
        "zero_crossing_rate": round(float(np.mean(zcr)), 6),
        "peak_freq_hz": round(peak_freq, 2),
        "mfcc_mean": [round(float(v), 4) for v in mfcc_mean],
        "spectral_contrast_mean": [round(float(v), 4) for v in contrast_mean],
    }


import librosa
import numpy as np
import soundfile as sf

log = logging.getLogger("s2s_audio_features")
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)

TARGET_SR = 16000  # 마야 파이프라인 표준 (Int16 mono 16kHz)
F0_MIN = 70.0
F0_MAX = 400.0


# ─────────────────────────────────────────────────────────────────────
# 오디오 로드
# ─────────────────────────────────────────────────────────────────────

def load_wav(path: str, sr: int = TARGET_SR) -> tuple[np.ndarray, int]:
    """WAV 파일을 float32 mono numpy 배열로 읽는다.

    다중 채널은 평균으로 mono 화한다. 샘플레이트가 다르면 리샘플링한다.
    """
    data, file_sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if file_sr != sr:
        data = librosa.resample(data, orig_sr=file_sr, target_sr=sr)
        file_sr = sr
    peak = float(np.max(np.abs(data))) or 1.0
    if peak > 1.0:
        data = data / peak
    return data, file_sr


# ─────────────────────────────────────────────────────────────────────
# 비교 (참조 vs 합성)
# ─────────────────────────────────────────────────────────────────────

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


# ─────────────────────────────────────────────────────────────────────
# emotion2vec 워커 통신 (IPC: 4B 길이 + JSON stdin, 1B 상태+4B 길이+JSON stdout)
# ─────────────────────────────────────────────────────────────────────

def _ipc_read_exact(stream, n: int) -> bytes:
    """스트림에서 정확히 n 바이트를 읽는다."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def extract_emotion_embedding_via_worker(
    wav_path: str, worker_path: str, timeout: float = 120.0
) -> Optional[list[float]]:
    """emotion2vec_worker 서브프로세스에 WAV 경로를 보내 임베딩을 받아온다.

    worker_path: emotion2vec_worker.py 의 절대/상대 경로
    반환: 768 차원 임베딩 리스트 또는 None (실패 시)
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, worker_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("emotion2vec 워커 시작 실패: %r", exc)
        return None

    try:
        # 요청 전송: 4B 길이 + JSON {"wav_path": "..."}
        req = json.dumps({"wav_path": wav_path}, ensure_ascii=False).encode("utf-8")
        assert proc.stdin is not None
        proc.stdin.write(struct.pack(">I", len(req)) + req)
        proc.stdin.flush()

        # 응답 읽기: 1B 상태 + 4B 길이 + JSON 페이로드
        assert proc.stdout is not None
        status_raw = _ipc_read_exact(proc.stdout, 1)
        if not status_raw or len(status_raw) < 1:
            log.warning("emotion2vec 워커 응답 상태 바이트 누락")
            return None
        status = status_raw[0]

        len_raw = _ipc_read_exact(proc.stdout, 4)
        if not len_raw or len(len_raw) < 4:
            log.warning("emotion2vec 워커 응답 길이 바이트 누락")
            return None
        payload_len = struct.unpack(">I", len_raw)[0]

        payload_raw = _ipc_read_exact(proc.stdout, payload_len)
        if not payload_raw or len(payload_raw) < payload_len:
            log.warning("emotion2vec 워커 응답 페이로드 누락")
            return None

        payload = json.loads(payload_raw.decode("utf-8"))

        proc.stdin.close()
        proc.wait(timeout=5)

        if status != 0:
            log.warning(
                "emotion2vec 워커 오류(%s): %s", wav_path, payload.get("error")
            )
            return None

        embedding = payload.get("embedding")
        if not embedding:
            log.warning("emotion2vec 워커 응답에 embedding 이 없습니다(%s)", wav_path)
            return None

        return [float(v) for v in embedding]

    except Exception as exc:  # noqa: BLE001
        log.warning("emotion2vec 워커 통신 실패(%s): %r", wav_path, exc)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return None


def compare_features(ref: dict, syn: dict) -> dict:
    """두 특징 딕셔너리를 비교해 차이/유사도를 반환한다.

    ref/syn 에 "emotion_embedding" 키가 있으면 cosine 유사도를 함께 산출한다.
    """
    f0_ref = ref["f0"]["median_hz"]
    f0_syn = syn["f0"]["median_hz"]
    f0_delta = f0_syn - f0_ref if f0_ref and f0_syn else 0.0

    cent_ref = ref["spectral_centroid"]["median_hz"]
    cent_syn = syn["spectral_centroid"]["median_hz"]
    cent_delta = cent_syn - cent_ref if cent_ref and cent_syn else 0.0

    mfcc_sim = _cosine_similarity(
        np.array(ref["mfcc_mean"]), np.array(syn["mfcc_mean"])
    )

    spec_corr = _cosine_similarity(
        np.array(
            [
                ref["spectral_centroid"]["median_hz"],
                ref["spectral_rolloff"]["median_hz"],
                ref["spectral_bandwidth"]["median_hz"],
            ]
        ),
        np.array(
            [
                syn["spectral_centroid"]["median_hz"],
                syn["spectral_rolloff"]["median_hz"],
                syn["spectral_bandwidth"]["median_hz"],
            ]
        ),
    )

    result = {
        "f0_delta_hz": round(f0_delta, 2),
        "f0_delta_percent": round(
            (f0_delta / f0_ref * 100) if f0_ref else 0.0, 2
        ),
        "spectral_centroid_delta_hz": round(cent_delta, 2),
        "mfcc_cosine_similarity": round(mfcc_sim, 4),
        "spectral_cosine_similarity": round(spec_corr, 4),
    }

    # ── 감정 임베딩 비교 (선택) ──
    ref_emb = ref.get("emotion_embedding")
    syn_emb = syn.get("emotion_embedding")
    if ref_emb and syn_emb:
        emotion_sim = _cosine_similarity(
            np.array(ref_emb), np.array(syn_emb)
        )
        result["emotion_cosine_similarity"] = round(emotion_sim, 4)
        result["emotion_embedding_dim"] = len(ref_emb)

    # ── 종합 판정 ──
    notes = []
    if f0_ref and f0_syn:
        notes.append(f"F0Δ={f0_delta:+.1f}Hz")
    notes.append(f"MFCC sim={mfcc_sim:.3f}")
    if "emotion_cosine_similarity" in result:
        notes.append(f"emotion sim={result['emotion_cosine_similarity']:.3f}")

    # 음색 유지 판정 임계값 (경험적, 조정 가능)
    voice_match = (
        abs(f0_delta) <= 30.0
        and abs(cent_delta) <= 500.0
        and mfcc_sim >= 0.85
    )
    # emotion2vec 임베딩 유사도가 있으면 판정에 반영 (임계값 0.8)
    if "emotion_cosine_similarity" in result:
        voice_match = voice_match and result["emotion_cosine_similarity"] >= 0.8
        notes.append(f"emotion sim={result['emotion_cosine_similarity']:.3f}")

    result["voice_match"] = bool(voice_match)
    result["note"] = (
        "음색 유지(클로닝 품질 양호)"
        if voice_match
        else "음색 변화 감지(참조 클립과 차이 큼)"
    )
    result["note"] = " | ".join(notes)
    return result



# ─────────────────────────────────────────────────────────────────────
# 파일/디렉터리 처리
# ─────────────────────────────────────────────────────────────────────

def _iter_wav(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in (".wav",) else []
    if path.is_dir():
        return sorted(p for p in path.rglob("*.wav") if p.is_file())
    return []


def analyze_one(
    wav_path: Path,
    reference_features: Optional[dict] = None,
    emotion2vec_worker: Optional[str] = None,
) -> dict:
    """단일 WAV 파일을 분석해 결과 딕셔너리를 반환한다.

    emotion2vec_worker 가 지정되면 해당 워커 서브프로세스를 통해
    감정 임베딩을 추출해 features 에 "emotion_embedding" 키로 추가한다.
    """
    try:
        y, sr = load_wav(str(wav_path), TARGET_SR)
        feats = extract_features(y, sr)

        # emotion2vec 임베딩 추출 (선택)
        if emotion2vec_worker:
            emb = extract_emotion_embedding_via_worker(
                str(wav_path), emotion2vec_worker
            )
            if emb is not None:
                feats["emotion_embedding"] = emb
                log.debug(
                    "[%s] emotion2vec 임베딩 추출 완료 (dim=%d)",
                    wav_path.name,
                    len(emb),
                )
            else:
                log.warning(
                    "[%s] emotion2vec 임베딩 추출 실패", wav_path.name
                )

        result: dict = {
            "file": str(wav_path),
            "status": "ok",
            "features": feats,
        }
        if reference_features is not None:
            result["comparison"] = compare_features(reference_features, feats)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("%s 분석 실패: %s", wav_path, exc)
        return {"file": str(wav_path), "status": "error", "error": str(exc)}


def main() -> int:
    global TARGET_SR
    ap = argparse.ArgumentParser(
        description="마야 시뮬레이션 결과 오디오 특징(F0·스펙트럼) 추출 및 비교"
    )
    ap.add_argument(
        "--wav", required=True,
        help="분석할 WAV 파일 경로 또는 디렉터리(재귀 탐색)",
    )
    ap.add_argument(
        "--reference", default=None,
        help="비교용 기준 클립 WAV (참조 음성). 지정하면 비교 모드",
    )
    ap.add_argument(
        "--out", default=None,
        help="JSON 결과 출력 경로(미지정 시 stdout)",
    )
    ap.add_argument(
        "--sr", type=int, default=TARGET_SR,
        help=f"대상 샘플레이트(기본 {TARGET_SR})",
    )
    ap.add_argument(
        "--emotion2vec-worker", default=None,
        help="emotion2vec_worker.py 경로(지정 시 감정 임베딩 비교 활성화)",
    )
    args = ap.parse_args()

    TARGET_SR = args.sr

    wav_path = Path(args.wav)
    wavs = _iter_wav(wav_path)
    if not wavs:
        print(f"WAV 파일을 찾을 수 없습니다: {wav_path}", file=sys.stderr)
        return 1

    # 기준 클립 특징 (비교 모드)
    reference_features: Optional[dict] = None
    if args.reference:
        try:
            ref_y, ref_sr = load_wav(args.reference, TARGET_SR)
            reference_features = extract_features(ref_y, ref_sr)
            log.info(
                "기준 클립 로드: %s (F0 median=%.1fHz, centroid=%.1fHz)",
                args.reference,
                reference_features["f0"]["median_hz"],
                reference_features["spectral_centroid"]["median_hz"],
            )
            # 기준 클립 감정 임베딩 추출
            if args.emotion2vec_worker:
                ref_emb = extract_emotion_embedding_via_worker(
                    args.reference, args.emotion2vec_worker
                )
                if ref_emb is not None:
                    reference_features["emotion_embedding"] = ref_emb
                    log.info(
                        "기준 클립 emotion2vec 임베딩 추출 완료 (dim=%d)",
                        len(ref_emb),
                    )
                else:
                    log.warning("기준 클립 emotion2vec 임베딩 추출 실패")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "기준 클립 로드 실패(비교 모드 비활성화): %s", exc
            )

    results = []
    for w in wavs:
        res = analyze_one(w, reference_features, args.emotion2vec_worker)
        results.append(res)
        if res["status"] == "ok":
            f = res["features"]
            log.info(
                "[%s] dur=%.2fs rms=%.4f F0~%.0fHz(voiced=%.0f%%) cent=%.0fHz",
                w.name,
                f["duration_seconds"],
                f["rms"],
                f["f0"]["median_hz"],
                f["f0"]["voiced_ratio"] * 100,
                f["spectral_centroid"]["median_hz"],
            )
            if "comparison" in res:
                c = res["comparison"]
                log.info(
                    "   ← 비교: F0Δ=%.1fHz(%.1f%%) centΔ=%.0fHz "
                    "MFCC sim=%.3f spec sim=%.3f → %s",
                    c["f0_delta_hz"],
                    c["f0_delta_percent"],
                    c["spectral_centroid_delta_hz"],
                    c["mfcc_cosine_similarity"],
                    c["spectral_cosine_similarity"],
                    c["note"],
                )
                if "emotion_cosine_similarity" in c:
                    log.info(
                    "      emotion sim=%.3f (dim=%d) voice_match=%s",
                    c["emotion_cosine_similarity"],
                    c.get("emotion_embedding_dim", "?"),
                    c["voice_match"],
                )
        else:
            log.error("[%s] 실패: %s", w.name, res.get("error"))

    report = {
        "reference": args.reference,
        "target_sr": TARGET_SR,
        "files_analyzed": len(results),
        "results": results,
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[emitted] {args.out} — {len(results)} files", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

