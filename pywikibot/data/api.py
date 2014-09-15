# -*- coding: utf-8  -*-
"""Interface to Mediawiki's api.php."""
#
# (C) Pywikibot team, 2007-2014
#
# Distributed under the terms of the MIT license.
#
__version__ = '$Id$'

from collections import MutableMapping
from pywikibot.comms import http
from email.mime.multipart import MIMEMultipart
from email.mime.nonmultipart import MIMENonMultipart
import datetime
import hashlib
import json
import mimetypes
import os
try:
    import cPickle as pickle
except ImportError:
    import pickle
import pprint
import re
import traceback
import time
from distutils.version import LooseVersion as LV

import pywikibot
from pywikibot import config, login
from pywikibot.exceptions import Server504Error, FatalServerError, Error

import sys

if sys.version_info[0] > 2:
    basestring = (str, )
    from urllib.parse import urlencode, unquote
    unicode = str
else:
    from urllib import urlencode, unquote

_logger = "data.api"

lagpattern = re.compile(r"Waiting for [\d.]+: (?P<lag>\d+) seconds? lagged")


class APIError(Error):

    """The wiki site returned an error message."""

    def __init__(self, code, info, **kwargs):
        """Save error dict returned by MW API."""
        self.code = code
        self.info = info
        self.other = kwargs
        self.unicode = unicode(self.__str__())

    def __repr__(self):
        return '{name}("{code}", "{info}", {other})'.format(
            name=self.__class__.__name__, **self.__dict__)

    def __str__(self):
        return "%(code)s: %(info)s" % self.__dict__


class UploadWarning(APIError):

    """Upload failed with a warning message (passed as the argument)."""

    def __init__(self, code, message):
        super(UploadWarning, self).__init__(code, message)

    @property
    def message(self):
        return self.info


class APIMWException(APIError):

    """The API site returned an error about a MediaWiki internal exception."""

    def __init__(self, mediawiki_exception_class_name, info, **kwargs):
        """Save error dict returned by MW API."""
        self.mediawiki_exception_class_name = mediawiki_exception_class_name
        code = 'internal_api_error_' + mediawiki_exception_class_name
        super(APIMWException, self).__init__(code, info, **kwargs)


class TimeoutError(Error):
    pass


class EnableSSLSiteWrapper(object):
    """Wrapper to change the site protocol to https."""

    def __init__(self, site):
        self._site = site

    def __repr__(self):
        return repr(self._site)

    def __eq__(self, other):
        return self._site == other

    def __getattr__(self, attr):
        return getattr(self._site, attr)

    def protocol(self):
        return 'https'


class Request(MutableMapping):

    """A request to a Site's api.php interface.

    Attributes of this object (except for the special parameters listed
    below) get passed as commands to api.php, and can be get or set using
    the dict interface.  All attributes must be strings (or unicode).  Use
    an empty string for parameters that don't require a value. For example,
    Request(action="query", titles="Foo bar", prop="info", redirects="")
    corresponds to the API request
    "api.php?action=query&titles=Foo%20bar&prop=info&redirects"

    This is the lowest-level interface to the API, and can be used for any
    request that a particular site's API supports. See the API documentation
    (https://www.mediawiki.org/wiki/API) and site-specific settings for
    details on what parameters are accepted for each request type.

    Uploading files is a special case: to upload, the parameter "mime" must
    be true, and the parameter "file" must be set equal to a valid
    filename on the local computer, _not_ to the content of the file.

    Returns a dict containing the JSON data returned by the wiki. Normally,
    one of the dict keys will be equal to the value of the 'action'
    parameter.  Errors are caught and raise an APIError exception.

    Example:

    >>> r = Request(site=mysite, action="query", meta="userinfo")
    >>> # This is equivalent to
    >>> # https://{path}/api.php?action=query&meta=userinfo&format=json
    >>> # change a parameter
    >>> r['meta'] = "userinfo|siteinfo"
    >>> # add a new parameter
    >>> r['siprop'] = "namespaces"
    >>> # note that "uiprop" param gets added automatically
    >>> r.params
    {'action': 'query', 'meta': 'userinfo|siteinfo', 'siprop': 'namespaces'}
    >>> data = r.submit()
    >>> type(data)
    <type 'dict'>
    >>> data.keys()
    [u'query']
    >>> data[u'query'].keys()
    [u'userinfo', u'namespaces']

    @param site: The Site to which the request will be submitted. If not
           supplied, uses the user's configured default Site.
    @param mime: If true, send in "multipart/form-data" format (default False)
    @param mime_params: A dictionary of parameter which should only be
           transferred via mime mode. If not None sets mime to True.
    @param max_retries: (optional) Maximum number of times to retry after
           errors, defaults to 25
    @param retry_wait: (optional) Minimum time to wait after an error,
           defaults to 5 seconds (doubles each retry until max of 120 is
           reached)
    @param format: (optional) Defaults to "json"

    """

    def __init__(self, **kwargs):
        """Constructor."""
        try:
            self.site = kwargs.pop("site")
        except KeyError:
            self.site = pywikibot.Site()
        if 'mime_params' in kwargs:
            self.mime_params = kwargs.pop('mime_params')
            # mime may not be different from mime_params
            if 'mime' in kwargs and kwargs.pop('mime') != self.mime:
                raise ValueError('If mime_params is set, mime may not differ '
                                 'from it.')
        else:
            self.mime = kwargs.pop('mime', False)
        self.throttle = kwargs.pop('throttle', True)
        self.max_retries = kwargs.pop("max_retries", pywikibot.config.max_retries)
        self.retry_wait = kwargs.pop("retry_wait", pywikibot.config.retry_wait)
        self.params = {}
        if "action" not in kwargs:
            raise ValueError("'action' specification missing from Request.")
        self.update(**kwargs)
        self._warning_handler = None
        # Actions that imply database updates on the server, used for various
        # things like throttling or skipping actions when we're in simulation
        # mode
        self.write = self.params["action"] in (
            "edit", "move", "rollback", "delete", "undelete",
            "protect", "block", "unblock", "watch", "patrol",
            "import", "userrights", "upload", "emailuser",
            "createaccount", "setnotificationtimestamp",
            "filerevert", "options", "purge", "revisiondelete",
            "wbeditentity", "wbsetlabel", "wbsetdescription",
            "wbsetaliases", "wblinktitles", "wbsetsitelink",
            "wbcreateclaim", "wbremoveclaims", "wbsetclaimvalue",
            "wbsetreference", "wbremovereferences"
        )
        # MediaWiki 1.23 allows assertion for any action,
        # whereas earlier WMF wikis and others used an extension which
        # could only allow assert for action=edit.
        #
        # When we can't easily check whether the extension is loaded,
        # to avoid cyclic recursion in the Pywikibot codebase, assume
        # that it is present, which will cause a API warning emitted
        # to the logging (console) if it is not present, but will not
        # otherwise be a problem.
        # This situation is only tripped when one of the first actions
        # on the site is a write action and the extension isn't installed.
        if ((self.write and LV(self.site.version()) >= LV("1.23")) or
                (self.params['action'] == 'edit' and
                 self.site.has_extension('AssertEdit'))):
            pywikibot.debug(u"Adding user assertion", _logger)
            self.params["assert"] = "user"  # make sure user is logged in

        if (self.site.protocol() == 'http' and (config.use_SSL_always or (
                self.params["action"] == "login" and config.use_SSL_onlogin))
                and self.site.family.name in config.available_ssl_project):
            self.site = EnableSSLSiteWrapper(self.site)

    # implement dict interface
    def __getitem__(self, key):
        return self.params[key]

    def __setitem__(self, key, value):
        self.params[key] = value

    def __delitem__(self, key):
        del self.params[key]

    def keys(self):
        return list(self.params.keys())

    def __contains__(self, key):
        return self.params.__contains__(key)

    def __iter__(self):
        return self.params.__iter__()

    def __len__(self):
        return len(self.params)

    def iteritems(self):
        return iter(self.params.items())

    def items(self):
        """Return a list of tuples containg the parameters in any order."""
        return list(self.params.items())

    @property
    def mime(self):
        """Return whether mime parameters are defined."""
        return self.mime_params is not None

    @mime.setter
    def mime(self, value):
        """
        Change whether mime parameter should be defined.

        This will clear the mime parameters.
        """
        try:
            self.mime_params = dict(value)
        except TypeError:
            self.mime_params = {} if value else None

    def http_params(self):
        """Return the parameters formatted for inclusion in an HTTP request.

        self.params MUST be either
           list of unicode
           unicode (may be |-separated list)
           str in site encoding (may be |-separated list)
        """
        if self.mime_params and set(self.params.keys()) & set(self.mime_params.keys()):
            raise ValueError('The mime_params and params may not share the '
                             'same keys.')
        for key in self.params:
            if isinstance(self.params[key], bytes):
                self.params[key] = self.params[key].decode(self.site.encoding())
            if isinstance(self.params[key], basestring):
                # convert a stringified sequence into a list
                self.params[key] = self.params[key].split("|")
            try:
                iter(self.params[key])
            except TypeError:
                # convert any non-iterable value into a single-element list
                self.params[key] = [str(self.params[key])]
        if self.params["action"] == ['query']:
            meta = self.params.get("meta", [])
            if "userinfo" not in meta:
                meta.append("userinfo")
                self.params["meta"] = meta
            uiprop = self.params.get("uiprop", [])
            uiprop = set(uiprop + ["blockinfo", "hasmsg"])
            self.params["uiprop"] = list(sorted(uiprop))
            if "properties" in self.params:
                if "info" in self.params["properties"]:
                    inprop = self.params.get("inprop", [])
                    info = set(inprop + ["protection", "talkid", "subjectid"])
                    self.params["info"] = list(info)
        if "maxlag" not in self.params and config.maxlag:
            self.params["maxlag"] = [str(config.maxlag)]
        if "format" not in self.params:
            self.params["format"] = ["json"]
        if self.params['format'] != ["json"]:
            raise TypeError("Query format '%s' cannot be parsed."
                            % self.params['format'])
        for key in self.params:
            try:
                self.params[key] = "|".join(self.params[key])
                self.params[key] = self.params[key].encode(self.site.encoding())
            except Exception:
                pywikibot.error(
                    u"http_params: Key '%s' could not be encoded to '%s'; params=%r"
                    % (key, self.site.encoding(), self.params[key]))
        return urlencode(self.params)

    def __str__(self):
        return unquote(self.site.scriptpath()
                              + "/api.php?"
                              + self.http_params())

    def __repr__(self):
        return "%s.%s<%s->%r>" % (self.__class__.__module__, self.__class__.__name__, self.site, str(self))

    def _simulate(self, action):
        if action and config.simulate and (self.write or action in config.actions_to_block):
            pywikibot.output(
                u'\03{lightyellow}SIMULATION: %s action blocked.\03{default}'
                % action)
            return {action: {'result': 'Success', 'nochange': ''}}

    def _is_wikibase_error_retryable(self, error):
        ERR_MSG = u'edit-already-exists'
        messages = error.pop("messages", None)
        # bug 66619, after gerrit 124323 breaking change we have a
        # list of messages
        if isinstance(messages, list):
            for item in messages:
                message = item["name"]
                if message == ERR_MSG:
                    break
            else:  # no break
                message = None
        elif isinstance(messages, dict):
            try:  # behaviour before gerrit 124323 braking change
                message = messages["0"]["name"]
            except KeyError:  # unsure the new output is always a list
                message = messages["name"]
        else:
            message = None
        return message == ERR_MSG

    @staticmethod
    def _generate_MIME_part(key, content, keytype, headers):
        if not keytype:
            try:
                content.encode("ascii")
                keytype = ("text", "plain")
            except UnicodeError:
                keytype = ("application", "octet-stream")
        submsg = MIMENonMultipart(*keytype)
        content_headers = {'name': key}
        if headers:
            content_headers.update(headers)
        submsg.add_header("Content-disposition", "form-data",
                          **content_headers)
        submsg.set_payload(content)
        return submsg

    def _post_process(self, result):
        """Post process the result and return if a retry is not necessary."""
        if self['action'] == 'query':
            if 'userinfo' in result.get('query', ()):
                if hasattr(self.site, '_userinfo'):
                    self.site._userinfo.update(result['query']['userinfo'])
                else:
                    self.site._userinfo = result['query']['userinfo']
            status = self.site._loginstatus  # save previous login status
            if (("error" in result
                 and result["error"]["code"].endswith("limit"))
                or (status >= 0
                    and self.site._userinfo['name'] != self.site._username[status])):
                # user is no longer logged in (session expired?)
                # reset userinfo, then make user log in again
                del self.site._userinfo
                self.site._loginstatus = -1
                if status < 0:
                    status = 0  # default to non-sysop login
                self.site.login(status)
                # retry the previous query
                return False
        if 'warnings' in result:
            for mod, warning in result['warnings'].items():
                if mod == 'info':
                    continue
                if '*' in warning:
                    text = warning['*']
                elif 'html' in warning:
                    # Bugzilla 49978
                    text = warning['html']['*']
                else:
                    pywikibot.warning(
                        u'API warning ({0})of unknown format: {1}'.
                        format(mod, warning))
                    continue
                # multiple warnings are in text separated by a newline
                for single_warning in text.splitlines():
                    if (not callable(self._warning_handler) or
                            not self._warning_handler(mod, single_warning)):
                        pywikibot.warning(u"API warning (%s): %s" % (mod, single_warning))
        return True

    def submit(self):
        """Submit a query and parse the response.

        @return: a dict containing data retrieved from api.php

        """
        while True:
            paramstring = self.http_params()
            action = self.params.get("action", "")
            simulate = self._simulate(action)
            if simulate:
                return simulate
            if self.throttle:
                self.site.throttle(write=self.write)
            else:
                pywikibot.log("Action '{0}' is submitted not throttled.".format(action))
            uri = self.site.scriptpath() + "/api.php"
            try:
                if self.mime:
                    # construct a MIME message containing all API key/values
                    container = MIMEMultipart(_subtype='form-data')
                    for key in self.params:
                        # key "file" requires special treatment in a multipart
                        # message
                        if key == "file":
                            local_filename = self.params[key]
                            filetype = mimetypes.guess_type(local_filename)[0] \
                                or 'application/octet-stream'
                            file_content = file(local_filename, "rb").read()
                            submsg = Request._generate_MIME_part(
                                key, file_content, filetype.split('/'),
                                {'filename': local_filename})
                        else:
                            submsg = Request._generate_MIME_part(
                                key, self.params[key], None, None)
                        container.attach(submsg)
                    for key, value in self.mime_params.items():
                        container.attach(Request._generate_MIME_part(key, *value))
                    # strip the headers to get the HTTP message body
                    body = container.as_string()
                    marker = "\n\n"  # separates headers from body
                    eoh = body.find(marker)
                    body = body[eoh + len(marker):]
                    # retrieve the headers from the MIME object
                    headers = dict(list(container.items()))
                else:
                    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                    body = paramstring

                rawdata = http.request(
                    self.site, uri, method="POST",
                    headers=headers, body=body)

#                import traceback
#                traceback.print_stack()
#                print rawdata
            except Server504Error:
                pywikibot.log(u"Caught HTTP 504 error; retrying")
                self.wait()
                continue
            except FatalServerError:
                # This error is not going to be fixed by just waiting
                pywikibot.error(traceback.format_exc())
                raise
            # TODO: what other exceptions can occur here?
            except Exception:
                # for any other error on the http request, wait and retry
                pywikibot.error(traceback.format_exc())
                pywikibot.log(u"%s, %s" % (uri, paramstring))
                self.wait()
                continue
            if not isinstance(rawdata, unicode):
                rawdata = rawdata.decode(self.site.encoding())
            pywikibot.debug(u"API response received:\n" + rawdata, _logger)
            if rawdata.startswith(u"unknown_action"):
                raise APIError(rawdata[:14], rawdata[16:])
            try:
                result = json.loads(rawdata)
            except ValueError:
                # if the result isn't valid JSON, there must be a server
                # problem.  Wait a few seconds and try again
                pywikibot.warning(
                    "Non-JSON response received from server %s; the server may be down."
                    % self.site)
                pywikibot.debug(rawdata, _logger)
                # there might also be an overflow, so try a smaller limit
                for param in self.params:
                    if param.endswith("limit"):
                        value = self.params[param]
                        try:
                            self.params[param] = str(int(value) // 2)
                            pywikibot.output(u"Set %s = %s"
                                             % (param, self.params[param]))
                        except:
                            pass
                self.wait()
                continue
            if not result:
                result = {}
            if not isinstance(result, dict):
                raise APIError("Unknown",
                               "Unable to process query response of type %s."
                               % type(result),
                               data=result)
            if not self._post_process(result):
                continue
            if "error" not in result:
                return result

            if "*" in result["error"]:
                # help text returned
                result['error']['help'] = result['error'].pop("*")
            code = result["error"].pop("code", "Unknown")
            info = result["error"].pop("info", None)
            if code == "maxlag":
                lag = lagpattern.search(info)
                if lag:
                    pywikibot.log(
                        u"Pausing due to database lag: " + info)
                    self.site.throttle.lag(int(lag.group("lag")))
                    continue

            if code.startswith(u'internal_api_error_'):
                class_name = code[len(u'internal_api_error_'):]
                if class_name in ['DBConnectionError',  # r 4984 & r 4580
                                  'DBQueryError',  # bug 58158
                                  'ReadOnlyError'  # bug 59227
                                  ]:

                    pywikibot.log(u'MediaWiki exception %s; retrying.'
                                  % class_name)
                    self.wait()
                    continue

                pywikibot.log(u"MediaWiki exception %s: query=\n%s"
                              % (class_name,
                                 pprint.pformat(self.params)))
                pywikibot.log(u"           response=\n%s" % result)

                raise APIMWException(class_name, info, **result["error"])

            # bugs 46535, 62126, 64494, 66619
            # maybe removed when it 46535 is solved
            if code == "failed-save" and \
               action == 'wbeditentity' and \
               self._is_wikibase_error_retryable(result["error"]):
                self.wait()
                continue
            # raise error
            try:
                pywikibot.log(u"API Error: query=\n%s"
                              % pprint.pformat(self.params))
                pywikibot.log(u"           response=\n%s"
                              % result)

                raise APIError(code, info, **result["error"])
            except TypeError:
                raise RuntimeError(result)

    def wait(self):
        """Determine how long to wait after a failed request."""
        self.max_retries -= 1
        if self.max_retries < 0:
            raise TimeoutError("Maximum retries attempted without success.")
        pywikibot.warning(u"Waiting %s seconds before retrying."
                          % self.retry_wait)
        time.sleep(self.retry_wait)
        # double the next wait, but do not exceed 120 seconds
        self.retry_wait = min(120, self.retry_wait * 2)


class CachedRequest(Request):
    def __init__(self, expiry, *args, **kwargs):
        """Construct a CachedRequest object.

        @param expiry: either a number of days or a datetime.timedelta object
        """
        super(CachedRequest, self).__init__(*args, **kwargs)
        if not isinstance(expiry, datetime.timedelta):
            expiry = datetime.timedelta(expiry)
        self.expiry = expiry
        self._data = None
        self._cachetime = None

    @staticmethod
    def _get_cache_dir():
        """Return the base directory path for cache entries.

        The directory will be created if it does not already exist.

        @return: basestring
        """
        path = os.path.join(pywikibot.config2.base_dir, 'apicache')
        CachedRequest._make_dir(path)
        return path

    @staticmethod
    def _make_dir(dir):
        """Create directory if it does not exist already.

        The directory name (dir) is returned unmodified.

        @param dir: directory path
        @type dir: basestring

        @return: basestring
        """
        try:
            os.makedirs(dir)
        except OSError:
            # directory already exists
            pass
        return dir

    def _uniquedescriptionstr(self):
        """Return unique description for the cache entry.

        If this is modified, please also update
        scripts/maintenance/cache.py to support
        the new key and all previous keys.
        """

        login_status = self.site._loginstatus

        if login_status > pywikibot.site.LoginStatus.NOT_LOGGED_IN and \
                hasattr(self.site, '_userinfo') and \
                'name' in self.site._userinfo:
            # This uses the format of Page.__repr__(), without the encoding
            # it performs. This string cant be encoded otherwise it creates an
            # exception when _create_file_name() tries to encode it again.
            user_key = u'User(User:%s)' % self.site._userinfo['name']
        else:
            user_key = pywikibot.site.LoginStatus(
                max(login_status, pywikibot.site.LoginStatus.NOT_LOGGED_IN))
            user_key = repr(user_key)

        return repr(self.site) + user_key + repr(sorted(self.items()))

    def _create_file_name(self):
        self.http_params()  # normalize self.params
        return hashlib.sha256(
            self._uniquedescriptionstr().encode('utf-8')
        ).hexdigest()

    def _cachefile_path(self):
        return os.path.join(CachedRequest._get_cache_dir(),
                            self._create_file_name())

    def _expired(self, dt):
        return dt + self.expiry < datetime.datetime.now()

    def _load_cache(self):
        """Return whether the cache can be used."""
        try:
            with open(self._cachefile_path(), 'rb') as f:
                uniquedescr, self._data, self._cachetime = pickle.load(f)
            assert(uniquedescr == str(self._uniquedescriptionstr()))
            if self._expired(self._cachetime):
                self._data = None
                return False
            return True
        except IOError as e:
            # file not found
            return False
        except Exception as e:
            pywikibot.output("Could not load cache: %r" % e)
            return False

    def _write_cache(self, data):
        """Write data to self._cachefile_path()."""
        data = [self._uniquedescriptionstr(), data, datetime.datetime.now()]
        with open(self._cachefile_path(), 'wb') as f:
            pickle.dump(data, f)

    def submit(self):
        cached_available = self._load_cache()
        if not cached_available:
            self._data = super(CachedRequest, self).submit()
            self._write_cache(self._data)
        else:
            self._post_process(self._data)
        return self._data


class QueryGenerator(object):

    """Base class for iterators that handle responses to API action=query.

    By default, the iterator will iterate each item in the query response,
    and use the query-continue element, if present, to continue iterating as
    long as the wiki returns additional values.  However, if the iterator's
    limit attribute is set to a positive int, the iterator will stop after
    iterating that many values. If limit is negative, the limit parameter
    will not be passed to the API at all.

    Most common query types are more efficiently handled by subclasses, but
    this class can be used directly for custom queries and miscellaneous
    types (such as "meta=...") that don't return the usual list of pages or
    links. See the API documentation for specific query options.

    """

    def __init__(self, **kwargs):
        """Construct a QueryGenerator object.

        kwargs are used to create a Request object; see that object's
        documentation for values. 'action'='query' is assumed.

        """
        if "action" in kwargs and kwargs["action"] != "query":
            raise Error("%s: 'action' must be 'query', not %s"
                        % (self.__class__.__name__, kwargs["action"]))
        else:
            kwargs["action"] = "query"
        try:
            self.site = kwargs["site"]
        except KeyError:
            self.site = pywikibot.Site()
            kwargs["site"] = self.site
        # make sure request type is valid, and get limit key if any
        for modtype in ("generator", "list", "prop", "meta"):
            if modtype in kwargs:
                self.module = kwargs[modtype]
                break
        else:
            raise Error("%s: No query module name found in arguments."
                        % self.__class__.__name__)

        kwargs["indexpageids"] = ""  # always ask for list of pageids
        self.request = Request(**kwargs)
        self.prefix = None
        self.api_limit = None
        self.update_limit()  # sets self.prefix
        if self.api_limit is not None and "generator" in kwargs:
            self.prefix = "g" + self.prefix
        self.limit = None
        self.query_limit = self.api_limit
        if "generator" in kwargs:
            self.resultkey = "pages"        # name of the "query" subelement key
        else:                               # to look for when iterating
            self.resultkey = self.module

        # usually the query-continue key is the same as the querymodule,
        # but not always
        # API can return more than one query-continue key, if multiple properties
        # are requested by the query, e.g.
        # "query-continue":{
        #     "langlinks":{"llcontinue":"12188973|pt"},
        #     "templates":{"tlcontinue":"310820|828|Namespace_detect"}}
        # self.continuekey is a list
        self.continuekey = self.module.split('|')

    @property
    def __modules(self):
        """
        Cache paraminfo in this request's Site object.

        Hold the query data for paraminfo on
        querymodule=self.module at self.site.

        """
        if not hasattr(self.site, "_modules"):
            setattr(self.site, "_modules", dict())
        return self.site._modules

    @__modules.deleter
    def __modules(self):
        """Delete the instance cache - maybe we don't need it."""
        if hasattr(self.site, "_modules"):
            del self.site._modules

    @property
    def _modules(self):
        """Query api on self.site for paraminfo on querymodule=self.module."""
        if not set(self.module.split('|')) <= set(self.__modules.keys()):
            paramreq = CachedRequest(expiry=config.API_config_expiry,
                                     site=self.site, action="paraminfo",
                                     querymodules=self.module)
            data = paramreq.submit()
            assert "paraminfo" in data
            assert "querymodules" in data["paraminfo"]
            assert len(data["paraminfo"]["querymodules"]) == 1 + self.module.count("|")
            for paraminfo in data["paraminfo"]["querymodules"]:
                assert paraminfo["name"] in self.module
                if "missing" in paraminfo:
                    raise Error("Invalid query module name '%s'." % self.module)
                self.__modules[paraminfo["name"]] = paraminfo
        _modules = {}
        for m in self.module.split('|'):
            _modules[m] = self.__modules[m]
        return _modules

    def set_query_increment(self, value):
        """Set the maximum number of items to be retrieved per API query.

        If not called, the default is to ask for "max" items and let the
        API decide how many to send.

        """
        limit = int(value)

        # don't update if limit is greater than maximum allowed by API
        if self.api_limit is None:
            self.query_limit = limit
        else:
            self.query_limit = min(self.api_limit, limit)
        pywikibot.debug(u"%s: Set query_limit to %i."
                        % (self.__class__.__name__, self.query_limit),
                        _logger)

    def set_maximum_items(self, value):
        """Set the maximum number of items to be retrieved from the wiki.

        If not called, most queries will continue as long as there is
        more data to be retrieved from the API.

        If set to -1 (or any negative value), the "limit" parameter will be
        omitted from the request. For some request types (such as
        prop=revisions), this is necessary to signal that only current
        revision is to be returned.

        """
        self.limit = int(value)

    def update_limit(self):
        """Set query limit for self.module based on api response."""

        for mod in self.module.split('|'):
            for param in self._modules[mod].get("parameters", []):
                if param["name"] == "limit":
                    if self.site.logged_in() and self.site.has_right('apihighlimits'):
                        self.api_limit = int(param["highmax"])
                    else:
                        self.api_limit = int(param["max"])
                    if self.prefix is None:
                        self.prefix = self._modules[mod]["prefix"]
                    pywikibot.debug(u"%s: Set query_limit to %i."
                                    % (self.__class__.__name__,
                                       self.api_limit),
                                    _logger)
                    return

    def set_namespace(self, namespaces):
        """Set a namespace filter on this query.

        @param namespaces: Either an int or a list of ints

        """
        if isinstance(namespaces, list):
            namespaces = "|".join(str(n) for n in namespaces)
        else:
            namespaces = str(namespaces)
        for mod in self.module.split('|'):
            for param in self._modules[mod].get("parameters", []):
                if param["name"] == "namespace":
                    self.request[self.prefix + "namespace"] = namespaces
                    return

    def __iter__(self):
        """Submit request and iterate the response based on self.resultkey.

        Continues response as needed until limit (if any) is reached.

        """
        count = 0
        while True:
            if self.query_limit is not None:
                if self.limit is None:
                    new_limit = self.query_limit
                elif self.limit > 0:
                    new_limit = min(self.query_limit, self.limit - count)
                else:
                    new_limit = None

                if new_limit and \
                        "rvprop" in self.request \
                        and "content" in self.request["rvprop"]:
                    # queries that retrieve page content have lower limits
                    # Note: although API allows up to 500 pages for content
                    #   queries, these sometimes result in server-side errors
                    #   so use 250 as a safer limit
                    new_limit = min(new_limit, self.api_limit // 10, 250)
                if new_limit is not None:
                    self.request[self.prefix + "limit"] = str(new_limit)
            if not hasattr(self, "data"):
                self.data = self.request.submit()
            if not self.data or not isinstance(self.data, dict):
                pywikibot.debug(
                    u"%s: stopped iteration because no dict retrieved from api."
                    % self.__class__.__name__,
                    _logger)
                return
            if "query" not in self.data:
                pywikibot.debug(
                    u"%s: stopped iteration because 'query' not found in api response."
                    % (self.__class__.__name__, self.resultkey),
                    _logger)
                pywikibot.debug(unicode(self.data), _logger)
                return
            if self.resultkey in self.data["query"]:
                resultdata = self.data["query"][self.resultkey]
                if isinstance(resultdata, dict):
                    pywikibot.debug(u"%s received %s; limit=%s"
                                    % (self.__class__.__name__,
                                       list(resultdata.keys()),
                                       self.limit),
                                    _logger)
                    if "results" in resultdata:
                        resultdata = resultdata["results"]
                    elif "pageids" in self.data["query"]:
                        # this ensures that page data will be iterated
                        # in the same order as received from server
                        resultdata = [resultdata[k]
                                      for k in self.data["query"]["pageids"]]
                    else:
                        resultdata = [resultdata[k]
                                      for k in sorted(resultdata.keys())]
                else:
                    pywikibot.debug(u"%s received %s; limit=%s"
                                    % (self.__class__.__name__,
                                       resultdata,
                                       self.limit),
                                    _logger)
                if "normalized" in self.data["query"]:
                    self.normalized = dict((item['to'], item['from'])
                                           for item in
                                           self.data["query"]["normalized"])
                else:
                    self.normalized = {}
                for item in resultdata:
                    yield self.result(item)
                    if isinstance(item, dict) and set(self.continuekey) & set(item.keys()):
                        # if we need to count elements contained in items in
                        # self.data["query"]["pages"], we want to count
                        # item[self.continuekey] (e.g. 'revisions') and not
                        # self.resultkey (i.e. 'pages')
                        for key in set(self.continuekey) & set(item.keys()):
                            count += len(item[key])
                    # otherwise we proceed as usual
                    else:
                        count += 1
                    # note: self.limit could be -1
                    if self.limit and self.limit > 0 and count >= self.limit:
                        return
            else:
                # No results.
                return
            if self.module == "random" and self.limit:
                # "random" module does not return "query-continue"
                # now we loop for a new random query
                continue
            if "query-continue" not in self.data:
                return
            if all(key not in self.data["query-continue"] for key in self.continuekey):
                pywikibot.log(
                    u"Missing '%s' key(s) in ['query-continue'] value."
                    % self.continuekey)
                return
            query_continue_pairs = self.data["query-continue"].values()
            for query_continue_pair in query_continue_pairs:
                for key, value in query_continue_pair.items():
                    # query-continue can return ints
                    if isinstance(value, int):
                        value = str(value)
                    self.request[key] = value

            del self.data  # a new request with query-continue is needed

    def result(self, data):
        """Process result data as needed for particular subclass."""
        return data


class PageGenerator(QueryGenerator):

    """Iterator for response to a request of type action=query&generator=foo.

    This class can be used for any of the query types that are listed in the
    API documentation as being able to be used as a generator. Instances of
    this class iterate Page objects.

    """

    def __init__(self, generator, g_content=False, **kwargs):
        """
        Constructor.

        Required and optional parameters are as for C{Request}, except that
        action=query is assumed and generator is required.

        @param generator: the "generator=" type from api.php
        @type generator: str
        @param g_content: if True, retrieve the contents of the current
            version of each Page (default False)

        """
        def appendParams(params, key, value):
            if key in params:
                params[key] += '|' + value
            else:
                params[key] = value
        # get some basic information about every page generated
        appendParams(kwargs, 'prop', 'info|imageinfo|categoryinfo')
        if g_content:
            # retrieve the current revision
            appendParams(kwargs, 'prop', 'revisions')
            appendParams(kwargs, 'rvprop', 'ids|timestamp|flags|comment|user|content')
        if not ('inprop' in kwargs and 'protection' in kwargs['inprop']):
            appendParams(kwargs, 'inprop', 'protection')
        appendParams(kwargs, 'iiprop', 'timestamp|user|comment|url|size|sha1|metadata')
        QueryGenerator.__init__(self, generator=generator, **kwargs)
        self.resultkey = "pages"  # element to look for in result

    def result(self, pagedata):
        """Convert page dict entry from api to Page object.

        This can be overridden in subclasses to return a different type
        of object.

        """
        p = pywikibot.Page(self.site, pagedata['title'], pagedata['ns'])
        update_page(p, pagedata)
        return p


class CategoryPageGenerator(PageGenerator):

    """Like PageGenerator, but yields Category objects instead of Pages."""

    def result(self, pagedata):
        p = PageGenerator.result(self, pagedata)
        return pywikibot.Category(p)


class ImagePageGenerator(PageGenerator):

    """Like PageGenerator, but yields FilePage objects instead of Pages."""

    def result(self, pagedata):
        p = PageGenerator.result(self, pagedata)
        filepage = pywikibot.FilePage(p)
        if 'imageinfo' in pagedata:
            filepage._imageinfo = pagedata['imageinfo'][0]
        return filepage


class PropertyGenerator(QueryGenerator):

    """Iterator for queries of type action=query&prop=foo.

    See the API documentation for types of page properties that can be
    queried.

    This iterator yields one or more dict object(s) corresponding
    to each "page" item(s) from the API response; the calling module has to
    decide what to do with the contents of the dict. There will be one
    dict for each page queried via a titles= or ids= parameter (which must
    be supplied when instantiating this class).

    """

    def __init__(self, prop, **kwargs):
        """
        Constructor.

        Required and optional parameters are as for C{Request}, except that
        action=query is assumed and prop is required.

        @param prop: the "prop=" type from api.php
        @type prop: str

        """
        QueryGenerator.__init__(self, prop=prop, **kwargs)
        self.resultkey = "pages"


class ListGenerator(QueryGenerator):

    """Iterator for queries of type action=query&list=foo.

    See the API documentation for types of lists that can be queried.  Lists
    include both side-wide information (such as 'allpages') and page-specific
    information (such as 'backlinks').

    This iterator yields a dict object for each member of the list returned
    by the API, with the format of the dict depending on the particular list
    command used.  For those lists that contain page information, it may be
    easier to use the PageGenerator class instead, as that will convert the
    returned information into a Page object.

    """

    def __init__(self, listaction, **kwargs):
        """
        Constructor.

        Required and optional parameters are as for C{Request}, except that
        action=query is assumed and listaction is required.

        @param listaction: the "list=" type from api.php
        @type listaction: str

        """
        QueryGenerator.__init__(self, list=listaction, **kwargs)


class LogEntryListGenerator(ListGenerator):

    """
    Iterator for queries of list 'logevents'.

    Yields LogEntry objects instead of dicts.
    """

    def __init__(self, logtype=None, **kwargs):
        """Constructor."""
        ListGenerator.__init__(self, "logevents", **kwargs)

        from pywikibot import logentries
        self.entryFactory = logentries.LogEntryFactory(logtype)

    def result(self, pagedata):
        return self.entryFactory.create(pagedata)


class LoginManager(login.LoginManager):

    """Supply getCookie() method to use API interface."""

    def getCookie(self, remember=True, captchaId=None, captchaAnswer=None):
        """Login to the site.

        Parameters are all ignored.

        @return: cookie data if successful, None otherwise.

        """
        if hasattr(self, '_waituntil'):
            if datetime.datetime.now() < self._waituntil:
                diff = self._waituntil - datetime.datetime.now()
                pywikibot.warning(u"Too many tries, waiting %s seconds before retrying."
                                  % diff.seconds)
                time.sleep(diff.seconds)
        login_request = Request(site=self.site,
                                action="login",
                                lgname=self.username,
                                lgpassword=self.password)
        self.site._loginstatus = -2
        while True:
            login_result = login_request.submit()
            if u"login" not in login_result:
                raise RuntimeError("API login response does not have 'login' key.")
            if login_result['login']['result'] == "Success":
                prefix = login_result['login']['cookieprefix']
                cookies = []
                for key in ('Token', 'UserID', 'UserName'):
                    cookies.append("%s%s=%s"
                                   % (prefix, key,
                                      login_result['login']['lg' + key.lower()]))
                self.username = login_result['login']['lgusername']
                return "\n".join(cookies)
            elif login_result['login']['result'] == "NeedToken":
                token = login_result['login']['token']
                login_request["lgtoken"] = token
                continue
            elif login_result['login']['result'] == "Throttled":
                self._waituntil = datetime.datetime.now() + datetime.timedelta(
                    seconds=int(login_result["login"]["wait"]))
                break
            else:
                break
        raise APIError(code=login_result["login"]["result"], info="")

    def storecookiedata(self, data):
        # ignore data; cookies are set by threadedhttp module
        pywikibot.cookie_jar.save()


def update_page(page, pagedict):
    """Update attributes of Page object page, based on query data in pagedict.

    @param page: object to be updated
    @type page: Page
    @param pagedict: the contents of a "page" element of a query response
    @type pagedict: dict

    """
    if "pageid" in pagedict:
        page._pageid = int(pagedict['pageid'])
    elif "missing" in pagedict:
        page._pageid = 0    # Non-existent page
    else:
        raise AssertionError(
            "Page %s has neither 'pageid' nor 'missing' attribute" % pagedict['title'])
    page._isredir = 'redirect' in pagedict
    if 'touched' in pagedict:
        page._timestamp = pagedict['touched']
    if 'protection' in pagedict:
        page._protection = {}
        for item in pagedict['protection']:
            page._protection[item['type']] = item['level'], item['expiry']
    if 'revisions' in pagedict:
        for rev in pagedict['revisions']:
            revision = pywikibot.page.Revision(
                revid=rev['revid'],
                timestamp=pywikibot.Timestamp.fromISOformat(rev['timestamp']),
                user=rev.get('user', u''),
                anon='anon' in rev,
                comment=rev.get('comment', u''),
                minor='minor' in rev,
                text=rev.get('*', None),
                rollbacktoken=rev.get('rollbacktoken', None)
            )
            page._revisions[revision.revid] = revision

    if 'lastrevid' in pagedict:
        page._revid = pagedict['lastrevid']
        if page._revid in page._revisions:
            page._text = page._revisions[page._revid].text

    if "categoryinfo" in pagedict:
        page._catinfo = pagedict["categoryinfo"]

    if "templates" in pagedict:
        templates = [pywikibot.Page(page.site, tl['title'])
                     for tl in pagedict['templates']]
        if hasattr(page, "_templates"):
            page._templates.extend(templates)
        else:
            page._templates = templates

    if "langlinks" in pagedict:
        links = []
        for ll in pagedict["langlinks"]:
            link = pywikibot.Link.langlinkUnsafe(ll['lang'],
                                                 ll['*'],
                                                 source=page.site)
            links.append(link)

        if hasattr(page, "_langlinks"):
            page._langlinks.extend(links)
        else:
            page._langlinks = links

    if "coordinates" in pagedict:
        coords = []
        for co in pagedict['coordinates']:
            coord = pywikibot.Coordinate(lat=co['lat'],
                                         lon=co['lon'],
                                         typ=co.get('type', ''),
                                         name=co.get('name', ''),
                                         dim=int(co['dim']),
                                         globe=co['globe'],  # See [[gerrit:67886]]
                                         )
            coords.append(coord)
        page._coords = coords

    if "pageprops" in pagedict:
        page._pageprops = pagedict['pageprops']

    if 'preload' in pagedict:
        page._preloadedtext = pagedict['preload']

    if "flowinfo" in pagedict:
        page._flowinfo = pagedict['flowinfo']['flow']


if __name__ == "__main__":
    from pywikibot import Site, logging
    logging.getLogger("pywiki.data.api").setLevel(logging.DEBUG)
    mysite = Site("en", "wikipedia")
    pywikibot.output(u"starting test....")

    def _test():
        import doctest
        doctest.testmod()
    try:
        _test()
    finally:
        pywikibot.stopme()
