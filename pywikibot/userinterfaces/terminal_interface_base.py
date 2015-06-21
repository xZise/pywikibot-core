# -*- coding: utf-8 -*-
"""Base for terminal user interfaces."""
#
# (C) Pywikibot team, 2003-2015
#
# Distributed under the terms of the MIT license.
#
from __future__ import unicode_literals

__version__ = '$Id$'

import getpass
import logging
import math
import re
import sys

from . import transliteration
import pywikibot
from pywikibot import config
from pywikibot.bot import VERBOSE, INFO, STDOUT, INPUT, WARNING
from pywikibot.tools import deprecated, PY2

transliterator = transliteration.transliterator(config.console_encoding)

colors = [
    'default',
    'black',
    'blue',
    'green',
    'aqua',
    'red',
    'purple',
    'yellow',
    'lightgray',
    'gray',
    'lightblue',
    'lightgreen',
    'lightaqua',
    'lightred',
    'lightpurple',
    'lightyellow',
    'white',
]

colorTagR = re.compile('\03{(?P<name>%s)}' % '|'.join(colors))


class Option(object):

    def __init__(self, stop=True):
        super(Option, self).__init__()
        self._stop = stop

    @staticmethod
    def formatted(text, options, default):
        formatted_options = []
        for option in options:
            formatted_options.append(option.format(default))
        return '{0} ({1})'.format(text, ', '.join(formatted_options))

    @property
    def stop(self):
        """Return whether this option stops asking."""
        return self._stop

    def test(self, value):
        return self.test_shortcut(value) or self.test_option(value)

class StandardOption(Option):

    def __init__(self, option, shortcut, stop=True):
        super(StandardOption, self).__init__(stop)
        self.option = option
        self.shortcut = shortcut.lower()

    def format(self, default):
        index = self.option.lower().find(self.shortcut)
        shortcut = self.shortcut
        if self.shortcut == default:
            shortcut = self.shortcut.upper()
        if index >= 0:
            return '{0}[{1}]{2}'.format(self.option[:index], shortcut,
                                         self.option[index + len(self.shortcut):])
        else:
            return '{0} [{1}]'.format(self.option, shortcut)

    def test_shortcut(self, shortcut):
        return self.shortcut.lower() == shortcut.lower()

    def test_option(self, option):
        return self.option.lower() == option.lower()

    def handled(self, value):
        if self.test(value):
            return self
        else:
            return None

    def result(self, value):
        return value

class NestedOption(StandardOption):
    def __init__(self, option, shortcut, description, options):
        super(NestedOption, self).__init__(option, shortcut, False)
        self.description = description
        self.options = options

    def format(self, default):
        self.output = UI.Option.formatted(self.description, self.options, default)
        return super(NestedOption, self).format(default)

    def test(self, value):
        if self.handled(value) is not None:
            return True
        else:
            return super(NestedOption, self).test(value)

    def handled(self, value):
        for option in self.options:
            handled = option.handled(value)
            if handled is not None:
                return handled
        else:
            return super(NestedOption, self).handled(value)

    def result(self, value):
        pywikibot.output(self.output)

class IntegerOption(Option):
    def __init__(self, minimum=1, maximum=None):
        super(IntegerOption, self).__init__()
        self.minimum = minimum
        self.maximum = maximum

    def test_shortcut(self, shortcut):
        try:
            value = int(shortcut)
        except ValueError:
            return False
        else:
            return ((self.minimum is None or value >= self.minimum) and
                    (self.maximum is None or value <= self.maximum))

    def test_option(self, option):
        return self.test_shortcut(option)

    def format(self, default):
        if self.minimum is not None or self.maximum is not None:
            _min = '' if self.minimum is None else str(self.minimum)
            _max = '' if self.maximum is None else str(self.maximum)
            rng = _min + '-' + _max
        else:
            rng = 'any'
        return '<number> [' + rng + ']'

    def result(self, value):
        return int(value)


class ChoiceException(StandardOption, Exception):

    """A choice for input_choice which result in this exception."""

    def result(self, value):
        return self


class QuitKeyboardInterrupt(ChoiceException, KeyboardInterrupt):

    """The user has cancelled processing at a prompt."""

    def __init__(self):
        """Constructor using the 'quit' ('q') in input_choice."""
        super(QuitKeyboardInterrupt, self).__init__('quit', 'q')


class UI:

    """Base for terminal user interfaces."""

    def __init__(self):
        """
        Initialize the UI.

        This caches the std-streams locally so any attempts to monkey-patch the
        streams later will not work.
        """
        self.stdin = sys.stdin
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.argv = sys.argv
        self.encoding = config.console_encoding
        self.transliteration_target = config.transliteration_target

        self.stderr = sys.stderr
        self.stdout = sys.stdout

    def init_handlers(self, root_logger, default_stream='stderr'):
        """Initialize the handlers for user output.

        This method initializes handler(s) for output levels VERBOSE (if
        enabled by config.verbose_output), INFO, STDOUT, WARNING, ERROR,
        and CRITICAL.  STDOUT writes its output to sys.stdout; all the
        others write theirs to sys.stderr.

        """
        if default_stream == 'stdout':
            default_stream = self.stdout
        elif default_stream == 'stderr':
            default_stream = self.stderr

        # default handler for display to terminal
        default_handler = TerminalHandler(self, strm=default_stream)
        if config.verbose_output:
            default_handler.setLevel(VERBOSE)
        else:
            default_handler.setLevel(INFO)
        # this handler ignores levels above INPUT
        default_handler.addFilter(MaxLevelFilter(INPUT))
        default_handler.setFormatter(
            TerminalFormatter(fmt="%(message)s%(newline)s"))
        root_logger.addHandler(default_handler)

        # handler for level STDOUT
        output_handler = TerminalHandler(self, strm=self.stdout)
        output_handler.setLevel(STDOUT)
        output_handler.addFilter(MaxLevelFilter(STDOUT))
        output_handler.setFormatter(
            TerminalFormatter(fmt="%(message)s%(newline)s"))
        root_logger.addHandler(output_handler)

        # handler for levels WARNING and higher
        warning_handler = TerminalHandler(self, strm=self.stderr)
        warning_handler.setLevel(WARNING)
        warning_handler.setFormatter(
            TerminalFormatter(fmt="%(levelname)s: %(message)s%(newline)s"))
        root_logger.addHandler(warning_handler)

        warnings_logger = logging.getLogger("py.warnings")
        warnings_logger.addHandler(warning_handler)

    def printNonColorized(self, text, targetStream):
        """
        Write the text non colorized to the target stream.

        To each line which contains a color tag a ' ***' is added at the end.
        """
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if i > 0:
                line = "\n" + line
            line, count = colorTagR.subn('', line)
            if count > 0:
                line += ' ***'
            if PY2:
                line = line.encode(self.encoding, 'replace')
            targetStream.write(line)

    printColorized = printNonColorized

    def _print(self, text, targetStream):
        if config.colorized_output:
            self.printColorized(text, targetStream)
        else:
            self.printNonColorized(text, targetStream)

    def output(self, text, toStdout=False, targetStream=None):
        """
        Output text to a stream.

        If a character can't be displayed in the encoding used by the user's
        terminal, it will be replaced with a question mark or by a
        transliteration.
        """
        if config.transliterate:
            # Encode our unicode string in the encoding used by the user's
            # console, and decode it back to unicode. Then we can see which
            # characters can't be represented in the console encoding.
            # We need to take min(console_encoding, transliteration_target)
            # the first is what the terminal is capable of
            # the second is how unicode-y the user would like the output
            codecedText = text.encode(self.encoding,
                                      'replace').decode(self.encoding)
            if self.transliteration_target:
                codecedText = codecedText.encode(self.transliteration_target,
                                                 'replace').decode(self.transliteration_target)
            transliteratedText = ''
            # Note: A transliteration replacement might be longer than the
            # original character, e.g. ч is transliterated to ch.
            prev = "-"
            for i in range(len(codecedText)):
                # work on characters that couldn't be encoded, but not on
                # original question marks.
                if codecedText[i] == '?' and text[i] != u'?':
                    try:
                        transliterated = transliterator.transliterate(
                            text[i], default='?', prev=prev, next=text[i + 1])
                    except IndexError:
                        transliterated = transliterator.transliterate(
                            text[i], default='?', prev=prev, next=' ')
                    # transliteration was successful. The replacement
                    # could consist of multiple letters.
                    # mark the transliterated letters in yellow.
                    transliteratedText += '\03{lightyellow}%s\03{default}' \
                                          % transliterated
                    # memorize if we replaced a single letter by multiple
                    # letters.
                    if len(transliterated) > 0:
                        prev = transliterated[-1]
                else:
                    # no need to try to transliterate.
                    transliteratedText += codecedText[i]
                    prev = codecedText[i]
            text = transliteratedText

        if not targetStream:
            if toStdout:
                targetStream = self.stdout
            else:
                targetStream = self.stderr

        self._print(text, targetStream)

    def _raw_input(self):
        if not PY2:
            return input()
        else:
            return raw_input()  # noqa

    def input(self, question, password=False, default='', force=False):
        """
        Ask the user a question and return the answer.

        Works like raw_input(), but returns a unicode string instead of ASCII.

        Unlike raw_input, this function automatically adds a colon and space
        after the question if they are not already present.  Also recognises
        a trailing question mark.

        @param question: The question, without trailing whitespace.
        @type question: basestring
        @param password: if True, hides the user's input (for password entry).
        @type password: bool
        @param default: The default answer if none was entered. None to require
            an answer.
        @type default: basestring
        @param force: Automatically use the default
        @type force: bool
        @rtype: unicode
        """
        assert(not password or not default)
        end_marker = ':'
        question = question.strip()
        if question[-1] == ':':
            question = question[:-1]
        elif question[-1] == '?':
            question = question[:-1]
            end_marker = '?'
        if default:
            question = question + ' (default: %s)' % default
        question = question + end_marker
        if force:
            self.output(question + '\n')
            return default
        # sound the terminal bell to notify the user
        if config.ring_bell:
            sys.stdout.write('\07')
        # TODO: make sure this is logged as well
        while True:
            self.output(question + ' ')
            text = self._input_reraise_cntl_c(password)
            if text:
                return text

            if default is not None:
                return default

    def _input_reraise_cntl_c(self, password):
        """Input and decode, and re-raise Control-C."""
        try:
            if password:
                # Python 3 requires that stderr gets flushed, otherwise is the
                # message only visible after the query.
                self.stderr.flush()
                text = getpass.getpass('')
            else:
                text = self._raw_input()
        except KeyboardInterrupt:
            raise QuitKeyboardInterrupt()
        if PY2:
            text = text.decode(self.encoding)
        return text

    def input_choice(self, question, options, default=None, return_shortcut=True,
                     automatic_quit=True, force=False):
        """
        Ask the user and returns a value from the options.

        Depending on the options setting return_shortcut to False may not be
        sensible when the option supports multiple values as it'll return an
        ambiguous index.

        @param question: The question, without trailing whitespace.
        @type question: basestring
        @param options: All available options. Each entry contains the full
            length answer and a shortcut of only one character. The shortcut
            must not appear in the answer. Alternatively they may be a
            ChoiceException (or subclass) instance which has a full option and
            shortcut. It will raise that exception when selected.
        @type options: iterable containing sequences of length 2 or
            ChoiceException
        @param default: The default answer if no was entered. None to require
            an answer.
        @type default: basestring
        @param return_shortcut: Whether the shortcut or the index in the option
            should be returned.
        @type return_shortcut: bool
        @param automatic_quit: Adds the option 'Quit' ('q') if True and throws a
            L{QuitKeyboardInterrupt} if selected.
        @type automatic_quit: bool
        @param force: Automatically use the default
        @type force: bool
        @return: If return_shortcut the shortcut of options or the value of
            default (if it's not None). Otherwise the index of the answer in
            options. If default is not a shortcut, it'll return -1.
        @rtype: int (if not return_shortcut), lowercased basestring (otherwise)
        """
        options = list(options)
        if len(options) == 0:
            raise ValueError(u'No options are given.')
        if automatic_quit:
            options += [QuitKeyboardInterrupt()]
        default_index = -1
        for i, option in enumerate(options):
            if not isinstance(option, Option):
                if len(option) != 2:
                    raise ValueError(u'Option #{0} does not consist of an '
                                     u'option and shortcut.'.format(i))
                options[i] = StandardOption(*option)

            if default and options[i].handled(default):
                default_index = i
            # TODO: Test for uniquity

        question = Option.formatted(question, options, default)
        handled = False
        while not handled:
            if force:
                self.output(question + '\n')
                answer = None
            else:
                answer = self.input(question)
            if default and not answer:  # nothing entered
                answer = default
                index = default_index
            else:
                for index, option in enumerate(options):
                    if option.handled(answer):
                        answer = option.result(answer)
                        handled = option.stop
                        break

        if isinstance(answer, ChoiceException):
            raise answer
        elif not return_shortcut:
            return index
        else:
            return answer

    @deprecated('input_choice')
    def inputChoice(self, question, options, hotkeys, default=None):
        """
        Ask the user a question with a predefined list of acceptable answers.

        DEPRECATED: Use L{input_choice} instead!

        Directly calls L{input_choice} with the options and hotkeys zipped
        into a tuple list. It always returns the hotkeys and throws no
        L{QuitKeyboardInterrupt} if quit was selected.
        """
        return self.input_choice(question=question, options=zip(options, hotkeys),
                                 default=default, return_shortcut=True,
                                 automatic_quit=False)

    def input_list_choice(self, question, answers, default=None, force=False):
        """Ask the user to select one entry from a list of entries."""
        message = question
        clist = answers

        line_template = u"{{0: >{0}}}: {{1}}".format(int(math.log10(len(clist)) + 1))
        for n, i in enumerate(clist):
            pywikibot.output(line_template.format(n + 1, i))

        while True:
            choice = self.input(message, default=default, force=force)
            try:
                choice = int(choice) - 1
            except ValueError:
                try:
                    choice = clist.index(choice)
                except IndexError:
                    choice = -1

            # User typed choice number
            if 0 <= choice < len(clist):
                return clist[choice]
            else:
                pywikibot.error("Invalid response")

    def editText(self, text, jumpIndex=None, highlight=None):
        """Return the text as edited by the user.

        Uses a Tkinter edit box because we don't have a console editor

        @param text: the text to be edited
        @type text: unicode
        @param jumpIndex: position at which to put the caret
        @type jumpIndex: int
        @param highlight: each occurrence of this substring will be highlighted
        @type highlight: unicode
        @return: the modified text, or None if the user didn't save the text
            file in his text editor
        @rtype: unicode or None
        """
        try:
            from pywikibot.userinterfaces import gui
        except ImportError as e:
            print('Could not load GUI modules: %s' % e)
            return text
        editor = gui.EditBoxWindow()
        return editor.edit(text, jumpIndex=jumpIndex, highlight=highlight)

    def askForCaptcha(self, url):
        """Show the user a CAPTCHA image and return the answer."""
        try:
            import webbrowser
            pywikibot.output(u'Opening CAPTCHA in your web browser...')
            if webbrowser.open(url):
                return pywikibot.input(
                    u'What is the solution of the CAPTCHA that is shown in '
                    u'your web browser?')
            else:
                raise
        except:
            pywikibot.output(u'Error in opening web browser: %s'
                             % sys.exc_info()[0])
            pywikibot.output(
                u'Please copy this url to your web browser and open it:\n %s'
                % url)
            return pywikibot.input(
                u'What is the solution of the CAPTCHA at this url ?')

    def argvu(self):
        """Return the decoded arguments from argv."""
        try:
            return [s.decode(self.encoding) for s in self.argv]
        except AttributeError:  # in python 3, self.argv is unicode and thus cannot be decoded
            return [s for s in self.argv]


class TerminalHandler(logging.Handler):

    """A handler class that writes logging records to a terminal.

    This class does not close the stream,
    as sys.stdout or sys.stderr may be (and usually will be) used.

    Slightly modified version of the StreamHandler class that ships with
    logging module, plus code for colorization of output.

    """

    # create a class-level lock that can be shared by all instances
    import threading
    sharedlock = threading.RLock()

    def __init__(self, UI, strm=None):
        """Initialize the handler.

        If strm is not specified, sys.stderr is used.

        """
        logging.Handler.__init__(self)
        # replace Handler's instance-specific lock with the shared class lock
        # to ensure that only one instance of this handler can write to
        # the console at a time
        self.lock = TerminalHandler.sharedlock
        if strm is None:
            strm = sys.stderr
        self.stream = strm
        self.formatter = None
        self.UI = UI

    def flush(self):
        """Flush the stream."""
        self.stream.flush()

    def emit(self, record):
        """Emit the record formatted to the output and return it."""
        if record.name == 'py.warnings':
            # Each warning appears twice
            # the second time it has a 'message'
            if 'message' in record.__dict__:
                return

            # Remove the last line, if it appears to be the warn() call
            msg = record.args[0]
            is_useless_source_output = any(
                s in msg for s in
                (str('warn('), str('exceptions.'), str('Warning)'), str('Warning,')))

            if is_useless_source_output:
                record.args = ('\n'.join(record.args[0].splitlines()[0:-1]),)

            if 'newline' not in record.__dict__:
                record.__dict__['newline'] = '\n'

        text = self.format(record)
        return self.UI.output(text, targetStream=self.stream)


class TerminalFormatter(logging.Formatter):

    """Terminal logging formatter."""

    pass


class MaxLevelFilter(logging.Filter):

    """Filter that only passes records at or below a specific level.

    (setting handler level only passes records at or *above* a specified level,
    so this provides the opposite functionality)

    """

    def __init__(self, level=None):
        """Constructor."""
        self.level = level

    def filter(self, record):
        """Return true if the level is below or equal to the set level."""
        if self.level:
            return record.levelno <= self.level
        else:
            return True
