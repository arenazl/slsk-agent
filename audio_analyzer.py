import librosa
import numpy as np
import json
import os
import traceback


def _normalize_bpm(bpm):
    """Fold half/double-time detections into club range [85, 175)."""
    if bpm <= 0:
        return bpm
    while bpm < 85:
        bpm *= 2
    while bpm >= 175:
        bpm /= 2
    return bpm


def _fit_rigid_grid(beat_times):
    """Grilla rígida estilo rekordbox: BPM exacto + ancla del primer golpe.

    Los golpes que devuelve beat_track flotan (~±15 ms de jitter de detección),
    y el tempo global viene cuantizado a la grilla discreta de librosa (p.ej.
    todo lo que está entre ~122 y ~124 reporta 123.047). Acá se saca el BPM
    fino promediando spans largos de 128 golpes (el ruido de frame se promedia
    a ~0.05 BPM de precisión), se snapea a múltiplos de 0.5 BPM si está muy
    cerca (producción en DAW), y se ancla el offset de la grilla por mediana.
    Devuelve (bpm, beat_len, anchor, resid_ms): anchor es la fase del golpe 1
    en [0, beat_len) y resid_ms cuánto flotan los golpes reales vs la grilla
    (<30 ms = tema clavado; >80 ms = no confiar en la grilla).
    """
    n = len(beat_times)
    mid = beat_times[int(n * 0.15):int(n * 0.85)]
    m = len(mid)
    lag = min(128, m // 2)
    if lag < 16:
        return None
    spans = (mid[lag:] - mid[:-lag]) / lag
    beat_len = float(np.median(spans))
    bpm = _normalize_bpm(60.0 / beat_len)
    cand = round(bpm * 2) / 2
    if cand > 0 and abs(bpm - cand) / cand < 0.0008:
        bpm = cand
    beat_len = 60.0 / bpm
    idx = np.round((mid - mid[0]) / beat_len)
    resid = mid - (mid[0] + idx * beat_len)
    off = float(np.median(resid))
    anchor = float((mid[0] + off) % beat_len)
    resid_ms = float(np.std((resid - off) * 1000))
    return bpm, beat_len, anchor, resid_ms


def _snap_to_grid(t, anchor, beat_len):
    """Tiempo del golpe de grilla más cercano a t."""
    return anchor + round((t - anchor) / beat_len) * beat_len


def _low_env(y, sr, hop=32):
    """Envolvente de graves (<150 Hz) suavizada, con tiempos. Base común de
    la medición de anclas y del ancla por primer kick."""
    from scipy import signal as sp_signal
    b, a = sp_signal.butter(4, 150 / (sr / 2), btype='low')
    low = sp_signal.filtfilt(b, a, y)
    env = np.abs(low)
    nfr = len(env) // hop
    if nfr < 20:
        return None, None
    envf = env[:nfr * hop].reshape(nfr, hop).mean(axis=1)
    win = 5
    envs = np.convolve(envf, np.ones(win) / win, mode='same')
    t = (np.arange(nfr) + 0.5) * hop / sr
    return envs, t


def _first_kick_anchor(y, sr, beat_len):
    """Algoritmo "del dibujo" (spec del dueño, DJ): para intros que arrancan
    con el beat, el 0.0.0 es el PICO del primer transitorio grave prominente
    — no el inicio de la subida (la falda), que fue la fuente del bias de
    -40ms medido. Devuelve (t_kick, n_kicks_intro) o (None, n) si la intro
    no arranca con beat (queda fuera de la ecuación, decisión del dueño).
    """
    from scipy import signal as sp_signal
    seg = y[:min(len(y), int(20 * sr))]
    envs, t = _low_env(seg, sr)
    if envs is None or envs.max() <= 0:
        return None, 0
    peaks, _ = sp_signal.find_peaks(envs, height=envs.max() * 0.45, distance=int(0.25 * sr / 32))
    n_kicks = int(len(peaks))
    # "arranca con el beat": pulso grave sostenido desde el inicio (~16s a
    # 4 golpes/compás son >=25 picos; pedimos 16 para tolerar breaks cortos)
    if n_kicks < 16:
        return None, n_kicks
    k = int(peaks[0])
    frac = 0.0
    if 0 < k < len(envs) - 1:
        p, c, n = envs[k - 1], envs[k], envs[k + 1]
        den = p - 2 * c + n
        frac = 0.5 * (p - n) / den if den != 0 else 0.0
    return float(t[k] + frac * 32 / sr), n_kicks


def _measure_anchor_error(y, sr, beat_len, anchor, spans):
    """Mediana del corrimiento de los ataques reales de graves vs la grilla.

    Verificación objetiva del ancla (2026-08-02): el beat tracker puede anclar
    en el contratiempo (golpe cruzado, medio beat) o traer bias fijo. Acá se
    miden los ataques de graves (<150 Hz) en las regiones que usa la mezcla y
    se devuelve la mediana del corrimiento contra la grilla — ese valor
    corrige el ancla y los markers. None si no hay señal suficiente.
    """
    from scipy import signal as sp_signal
    devs = []
    for t0, dur in spans:
        i0 = max(0, int(t0 * sr))
        i1 = min(len(y), int((t0 + dur) * sr))
        if i1 - i0 < sr:
            continue
        # Convención de PICO (el centro de la montañita, spec del dueño), no
        # de falda: la derivada picaba en la subida y metía -30/-60ms de bias.
        envs, tt = _low_env(y[i0:i1], sr)
        if envs is None or envs.max() <= 0:
            continue
        peaks, _ = sp_signal.find_peaks(envs, height=envs.max() * 0.35, distance=int(0.25 * sr / 32))
        if len(peaks) < 4:
            continue
        pt = tt[peaks] + i0 / sr
        dv = ((pt - anchor + beat_len / 2) % beat_len) - beat_len / 2
        devs.extend(dv.tolist())
    if len(devs) < 8:
        return None
    return float(np.median(np.array(devs)))


def _refine_kick_anchor(y, sr, beat_len, anchor, t_start, t_end):
    """Afina el ancla de grilla contra el ATAQUE real del bombo.

    El ancla estadística (mediana de beats de librosa) arrastra un bias por
    tema: el pico de energía no es el ataque perceptual, y cambia según el
    sonido del kick. Acá se filtran los graves (<150 Hz), se toma la derivada
    positiva de la envolvente (el ataque), se dobla todo módulo beat_len y el
    pico del histograma (interpolado parabólico, ~5 ms de resolución) es la
    fase real del bombo. Devuelve el ancla corregida en [0, beat_len).
    """
    from scipy import signal as sp_signal
    b, a = sp_signal.butter(4, 150 / (sr / 2), btype='low')
    low = sp_signal.filtfilt(b, a, y)
    env = np.abs(low)
    hop = 128
    nfr = len(env) // hop
    if nfr < 100:
        return anchor
    env_f = env[:nfr * hop].reshape(nfr, hop).mean(axis=1)
    d = np.diff(env_f, prepend=env_f[:1])
    d[d < 0] = 0
    t = (np.arange(nfr) * hop + hop / 2) / sr
    m = (t >= t_start) & (t <= t_end)
    if not np.any(m):
        return anchor
    phases = (t[m] - anchor) % beat_len
    nb = 96
    hist, _ = np.histogram(phases, bins=nb, range=(0, beat_len), weights=d[m])
    if hist.max() <= 0:
        return anchor
    k = int(np.argmax(hist))
    prev, nxt = hist[(k - 1) % nb], hist[(k + 1) % nb]
    denom = prev - 2 * hist[k] + nxt
    frac = 0.5 * (prev - nxt) / denom if denom != 0 else 0.0
    delta = (k + 0.5 + frac) * (beat_len / nb)
    return float((anchor + delta) % beat_len)


def analyze_track(filepath):
    """
    Analyzes an audio file using librosa: BPM, beat grid anchor, energy curve
    and mix markers (mixIn / mixOut / recommendedFade) for automatic transitions.
    """
    try:
        if not os.path.exists(filepath):
            return {"error": "File not found"}

        # Real duration comes from the header; the decoded audio is capped to
        # 15 min so a long DJ mix can't blow up memory.
        real_duration = float(librosa.get_duration(path=filepath))
        y, sr = librosa.load(filepath, sr=22050, mono=True, duration=900)

        # 1. BPM & beat grid — beat_track detecta los golpes, la grilla rígida
        # (estilo rekordbox) saca el BPM fino y el ancla; los beats crudos
        # quedan solo para leer energía en cada golpe.
        tempo_array, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo_array[0]) if isinstance(tempo_array, np.ndarray) else float(tempo_array)
        bpm = _normalize_bpm(bpm)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)

        grid_fit = _fit_rigid_grid(beat_times) if len(beat_times) > 64 else None
        if grid_fit:
            bpm, _, grid_anchor, grid_resid_ms = grid_fit
        else:
            grid_anchor = float(beat_times[0]) if len(beat_times) else 0.0
            grid_resid_ms = None

        kick_anchor = grid_anchor
        if grid_fit and len(beat_times) > 64:
            n0 = len(beat_times)
            kick_anchor = _refine_kick_anchor(
                y, sr, 60.0 / bpm, grid_anchor,
                float(beat_times[int(n0 * 0.15)]), float(beat_times[int(n0 * 0.85)]))

        # 2. Smoothed RMS energy (~1.2 s window at hop 512 / 22050 Hz)
        rms = librosa.feature.rms(y=y)[0]
        win = 50
        smoothed = np.convolve(rms, np.ones(win) / win, mode='same')
        frame_times = librosa.frames_to_time(np.arange(len(smoothed)), sr=sr)
        analyzed_end = float(frame_times[-1]) if len(frame_times) else real_duration

        # 1 point per second for the UI energy curve
        frames_per_sec = max(1, int(sr / 512))
        energy_curve = [float(smoothed[i]) for i in range(0, len(smoothed), frames_per_sec)]

        # Waveform compacta para la UI (240 puntos 0-99, pow 0.7): cualquier
        # player la dibuja al instante sin decodificar el audio (la pizarra
        # del dueño para señalar patrones).
        wave_compact = []
        if len(smoothed) > 1:
            idxs = np.linspace(0, len(smoothed) - 1, 240)
            wv = np.interp(idxs, np.arange(len(smoothed)), smoothed)
            mmax = float(wv.max()) or 1.0
            wave_compact = [int(v) for v in (np.power(wv / mmax, 0.7) * 99)]

        beat_len = 60.0 / bpm if bpm > 0 else 0.5
        bar_len = 4 * beat_len

        # 0.0.0 por PRIMER KICK (algoritmo "del dibujo", spec del dueño DJ):
        # en EDM el ~90% de los temas arranca con el bombo — el PICO del primer
        # transitorio grave prominente ES el marker, determinístico. Si la
        # intro no arranca con beat (ambient), queda la grilla estadística.
        anchor_source = "stats"
        kick000 = None
        if grid_fit:
            kick000, _n_intro = _first_kick_anchor(y, sr, beat_len)
            if kick000 is not None:
                grid_anchor = float(kick000 % beat_len)
                anchor_source = "first-kick"

        # 3. Mix markers — SIEMPRE snapeados a la grilla rígida, no a los beats
        # crudos. mixIn: el 0.0.0 del primer kick si existe; si no, primer
        # golpe de grilla desde el primer beat detectado.
        raw_first = float(beat_times[0]) if len(beat_times) else 0.0
        if kick000 is not None:
            mix_in = float(kick000)
        else:
            mix_in = _snap_to_grid(raw_first, grid_anchor, beat_len)
            if mix_in < 0:
                mix_in += beat_len
        mix_out = max(0.0, analyzed_end - 30.0)  # fallback for short/odd tracks

        mix_candidates = []
        intro_lead_bars = 0
        sections = []
        # mixOut sobre la GRILLA COMPLETA, no sobre los beats detectados: si
        # el tracker colapsa a mitad de tema, su "último 35%" cae al MEDIO —
        # el dueño lo vio en la waveform (marker al 40%, sin cambio de energía
        # siquiera). La grilla es matemática infinita: se escanea la energía
        # de TODA la duración real.
        grid_beats = np.arange(grid_anchor, max(grid_anchor + 1.0, analyzed_end - 0.5), beat_len)
        if len(grid_beats) > 64:
            # Spec del dueño ("ahí hay un cambio de dB y frecuencias medible"):
            # el outro es el BOMBO yéndose — se mide la BANDA GRAVE (<150 Hz),
            # no el RMS total: en masters comprimidos la pared tapa el cambio
            # (el detector viejo caía en plena pared, visto en la waveform).
            env_low, t_low = _low_env(y, sr)
            if env_low is not None and env_low.max() > 0:
                beat_low = np.interp(grid_beats, t_low, env_low)
            else:
                beat_low = np.interp(grid_beats, frame_times, smoothed)
            ref = float(np.percentile(beat_low, 75))
            n = len(grid_beats)
            # REGLA DJ (caso Alive/Solomun marcado por el dueño): el corte va
            # donde TERMINA EL ÚLTIMO PLATEAU de bombo fuerte (fin del último
            # drop) — vuelva o no el bajo después. La regla anterior ("colapso
            # que no vuelve nunca") vetaba todos los momentos válidos y
            # terminaba en el fade (98% del tema).
            # PRINCIPIO DEL DUEÑO ("el compás manda, el drop te da la pauta"):
            # candidatos de corte = RE-ARRANQUES tras un pozo sostenido. El
            # pozo (drop/breakdown, >=8 beats bajo el 60% de referencia) marca
            # la frontera de sección; el corte va en el PRIMER compás de la
            # sección que nace (sea la pared que vuelve o un synth solo). Se
            # elige el re-arranque del ÚLTIMO pozo de la mitad final del tema.
            beat_full = np.interp(grid_beats, frame_times, smoothed)
            ref_full = float(np.percentile(beat_full, 75))
            # Banda alta (>2 kHz) para los "cambios de frecuencia" del dueño
            from scipy import signal as _sp
            _bh, _ah = _sp.butter(4, 2000 / (sr / 2), btype='high')
            _envh = np.abs(_sp.filtfilt(_bh, _ah, y))
            _hop = 512
            _nfr = len(_envh) // _hop
            _envh = _envh[:_nfr * _hop].reshape(_nfr, _hop).mean(axis=1)
            _th = (np.arange(_nfr) + 0.5) * _hop / sr
            beat_high = np.interp(grid_beats, _th, _envh)
            ref_high = float(np.percentile(beat_high, 75)) or 1e-9
            out_idx = None
            # SPEC FINAL DEL DUEÑO ("es el CONJUNTO de datos: posición en la
            # sección + qué pasa después"): los candidatos SOLO nacen en
            # FRONTERAS DE FRASE (múltiplos de 16 compases desde el 0.0.0) —
            # un evento de energía fuera de la grilla de frases es un fill del
            # arreglo, no una sección (caso DISCOTEKA beat 479, mod64=31).
            # Cada frontera se puntúa: "venimos de 16 compases suaves y de
            # repente se sube toda la frecuencia = momento mezclable".
            # MAPA DE SEGMENTOS ("empezá por dividir el tema en segmentos"):
            # bloques de frase (16 compases desde el 0.0.0) con su huella de
            # frecuencias [total, graves, altos]; sección nueva donde la
            # huella cambia fuerte. El corte sale del mapa + cambios de freq.
            dips = []
            sections = []
            i0 = int(round((mix_in - grid_anchor) / beat_len))
            PHRASE = 64  # 16 compases
            prev_vec = None
            for b in range(i0, n - 8, PHRASE):
                blk = slice(b, min(n, b + PHRASE))
                vec = (
                    float(np.median(beat_full[blk])) / ref_full,
                    float(np.median(beat_low[blk])) / (ref or 1e-9),
                    float(np.median(beat_high[blk])) / ref_high,
                )
                changed = prev_vec is None or any(abs(vec[j] - prev_vec[j]) > 0.35 for j in range(3))
                if changed:
                    sections.append((float(grid_beats[b]), round(vec[0], 2), round(vec[1], 2), round(vec[2], 2)))
                    # Frontera MEZCLABLE: veníamos suaves y sube la energía o
                    # se abre la frecuencia (regla del dueño)
                    if prev_vec is not None and b > i0 + PHRASE:
                        soft = prev_vec[0] <= 0.75
                        rise = vec[0] >= prev_vec[0] * 1.3 or vec[2] >= prev_vec[2] * 1.5
                        if soft and rise:
                            dips.append(b)
                prev_vec = vec
            # mixIn v2 ("lo detectás mal y lo mandás sin entender cuándo tiene
            # que entrar"): la ENTRADA es el primer TREN de bombos sostenido en
            # grilla (>=16 beats con ataque grave fuerte) — en intros melódicas
            # el primer pico engaña (es la melodía). Lo previo al tren se mide
            # como introLead (compases de melodía para el enganche melódico).
            run = 0
            kick_entry = None
            for i in range(min(n, 256)):
                if beat_low[i] > 0.5 * ref:
                    run += 1
                    if run >= 16:
                        kick_entry = i - 15
                        break
                else:
                    run = 0
            if kick_entry is not None:
                melody_j = kick_entry
                for j in range(kick_entry):
                    if beat_full[j] > 0.25 * ref_full:
                        melody_j = j
                        break
                intro_lead_bars = max(0, round((kick_entry - melody_j) / 4))
                mix_in = float(grid_beats[kick_entry])

            cand = [e for e in dips if grid_beats[e] > analyzed_end * 0.5 and e < n - 8]
            mix_candidates = [float(grid_beats[e]) for e in cand]
            if cand:
                out_idx = cand[-1]
            else:
                # Fallback: fin del último plateau de banda grave fuerte
                strong = beat_low > 0.8 * ref
                run_end = None
                run_len = 0
                for i in range(n):
                    if strong[i]:
                        run_len += 1
                        if run_len >= 16:
                            run_end = i
                    else:
                        run_len = 0
                if run_end is not None:
                    out_idx = min(n - 1, run_end + 1)
                    for i in range(run_end + 1, n - 4):
                        if np.all(beat_low[i:i + 4] < 0.5 * ref):
                            out_idx = i
                            break
            if out_idx is None:
                out_idx = max(0, n - 96)  # ~45s antes del final real, en grilla
            # Guard: pegado al final no sirve para mezclar
            if float(grid_beats[out_idx]) > analyzed_end - 25.0:
                out_idx = max(0, n - 96)
            mix_out = float(grid_beats[out_idx])

        # Auto-calibración del ancla contra el audio real: si los ataques de
        # graves en las zonas de mezcla están corridos vs la grilla (bias del
        # tracker o ancla en contratiempo = golpe cruzado), se corrige el
        # ancla Y los markers por la mediana medida.
        # AUTO-VALIDACIÓN universal del ancla ("se tiene que poder", 2026-08-02):
        # el primer kick puede ser un impostor (FX/swell 69ms corrido, caso
        # Feeling Good) — TODA ancla se verifica contra la fase del CUERPO del
        # tema y se corrige sola si desentona >25ms. Detección + verificación,
        # como los grandes.
        anchor_fix = 0.0
        if grid_fit:
            spans = ([(mix_in + 0.3, 30.0)] if kick000 is not None
                     else [(max(0.0, mix_out - 2.0), 16.0), (max(0.0, mix_in), 16.0)])
            err = _measure_anchor_error(y, sr, beat_len, grid_anchor, spans)
            if err is not None and abs(err) > 0.025:
                anchor_fix = err
                anchor_source += "+fix"
                grid_anchor = float((grid_anchor + err) % beat_len)
                mix_in += err
                if mix_in < 0:
                    mix_in += beat_len
                mix_out += err
                mix_candidates = [c + err for c in mix_candidates]

        # Fade expressed in musical time: 8 bars, capped to end >=3 s before the track dies
        fade = min(8 * bar_len, max(4.0, analyzed_end - mix_out - 3.0))

        return {
            "bpm": round(bpm, 3),
            "duration": round(real_duration, 2),
            "mix": {
                "mixIn": round(mix_in, 3),
                "mixOut": round(mix_out, 3),
                "recommendedFade": round(fade, 1),
                # Mapa de momentos de mezcla (spec del dueño: "todos los temas
                # tienen al menos 2 momentos") — re-arranques de sección de la
                # mitad final, para elegir entre ellos.
                "outCandidates": [round(c, 3) for c in mix_candidates],
                # True = ningún candidato estructural pasó la vara de contraste:
                # el punto es un fallback débil — marcar a mano con el Cue.
                "outWeak": len(mix_candidates) == 0,
                # Compases de melodía ANTES del primer tren de bombos: material
                # para el enganche melódico (B puede entrar introLead antes,
                # con su kick aterrizando en el 1 objetivo).
                "introLeadBars": intro_lead_bars,
            },
            # Grilla rígida para phase-lock: beat(n) = offset + n * beatLen.
            # quality = std en ms de los beats reales vs la grilla (<30 clavado,
            # >80 la grilla no es confiable — tema mal cuantizado o análisis pobre).
            "grid": {
                "offset": round(grid_anchor, 4),
                "kickOffset": round(kick_anchor, 4),
                "beatLen": round(beat_len, 5),
                "barLen": round(bar_len, 5),
                "quality": round(grid_resid_ms, 1) if grid_resid_ms is not None else None,
                "anchorFixMs": round(anchor_fix * 1000, 1),
                "anchorSource": anchor_source,
            },
            # Mapa de secciones: [startSec, nivelTotal, nivelGraves, nivelAltos]
            # por cambio de huella en bloques de 16 compases desde el 0.0.0.
            "sections": [[round(s[0], 2), s[1], s[2], s[3]] for s in sections],
            "wave": wave_compact,
            "energyCurve": energy_curve,
        }

    except Exception as e:
        print(f"[ANALYSIS ERROR] {e}")
        traceback.print_exc()
        return {"error": str(e)}


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        res = analyze_track(sys.argv[1])
        print(json.dumps(res, indent=2))
