"""Deployment contract checks; runnable directly without PostgreSQL fixtures."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "deploy.sh").read_text()


class DeployConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(ROOT / "docker-compose.yml", self.root / "docker-compose.yml")
        shutil.copyfile(ROOT / "deploy.sh", self.root / "deploy.sh")
        self.env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
        self.envfile = self.root / ".env"
        self.envfile.write_text("POSTGRES_PASSWORD=fixture-postgres\n")

    def render(self, **overrides):
        result = subprocess.run(
            ["docker", "compose", "--project-directory", str(self.root),
             "--env-file", str(self.envfile), "-f", str(self.root / "docker-compose.yml"),
             "config", "--format", "json"],
            env={**self.env, **overrides}, capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)["services"]

    def script_config(self, **overrides):
        # Execute initialization only; never enter deployment subcommands.
        prefix = SCRIPT.split("# ── 子命令", 1)[0]
        (self.root / "deploy.sh").write_text(prefix + '\nprintf "%s\\n%s" "$IMAGE_NAME" "$HOST_PORT"\n')
        result = subprocess.run(
            ["bash", str(self.root / "deploy.sh")], cwd=self.root,
            env={**self.env, **overrides}, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def script_port(self, **overrides):
        return self.script_config(**overrides)[1]

    def test_compose_image_name_from_dotenv_and_process_override(self):
        with self.envfile.open("a") as stream:
            stream.write("IMAGE_NAME=registry.example/from-dotenv\n")
        self.assertEqual(self.script_config()[0], "registry.example/from-dotenv")
        self.assertEqual(
            self.script_config(IMAGE_NAME="registry.example/from-env")[0],
            "registry.example/from-env",
        )

    def test_compose_image_name_defaults_when_unset(self):
        self.assertEqual(self.script_config()[0], "llm-modeler")

    def test_compose_image_name_controls_app_and_mock(self):
        services = self.render(IMAGE_NAME="registry.example/modeler", TAG="contract")
        self.assertEqual(services["app"]["image"], "registry.example/modeler:contract")
        self.assertEqual(services["metrology-mcp"]["image"], services["app"]["image"])

    def test_compose_does_not_inject_vector_backend(self):
        self.assertNotIn("VECTOR_BACKEND", self.render()["app"]["environment"])

    def test_external_database_still_requires_complete_stack(self):
        services = self.render(DATABASE_URL="postgresql://fixture@external.invalid/db")
        self.assertEqual(services["app"]["depends_on"]["postgres"]["condition"], "service_healthy")
        self.assertEqual(services["app"]["depends_on"]["metrology-mcp"]["condition"], "service_started")

    def test_script_port_uses_dotenv_without_shell_execution(self):
        marker = self.root / "shell-executed"
        with self.envfile.open("a") as stream:
            stream.write(f"HOST_PORT='19234' # quoted dotenv value\nPAYLOAD='$(touch {marker})'\n")
        self.assertEqual(self.script_port(), "19234")
        self.assertFalse(marker.exists())

    def test_script_port_process_environment_wins(self):
        with self.envfile.open("a") as stream:
            stream.write("HOST_PORT=19234\n")
        self.assertEqual(self.script_port(HOST_PORT="19345"), "19345")

    def test_script_port_defaults_to_19090(self):
        self.assertEqual(self.script_port(), "19090")

    def test_python_image_is_shared_by_preparation_and_final_build(self):
        dockerfile = (ROOT / "deploy/single/Dockerfile").read_text()
        default = re.search(r'PY_IMAGE="\$\{PY_IMAGE:-([^}]+)\}"', SCRIPT).group(1)
        self.assertIn(f"ARG PY_IMAGE={default}\nFROM ${{PY_IMAGE}}", dockerfile)
        self.assertIn('--build-arg PY_IMAGE="$PY_IMAGE"', SCRIPT)
        self.assertNotIn("bookworm main", SCRIPT)
        self.assertIn(".deps/.python-image-id", SCRIPT)
        self.assertIn("debs/.python-image-id", SCRIPT)

    def test_provider_probe_is_authenticated_and_not_silently_ignored(self):
        self.assertIn('/ai-modeler/api/admin/llm-providers', SCRIPT)
        self.assertIn("dc exec -T app python", SCRIPT)
        self.assertIn("load_providers() is not None", SCRIPT)
        self.assertNotIn('_prov=$(curl', SCRIPT)
        self.assertIn("供应商配置探测失败", SCRIPT)

    def test_current_guide_does_not_direct_server_deployment(self):
        guide = (ROOT / "deploy/single/README.md").read_text()
        self.assertNotIn("192.168.99.22", guide)
        self.assertNotIn("rsync -az", guide)
        self.assertIn("service_healthy", guide)


if __name__ == "__main__":
    unittest.main()
