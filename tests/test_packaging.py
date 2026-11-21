"""The .deb: built by scripts/build-deb, as the release workflow does."""

import shutil
import subprocess
import sys
import unittest

from tests.helpers import ROOT, short_tmp


@unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb")
class DebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = short_tmp("cbd-")
        built = subprocess.run([str(ROOT / "scripts" / "build-deb"), "v1.2.3", str(cls.tmp / "out")],
                               capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "PYTHON": sys.executable})
        if built.returncode:
            raise AssertionError(built.stderr)
        cls.deb = cls.tmp / "out" / "claude-broker_1.2.3_all.deb"
        subprocess.run(["dpkg-deb", "-x", str(cls.deb), str(cls.tmp / "root")], check=True)
        cls.app = cls.tmp / "root" / "usr" / "bin" / "claude-broker"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_control(self):
        control = subprocess.run(["dpkg-deb", "-f", str(self.deb)], capture_output=True, text=True).stdout
        for line in ("Package: claude-broker", "Version: 1.2.3", "Architecture: all", "Depends: python3 (>= 3.12)"):
            self.assertIn(line, control)
        self.assertNotIn("@", control.split("Maintainer:")[0])         # every placeholder filled

    def test_contents(self):
        listing = subprocess.run(["dpkg-deb", "-c", str(self.deb)], capture_output=True, text=True).stdout
        for path in ("./usr/bin/claude-broker", "./usr/lib/systemd/user/claude-broker.service",
                     "./usr/share/doc/claude-broker/copyright", "./usr/share/doc/claude-broker/claude_broker_client.py"):
            self.assertIn(path, listing)
        self.assertIn("-rwxr-xr-x root/root", [l for l in listing.splitlines() if l.endswith("/usr/bin/claude-broker")][0])
        self.assertNotIn("__pycache__", listing)

    def test_the_zipapp_runs(self):
        out = subprocess.run([sys.executable, str(self.app), "version"], capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "1.2.3"))
        bad = subprocess.run([sys.executable, str(self.app), "call"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)                              # main()'s status comes through
        argv = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                               "from claude_broker.server import self_command; print(self_command()[0][1])",
                               str(self.app)], capture_output=True, text=True)
        self.assertEqual(argv.stdout.strip(), str(self.app))            # mcp-proxy starts the zipapp itself


if __name__ == "__main__":
    unittest.main()
