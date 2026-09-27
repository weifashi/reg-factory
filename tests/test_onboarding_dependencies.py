"""Local regression for the pinned validation stack; no HTTP or real files."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class DependencySecurityTests(unittest.TestCase):
    def test_requests_zip_extraction_does_not_reuse_predictable_file(self):
        from requests.utils import extract_zipped_paths
        with tempfile.TemporaryDirectory(prefix='rf-dependency-') as directory:
            root = Path(directory)
            archive = root / 'fixture.zip'
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr('package/fixture.pem', b'expected-fixture-content')
            predictable = root / 'fixture.pem'
            predictable.write_bytes(b'untrusted-fixture-content')
            with patch.object(tempfile, 'tempdir', directory):
                extracted = Path(extract_zipped_paths(str(archive / 'package' / 'fixture.pem')))
            self.assertEqual(extracted.read_bytes(), b'expected-fixture-content')
            self.assertNotEqual(extracted, predictable)
            self.assertEqual(predictable.read_bytes(), b'untrusted-fixture-content')


if __name__ == '__main__':
    unittest.main()
