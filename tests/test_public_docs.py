"""Public documentation/package contracts; no host install or executor actions."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_guidance
import package
import profile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ("README.md", "CONTRIBUTING.md", "CHANGELOG.md",
          "docs/getting-started.md", "docs/workflow.md")
# SHA-256 of the standard MIT text, LF, with Copyright (c) 2026 suuny-ab.
# Template: https://choosealicense.com/licenses/mit/ ; no network required in tests.
MIT_SHA256 = "83974a7571d42ddd41131afd347b7a6029c41c7560404df3a428b0c6702929f7"


class PublicDocumentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="public-docs-", dir=profile.filesystem(Path(tempfile.gettempdir())))
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.archive = Path(cls.temporary.name) / "public.zip"
        cls.source = package.payload(ROOT)
        package.build(ROOT, cls.archive)
        cls.manifest, cls.packaged = package.inspect(cls.archive)

    def test_standard_mit_license_and_package_bytes_match(self):
        source = self.source["LICENSE"]
        self.assertEqual(package.digest(source.replace(b"\r\n", b"\n")), MIT_SHA256)
        self.assertEqual(self.packaged["LICENSE"], source)
        self.assertEqual(self.manifest["files"]["LICENSE"], package.digest(source))

    def test_new_public_documents_are_in_package(self):
        for name in PUBLIC:
            with self.subTest(name=name):
                self.assertEqual(self.packaged[name], self.source[name])

    def test_current_and_public_links_and_anchors_in_source_and_archive(self):
        documents = tuple(dict.fromkeys(check_guidance.CURRENT + PUBLIC))
        with patch.object(check_guidance, "CURRENT", documents):
            for scope, files in (("source", self.source), ("archive", self.packaged)):
                with self.subTest(scope=scope):
                    result = check_guidance.check(files)
                    self.assertGreater(result["local_links"], 0)
                    self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
