#!/usr/bin/env python3
#
# Copyright (c) 2016, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

import hashlib

from facebook.eden.ttypes import EdenError
from .lib import testcase


@testcase.eden_repo_test
class ThriftTest:
    def populate_repo(self):
        self.repo.write_file('hello', 'hola\n')
        self.repo.write_file('adir/file', 'foo!\n')
        self.repo.symlink('slink', 'hello')
        self.repo.commit('Initial commit.')

    def setUp(self):
        super().setUp()
        self.client = self.get_thrift_client()
        self.client.open()

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_list_mounts(self):
        mounts = self.client.listMounts()
        self.assertEqual(1, len(mounts))

        mount = mounts[0]
        self.assertEqual(self.mount, mount.mountPoint)
        # Currently, edenClientPath is not set.
        self.assertEqual('', mount.edenClientPath)

    def test_get_sha1(self):
        expected_sha1_for_hello = hashlib.sha1(b'hola\n').digest()
        self.assertEqual(expected_sha1_for_hello,
                         self.client.getSHA1(self.mount, 'hello'))

        expected_sha1_for_adir_file = hashlib.sha1(b'foo!\n').digest()
        self.assertEqual(expected_sha1_for_adir_file,
                         self.client.getSHA1(self.mount, 'adir/file'))

    def test_get_sha1_throws_for_empty_string(self):
        def try_empty_string_for_path():
            self.client.getSHA1(self.mount, '')

        self.assertRaisesRegexp(EdenError, 'path cannot be the empty string',
                                try_empty_string_for_path)

    def test_get_sha1_throws_for_directory(self):
        def try_directory():
            self.client.getSHA1(self.mount, 'adir')

        self.assertRaisesRegexp(EdenError,
                                'Found a directory instead of a file: adir',
                                try_directory)

    def test_get_sha1_throws_for_non_existent_file(self):
        def try_non_existent_file():
            self.client.getSHA1(self.mount, 'i_do_not_exist')

        self.assertRaisesRegexp(EdenError,
                                'No such file or directory: i_do_not_exist',
                                try_non_existent_file)

    def test_get_sha1_throws_for_symlink(self):
        '''Fails because caller should resolve the symlink themselves.'''
        def try_symlink():
            self.client.getSHA1(self.mount, 'slink')

        self.assertRaisesRegexp(EdenError, 'Not an ordinary file: slink',
                                try_symlink)
