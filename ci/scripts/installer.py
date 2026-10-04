#!/usr/bin/env python3
"""Deterministic, transactional installer for the Codex Android/Termux port."""
from __future__ import annotations

import argparse, hashlib, json, os, pathlib, re, shutil, stat, subprocess
import sys, tarfile, tempfile, urllib.parse, urllib.request, zipfile

SCHEMA = "codex-termux/v1"
COMPONENTS = ("codex",)
DEFAULT_PREFIX = "/data/data/com.termux/files/usr"
DEFAULT_TMP = os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")
ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")

def fail(message: str) -> "NoReturn":
    print(f"[✗] {message}", file=sys.stderr); raise SystemExit(1)

def release(value: str) -> str:
    value = value[1:] if value.startswith("@") else value
    if value == "latest": return value
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?", value): fail(f"Release inválida: {value}")
    return value.removeprefix("v")

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Instala codex-termux desde un manifiesto firmado por checksum")
    p.add_argument("release", nargs="?", default="latest"); p.add_argument("--release", dest="release_opt")
    p.add_argument("--manifest"); p.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"prefijo de instalación (default: {DEFAULT_PREFIX})")
    p.add_argument("--just", action="append", choices=COMPONENTS); p.add_argument("--all", action="store_true")
    p.add_argument("--yes", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--smoke-test", action="store_true", help="ejecuta --version después de instalar; no bloquea la instalación")
    p.add_argument("--version", action="version", version="codex-termux-installer 1.0.0")
    a = p.parse_args()
    if a.release_opt is not None: a.release = a.release_opt
    a.release = release(a.release)
    a.components = list(COMPONENTS) if a.all or not a.just else list(dict.fromkeys(a.just))
    a.strict = a.just is not None
    return a

def preflight(args: argparse.Namespace) -> pathlib.Path:
    test = os.environ.get("CODEX_INSTALL_TEST_MODE") == "1"
    if not test and not pathlib.Path("/data/data/com.termux").is_dir(): fail("Este instalador requiere Termux/Android")
    if not test and os.uname().machine != "aarch64": fail(f"Arquitectura no soportada: {os.uname().machine}")
    for tool in ("curl", "mktemp", "sha256sum", "tar", "unzip", "file", "python3"):
        if not shutil.which(tool): fail(f"Falta '{tool}'. Instálalo con: pkg install {tool}")
    tmp = pathlib.Path(DEFAULT_TMP); tmp.mkdir(parents=True, exist_ok=True)
    if not os.access(tmp, os.W_OK): fail(f"TMPDIR no escribible: {tmp}")
    prefix = pathlib.Path(args.prefix)
    if not args.dry_run: prefix.mkdir(parents=True, exist_ok=True)
    sdk = shutil.which("getprop")
    if sdk and not test:
        try: args.device_api = int(subprocess.check_output([sdk, "ro.build.version.sdk"], text=True).strip())
        except (ValueError, subprocess.CalledProcessError): args.device_api = None
    else: args.device_api = None
    return tmp

def source_manifest(args: argparse.Namespace, tmp: pathlib.Path) -> tuple[pathlib.Path, str]:
    repo = os.environ.get("CODEX_INSTALL_REPO", "Leonisaurov/codex-termux")
    base = os.environ.get("CODEX_INSTALL_MANIFEST_BASE", f"https://github.com/{repo}/releases/download")
    if args.manifest: source = args.manifest
    elif args.release == "latest": source = f"https://github.com/{repo}/releases/latest/download/manifest.json"
    else: source = f"{base}/codex-v{args.release}/manifest.json"
    fd, raw = tempfile.mkstemp(prefix="codex-manifest.", dir=tmp)
    os.close(fd); dst = pathlib.Path(raw)
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in ("http", "https"):
        try: urllib.request.urlretrieve(source, dst)
        except Exception as e: fail(f"No existe el manifiesto: {source} ({e})")
        origin = source
    else:
        path = pathlib.Path(source).expanduser()
        if not path.is_file(): fail(f"No existe el manifiesto: {source}")
        shutil.copyfile(path, dst); origin = path.resolve().as_uri()
    return dst, origin

def safe_name(name: object, label: str) -> str:
    if not isinstance(name, str) or not name or ABSOLUTE.match(name) or "\\" in name:
        fail(f"{label}: ruta inválida")
    parts = pathlib.PurePosixPath(name).parts
    if any(x in ("", ".", "..") for x in parts): fail(f"{label}: ruta inválida")
    return name

def validate_manifest(path: pathlib.Path, requested: str, device_api: int | None) -> dict:
    try: data = json.loads(path.read_text())
    except Exception as e: fail(f"JSON de manifiesto inválido: {e}")
    if data.get("schema") != SCHEMA: fail("schema de manifiesto no soportado")
    if data.get("documentation_only"): fail("este manifiesto es solo documentación")
    if data.get("stability") != "stable": fail("solo se pueden instalar releases estables")
    actual = str(data.get("release", "")).removeprefix("v")
    if not actual or (requested != "latest" and actual != requested): fail("release del manifiesto no coincide")
    android = data.get("android", {})
    if android.get("arch") != "aarch64" or android.get("abi") != "arm64-v8a": fail("arquitectura/ABI incompatible")
    if not isinstance(android.get("api"), int) or android["api"] < 1: fail("API Android inválida")
    if device_api is not None and android["api"] > device_api: fail(f"requiere Android API {android['api']}, dispositivo API {device_api}")
    comps = data.get("components")
    if not isinstance(comps, dict): fail("componentes inválidos")
    for name in COMPONENTS:
        if name not in comps: continue
        c = comps[name]
        for key in ("version", "asset", "sha256", "size", "archive", "files", "depends"):
            if key not in c: fail(f"{name}: falta {key}")
        if not isinstance(c["version"], str) or not c["version"]: fail(f"{name}: versión inválida")
        if not re.fullmatch(r"[0-9a-f]{64}", c["sha256"]): fail(f"{name}: checksum inválido")
        if not isinstance(c["size"], int) or c["size"] <= 0: fail(f"{name}: tamaño inválido")
        if c["archive"] not in ("tar.gz", "zip", "file"): fail(f"{name}: archive inválido")
        if not isinstance(c["files"], list) or not c["files"]: fail(f"{name}: files inválido")
        for f in c["files"]: safe_name(f, f"{name}.files")
        if not isinstance(c["depends"], list) or any(d not in COMPONENTS for d in c["depends"]): fail(f"{name}: dependencias inválidas")
        asset = c["asset"]
        if not isinstance(asset, str) or not asset or (not urllib.parse.urlparse(asset).scheme and (ABSOLUTE.match(asset) or ".." in pathlib.PurePosixPath(asset).parts)):
            fail(f"{name}: asset inválido")
    return data

def selected(data: dict, names: list[str], strict: bool = True) -> list[str]:
    comps = data["components"]
    chosen = []
    for name in names:
        if name not in comps:
            if strict: fail(f"componente ausente: {name}")
            continue
        for dep in comps[name]["depends"]:
            if dep not in names: fail(f"{name} requiere el componente {dep}; selección incompatible")
        chosen.append(name)
    return chosen

def download(asset: str, origin: str, actual_release: str, base: pathlib.Path) -> pathlib.Path:
    parsed = urllib.parse.urlparse(asset)
    if parsed.scheme in ("http", "https", "file"): url = asset
    elif urllib.parse.urlparse(origin).scheme in ("http", "https"):
        url = urllib.parse.urljoin(origin.rsplit("/", 1)[0] + "/", asset)
    else:
        url = (pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(origin).path)).parent / asset).as_uri()
    dst = base / "download"; dst.parent.mkdir(parents=True, exist_ok=True)
    if urllib.parse.urlparse(url).scheme == "file": shutil.copyfile(pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)), dst)
    else:
        try: urllib.request.urlretrieve(url, dst)
        except Exception as e: fail(f"falló la descarga: {url} ({e})")
    return dst

def validate_archive(path: pathlib.Path, kind: str, expected: list[str], out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if kind == "file": shutil.copyfile(path, out / expected[0]); return
    try:
        if kind == "tar.gz":
            with tarfile.open(path, "r:gz") as t:
                for m in t.getmembers():
                    safe_name(m.name, "tar member")
                    if m.issym() or m.islnk() or not (m.isfile() or m.isdir()): fail("archive tar contiene enlace o tipo peligroso")
                t.extractall(out)
        elif kind == "zip":
            with zipfile.ZipFile(path) as z:
                for i in z.infolist():
                    safe_name(i.filename, "zip member")
                    mode = (i.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK: fail("archive zip contiene symlink")
                z.extractall(out)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as e: fail(f"archive corrupto: {e}")
    for f in expected:
        if not (out / f).is_file() or (out / f).is_symlink(): fail(f"falta archivo esperado: {f}")

def verify_file(path: pathlib.Path, component: str, name: str) -> None:
    if name == "codex-linux-sandbox":
        if subprocess.run(["bash", "-n", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode: fail("codex-linux-sandbox no es un script Bash válido")
        return
    if name.endswith(".so") or name in ("bun", "opencode", "kilo", "codex", "codex-android", "codex-code-mode-host"):
        if os.environ.get("CODEX_INSTALL_TEST_MODE") == "1": return
        info = subprocess.check_output(["file", str(path)], text=True)
        if "ELF" not in info or not re.search(r"aarch64|ARM aarch64", info): fail(f"{component}: arquitectura ELF inválida en {name}")

def smoke(path: pathlib.Path, component: str) -> bool:
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30)
    except OSError as e:
        print(f"[!] smoke test falló: {component} --version no se pudo ejecutar: {e}\n  ejecutable: {path}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired as e:
        print(f"[!] smoke test falló: {component} --version excedió 30s; stdout={e.stdout or '<vacío>'!r}; stderr={e.stderr or '<vacío>'!r}\n  ejecutable: {path}", file=sys.stderr)
        return False
    if result.returncode == 0: return True
    try: info = subprocess.check_output(["file", str(path)], text=True).strip()
    except subprocess.CalledProcessError as e: info = f"file falló con código {e.returncode}"
    print(
        f"smoke test falló: {component} --version (código {result.returncode})\n"
        f"  ejecutable: {path}\n"
        f"  tipo: {info}\n"
        f"  stdout: {result.stdout.strip() or '<vacío>'}\n"
        f"  stderr: {result.stderr.strip() or '<vacío>'}", file=sys.stderr
    )
    return False

def main() -> None:
    args = parse_args(); tmp = preflight(args); manifest, origin = source_manifest(args, tmp)
    try:
        data = validate_manifest(manifest, args.release, args.device_api); names = selected(data, args.components, args.strict)
        print("Componentes y versiones:", file=sys.stderr)
        for n in names: print(f"  {n} {data['components'][n]['version']}", file=sys.stderr)
        if not args.dry_run and not args.yes:
            if not sys.stdin.isatty(): fail("La entrada no es TTY; usa --yes")
            if input("¿Continuar? [s/N] ").lower() != "s": return
        stage = pathlib.Path(tempfile.mkdtemp(prefix="codex-stage.", dir=tmp)); payload = stage / "payload"
        try:
            actual = str(data["release"]).removeprefix("v")
            for n in names:
                c = data["components"][n]; dl = download(c["asset"], origin, actual, stage / n)
                if dl.stat().st_size != c["size"]: fail(f"{n}: tamaño inválido")
                if hashlib.sha256(dl.read_bytes()).hexdigest() != c["sha256"]: fail(f"{n}: checksum inválido")
                ext = stage / n / "extracted"; validate_archive(dl, c["archive"], c["files"], ext)
                for f in c["files"]: verify_file(ext / f, n, f)
                dest = {"codex":"bin/codex-android"}[n]
                for f in c["files"]:
                    target = dest if f == c["files"][0] else f"bin/{f}"
                    target_path = payload / target; target_path.parent.mkdir(parents=True, exist_ok=True)
                    try: os.replace(ext / f, target_path)
                    except OSError: shutil.copy2(ext / f, target_path)
                    target_path.chmod(0o755)
            if args.dry_run: print("Dry-run válido.", file=sys.stderr); return
            prefix = pathlib.Path(args.prefix); backup = pathlib.Path(tempfile.mkdtemp(prefix="codex-backup.", dir=tmp)); moved=[]
            smoke_failures = []
            try:
                files = [p.relative_to(payload) for p in payload.rglob("*") if p.is_file()]
                for rel in files:
                    dst = prefix / rel
                    if dst.exists():
                        b = backup / rel; b.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(dst, b)
                for rel in files:
                    dst = prefix / rel; dst.parent.mkdir(parents=True, exist_ok=True); os.replace(payload / rel, dst); moved.append((rel, dst))
                for n in names:
                    if not args.smoke_test: continue
                    exe = prefix / {"codex":"bin/codex-android"}[n]
                    if not smoke(exe, n): smoke_failures.append(n)
                print("[✓] Instalación completa: archivos, checksum y arquitectura validados.", file=sys.stderr)
            except BaseException:
                for _, dst in reversed(moved):
                    if dst.exists(): dst.unlink()
                for rel in [p.relative_to(backup) for p in backup.rglob("*") if p.is_file()]:
                    dst=prefix/rel; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(backup/rel, dst)
                raise
            finally: shutil.rmtree(backup, ignore_errors=True)
            if smoke_failures:
                fail("instalación completada, pero falló el smoke test opcional para: " + ", ".join(smoke_failures))
        finally: shutil.rmtree(stage, ignore_errors=True)
    finally: manifest.unlink(missing_ok=True)

if __name__ == "__main__": main()
