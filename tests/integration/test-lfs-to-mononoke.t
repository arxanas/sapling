  $ CACHEDIR=$PWD/cachepath
  $ . $TESTDIR/library.sh

# setup config repo

  $ REPOTYPE="blob:files"
  $ export LFS_THRESHOLD="1000"
  $ setup_common_config $REPOTYPE
  $ cd $TESTTMP

# Test Scenario (see more in test-lfs.t):
# 1. Setup server hg nolfs repo and make several commits to it
# 2. Blobimport hg nolfs to mononoke (blob:files).
#   2.a Motivation: Blobimport for now does not support import of lfs hg repos. (Error with RevlogRepo parsing).
#       So we need to blobimport hg repo without lsf extention.
#   2.b Motivation: For blob:files storage, is because we need to run Mononoke and Mononoke API server.
#       We cannot have 2 processes for 1 RocksDB repo, as RocksDb does not allows us to do that.
#   2.c Still Mononoke config is blobimported to Rocks DB. As Api server and Mononoke server are using them separately.
# 3. Setup Mononoke. Introduce LFS_THRESHOLD into Mononoke server config.
# 4. Setup Mononoke API server.
# 5. Clone hg nolfs repo to lfs client hg repo. Setup small threshold for large file size.
# 6. Hg push from hg client repo.
# 6.1 Hg push renamed file.
# 7. Hg pull from hg client repo.


# 1. Setup nolfs hg repo, create several commit to it
  $ hginit_treemanifest repo-hg-nolfs
  $ cd repo-hg-nolfs
  $ setup_hg_server

# Commit small file
  $ echo s > smallfile
  $ hg commit -Aqm "add small file"

  $ hg bookmark master_bookmark -r tip

  $ cd ..

# 2. Blobimport hg nolfs to mononoke (blob:files).
#   2.a Motivation: Blobimport for now does not support import of lfs hg repos. (Error with RevlogRepo parsing).
#       So we need to blobimport hg repo without lsf extention.
#   2.b Motivation: For blob:files storage, is because we need to run Mononoke and Mononoke API server.
#       We cannot have 2 processes for 1 RocksDB repo, as RocksDb does not allows us to do that.
#   2.c Still Mononoke config is blobimported to Rocks DB. As Api server and Mononoke server are using them separately.
  $ blobimport files repo-hg-nolfs/.hg repo

# 3. Setup Mononoke. Introduce LFS_THRESHOLD into Mononoke server config.
  $ mononoke
  $ wait_for_mononoke $TESTTMP/repo

# 4. Setup Mononoke API server.
  $ no_ssl_apiserver --http-host "127.0.0.1" --http-port $(get_free_socket)
  $ wait_for_apiserver --no-ssl

# 5. Clone hg nolfs repo to lfs client hg repo. Setup small threshold for large file size.
  $ hgclone_treemanifest ssh://user@dummy/repo-hg-nolfs repo-hg-lfs --noupdate --config extensions.remotenames=
  $ cd repo-hg-lfs
  $ setup_hg_client

  $ cat >> .hg/hgrc <<EOF
  > [extensions]
  > pushrebase =
  > remotenames =
  > lfs=
  > [lfs]
  > threshold=1000B
  > usercache=$TESTTMP/lfs-cache
  > url=$APISERVER/repo
  > EOF

# get smallfile
  $ hgmn pull -q
  devel-warn: applied empty changegroup at* (glob)
  $ hgmn update -r master_bookmark -q

# 6. Hg push from hg client repo.
  $ LONG=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC
  $ echo $LONG > lfs-largefile
  $ echo $LONG"for-rename" > lfs-largefile-for-rename
  $ hg commit -Aqm "add lfs-large files"
  $ hgmn push -r . --to master_bookmark -v
  remote: * DEBG Session with Mononoke started with uuid: * (glob)
  pushing rev cbf96639d87c to destination ssh://user@dummy/repo bookmark master_bookmark
  searching for changes
  lfs: need to transfer 2 objects (2.94 KB)
  lfs: uploading f11e77c257047a398492d8d6cb9f6acf3aa7c4384bb23080b43546053e183e4b (1.47 KB)
  lfs: processed: f11e77c257047a398492d8d6cb9f6acf3aa7c4384bb23080b43546053e183e4b
  lfs: uploading 8e861bc81e64491883d375bf97e9b5dbe4626f8651483cfa9c95db0e32da4a00 (1.48 KB)
  lfs: processed: 8e861bc81e64491883d375bf97e9b5dbe4626f8651483cfa9c95db0e32da4a00
  validated revset for rebase
  1 changesets found
  uncompressed size of bundle content:
       231 (changelog)
       282  lfs-largefile
       293  lfs-largefile-for-rename
  adding changesets
  adding manifests
  adding file changes
  added 0 changesets with 0 changes to 0 files
  server ignored bookmark master_bookmark update

# 6.1 Rename file
  $ hg mv lfs-largefile-for-rename lfs-largefile-renamed
  $ hg commit -Aqm "rename"
  $ hgmn push -r . --to master_bookmark -v
  remote: * DEBG Session with Mononoke started with uuid: * (glob)
  pushing rev 5ff46b53dca4 to destination ssh://user@dummy/repo bookmark master_bookmark
  searching for changes
  lfs: uploading 8e861bc81e64491883d375bf97e9b5dbe4626f8651483cfa9c95db0e32da4a00 (1.48 KB)
  lfs: processed: 8e861bc81e64491883d375bf97e9b5dbe4626f8651483cfa9c95db0e32da4a00
  validated revset for rebase
  1 changesets found
  uncompressed size of bundle content:
       226 (changelog)
       379  lfs-largefile-renamed
  adding changesets
  adding manifests
  adding file changes
  added 0 changesets with 0 changes to 0 files
  server ignored bookmark master_bookmark update


  $ cd ..
7. Hg pull from hg client repo.
  $ hgclone_treemanifest ssh://user@dummy/repo-hg-nolfs repo-hg-lfs2 --noupdate --config extensions.remotenames=
  $ cd repo-hg-lfs2
  $ setup_hg_client

  $ hgmn pull -v
  pulling from ssh://user@dummy/repo
  remote: * DEBG Session with Mononoke started with uuid: * (glob)
  searching for changes
  all local heads known remotely
  adding changesets
  adding manifests
  adding file changes
  added 2 changesets with 0 changes to 0 files
  adding remote bookmark master_bookmark
  new changesets cbf96639d87c:5ff46b53dca4
  (run 'hg update' to get a working copy)

  $ hgmn update -r master_bookmark -v
  remote: * DEBG Session with Mononoke started with uuid: * (glob)
  resolving manifests
  getting lfs-largefile
  getting lfs-largefile-renamed
  getting smallfile
  calling hook update.prefetch: hgext.remotefilelog.wcpprefetch
  3 files updated, 0 files merged, 0 files removed, 0 files unresolved
  (activating bookmark master_bookmark)

  $ cat lfs-largefile
  AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC

  $ cat lfs-largefile-renamed
  AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCfor-rename

  $ hgmn st --change . -C
  remote: * DEBG Session with Mononoke started with uuid: * (glob)
  A lfs-largefile-renamed
    lfs-largefile-for-rename
  R lfs-largefile-for-rename
