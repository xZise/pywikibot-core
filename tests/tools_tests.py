#!/usr/bin/python
"""Test tools package alone which don't fit into other tests."""
# -*- coding: utf-8  -*-
#
# (C) Pywikibot team, 2015
#
# Distributed under the terms of the MIT license.
from __future__ import unicode_literals

__version__ = '$Id$'

import collections
import decimal
import os.path
import subprocess
import sys

from pywikibot import tools

from tests import _data_dir
from tests.aspects import unittest, TestCase
from tests.utils import expected_failure_if

_xml_data_dir = os.path.join(_data_dir, 'xml')


class ContextManagerWrapperTestCase(TestCase):

    """Test that ContextManagerWrapper is working correctly."""

    class DummyClass(object):

        """A dummy class which has some values and a close method."""

        class_var = 42

        def __init__(self):
            """Create instance with dummy values."""
            self.instance_var = 1337
            self.closed = False

        def close(self):
            """Just store that it has been closed."""
            self.closed = True

    net = False

    def test_wrapper(self):
        """Create a test instance and verify the wrapper redirects."""
        obj = self.DummyClass()
        wrapped = tools.ContextManagerWrapper(obj)
        self.assertIs(wrapped.class_var, obj.class_var)
        self.assertIs(wrapped.instance_var, obj.instance_var)
        self.assertIs(wrapped._wrapped, obj)
        self.assertFalse(obj.closed)
        with wrapped as unwrapped:
            self.assertFalse(obj.closed)
            self.assertIs(unwrapped, obj)
            unwrapped.class_var = 47
        self.assertTrue(obj.closed)
        self.assertEqual(wrapped.class_var, 47)

    def test_exec_wrapper(self):
        """Check that the wrapper permits exceptions."""
        wrapper = tools.ContextManagerWrapper(self.DummyClass())
        self.assertFalse(wrapper.closed)
        with self.assertRaises(ZeroDivisionError):
            with wrapper:
                1 / 0
        self.assertTrue(wrapper.closed)


class OpenCompressedTestCase(TestCase):

    """
    Unit test class for tools.

    The tests for open_compressed requires that article-pyrus.xml* contain all
    the same content after extraction. The content itself is not important.
    The file article-pyrus.xml_invalid.7z is not a valid 7z file and
    open_compressed will fail extracting it using 7za.
    """

    net = False

    @classmethod
    def setUpClass(cls):
        """Define base_file and original_content."""
        super(OpenCompressedTestCase, cls).setUpClass()
        cls.base_file = os.path.join(_xml_data_dir, 'article-pyrus.xml')
        with open(cls.base_file, 'rb') as f:
            cls.original_content = f.read()

    @staticmethod
    def _get_content(*args):
        """Use open_compressed and return content using a with-statement."""
        with tools.open_compressed(*args) as f:
            return f.read()

    def test_open_compressed_normal(self):
        """Test open_compressed with no compression in the standard library."""
        self.assertEqual(self._get_content(self.base_file), self.original_content)

    def test_open_compressed_bz2(self):
        """Test open_compressed with bz2 compressor in the standard library."""
        self.assertEqual(self._get_content(self.base_file + '.bz2'), self.original_content)
        self.assertEqual(self._get_content(self.base_file + '.bz2', True), self.original_content)

    def test_open_compressed_gz(self):
        """Test open_compressed with gz compressor in the standard library."""
        self.assertEqual(self._get_content(self.base_file + '.gz'), self.original_content)

    def test_open_compressed_7z(self):
        """Test open_compressed with 7za if installed."""
        try:
            subprocess.Popen(['7za'], stdout=subprocess.PIPE).stdout.close()
        except OSError:
            raise unittest.SkipTest('7za not installed')
        self.assertEqual(self._get_content(self.base_file + '.7z'), self.original_content)
        self.assertRaises(OSError, self._get_content, self.base_file + '_invalid.7z', True)


class MergeUniqueDicts(TestCase):

    """Test merge_unique_dicts."""

    net = False
    dct1 = {'foo': 'bar', '42': 'answer'}
    dct2 = {47: 'Star', 74: 'Trek'}
    dct_both = dct1.copy()
    dct_both.update(dct2)

    def test_single(self):
        """Test that it returns the dict itself when there is only one."""
        self.assertEqual(tools.merge_unique_dicts(self.dct1), self.dct1)
        self.assertEqual(tools.merge_unique_dicts(**self.dct1), self.dct1)

    def test_multiple(self):
        """Test that it actually merges dicts."""
        self.assertEqual(tools.merge_unique_dicts(self.dct1, self.dct2),
                         self.dct_both)
        self.assertEqual(tools.merge_unique_dicts(self.dct2, **self.dct1),
                         self.dct_both)

    def test_different_type(self):
        """Test that the keys can be different types."""
        self.assertEqual(tools.merge_unique_dicts({'1': 'str'}, {1: 'int'}),
                         {'1': 'str', 1: 'int'})

    def test_conflict(self):
        """Test that it detects conflicts."""
        self.assertRaisesRegex(
            ValueError, '42', tools.merge_unique_dicts, self.dct1, **{'42': 'bad'})
        self.assertRaisesRegex(
            ValueError, '42', tools.merge_unique_dicts, self.dct1, self.dct1)
        self.assertRaisesRegex(
            ValueError, '42', tools.merge_unique_dicts, self.dct1, **self.dct1)


def passthrough(x):
    """Return x."""
    return x


class SkipList(set):

    """Container that ignores items."""

    skip_list = [1, 3]

    def __contains__(self, item):
        """Override to not process some items."""
        if item in self.skip_list:
            return True
        else:
            return super(SkipList, self).__contains__(item)


class ProcessAgainList(set):

    """Container that keeps processing certain items."""

    process_again_list = [1, 3]

    def add(self, item):
        """Override to not add some items."""
        if item in self.process_again_list:
            return
        else:
            return super(ProcessAgainList, self).add(item)


class ContainsStopList(set):

    """Container that stops when encountering items."""

    stop_list = []

    def __contains__(self, item):
        """Override to stop on encountering items."""
        if item in self.stop_list:
            raise StopIteration
        else:
            return super(ContainsStopList, self).__contains__(item)


class AddStopList(set):

    """Container that stops when encountering items."""

    stop_list = []

    def add(self, item):
        """Override to not continue on encountering items."""
        if item in self.stop_list:
            raise StopIteration
        else:
            super(AddStopList, self).add(item)


class TestFilterUnique(TestCase):

    """Test filter_unique."""

    net = False

    ints = [1, 3, 2, 1, 2, 1, 2, 4, 2]
    strs = [str(i) for i in ints]
    decs = [decimal.Decimal(i) for i in ints]

    def _test_dedup_int(self, deduped, deduper, key=None):
        """Test filter_unique results for int."""
        if not key:
            key = passthrough

        self.assertEqual(len(deduped), 0)

        self.assertEqual(next(deduper), 1)
        self.assertEqual(next(deduper), 3)

        if key in (hash, passthrough):
            if isinstance(deduped, tools.OrderedDict):
                self.assertEqual(list(deduped.keys()), [1, 3])
            elif isinstance(deduped, collections.Mapping):
                self.assertCountEqual(list(deduped.keys()), [1, 3])
            else:
                self.assertEqual(deduped, set([1, 3]))

        self.assertEqual(next(deduper), 2)
        self.assertEqual(next(deduper), 4)

        if key in (hash, passthrough):
            if isinstance(deduped, tools.OrderedDict):
                self.assertEqual(list(deduped.keys()), [1, 3, 2, 4])
            elif isinstance(deduped, collections.Mapping):
                self.assertCountEqual(list(deduped.keys()), [1, 2, 3, 4])
            else:
                self.assertEqual(deduped, set([1, 2, 3, 4]))

        self.assertRaises(StopIteration, next, deduper)

    def _test_dedup_str(self, deduped, deduper, key=None):
        """Test filter_unique results for str."""
        if not key:
            key = passthrough

        self.assertEqual(len(deduped), 0)

        self.assertEqual(next(deduper), '1')
        self.assertEqual(next(deduper), '3')

        if key in (hash, passthrough):
            if isinstance(deduped, collections.Mapping):
                self.assertEqual(deduped.keys(), [key('1'), key('3')])
            else:
                self.assertEqual(deduped, set([key('1'), key('3')]))

        self.assertEqual(next(deduper), '2')
        self.assertEqual(next(deduper), '4')

        if key in (hash, passthrough):
            if isinstance(deduped, collections.Mapping):
                self.assertEqual(deduped.keys(), [key(i) for i in self.strs])
            else:
                self.assertEqual(deduped, set(key(i) for i in self.strs))

        self.assertRaises(StopIteration, next, deduper)

    def test_set(self):
        """Test filter_unique with a set."""
        deduped = set()
        deduper = tools.filter_unique(self.ints, container=deduped)
        self._test_dedup_int(deduped, deduper)

    def test_dict(self):
        """Test filter_unique with a dict."""
        deduped = dict()
        deduper = tools.filter_unique(self.ints, container=deduped)
        self._test_dedup_int(deduped, deduper)

    def test_OrderedDict(self):
        """Test filter_unique with a OrderedDict."""
        deduped = tools.OrderedDict()
        deduper = tools.filter_unique(self.ints, container=deduped)
        self._test_dedup_int(deduped, deduper)

    def test_int_hash(self):
        """Test filter_unique with ints using hash as key."""
        deduped = set()
        deduper = tools.filter_unique(self.ints, container=deduped, key=hash)
        self._test_dedup_int(deduped, deduper, hash)

    def test_int_id(self):
        """Test filter_unique with ints using id as key."""
        deduped = set()
        deduper = tools.filter_unique(self.ints, container=deduped, key=id)
        self._test_dedup_int(deduped, deduper, id)

    def test_obj(self):
        """Test filter_unique with objects."""
        deduped = set()
        deduper = tools.filter_unique(self.decs, container=deduped)
        self._test_dedup_int(deduped, deduper)

    def test_obj_hash(self):
        """Test filter_unique with objects using hash as key."""
        deduped = set()
        deduper = tools.filter_unique(self.decs, container=deduped, key=hash)
        self._test_dedup_int(deduped, deduper, hash)

    @unittest.expectedFailure
    def test_obj_id(self):
        """Test filter_unique with objects using id as key, which fails."""
        # Two objects which may be equal do not have the same id.
        deduped = set()
        deduper = tools.filter_unique(self.decs, container=deduped, key=id)
        self._test_dedup_int(deduped, deduper, id)

    def test_str(self):
        """Test filter_unique with str."""
        deduped = set()
        deduper = tools.filter_unique(self.strs, container=deduped)
        self._test_dedup_str(deduped, deduper)

    def test_str_hash(self):
        """Test filter_unique with str using hash as key."""
        deduped = set()
        deduper = tools.filter_unique(self.strs, container=deduped, key=hash)
        self._test_dedup_str(deduped, deduper, hash)

    @expected_failure_if(sys.version_info[0] >= 3)
    def test_str_id(self):
        """Test str using id as key fails on Python 3."""
        # str in Python 3 behave like objects.
        deduped = set()
        deduper = tools.filter_unique(self.strs, container=deduped, key=id)
        self._test_dedup_str(deduped, deduper, id)

    def test_for_resumable(self):
        """Test filter_unique is resumable after a for loop."""
        gen2 = tools.filter_unique(self.ints)
        deduped = []
        for item in gen2:
            deduped.append(item)
            if len(deduped) == 3:
                break
        self.assertEqual(deduped, [1, 3, 2])
        last = next(gen2)
        self.assertEqual(last, 4)
        self.assertRaises(StopIteration, next, gen2)

    def test_skip(self):
        """Test filter_unique with a container that skips items."""
        deduped = SkipList()
        deduper = tools.filter_unique(self.ints, container=deduped)
        deduped_out = list(deduper)
        self.assertCountEqual(deduped, deduped_out)
        self.assertEqual(deduped, set([2, 4]))

    def test_process_again(self):
        """Test filter_unique with an ignoring container."""
        deduped = ProcessAgainList()
        deduper = tools.filter_unique(self.ints, container=deduped)
        deduped_out = list(deduper)
        self.assertEqual(deduped_out, [1, 3, 2, 1, 1, 4])
        self.assertEqual(deduped, set([2, 4]))

    def test_stop(self):
        """Test filter_unique with an ignoring container."""
        deduped = ContainsStopList()
        deduped.stop_list = [2]
        deduper = tools.filter_unique(self.ints, container=deduped)
        deduped_out = list(deduper)
        self.assertCountEqual(deduped, deduped_out)
        self.assertEqual(deduped, set([1, 3]))

        # And it should not resume
        self.assertRaises(StopIteration, next, deduper)

        deduped = AddStopList()
        deduped.stop_list = [4]
        deduper = tools.filter_unique(self.ints, container=deduped)
        deduped_out = list(deduper)
        self.assertCountEqual(deduped, deduped_out)
        self.assertEqual(deduped, set([1, 2, 3]))

        # And it should not resume
        self.assertRaises(StopIteration, next, deduper)


class TestArgSpec(TestCase):

    """Test getargspec and ArgSpec from tools."""

    net = False

    @staticmethod
    def _method_test_args(a, b):
        """Test method with two positional arguments."""
        return (['a', 'b'], None, None, None)

    @staticmethod
    def _method_test_kwargs(a, b=42):
        """Test method with one positional and one keyword argument."""
        return (['a', 'b'], None, None, (42,))

    @staticmethod
    def _method_test_varargs(a, b, *var):
        """Test method with two positional arguments and var args."""
        return (['a', 'b'], 'var', None, None)

    @staticmethod
    def _method_test_varkwargs(a, b, **var):
        """Test method with two positional arguments and var kwargs."""
        return (['a', 'b'], None, 'var', None)

    @staticmethod
    def _method_test_vars(a, b, *args, **kwargs):
        """Test method with two positional arguments and both var args."""
        return (['a', 'b'], 'args', 'kwargs', None)

    def test_argspec(self):
        """Test the different methods."""
        for m in dir(TestArgSpec):
            if not m.startswith('_method_test_'):
                continue
            m = getattr(self, m)
            # all expect at least a and b
            expected = m(1, 2)
            self.assertEqual(tools.getargspec(m), expected)
            self.assertIsInstance(tools.getargspec(m), tools.ArgSpec)


if __name__ == '__main__':
    try:
        unittest.main()
    except SystemExit:
        pass
