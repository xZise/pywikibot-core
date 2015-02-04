# -*- coding: utf-8  -*-
"""
Mechanics to slow down wiki read and/or write rate.

To avoid race conditions it's using a sqlite database and writes for each site
the next time a request can be issued.
"""
#
# (C) Pywikibot team, 2008-2015
#
# Distributed under the terms of the MIT license.
#

import sqlite3
import threading
import time

import pywikibot
from pywikibot import config
from pywikibot.tools import deprecated_args, deprecated

_logger = "wiki.throttle"


class Throttle(object):

    """Control rate of access to wiki server.

    Calling this object blocks the calling thread until at least 'delay'
    seconds have passed since the previous call.

    Each Site initiates one Throttle object (site.throttle) to control the
    rate of access.

    """

    @deprecated_args(mindelay='read_delay', maxdelay=None,
                     writedelay='write_delay', multiplydelay=None)
    def __init__(self, site, read_delay=None, write_delay=None):
        """Constructor."""
        self.lock = threading.RLock()
        self.mysite = str(site)
        self._database = sqlite3.connect(config.datafilepath('throttle.db'))
        self._database.create_function('now', 0, time.time)
        with self._database:
            cursor = self._database.cursor()
            cursor.execute('SELECT name FROM sqlite_master WHERE type="table" AND name="throttle"')
            if not cursor.fetchall():
                cursor.execute('CREATE TABLE "throttle" ('
                               'site TEXT, '
                               'expiry INTEGER, '
                               'read INT CHECK (read IS 0 OR read IS 1), '
                               'UNIQUE (site, read))')

        self._last_read = 0
        self._last_write = 0

        self.delay = 0
        self.set_delays(read_delay, write_delay)

    @property
    def last_read(self):
        """Last time a read request has been send to the server."""
        return self._last_read

    @property
    def last_write(self):
        """Last time a write request has been send to the server."""
        return self._last_write

    @deprecated
    def checkMultiplicity(self):
        """DEPRECATED: This method call is not necessary anymore."""
        pass

    @deprecated('Throttle.set_delays')
    def setDelays(self, delay=None, writedelay=None, absolute=False):
        """Set the nominal delays in seconds. Defaults to config values."""
        self.set_delays(delay, writedelay)

    def set_delays(self, read_delay=None, write_delay=None):
        """Set the nominal delays in seconds. Defaults to config values."""
        self.lock.acquire()
        try:
            if read_delay is None:
                read_delay = config.minthrottle
            if write_delay is None:
                write_delay = config.put_throttle
            self._read_delay = read_delay
            self._write_delay = write_delay
            # Start the delay count now, not at the next check
            self._last_read = self._last_write = time.time()
        finally:
            self.lock.release()

    @deprecated('Throttle.get_delay()')
    def getDelay(self, write=False):
        """DEPRECATED: Return waiting time in seconds."""
        return self.get_delay(write=write)

    @deprecated('Throttle.get_delay()')
    def waittime(self, write=False):
        """DEPRECATED: Return waiting time in seconds."""
        return self.get_delay(write=write)

    @deprecated
    def drop(self):
        """DEPRECATED: This method call is not necessary anymore."""
        pass

    def get_delay(self, write=False):
        """Return the actual delay, accounting for multiple processes.

        This value is the maximum wait between reads/writes, not taking
        account of how much time has elapsed since the last access.

        """
        self.lock.acquire()
        try:
            delay = self._write_delay if write else self._read_delay
            read = 0 if write else 1
            with self._database:
                cursor = self._database.cursor()
                cursor.execute('BEGIN TRANSACTION')
                # make sure there is an entry
                cursor.execute('INSERT OR IGNORE INTO throttle '
                               '(site, expiry, read) '
                               'VALUES (?, now(), ?) ',
                               (self.mysite, read))
                cursor.execute('UPDATE throttle '
                               'SET expiry=max(now(), expiry) + ? '
                               'WHERE site=? AND read=?',
                               (delay, self.mysite, read))
                # remove all expired entries
                cursor.execute('DELETE FROM throttle '
                               'WHERE expiry < now() AND site != ?',
                               (self.mysite, ))
                cursor.execute('SELECT expiry FROM throttle '
                               'WHERE site = ? AND read = ?',
                               (self.mysite, read))
                result = cursor.fetchall()
            assert(len(result) == 1)
            assert(len(result[0]) == 1)
            # It reads the expiry, this one can execute immediately
            return max(result[0][0] - delay - time.time(), 0.0)
        finally:
            self.lock.release()

    def wait(self, seconds):
        """Wait for seconds seconds.

        Announce the delay if it exceeds a preset limit.

        """
        if seconds <= 0:
            return

        message = (u"Sleeping for %(seconds).1f seconds, %(now)s" % {
            'seconds': seconds,
            'now': time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime())
        })
        if seconds > config.noisysleep:
            pywikibot.output(message)
        else:
            pywikibot.log(message)

        time.sleep(seconds)

    def __call__(self, requestsize=1, write=False):
        """Block the calling program if the throttle time has not expired.

        Parameter requestsize is the number of Pages to be read/written;
        multiply delay time by an appropriate factor.

        Because this seizes the throttle lock, it will prevent any other
        thread from writing to the same site until the wait expires.

        """
        self.lock.acquire()
        try:
            wait = self.get_delay(write=write)
            self.wait(wait)

            if write:
                self._last_write = time.time()
            else:
                self._last_read = time.time()
        finally:
            self.lock.release()

    def lag(self, lagtime):
        """Seize the throttle lock due to server lag.

        This will prevent any thread from accessing this site.

        """
        started = time.time()
        self.lock.acquire()
        try:
            # start at 1/2 the current server lag time
            # wait at least 5 seconds but not more than 120 seconds
            delay = min(max(5, lagtime // 2), 120)
            # account for any time we waited while acquiring the lock
            wait = delay - (time.time() - started)

            self.wait(wait)

        finally:
            self.lock.release()
