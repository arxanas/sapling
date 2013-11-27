# db.py
#
# Copyright 2013 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

#CREATE TABLE revs(
#id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
#path VARCHAR(256) NOT NULL,
#chunk INT UNSIGNED NOT NULL,
#chunkcount INT UNSIGNED NOT NULL,
#linkrev INT UNSIGNED NOT NULL,
#entry BINARY(64) NOT NULL,
#data0 CHAR(1) NOT NULL,
#data1 LONGBLOB NOT NULL,
#createdtime DATETIME NOT NULL,
#INDEX linkrev_index (linkrev)
#);

#CREATE TABLE headsbookmarks(
#id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
#node char(40) NOT NULL,
#name VARCHAR(256) UNIQUE
#);

from mercurial.node import bin, hex, nullid, nullrev
from mercurial.i18n import _
from mercurial.extensions import wrapfunction, wrapcommand
from mercurial import changelog, error, cmdutil, revlog, localrepo, transaction
from mercurial import wireproto, bookmarks, repair, commands, hg, mdiff, phases
import MySQLdb, struct, time, Queue, threading
from MySQLdb import cursors

cmdtable = {}
command = cmdutil.command(cmdtable)
testedwith = 'internal'

bookmarklock = 'bookmark_lock'
commitlock = 'commit_lock'

maxrecordsize = 1024 * 1024

class CorruptionException(Exception):
    pass

def uisetup(ui):
    wrapcommand(commands.table, 'pull', pull)

    wrapfunction(revlog.revlog, '_addrevision', addrevision)
    wrapfunction(revlog.revlog, 'addgroup', addgroup)
    wrapfunction(bookmarks.bmstore, 'write', bookmarkwrite)

    wrapfunction(wireproto, 'unbundle', unbundle)
    wireproto.commands['unbundle'] = (wireproto.unbundle, 'heads')

def reposetup(ui, repo):
    if repo.ui.configbool("hgsql", "enabled"):
        ui.setconfig("hooks", "pretxnchangegroup.remotefilelog", pretxnchangegroup)
        ui.setconfig("hooks", "pretxncommit.remotefilelog", pretxnchangegroup)

        wraprepo(repo)

        if not hasattr(ui, 'disableinitialsync') or not ui.disableinitialsync:
            repo.sqlconnect()
            try:
                repo.syncdb()
            finally:
                repo.sqlclose()

# Handle incoming commits
def unbundle(orig, repo, proto, heads):
    repo.sqlconnect()
    repo.sqllock(commitlock)
    try:
        repo.syncdb()
        return orig(repo, proto, heads)
    finally:
        try:
            repo.sqlunlock(commitlock)
            repo.sqlclose()
        except _mysql_exceptions.ProgrammingError, ex:
            # ignore sql exceptions, so real exceptions propagate up
            pass

def pull(orig, ui, repo, source="default", **opts):
    repo.sqlconnect()
    repo.sqllock(commitlock)
    try:
        repo.syncdb()
        return orig(ui, repo, source, **opts)
    finally:
        try:
            repo.sqlunlock(commitlock)
            repo.sqlclose()
        except _mysql_exceptions.ProgrammingError, ex:
            # ignore sql exceptions, so real exceptions propagate up
            pass

def wraprepo(repo):
    class sqllocalrepo(repo.__class__):
        def sqlconnect(self):
            if self.sqlconn:
                raise Exception("SQL connection already open")
            if self.sqlcursor:
                raise Exception("SQL cursor already open without connection")
            self.sqlconn = MySQLdb.connect(**dbargs)
            self.sqlconn.autocommit(False)
            self.sqlconn.query("SET SESSION wait_timeout=300;")
            self.sqlcursor = self.sqlconn.cursor()

        def sqlclose(self):
            self.sqlcursor.close()
            self.sqlconn.close()
            self.sqlcursor = None
            self.sqlconn = None

        def sqllock(self, name, timeout=60):
            name = self.sqlconn.escape_string(name)
            # cast to int to prevent passing bad sql data
            timeout = int(timeout)
            self.sqlconn.query("SELECT GET_LOCK('%s', %s)" % (name, timeout))
            result = self.sqlconn.store_result().fetch_row()[0][0]
            if result != 1:
                raise Exception("unable to obtain %s lock" % name)

        def sqlunlock(self, name):
            name = self.sqlconn.escape_string(name)
            self.sqlconn.query("SELECT RELEASE_LOCK('%s')" % (name,))
            self.sqlconn.store_result().fetch_row()

        def transaction(self, *args, **kwargs):
            tr = super(sqllocalrepo, self).transaction(*args, **kwargs)

            def transactionclose(orig):
                result = orig()
                if tr.count == 0:
                    del self.pendingrevs[:]
                return result

            wrapfunction(tr, "_abort", transactionclose)
            wrapfunction(tr, "close", transactionclose)
            tr.repo = self
            return tr

        def needsync(self):
            # Check latest db rev number
            self.sqlcursor.execute("SELECT * FROM headsbookmarks")
            sqlheads = set()
            sqlbookmarks = {}
            for _, node, name in self.sqlcursor:
                if not name:
                    sqlheads.add(bin(node))
                else:
                    sqlbookmarks[name] = bin(node)

            heads = self.heads()
            bookmarks = self._bookmarks

            if (not sqlheads or len(heads) != len(sqlheads) or
                len(bookmarks) != len(sqlbookmarks)):
                return True

            for head in sqlheads:
                if head not in heads:
                    return True

            for bookmark in sqlbookmarks:
                if (not bookmark in bookmarks or
                    bookmarks[bookmark] != sqlbookmarks[bookmark]):
                    return True

            return False

        def syncdb(self):
            if not self.needsync():
                return

            ui = self.ui
            ui.debug("syncing with mysql\n")

            lock = None
            try:
                lock = self.lock(wait=False)
            except error.LockHeld:
                # If the lock is held, someone else is doing the pull for us.
                # Wait until they are done.
                # TODO: I don't think this is actually true...
                lock = self.lock()
                lock.release()
                return

            transaction = self.transaction("syncdb")

            try:
                # Refresh the changelog now that we have the lock
                fetchstart = len(self.changelog)

                queue = Queue.Queue()
                abort = threading.Event()

                t = threading.Thread(target=self.fetchthread, args=(queue, abort, fetchstart))
                t.setDaemon(True)
                try:
                    t.start()
                    addentries(self, queue, transaction)
                finally:
                    abort.set()

                phases.advanceboundary(self, phases.public, self.heads())

                transaction.close()
            finally:
                transaction.release()
                lock.release()

            self.invalidate()
            self.invalidatedirstate()
            self._filecache.clear()

            self.disablesync = True
            try:
                self.sqlcursor.execute("SELECT * FROM headsbookmarks WHERE name IS NOT NULL")
                bm = self._bookmarks
                bm.clear()
                for _, node, name in self.sqlcursor:
                    node = bin(node)
                    if node in self:
                        bm[name] = node
                bm.write()
            finally:
                self.disablesync = False

        def fetchthread(self, queue, abort, fetchstart):
            ui = self.ui
            clrev = fetchstart
            chunksize = 1000
            while True:
                if abort.isSet():
                    break

                self.sqlcursor.execute("""SELECT * FROM revs WHERE linkrev > %s AND
                    linkrev < %s ORDER BY linkrev ASC""", (clrev - 1, clrev + chunksize))

                # put split chunks back together
                groupedrevdata = {}
                for revdata in self.sqlcursor:
                    name = revdata[1]
                    chunk = revdata[2]
                    linkrev = revdata[4]
                    groupedrevdata.setdefault((name, linkrev), {})[chunk] = revdata

                if not groupedrevdata:
                    break

                fullrevisions = []
                for chunks in groupedrevdata.itervalues():
                    chunkcount = chunks[0][3]
                    if chunkcount == 1:
                        fullrevisions.append(chunks[0])
                    elif chunkcount == len(chunks):
                        fullchunk = list(chunks[0])
                        data1 = ""
                        for i in range(0, chunkcount):
                            data1 += chunks[i][7]
                        fullchunk[7] = data1
                        fullrevisions.append(tuple(fullchunk))
                    else:
                        raise Exception("missing revision chunk - expected %s got %s" %
                            (chunkcount, len(chunks)))

                fullrevisions = sorted(fullrevisions, key=lambda revdata: revdata[4])
                for revdata in fullrevisions:
                    queue.put(revdata)

                clrev += chunksize
                if (clrev - fetchstart) % 5000 == 0:
                    ui.debug("Queued %s\n" % (clrev))

            queue.put(False)

    repo.sqlconn = None
    repo.sqlcursor = None
    repo.disablesync = False
    repo.pendingrevs = []
    repo.__class__ = sqllocalrepo

class bufferedopener(object):
    def __init__(self, opener, path, mode):
        self.opener = opener
        self.path = path
        self.mode = mode
        self.buffer = []
        self.closed = False
        self.lock = threading.Lock()

    def write(self, value):
        self.lock.acquire()
        self.buffer.append(value)
        self.lock.release()

    def flush(self):
        self.lock.acquire()
        buffer = self.buffer
        self.buffer = []
        self.lock.release()
        
        if buffer:
            fp = self.opener(self.path, self.mode)
            fp.write(''.join(buffer))
            fp.close()

    def close(self):
        self.flush()
        self.closed = True

def addentries(repo, queue, transaction):
    opener = repo.sopener

    revlogs = {}
    def writeentry(revdata):
        _, path, chunk, chunkcount, link, entry, data0, data1, createdtime = revdata
        revlog = revlogs.get(path)
        if not revlog:
            revlog = EntryRevlog(opener, path)
            revlogs[path] = revlog

        if not hasattr(revlog, 'ifh') or revlog.ifh.closed:
            dfh = None
            if not revlog._inline:
                dfh = bufferedopener(opener, revlog.datafile, "a")
            ifh = bufferedopener(opener, revlog.indexfile, "a+")
            revlog.ifh = ifh
            revlog.dfh = dfh

        revlog.addentry(transaction, revlog.ifh, revlog.dfh, entry,
                        data0, data1)
        revlog.dirty = True

    clrev = len(repo)
    startclrev = clrev
    start = time.time()
    last = start
    estimatedtotal = 600000
    leftover = None
    exit = False
    while not exit:
        currentlinkrev = -1

        revisions = []
        if leftover:
            revisions.append(leftover)
            leftover = None

        while True:
            revdata = queue.get()
            if not revdata:
                exit = True
                break

            linkrev = revdata[4]
            if currentlinkrev == -1:
                currentlinkrev = linkrev
            if linkrev == currentlinkrev:
                revisions.append(revdata)
            elif linkrev < currentlinkrev:
                raise Exception("SQL data is not in linkrev order")
            else:
                leftover = revdata
                currentlinkrev = linkrev
                break

        if not revisions:
            continue

        # Write filelogs first, then manifests, then changelogs,
        # just like Mercurial does normally.
        changelog = []
        manifest = []
        for revdata in revisions:
            name = revdata[1]
            if name == "00changelog.i":
                changelog.append(revdata)
            elif name == "00manifest.i":
                manifest.append(revdata)
            else:
                writeentry(revdata)

        for revdata in manifest:
            writeentry(revdata)

        for revdata in changelog:
            writeentry(revdata)

        clrev += 1

        if clrev % 5000 == 0:
            duration = time.time() - start
            perrev = (time.time() - last) / 5000
            estimate = perrev * (estimatedtotal - clrev)
            last = time.time()
            print "Added %s (%s revlogs open - estimated remaining %0.0f:%02.0f - %0.0f:%02.0f)" % (clrev, len(revlogs), estimate / 60, estimate % 60, duration / 60, duration % 60)

    total = len(revlogs)
    count = 0
    for revlog in revlogs.itervalues():
        if not revlog.ifh.closed:
            revlog.ifh.flush()
        if revlog.dfh and not revlog.dfh.closed:
            revlog.dfh.flush()
        count += 1
        if count % 5000 == 0:
            print "Flushed %0.0f" % (float(count) / float(total) * 100)

class EntryRevlog(revlog.revlog):
    def addentry(self, transaction, ifh, dfh, entry, data0, data1):
        curr = len(self)
        offset = self.end(curr)

        e = struct.unpack(revlog.indexformatng, entry)
        offsettype, datalen, textlen, base, link, p1r, p2r, node = e
        if curr == 0:
            elist = list(e)
            type = revlog.gettype(offsettype)
            offsettype = revlog.offset_type(0, type)
            elist[0] = offsettype
            e = tuple(elist)

        # Verify that the revlog is in a good state
        if p1r >= curr or p2r >= curr:
            raise CorruptionException("parent revision is not in revlog: %s" % self.indexfile)
        if base > curr:
            raise CorruptionException("base revision is not in revlog: %s" % self.indexfile)

        expectedoffset = revlog.getoffset(offsettype)
        actualoffset = self.end(curr - 1)
        if expectedoffset != 0 and expectedoffset != actualoffset:
            raise CorruptionException("revision offset doesn't match prior length " +
                "(%s offset vs %s length): %s" %
                (expectedoffset, actualoffset, self.indexfile))

        if node not in self.nodemap:
            self.index.insert(-1, e)
            self.nodemap[node] = len(self) - 1

        if not self._inline:
            transaction.add(self.datafile, offset)
            transaction.add(self.indexfile, curr * len(entry))
            if data0:
                dfh.write(data0)
            dfh.write(data1)
            ifh.write(entry)
        else:
            offset += curr * self._io.size
            transaction.add(self.indexfile, offset, curr)
            ifh.write(entry)
            ifh.write(data0)
            ifh.write(data1)
            self.checkinlinesize(transaction, ifh)

class interceptopener(object):
    def __init__(self, fp, onwrite):
        object.__setattr__(self, 'fp', fp)
        object.__setattr__(self, 'onwrite', onwrite)

    def write(self, data):
        self.fp.write(data)
        self.onwrite(data)

    def __getattr__(self, attr):
        return getattr(self.fp, attr)

    def __setattr__(self, attr, value):
        return setattr(self.fp, attr, value)

    def __delattr__(self, attr):
        return delattr(self.fp, attr)

def addrevision(orig, self, node, text, transaction, link, p1, p2,
                cachedelta, ifh, dfh):
    entry = []
    data0 = []
    data1 = []
    def iwrite(data):
        if not entry:
            # sometimes data0 is skipped
            if data0 and not data1:
                data1.append(data0[0])
                del data0[:]
            entry.append(data)
        elif not data0:
            data0.append(data)
        elif not data1:
            data1.append(data)

    def dwrite(data):
        if not data0:
            data0.append(data)
        elif not data1:
            data1.append(data)

    iopener = interceptopener(ifh, iwrite)
    dopener = interceptopener(dfh, dwrite) if dfh else None

    result = orig(self, node, text, transaction, link, p1, p2, cachedelta,
                  iopener, dopener)

    transaction.repo.pendingrevs.append((-1, self.indexfile, link,
        entry[0], data0[0] if data0 else '', data1[0]))

    return result

def addgroup(orig, self, bundle, linkmapper, transaction):
    """
    copy paste of revlog.addgroup, but we ensure that the revisions are added
    in linkrev order.
    """
    # track the base of the current delta log
    content = []
    node = None

    r = len(self)
    end = 0
    if r:
        end = self.end(r - 1)
    ifh = self.opener(self.indexfile, "a+")
    isize = r * self._io.size
    if self._inline:
        transaction.add(self.indexfile, end + isize, r)
        dfh = None
    else:
        transaction.add(self.indexfile, isize, r)
        transaction.add(self.datafile, end)
        dfh = self.opener(self.datafile, "a")

    try:
        # loop through our set of deltas
        chunkdatas = []
        chunkmap = {}

        lastlinkrev = -1
        reorder = False

        chain = None
        while True:
            chunkdata = bundle.deltachunk(chain)
            if not chunkdata:
                break

            node = chunkdata['node']
            cs = chunkdata['cs']
            link = linkmapper(cs)
            if link < lastlinkrev:
                reorder = True
            lastlinkrev = link
            chunkdatas.append((link, chunkdata))
            chunkmap[node] = chunkdata
            chain = node

        if reorder:
            chunkdatas = sorted(chunkdatas)

            fulltexts = {}
            def getfulltext(node):
                if node in fulltexts:
                    return fulltexts[node]
                if node in self.nodemap:
                    return self.revision(node)

                chunkdata = chunkmap[node]
                deltabase = chunkdata['deltabase']
                delta = chunkdata['delta']

                deltachain = []
                currentbase = deltabase
                while True:
                    if currentbase in fulltexts:
                        deltachain.append(fulltexts[currentbase])
                        break
                    elif currentbase in self.nodemap:
                        deltachain.append(self.revision(currentbase))
                        break
                    elif currentbase == nullid:
                        break
                    else:
                        deltachunk = chunkmap[currentbase]
                        currentbase = deltachunk['deltabase']
                        deltachain.append(deltachunk['delta'])

                prevtext = deltachain.pop()
                while deltachain:
                    prevtext = mdiff.patch(prevtext, deltachain.pop())

                fulltext = mdiff.patch(prevtext, delta)
                fulltexts[node] = fulltext
                return fulltext

            reorders = 0
            visited = set()
            prevnode = self.node(len(self) - 1)
            for link, chunkdata in chunkdatas:
                node = chunkdata['node']
                deltabase = chunkdata['deltabase']
                if (not deltabase in self.nodemap and
                    not deltabase in visited):
                    fulltext = getfulltext(node)
                    ptext = getfulltext(prevnode)
                    delta = mdiff.textdiff(ptext, fulltext)

                    chunkdata['delta'] = delta
                    chunkdata['deltabase'] = prevnode
                    reorders += 1

                prevnode = node
                visited.add(node)

        for link, chunkdata in chunkdatas:
            node = chunkdata['node']
            p1 = chunkdata['p1']
            p2 = chunkdata['p2']
            cs = chunkdata['cs']
            deltabase = chunkdata['deltabase']
            delta = chunkdata['delta']

            content.append(node)

            link = linkmapper(cs)
            if node in self.nodemap:
                # this can happen if two branches make the same change
                continue

            for p in (p1, p2):
                if p not in self.nodemap:
                    raise LookupError(p, self.indexfile,
                                      _('unknown parent'))

            if deltabase not in self.nodemap:
                raise LookupError(deltabase, self.indexfile,
                                  _('unknown delta base'))

            baserev = self.rev(deltabase)
            self._addrevision(node, None, transaction, link,
                                      p1, p2, (baserev, delta), ifh, dfh)
            if not dfh and not self._inline:
                # addrevision switched from inline to conventional
                # reopen the index
                ifh.close()
                dfh = self.opener(self.datafile, "a")
                ifh = self.opener(self.indexfile, "a")
    finally:
        if dfh:
            dfh.close()
        ifh.close()

    return content

def pretxnchangegroup(ui, repo, *args, **kwargs):
    if repo.sqlconn == None:
        raise Exception("invalid repo change - only hg push and pull are allowed")

    # Commit to db
    try:
        cursor = repo.sqlcursor
        for revision in repo.pendingrevs:
            _, path, linkrev, entry, data0, data1 = revision

            start = 0
            chunk = 0
            datalen = len(data1)
            chunkcount = datalen / maxrecordsize
            if datalen % maxrecordsize != 0 or datalen == 0:
                chunkcount += 1
            while chunk == 0 or start < len(data1):
                end = min(len(data1), start + maxrecordsize)
                datachunk = data1[start:end]
                cursor.execute("""INSERT INTO revs(path, chunk, chunkcount, linkrev, entry, data0,
                    data1, createdtime) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (path, chunk, chunkcount, linkrev, entry, data0, datachunk,
                     time.strftime('%Y-%m-%d %H:%M:%S')))
                chunk += 1
                start = end

        cursor.execute("""DELETE FROM headsbookmarks WHERE name IS NULL""")

        for head in repo.heads():
            cursor.execute("""INSERT INTO headsbookmarks(node) VALUES(%s)""",
                (hex(head)))

        repo.sqlconn.commit()
    except:
        repo.sqlconn.rollback()
        raise
    finally:
        del repo.pendingrevs[:]

def bookmarkwrite(orig, self):
    repo = self._repo
    if repo.disablesync:
        return orig(self)

    repo.sqlconnect()
    repo.sqllock(bookmarklock)
    try:
        cursor = repo.sqlcursor
        cursor.execute("""DELETE FROM headsbookmarks WHERE name IS NOT NULL""")

        for k, v in self.iteritems():
            cursor.execute("""INSERT INTO headsbookmarks(node, name) VALUES(%s, %s)""",
                (hex(v), k))
        repo.sqlconn.commit()
        return orig(self)
    except:
        repo.sqlconn.rollback()
        raise
    finally:
        repo.sqlunlock(bookmarklock)
        repo.sqlclose()

# recover must be a norepo command because loading the repo fails
commands.norepo += " sqlrecover"

@command('^sqlrecover', [
    ('f', 'force', '', _('strips as far back as necessary'), ''),
    ], _('hg sqlrecover'))
def sqlrecover(ui, *args, **opts):
    """
    Strips commits from the local repo until it is back in sync with the SQL
    server.
    """

    ui.disableinitialsync = True
    repo = hg.repository(ui, ui.environ['PWD'])
    repo.disablesync = True

    if repo.recover():
        ui.status("recovered from incomplete transaction")

    def iscorrupt():
        repo.sqlconnect()
        try:
            repo.syncdb()
        except CorruptionException:
            return True
        finally:
            repo.sqlclose()

        return False

    reposize = len(repo)

    stripsize = 10
    while iscorrupt():
        if reposize > len(repo) + 10000:
            ui.warn("unable to fix repo after stripping 10000 commits (use -f to strip more)")
        striprev = max(0, len(repo) - stripsize)
        nodelist = [repo[striprev].node()]
        repair.strip(ui, repo, nodelist, backup="none", topic="sqlrecover")
        stripsize *= 5

    if len(repo) == 0:
        ui.warn(_("unable to fix repo corruption\n"))
    elif len(repo) == reposize:
        ui.status(_("local repo was not corrupt - no action taken\n"))
    else:
        ui.status(_("local repo now matches SQL\n"))
