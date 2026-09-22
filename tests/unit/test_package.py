import subprocess
import sys

import media_tools


def test_version_is_exposed():
    assert media_tools.__version__ == "0.1.0"


def test_module_entrypoint_prints_version():
    out = subprocess.run(
        [sys.executable, "-m", "media_tools", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "0.1.0" in out.stdout
