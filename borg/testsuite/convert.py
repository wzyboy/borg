from binascii import hexlify
import os
import pytest
import shutil
import tempfile

import attic.repository
import attic.key
import attic.helpers

from ..helpers import IntegrityError, get_keys_dir
from ..repository import Repository, MAGIC
from ..key import KeyfileKey, KeyfileNotFoundError
from . import BaseTestCase

class NotImplementedException(Exception):
    pass

class ConversionTestCase(BaseTestCase):

    class MockArgs:
        def __init__(self, path):
            self.repository = attic.helpers.Location(path)

    def open(self, path, repo_type  = Repository, create=False):
        return repo_type(os.path.join(path, 'repository'), create = create)

    def setUp(self):
        self.tmppath = tempfile.mkdtemp()
        self.attic_repo = self.open(self.tmppath,
                                    repo_type = attic.repository.Repository,
                                    create = True)
        # throw some stuff in that repo, copied from `RepositoryTestCase.test1`_
        for x in range(100):
            self.attic_repo.put(('%-32d' % x).encode('ascii'), b'SOMEDATA')

        # we use the repo dir for the created keyfile, because we do
        # not want to clutter existing keyfiles
        os.environ['ATTIC_KEYS_DIR'] = self.tmppath

        # we use the same directory for the converted files, which
        # will clutter the previously created one, which we don't care
        # about anyways. in real runs, the original key will be retained.
        os.environ['BORG_KEYS_DIR'] = self.tmppath
        os.environ['ATTIC_PASSPHRASE'] = 'test'
        self.key = attic.key.KeyfileKey.create(self.attic_repo, self.MockArgs(self.tmppath))
        self.attic_repo.close()

    def tearDown(self):
        shutil.rmtree(self.tmppath)

    def test_convert(self):
        self.repository = self.open(self.tmppath)
        # check should fail because of magic number
        print("this will show an error, it is expected")
        assert not self.repository.check() # can't check raises() because check() handles the error
        self.repository.close()
        print("opening attic repository with borg and converting")
        self.open(self.tmppath, repo_type = AtticRepositoryConverter).convert()
        # check that the new keyfile is alright
        keyfile = os.path.join(get_keys_dir(),
                               os.path.basename(self.key.path))
        with open(keyfile, 'r') as f:
            assert f.read().startswith(KeyfileKey.FILE_ID)
        self.repository = self.open(self.tmppath)
        assert self.repository.check()
        self.repository.close()

class AtticRepositoryConverter(Repository):
    def convert(self):
        '''convert an attic repository to a borg repository

        those are the files that need to be converted here, from most
        important to least important: segments, key files, and various
        caches, the latter being optional, as they will be rebuilt if
        missing.'''
        print("reading segments from attic repository using borg")
        segments = [ filename for i, filename in self.io.segment_iterator() ]
        try:
            keyfile = self.find_attic_keyfile()
        except KeyfileNotFoundError:
            print("no key file found for repository")
        else:
            self.convert_keyfiles(keyfile)
        self.close()
        self.convert_segments(segments)
        with pytest.raises(NotImplementedException):
            self.convert_cache()

    def convert_segments(self, segments):
        '''convert repository segments from attic to borg

        replacement pattern is `s/ATTICSEG/BORG_SEG/` in files in
        `$ATTIC_REPO/data/**`.

        luckily the segment length didn't change so we can just
        replace the 8 first bytes of all regular files in there.'''
        for filename in segments:
            print("converting segment %s in place" % filename)
            with open(filename, 'r+b') as segment:
                segment.seek(0)
                segment.write(MAGIC)

    def find_attic_keyfile(self):
        '''find the attic keyfiles

        the keyfiles are loaded by `KeyfileKey.find_key_file()`. that
        finds the keys with the right identifier for the repo

        this is expected to look into $HOME/.attic/keys or
        $ATTIC_KEYS_DIR for key files matching the given Borg
        repository.

        it is expected to raise an exception (KeyfileNotFoundError) if
        no key is found. whether that exception is from Borg or Attic
        is unclear.

        this is split in a separate function in case we want to use
        the attic code here directly, instead of our local
        implementation.'''
        return AtticKeyfileKey.find_key_file(self)

    def convert_keyfiles(self, keyfile):

        '''convert key files from attic to borg

        replacement pattern is `s/ATTIC KEY/BORG_KEY/` in
        `get_keys_dir()`, that is `$ATTIC_KEYS_DIR` or
        `$HOME/.attic/keys`, and moved to `$BORG_KEYS_DIR` or
        `$HOME/.borg/keys`.

        no need to decrypt to convert. we need to rewrite the whole
        key file because magic number length changed, but that's not a
        problem because the keyfiles are small (compared to, say,
        all the segments).'''
        print("converting keyfile %s" % keyfile)
        with open(keyfile, 'r') as f:
            data = f.read()
        data = data.replace(AtticKeyfileKey.FILE_ID,
                            KeyfileKey.FILE_ID,
                            1)
        keyfile = os.path.join(get_keys_dir(),
                               os.path.basename(keyfile))
        print("writing borg keyfile to %s" % keyfile)
        with open(keyfile, 'w') as f:
            f.write(data)
        with open(keyfile, 'r') as f:
            data = f.read()
        assert data.startswith(KeyfileKey.FILE_ID)

    def convert_cache(self):
        '''convert caches from attic to borg

        those are all hash indexes, so we need to
        `s/ATTICIDX/BORG_IDX/` in a few locations:
        
        * the repository index (in `$ATTIC_REPO/index.%d`, where `%d`
          is the `Repository.get_index_transaction_id()`), which we
          should probably update, with a lock, see
          `Repository.open()`, which i'm not sure we should use
          because it may write data on `Repository.close()`...

        * the `files` and `chunks` cache (in
          `$HOME/.cache/attic/<repoid>/`), which we could just drop,
          but if we'd want to convert, we could open it with the
          `Cache.open()`, edit in place and then `Cache.close()` to
          make sure we have locking right
        '''
        raise NotImplementedException('not implemented')

class AtticKeyfileKey(KeyfileKey):
    '''backwards compatible Attick key file parser'''
    FILE_ID = 'ATTIC KEY'

    # verbatim copy from attic
    @staticmethod
    def get_keys_dir():
        """Determine where to repository keys and cache"""
        return os.environ.get('ATTIC_KEYS_DIR',
                              os.path.join(os.path.expanduser('~'), '.attic', 'keys'))

    @classmethod
    def find_key_file(cls, repository):
        '''copy of attic's `find_key_file`_

        this has two small modifications:

        1. it uses the above `get_keys_dir`_ instead of the global one,
           assumed to be borg's

        2. it uses `repository.path`_ instead of
           `repository._location.canonical_path`_ because we can't
           assume the repository has been opened by the archiver yet
        '''
        get_keys_dir = cls.get_keys_dir
        id = hexlify(repository.id).decode('ascii')
        keys_dir = get_keys_dir()
        for name in os.listdir(keys_dir):
            filename = os.path.join(keys_dir, name)
            with open(filename, 'r') as fd:
                line = fd.readline().strip()
                if line and line.startswith(cls.FILE_ID) and line[10:] == id:
                    return filename
        raise KeyfileNotFoundError(repository.path, get_keys_dir())
