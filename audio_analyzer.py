import librosa
import numpy as np
import json
import os
import traceback

def analyze_track(filepath):
    """
    Analyzes an audio file using librosa to extract BPM and beat grid.
    Returns a dictionary with the analysis results.
    """
    try:
        if not os.path.exists(filepath):
            return {"error": "File not found"}
            
        # Load audio (use mono and a lower sample rate for speed, 22050Hz is default)
        # We only load a maximum of 10 minutes to avoid memory explosions
        y, sr = librosa.load(filepath, sr=22050, duration=600)
        
        # 1. BPM & Beat Grid
        tempo_array, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo_array[0]) if isinstance(tempo_array, np.ndarray) else float(tempo_array)
        
        # Convert beat frames to times (seconds)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        
        # 2. RMS Energy
        # Calculate RMS energy for the track
        rms = librosa.feature.rms(y=y)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr)
        
        # Smooth the RMS curve
        smoothed_rms = np.convolve(rms, np.ones(50)/50, mode='same')
        
        # Sub-sample the energy curve to send to frontend (e.g., 1 point per second)
        # 1 frame is roughly 0.023s at 22050Hz hop_length=512
        frames_per_sec = int(1.0 / 0.02322)
        energy_curve = []
        for i in range(0, len(smoothed_rms), frames_per_sec):
            energy_curve.append(float(smoothed_rms[i]))
            
        # Basic Phrasing: Intro usually has lower energy, Drop has max energy
        # For a basic Outro detection: find the last drop in energy in the last 30% of the song
        duration = librosa.get_duration(y=y, sr=sr)
        
        mix_out = duration - 30.0 # Default fallback
        mix_in = 0.0
        
        if len(beat_times) > 32:
            # Look at the last quarter of the song for a significant energy drop aligned with a beat
            # To keep it simple in this first version, we just use basic beat alignments
            mix_in = float(beat_times[0]) # First downbeat
            # Find a beat roughly 32 beats from the end
            out_idx = max(0, len(beat_times) - 64)
            mix_out = float(beat_times[out_idx])

        structure = {
            "bpm": round(bpm, 2),
            "duration": round(duration, 2),
            "mix": {
                "mixIn": round(mix_in, 2),
                "mixOut": round(mix_out, 2),
                "recommendedFade": 12.0
            },
            "energyCurve": energy_curve
        }
        return structure
        
    except Exception as e:
        print(f"[ANALYSIS ERROR] {e}")
        traceback.print_exc()
        return {"error": str(e)}

if __name__ == '__main__':
    # Test script if run directly
    import sys
    if len(sys.argv) > 1:
        res = analyze_track(sys.argv[1])
        print(json.dumps(res, indent=2))
