#!/usr/bin/env python3
"""Auditoría de audiolibros: no mueve, repara ni borra originales.

Requiere FFmpeg y ffprobe en PATH, o --ffmpeg y --ffprobe.
Reanuda auditorías usando cache de archivos estables (ruta+tamaño+mtime+versión).
Importante: verifica decodificabilidad, NO puede certificar que no falte contenido
cuando el archivo fue editado limpiamente y los metadatos coinciden.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

VERSION = "1.1.0"
DEFAULT_ROOT = Path(r"C:\Users\look\Music\groove-new\Audiolibros")
EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff", ".aif"}
COVERS = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png"}
PART_PATTERN = re.compile(r"(?i)(?:^|[\s_\-.])(part(?:e)?|disc|disk|cd|track|cap(?:itulo|ítulo|ter)?|chapter)[\s_\-.]*0*(\d{1,3})(?:\b|$)")


def as_number(v, default=0.0):
    try:
        n = float(v)
        return n if math.isfinite(n) and n >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def sec_from_ffmpeg(v):
    try:
        a, b, c = v.split(":")
        return float(a) * 3600 + float(b) * 60 + float(c)
    except (TypeError, ValueError, AttributeError):
        return None


def normalized(v):
    t = unicodedata.normalize("NFKD", str(v or "")).casefold()
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"\s*\(\d+\)$", "", t)  # nombre (1), copia descargada
    t = re.sub(r"\b(unabridged|audiobook|audiolibro|audio book|complete edition)\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def identity_from_path(p, root):
    rel = p.relative_to(root)
    # Para archivos en carpetas dedicadas a una obra, el nombre de esa carpeta
    # suele ser más fiable que tags internos que a veces son de otro volumen.
    parent_is_category = normalized(rel.parent.name) in {"audiolibros", "audiobooks", "audio books", "books"}
    source = rel.parent.name if len(rel.parts) > 1 and not parent_is_category else p.stem
    source = re.sub(r"\s*\(\d+\)$", "", source)
    if " - " in source:
        author, title = source.split(" - ", 1)
        return normalized(title), normalized(author), source
    return normalized(source), "", source


def binary(name):
    requested = Path(name)
    p = shutil.which(name) or (str(requested) if requested.is_file() else None)
    if not p:
        basename = requested.stem if requested.suffix.lower() == ".exe" else requested.name
        candidate = Path(r"C:\bin") / (basename + ".exe")
        if candidate.is_file():
            p = str(candidate)
    if not p:
        raise SystemExit(f"No encuentro {name!r}. Instalá FFmpeg y agregalo al PATH o usá --ffmpeg/--ffprobe.")
    return p


def ff_version(ff):
    r = subprocess.run([ff, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
    if r.returncode:
        raise RuntimeError(f"No se puede ejecutar {ff}: {r.stderr[:150]}")
    return r.stdout.splitlines()[0]


def probe(p, ffprobe):
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", str(p)]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=120)
        if r.returncode:
            return {}, f"ffprobe rc={r.returncode}: {r.stderr[-900:]}"
        data = json.loads(r.stdout)
        if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
            return data, "No hay pista de audio"
        return data, None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {}, f"ffprobe: {exc}"


def decode_entire(p, ffmpeg, timeout_hours=48):
    """Lee/decodea del primer al último paquete. Tiempo por muestras, NO timestamps.

    -af asetpts=N/SR/TB hace que out_time mida muestras realmente decodificadas
    aun si los timestamps del MP4 saltan o anuncian una duración ficticia.
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-xerror",
           "-stats_period", "20", "-i", str(p), "-map", "0:a:0", "-vn", "-sn", "-dn",
           "-af", "asetpts=N/SR/TB", "-f", "null", "-", "-progress", "pipe:1"]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout_hours * 3600)
        secs = None
        ended = False
        for line in r.stdout.splitlines():
            if line.startswith("out_time="):
                n = sec_from_ffmpeg(line.partition("=")[2])
                if n is not None:
                    secs = n
            elif line == "progress=end":
                ended = True
        # No damos por sano un proceso sin duración utilizable.
        return {"decoded_s": secs, "decoder_rc": r.returncode,
                "decoder_finished": ended, "decoder_error": r.stderr[-4000:].strip()}
    except subprocess.TimeoutExpired:
        return {"decoded_s": None, "decoder_rc": None, "decoder_finished": False, "decoder_error": "Decodificación excedió el tiempo máximo"}
    except OSError as exc:
        return {"decoded_s": None, "decoder_rc": None, "decoder_finished": False, "decoder_error": str(exc)}


def threshold(s):
    return max(5.0, min(120.0, s * 0.001))


def verify_chapters(chapters, decoded):
    issues = []
    valid = []
    for i, ch in enumerate(chapters):
        a = as_number(ch.get("start_time"), -1)
        b = as_number(ch.get("end_time"), -1)
        if a < 0 or b <= a:
            issues.append(f"Capítulo {i+1}: tiempo inválido {a:g}-{b:g}")
            continue
        valid.append((a, b))
    for i, (a, b) in enumerate(valid):
        if decoded is not None and (a > decoded + threshold(decoded) or b > decoded + threshold(decoded)):
            issues.append(f"Capítulo {i+1} termina en {b/3600:.2f}h fuera de audio decodificado ({decoded/3600:.2f}h)")
        if i and a + 1 < valid[i-1][0]:
            issues.append(f"Capítulo {i+1}: capítulos fuera de orden")
        if i and a < valid[i-1][1] - 3:
            issues.append(f"Capítulo {i+1}: solapamiento >3s")
    return issues


def inspect_one(p, root, ffmpeg, ffprobe, deep=True):
    try:
        stat0 = p.stat()
    except OSError as exc:
        return {"path": str(p), "state": "ERROR", "issues": [str(exc)]}
    key, author, display = identity_from_path(p, root)
    record = {"path": str(p), "relative_path": str(p.relative_to(root)),
              "size": stat0.st_size, "mtime_ns": stat0.st_mtime_ns,
              "work_key": key, "author_key": author, "work": display,
              "state": "PENDIENTE", "issues": [], "warnings": [], "sha256": None}
    if stat0.st_size <= 0:
        record.update(state="ROTO", issues=["Archivo vacío"])
        return record
    data, problem = probe(p, ffprobe)
    if problem:
        record.update(state="ROTO", issues=[problem]); return record
    streams = data.get("streams", [])
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    stream = audios[0]
    tags = dict(data.get("format", {}).get("tags", {}))
    tags.update(stream.get("tags") or {})
    duration = as_number(data.get("format", {}).get("duration")) or as_number(stream.get("duration"))
    chapters = data.get("chapters", [])
    embedded = any(s.get("codec_type") == "video" and int((s.get("disposition") or {}).get("attached_pic") or 0) for s in streams)
    sidecar = any((p.parent / name).is_file() for name in COVERS)
    record.update(declared_s=duration, chapters=len(chapters),
                  chapter_end_s=max((as_number(c.get("end_time")) for c in chapters), default=0.0),
                  album=tags.get("album", ""), title=tags.get("title", ""), artist=tags.get("artist", ""),
                  bitrate=as_number(stream.get("bit_rate") or data.get("format", {}).get("bit_rate")),
                  codec=stream.get("codec_name", ""), cover=bool(embedded or sidecar),
                  cover_embedded=bool(embedded), audio_streams=len(audios))
    if not duration:
        record["warnings"].append("Sin duración declarada verificable")
    if len(audios) > 1:
        record["warnings"].append("Múltiples pistas audio: solo se audita la pista 0")
    if not chapters:
        record["warnings"].append("Sin tabla de capítulos; no prueba que falte contenido")
    elif len(chapters) == 1:
        record["warnings"].append("Un solo capítulo; puede ser normal o tabla ausente")
    if not record["cover"]:
        record["warnings"].append("Sin portada incrustada ni cover.jpg/folder.jpg")
    # Metadatos y nombre están deliberadamente separados; no usar tags solos
    # para decidir si dos archivos representan la misma obra.
    if record["album"] and key:
        ktag = normalized(record["album"])
        if (len(key.split()) > 2 and len(ktag.split()) > 2
                and SequenceMatcher(None, key, ktag).ratio() < 0.35):
            record["warnings"].append("Nombre/carpeta y tag album no coinciden; revisar edición")
    if not deep:
        record.update(state="SIN_DECODIFICAR", decoded_s=None)
        record["issues"].extend(verify_chapters(chapters, None))
        return record
    dec = decode_entire(p, ffmpeg)
    record.update(dec)
    if dec["decoder_rc"] != 0 or not dec["decoder_finished"] or dec["decoded_s"] is None:
        record["issues"].append("No terminó decodificación completa sin errores")
        if dec.get("decoder_error"):
            record["issues"].append(dec["decoder_error"][-700:])
    decoded = dec.get("decoded_s")
    if decoded is not None:
        if decoded <= 0:
            record["issues"].append("FFmpeg no obtuvo audio reproducible")
        if duration and duration - decoded > threshold(duration):
            record["issues"].append(f"Audio TRUNCADO: anuncia {duration/3600:.2f}h, decodifica {decoded/3600:.2f}h")
        if duration and decoded - duration > threshold(decoded):
            record["warnings"].append(f"Audio decodificado ({decoded/3600:.2f}h) excede duración declarada ({duration/3600:.2f}h)")
    record["issues"].extend(verify_chapters(chapters, decoded))
    if record["issues"]:
        record["state"] = "ROTO_O_INCONSISTENTE"
    else:
        record["state"] = "AUDIO_TECNICAMENTE_OK"
    try:
        stat1 = p.stat()
        if (stat0.st_size, stat0.st_mtime_ns) != (stat1.st_size, stat1.st_mtime_ns):
            record["state"] = "MODIFICADO_DURANTE_LECTURA"
            record["issues"].append("Archivo cambió durante auditoría; repetir cuando termine la descarga")
    except OSError:
        record["state"] = "ERROR"
        record["issues"].append("Archivo desapareció durante auditoría")
    return record


def hash_file(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def duplicate_analysis(records, cache):
    exact = []
    by_size = defaultdict(list)
    for r in records:
        if r.get("size", 0) > 0:
            by_size[r["size"]].append(r)
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        for r in group:
            p = Path(r["path"])
            try:
                st0 = p.stat()
                ck = str(p)
                old = cache.get(ck, {})
                if (old.get("size"), old.get("mtime_ns"), old.get("sha256")) == (st0.st_size, st0.st_mtime_ns, old.get("sha256")) and old.get("sha256"):
                    digest = old["sha256"]
                else:
                    digest = hash_file(p)
                    st1 = p.stat()
                    if (st0.st_size, st0.st_mtime_ns) != (st1.st_size, st1.st_mtime_ns):
                        raise OSError("Archivo cambió mientras se calculaba hash")
                r["sha256"] = digest
                cache.setdefault(ck, {}).update({"size": st0.st_size, "mtime_ns": st0.st_mtime_ns, "sha256": digest})
            except OSError as exc:
                r["warnings"].append(f"No se pudo calcular SHA-256: {exc}")
        by_hash = defaultdict(list)
        for r in group:
            if r.get("sha256"):
                by_hash[r["sha256"]].append(r)
        for h, g in by_hash.items():
            if len(g) > 1:
                exact.append({"sha256": h, "size": size, "paths": [r["path"] for r in g]})
    probable = []
    seen = set()
    # Esto es SOLO una propuesta: similitud de nombres+duración NO es identidad.
    for i, a in enumerate(records):
        for b in records[i+1:]:
            if a.get("sha256") and a.get("sha256") == b.get("sha256"):
                continue
            da = a.get("decoded_s") or a.get("declared_s") or 0
            db = b.get("decoded_s") or b.get("declared_s") or 0
            if min(da, db) <= 0 or abs(da-db)/max(da,db) > 0.02:
                continue
            ak, bk = a.get("work_key", ""), b.get("work_key", "")
            if not ak or not bk: continue
            sim = SequenceMatcher(None, ak, bk).ratio()
            # Si ambos autores identificados y distintos, no agrupar.
            if a.get("author_key") and b.get("author_key") and a["author_key"] != b["author_key"]:
                continue
            if sim < 0.87:
                continue
            pair = tuple(sorted([a["path"], b["path"]]))
            if pair in seen:
                continue
            seen.add(pair)
            probable.append({"paths": list(pair), "similarity": round(sim, 3),
                             "reason": "Título y duración parecidos, bytes no confirmados iguales; comparar narrador/edición"})
    return exact, probable


def missing_parts(records):
    folders = defaultdict(list)
    for r in records:
        p = Path(r["path"])
        m = PART_PATTERN.search(p.stem)
        if m:
            folders[(str(p.parent), m.group(1).lower())].append((int(m.group(2)), r["path"]))
    out = []
    for (folder, typ), items in folders.items():
        nums = sorted({i for i, _ in items})
        if len(nums) < 2:
            continue
        want = set(range(1, nums[-1]+1))
        missing = sorted(want - set(nums))
        if missing:
            out.append({"folder": folder, "part_type": typ, "present": nums, "missing": missing,
                        "caution": "La numeración local no acredita que el último volumen exista"})
    return out


def missing_manifest_parts(root, records):
    """Comprueba las partes esperadas por libro.json; ausencia de manifiesto no es OK editorial."""
    observed = {Path(r["path"]).resolve() for r in records}
    missing = []
    for manifest in root.rglob("libro.json"):
        if "_curador" in manifest.relative_to(root).parts:
            continue
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            missing.append({"manifest": str(manifest), "error": f"JSON ilegible: {exc}"})
            continue
        expected = doc.get("archivos") or []
        pending = []
        for item in expected:
            if isinstance(item, dict):
                name = item.get("nombre") or item.get("name") or item.get("archivo")
            elif isinstance(item, str):
                name = item
            else:
                continue
            if not name:
                continue
            # No leer archivos ajenos al directorio del libro, aunque el JSON esté mal formado.
            target = manifest.parent / Path(str(name).replace("\\", "/")).name
            if not target.exists():
                pending.append(target.name)
        declared_failures = doc.get("fallados") or []
        if pending or declared_failures:
            missing.append({"manifest": str(manifest), "missing": pending,
                            "declared_failures": declared_failures[:100]})
    return missing


def title_conflicts(records):
    """Obras con título igual y duraciones diferentes: alerta, nunca se fusionan."""
    by_key = defaultdict(list)
    for r in records:
        k = (r.get("author_key", ""), r.get("work_key", ""))
        if k[1]:
            by_key[k].append(r)
    warnings = []
    for key, group in by_key.items():
        values = [r.get("decoded_s") or r.get("declared_s") or 0 for r in group]
        positive = [x for x in values if x > 0]
        if len(positive) < 2 or max(positive) <= 0:
            continue
        if (max(positive) - min(positive)) / max(positive) >= 0.02:
            warnings.append({"author_title_key": list(key),
                             "reason": "Misma obra aparente, duraciones difieren >=2%; puede haber versión abreviada, edición diferente o copia cortada",
                             "copies": [{"path": r["path"], "decoded_s": r.get("decoded_s"), "declared_s": r.get("declared_s"), "state": r["state"]} for r in group]})
    return warnings


def decisions(records, exact, probable):
    # Sólo los hashes idénticos son duplicados CONFIRMADOS. Todo lo demás
    # es candidato de revisión; nunca se decide borrar automáticamente.
    by_path = {r["path"]: r for r in records}
    out = []
    for g in exact:
        rr = [by_path[p] for p in g["paths"]]
        def merit(r):
            return (r.get("state") == "AUDIO_TECNICAMENTE_OK", len(r.get("issues", [])) == 0,
                    r.get("chapters", 0), bool(r.get("cover_embedded")), bool(r.get("cover")))
        pick = max(rr, key=merit)
        out.append({"type": "DUPLICADO_EXACTO", "keep_suggested": pick["path"],
                    "other_copies": [r["path"] for r in rr if r is not pick],
                    "action": "REVISAR; NO SE BORRA AUTOMATICAMENTE",
                    "note": "Hash idéntico: los bytes son iguales; prioridad de carpeta/organización es decisión humana"})
    for g in probable:
        out.append({"type": "POSIBLE_DUPLICADO", "paths": g["paths"],
                    "action": "REVISAR NARRADOR/EDICION Y ESCUCHAR; NO BORRAR", "reason": g["reason"]})
    return out


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".curador-", suffix=".tmp", dir=path.parent)
    try:
        with open(handle, "w", encoding="utf-8", closefd=True) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        Path(temp).replace(path)
    finally:
        Path(temp).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audita íntegramente audiolibros y duplicados sin borrar archivos")
    parser.add_argument("--root", default=DEFAULT_ROOT, type=Path, help=f"Carpeta de audiolibros (default: {DEFAULT_ROOT})")
    parser.add_argument("--output", type=Path, help="Carpeta informes; default ROOT/_curador/_trabajo")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Ruta de ffmpeg.exe o nombre PATH")
    parser.add_argument("--ffprobe", default="ffprobe", help="Ruta de ffprobe.exe o nombre PATH")
    parser.add_argument("--metadata-only", action="store_true", help="Escaneo rápido; NO valida integridad de audio")
    parser.add_argument("--all-audio", action="store_true", help="Compatibilidad: la raíz de Audiolibros ya incluye todos los audios soportados")
    parser.add_argument("--force", action="store_true", help="Ignorar cache y volver a decodificar")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"No existe carpeta: {root}")
    out = (args.output or root / "_curador" / "_trabajo").expanduser().resolve()
    ffmpeg = binary(args.ffmpeg)
    ffprobe = binary(args.ffprobe)
    engine = ff_version(ffmpeg) + " | " + ff_version(ffprobe)
    mode = ("meta" if args.metadata_only else "full") + ":audiobooks"
    state_path = out / "cache_curador.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"version": VERSION, "engine": engine, "records": {}}
    cache = state.get("records", {}) if state.get("version") == VERSION and state.get("engine") == engine else {}
    # La raíz YA ES Audiolibros, por lo que se revisan M4B completos,
    # MP3/M4A por capítulos y todos los formatos admitidos sin salir de ella.
    # Se ignoran sólo resultados propios y descargas temporales.
    excluded_dirs = {"_curador", "_trabajo", "_armando", "_papelera", "_bajando"}
    def in_scope(p):
        if p.suffix.lower() not in EXTS or p.is_symlink() or not p.is_file():
            return False
        rel_parts = p.relative_to(root).parts[:-1]
        return not any(part.lower() in excluded_dirs for part in rel_parts)
    files = sorted((p for p in root.rglob("*") if in_scope(p)), key=lambda p: str(p).casefold())
    print(f"Archivos: {len(files)} | modo: {mode} | raíz: {root}", flush=True)
    records = []
    for idx, p in enumerate(files, start=1):
        try:
            st = p.stat()
            prev = cache.get(str(p), {})
            usable = (not args.force and prev.get("audit_version") == VERSION and
                      prev.get("engine") == engine and prev.get("mode") == mode and
                      prev.get("size") == st.st_size and prev.get("mtime_ns") == st.st_mtime_ns)
        except OSError:
            usable = False
        if usable:
            record = dict(prev)
            reused = True
        else:
            record = inspect_one(p, root, ffmpeg, ffprobe, deep=not args.metadata_only)
            record.update(audit_version=VERSION, engine=engine, mode=mode)
            reused = False
            old = cache.get(str(p), {})
            if old.get("sha256") and old.get("size") == record.get("size") and old.get("mtime_ns") == record.get("mtime_ns"):
                record["sha256"] = old["sha256"]
            cache[str(p)] = record
            # Guardar estado por archivo para recuperarse de cortes de luz/reinicio.
            save_json(state_path, {"version": VERSION, "engine": engine, "records": cache})
        records.append(record)
        dur = record.get("decoded_s") or record.get("declared_s") or 0
        print(f"[{idx}/{len(files)}] {'cache' if reused else 'nuevo':5s} {record.get('state','?'):29s} {dur/3600:6.2f}h {p.name}", flush=True)
    print("Calculando SHA-256 de grupos con igual tamaño...", flush=True)
    exact, probable = duplicate_analysis(records, cache)
    for r in records:
        if r.get("sha256") and str(r["path"]) in cache:
            cache[str(r["path"])]["sha256"] = r["sha256"]
    save_json(state_path, {"version": VERSION, "engine": engine, "records": cache})
    missing = missing_parts(records)
    missing_manifest = missing_manifest_parts(root, records)
    conflicts = title_conflicts(records)
    results = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "version": VERSION, "root": str(root),
               "mode": mode, "warning": "Integridad técnica NO certifica contenido narrativo completo sin referencia editorial",
               "total_files": len(records), "files": records, "exact_duplicates": exact, "possible_duplicates": probable,
               "missing_numbered_parts": missing, "missing_manifest_parts": missing_manifest,
               "same_title_different_duration": conflicts,
               "suggested_decisions": decisions(records, exact, probable)}
    save_json(out / "auditoria.json", results)
    with (out / "auditoria.csv").open("w", encoding="utf-8-sig", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=["state", "relative_path", "size", "declared_s", "decoded_s", "chapters", "chapter_end_s", "cover", "issues", "warnings"])
        w.writeheader()
        for rec in records:
            w.writerow({k: (" | ".join(rec.get(k, [])) if k in ("issues", "warnings") else rec.get(k, "")) for k in w.fieldnames})
    print("\nRESUMEN", flush=True)
    summary = defaultdict(int)
    for rec in records:
        summary[rec["state"]] += 1
    for k, n in sorted(summary.items()):
        print(f"  {k}: {n}")
    print(f"  Grupos duplicados EXACTOS: {len(exact)}")
    print(f"  Pares de POSIBLES duplicados: {len(probable)}")
    print(f"  Secuencias numeradas con huecos: {len(missing)}")
    print(f"  Manifiestos libro.json con partes faltantes/fallidas: {len(missing_manifest)}")
    print(f"  Obras con duraciones diferentes: {len(conflicts)}")
    print(f"  Informes: {out / 'auditoria.json'} | {out / 'auditoria.csv'}")
    print("No se ha movido, reparado ni eliminado ningún audiolibro.")
    return 1 if (summary["ROTO_O_INCONSISTENTE"] or summary["ROTO"] or summary["ERROR"]
                 or missing or missing_manifest) else 0


if __name__ == "__main__":
    sys.exit(main())