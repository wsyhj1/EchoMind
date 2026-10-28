import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy.backup import COMPONENTS,pack_components,verify_backup,restore_drill,main


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        for name in COMPONENTS:
            path=self.root/'runtime'/name/'nested'
            path.mkdir(parents=True)
            (path/'data').write_bytes(name.encode()+b'\x00\xff')

    def tearDown(self):
        self.directory.cleanup()

    def test_archive_integrity_and_restore_without_overwriting_live_or_existing_target(self):
        backup=self.root/'backup'
        manifest=pack_components(self.root/'runtime',backup)
        self.assertFalse(manifest['environment_file_included'])
        self.assertTrue(verify_backup(backup)['uncompressed_bytes'])
        allowed=self.root/'restore-drills'
        target=restore_drill(backup,allowed/'new',allowed)
        for name in COMPONENTS:
            self.assertEqual((target/name/'nested/data').read_bytes(),name.encode()+b'\x00\xff')
        with self.assertRaises(FileExistsError):restore_drill(backup,target,allowed)
        with self.assertRaises(ValueError):restore_drill(backup,self.root/'runtime',allowed)

    def test_tampered_archive_is_rejected(self):
        backup=self.root/'backup'
        pack_components(self.root/'runtime',backup)
        with (backup/'databases.tar.gz').open('ab') as stream:stream.write(b'bad')
        with self.assertRaises(ValueError):verify_backup(backup)

    def test_path_traversal_and_symlink_cannot_be_extracted_even_with_matching_digest(self):
        for name,link in [('postgres/../../escape',False),('postgres/symlink',True)]:
            backup=self.root/('bad-link' if link else 'bad-path')
            backup.mkdir()
            archive=backup/'databases.tar.gz'
            with tarfile.open(archive,'w:gz') as bundle:
                member=tarfile.TarInfo(name)
                if link:
                    member.type=tarfile.SYMTYPE
                    member.linkname='../outside'
                    bundle.addfile(member)
                else:
                    member.size=1
                    bundle.addfile(member,io.BytesIO(b'x'))
            (backup/'manifest.json').write_text(json.dumps({'format':1,'archive':archive.name,'components':list(COMPONENTS),
                'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}))
            with self.assertRaises(ValueError):restore_drill(backup,self.root/'drill/new',self.root/'drill')
            self.assertFalse((self.root/'escape').exists())

    def test_backup_dry_run_never_stops_services(self):
        with patch('sys.argv',['backup','backup']),patch('deploy.backup.compose') as compose,patch('deploy.backup.apps_stopped') as stop:
            main()
        compose.assert_not_called()
        stop.assert_not_called()
