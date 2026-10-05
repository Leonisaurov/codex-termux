#!/usr/bin/env python3
"""Fast local acceptance tests for installer invariants (no network/builds)."""
import hashlib, json, os, pathlib, subprocess, tarfile, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="installer-tests.", dir=os.environ.get("TMPDIR", "/data/data/com.termux/files/usr/tmp")))
        self.assets = self.tmp / "assets"; self.assets.mkdir()
        self.prefix = self.tmp / "prefix"; self.prefix.mkdir()
        self.manifest = self.tmp / "manifest.json"
        files = ["codex-android", "codex-code-mode-host", "codex-linux-sandbox"]
        for name, body in (
            ("codex-android", "#!/bin/sh\nprintf 'codex-android\\n' version\n"),
            ("codex-code-mode-host", "#!/bin/sh\nexit 0\n"),
            ("codex-linux-sandbox", "#!/usr/bin/env bash\nexit 78\n"),
        ):
            payload = self.assets / name; payload.write_text(body); payload.chmod(0o755)
        archive = self.assets / "codex.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in files: tar.add(self.assets / name, arcname=name)
        components = {"codex": {"version":"1.0.0", "tag":"test", "asset":"assets/" + archive.name, "sha256":hashlib.sha256(archive.read_bytes()).hexdigest(), "size":archive.stat().st_size, "archive":"tar.gz", "depends":[], "files":files}}
        self.manifest.write_text(json.dumps({"schema":"codex-termux/v1", "release":"v0.155.1", "stability":"stable", "android":{"arch":"aarch64","abi":"arm64-v8a","api":24}, "components":components}))
    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)
    def run_installer(self, *args):
        env = os.environ | {"CODEX_INSTALL_TEST_MODE":"1", "TMPDIR":str(self.tmp)}
        return subprocess.run(["bash", str(INSTALL), "--manifest", str(self.manifest), "--prefix", str(self.prefix), "--yes", *args], env=env, text=True, capture_output=True)
    def replace_codex_asset(self, archive_name):
        data = json.loads(self.manifest.read_text())
        component = data["components"]["codex"]
        archive = self.assets / archive_name
        component.update(asset="assets/" + archive_name, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size=archive.stat().st_size)
        self.manifest.write_text(json.dumps(data))
    def test_dry_run_does_not_touch_prefix(self):
        r = self.run_installer("--dry-run"); self.assertEqual(r.returncode, 0, r.stderr); self.assertFalse((self.prefix / "bin").exists())
    def test_full_install_places_every_codex_file(self):
        r = self.run_installer(); self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("codex-android", "codex-code-mode-host", "codex-linux-sandbox"):
            self.assertTrue((self.prefix / "bin" / name).is_file(), name)
    def test_just_codex_is_accepted(self):
        r = self.run_installer("--just", "codex"); self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_source_identity_block_does_not_break_install(self):
        # package-release.py registra el arbol vendeoreado en el manifest para que el
        # pipeline sepa si ya publico esta fuente; el instalador debe ignorarlo.
        data = json.loads(self.manifest.read_text())
        data["components"]["codex"]["source"] = {"commit": "c" * 40, "tree": "d" * 40}
        self.manifest.write_text(json.dumps(data))
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.prefix / "bin" / "codex-android").is_file())

    def test_unknown_component_is_rejected(self):
        r = self.run_installer("--just", "opencode"); self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.prefix / "bin").exists())

    def test_custom_prefix_uses_local_bin(self):
        prefix = self.tmp / ".local"
        r = self.run_installer("--prefix", str(prefix))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((prefix / "bin" / "codex-android").is_file())

    def test_smoke_failure_reports_command_diagnostics(self):
        payload = self.assets / "codex-android"
        payload.write_text("#!/bin/sh\nprintf '%s\\n' stdout\nprintf '%s\\n' stderr >&2\nexit 42\n")
        payload.chmod(0o755)
        archive = self.assets / "codex-failing.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("codex-android", "codex-code-mode-host", "codex-linux-sandbox"):
                tar.add(self.assets / name, arcname=name)
        self.replace_codex_asset("codex-failing.tar.gz")
        r = self.run_installer("--smoke-test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("código 42", r.stderr)
        self.assertIn("stdout", r.stderr)
        self.assertIn("stderr", r.stderr)
        self.assertTrue((self.prefix / "bin" / "codex-android").exists())
    def test_bad_checksum_keeps_prefix_untouched(self):
        data=json.loads(self.manifest.read_text()); data["components"]["codex"]["sha256"]="0"*64; self.manifest.write_text(json.dumps(data))
        r=self.run_installer(); self.assertNotEqual(r.returncode, 0); self.assertFalse((self.prefix / "bin").exists())
    def test_tar_traversal_is_rejected(self):
        bad=self.assets / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tar:
            tar.add(self.assets / "codex-android", arcname="../escaped")
        self.replace_codex_asset("bad.tar.gz")
        r=self.run_installer(); self.assertNotEqual(r.returncode, 0); self.assertFalse((self.tmp / "escaped").exists())
    def test_corrupt_archive_is_rejected(self):
        bad=self.assets / "corrupt.tar.gz"; bad.write_bytes(b"not-an-archive")
        self.replace_codex_asset("corrupt.tar.gz")
        r=self.run_installer(); self.assertNotEqual(r.returncode, 0); self.assertFalse((self.prefix / "bin").exists())
    def test_stack_manifest_schema_is_rejected(self):
        data=json.loads(self.manifest.read_text()); data["schema"]="opencode-termux.stack/v1"; self.manifest.write_text(json.dumps(data))
        r=self.run_installer(); self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema", r.stderr)

if __name__ == "__main__": unittest.main()
