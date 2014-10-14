#!/usr/bin/python
# -*- coding: utf-8  -*-
"""
Robot to import a CSV file into a Wikibase repository.

Requires an input CSV using tabs as delimiter (TSV), containing a table where
the first row is the header of the columns and each column is one of:
* title (label or alias), description, id (e.g. 'Q42'), uselang (e.g. 'fr'),
* property id, e.g. 'P31',
* property used as reference for a previous property, in the form 'P31_R_P854',
* same for qualifiers: 'P31_Q_P#'.

Each row must be about a single item, though an item may have multiple rows
about it. Within a row, each column applies to all the others where possible:
* title and/or id define the item for which all other columns are used;
* uselang applies to the label, alias and description [anything else?],
* references apply to all properties.

Empty cell means lack of information.
In certain circumstances, the program may fill blanks with information retrieved
from Wikibase while interacting with it.

As for dates, this first version probably assumes they have a precision of
year.

Currently, the script will fail horribly if any of the assumptions is not met.

"""
# (C) Pywikibot team and Fondazione BEIC, 2015
#
# Distributed under the terms of MIT License.
#
__version__ = '0.0.1-alpha'

import sys
import re
from collections import namedtuple, defaultdict

if sys.version_info[0] > 2:
    import csv
else:
    try:
        import unicodecsv as csv
    except ImportError:
        print('%s: unicodecsv package required for Python 2' % __name__)
        sys.exit(1)

import pywikibot
from pywikibot import pagegenerators, WikidataBot
from pywikibot.page import ItemPage, Property, Claim

# from scripts.harvest_template import HarvestRobot
# from scripts.claimit import ClaimRobot


class WikibaseCSVBot(WikidataBot):

    """A bot to create new items."""

    def __init__(self, generator, **kwargs):
        """Only accepts options defined in availableOptions."""
        self.availableOptions.update({
            'file': 'input.csv',
        })

        super(WikibaseCSVBot, self).__init__(**kwargs)
        self.generator = pagegenerators.PreloadingGenerator(generator)
        # FIXME: will not work if the repo is not client of itself.
        # Force the user to start from an actual client, and use that
        # client in get_current_entity() as well?
        self.repo = pywikibot.Site().data_repository()
        self.filename = self.getOption('file')
        self.summary = u'Import CSV data'

        store, props = self.read_CSV(self.filename)
        # TODO: Normalise the header to lowercase or uppercase?
        if not store or not props:
            pywikibot.error(u'Cannot import CSV')
            sys.exit(1)
            return

        for row in store:
            pywikibot.output(u'Now doing "%s"' % row.title)
            current = self.get_current_entity(row)
            self.prepare_entity(current, props, row)
            # Save the udpated item into the wiki.
            # item.editEntity(data=item.toJSON(), summary=self.summary)
            # TODO: Also output/merge the updated data to CSV?

    def read_CSV(self, filename):
        """Read the CSV."""
        if self.filename.endswith('.bz2'):
            import bz2
            f = bz2.BZ2File(self.filename)
        elif self.filename.endswith('.gz'):
            import gzip
            f = gzip.open(self.filename)
        else:
            # Assume it's an uncompressed CSV file
            f = open(self.filename, 'r')

        # FIXME: f itself should be properly encoded, no "encoding" in py3 csv
        source = csv.reader(f, delimiter='\t', encoding='utf-8')
        header = source.next()
        try:
            props = self.validate_header(header)
        except ValueError:
            f.close()
            return None, None
        else:
            store = namedtuple(u'store', header)
            store = [store(*row) for row in source]
            f.close()
            return store, props

    def validate_header(self, header):
        """Check the CSV has columns as we need them."""
        if 'id' not in header:
            raise ValueError(u'We need Q# in column "id". Empty to create from scratch.')
        if ('title' in header or 'aliases' in header) and 'uselang' not in header:
            raise ValueError(u'You specified title or alias, but not uselang.')

        refs = defaultdict(lambda: {'Q': {}, 'R': {}})
        props = {}
        for i, cell in enumerate(header):
            if cell in ['title', 'aliases', 'uselang', 'id']:
                continue
            # instead of case insensitive it's making it upper case so
            # p42_r_p1337 will also work with a P42 column
            prop = re.match(r'(P\d+)(?:_([QR])_(P\d+))?', cell.upper())
            if not prop:
                raise ValueError(u'Column {0} ("{1}") was not recognised'.format(i, cell))
            if prop.group(2):
                # is refprop
                ref_map = refs[prop.group(1)][prop.group(2)]
                if prop.group(3) in ref_map:
                    raise ValueError('… sth …')
                ref_map[prop.group(3)] = i
            else:
                if prop.group(1) in props:
                    raise ValueError(u'Property {0} is defined multiple times.'.format(prop.group(1)))
                props[prop.group(1)] = i

        undef_prop = set(refs) - set(props)
        if undef_prop:
            raise ValueError(u'Undefined property/properties: "{0}"'.format(u'", "'.join(sorted(undef_prop))))

        # normalize references into tuples; each tuple in the tuple is the
        # name of the property, the column of the property, a list of sources
        # and a list of qualifiers. the list of sources/qualifiers are tuples
        # with the property and column
        parsed_header = []
        for prop in props:
            ref_map = refs[prop]
            parsed_header += [(prop, props[prop], list(ref_map['Q'].items()),
                               list(ref_map['Q'].items()))]

        return parsed_header

    def get_current_entity(self, row):
        """Return any existing item matching this CSV row."""
        # TODO: should also use label or perhaps sitelink,
        # to find existing items and merge data into them.
        # May require pywikibot to implement wbsearchentities
        item = None
        if row.id:
            item = pywikibot.ItemPage(self.repo, 'Q%d' % row.id)
        elif row.title and row.uselang:
            # FIXME: replace with proper search or don't hardcode Wikipedia
            pywikibot.output(u'Looking for item via %s.wikipedia' % row.uselang)
            wikipedia = pywikibot.Site(row.uselang, u'wikipedia')
            article = pywikibot.Page(wikipedia, row.title)
            if article.isDisambig():
                pywikibot.output(u'"%s" is a disambig, will create new item.' % row.title)
                return None
            elif article.isRedirectPage():
                article = article.getRedirectTarget()
            if article.exists():
                item = ItemPage.fromPage(article)

        if item and item.exists():
            pywikibot.output(u'Found an item for "%s".' % row.title)
            item.get()
            # TODO: if the item exists, we might want to fetch
            # some or all of the statements and write them back
            # into the source CSV or a clone CSV, to allow syncs.
        else:
            pywikibot.output(u'No item found for "%s", will create.' % row.title)
            item = None
        return item

    def prepare_entity(self, item, props, row):
        """Take any current data and merge CSV row into it."""
        if item and item.exists():
            if row.title and row.uselang:
                if row.uselang not in item._content['labels'].keys():
                    item._content['labels'][row.uselang] = [row.title]
                elif item._content['labels'][row.uselang]['value'] != row.title:
                    alias = {'language': row.uselang, 'value': row.title}
                    if row.uselang not in item._content['aliases'].keys():
                        item._content['aliases'][row.uselang] = alias
                    elif row.title not in item._content['aliases'][row.uselang]:
                        item._content['aliases'][row.uselang].append(alias)
        else:
            if row.title and row.uselang:
                # Anything to borrow from NewItemRobot? Seems not, that's only
                # to create new items from existing pages (sitelinks).
                pywikibot.output(u'Create item: %s' % row.title)
                # FIXME: avoid creating duplicates
                item = ItemPage(self.repo, '-1')
                data = {'labels':
                            {row.uselang:
                                 {'language': row.uselang,
                                  'value': row.title}
                             }
                        }
                # Return the data and edit in __init__?
                item.editEntity(data, summary=self.summary)
                # item.editLabels({row.uselang: row.title})
            else:
                pywikibot.output(u'Cannot create item for this row: label or language missing')
                return None

        for prop in props:
            pid = prop[0]
            val = row[prop[1]]
            claim = self.make_claim(pid=pid, val=val)
            try:
                if pid in item.claims.keys():
                    pywikibot.output(u'Property already used, skipping...')
                    continue
            except AttributeError:
                # No claims at all, all good.
                pass
            # TODO: figure out how to integrate existing statements.
            # Reuse ClaimRobot.treat() functionality?
            item.addClaim(claim, bot=True)

            # Look for references and qualifiers set in other columns
            # for this column's statements.
            # TODO: figure out whether to allow multi-statement
            # references (e.g. item+url), which would use addSources
            for ref in prop[2]:
                ref = self.make_claim(pid=ref[0], val=row[ref[1]])
                claim.addSource(ref, bot=True)

            for qual in prop[3]:
                qual = self.make_claim(pid=qual[0], val=row[qual[1]])
                claim.addQualifier(qual, bot=True)

            # We should be done with this statement and its accessories

        return item

    def make_claim(self, pid=None, val=None):
        """Produce a claim object from two strings, property ID and value."""
        prop = Property(self.repo, id=pid)
        claim = Claim(self.repo, prop.getID())
        pywikibot.output(u'Making claim: %s → %s' % (pid, val))

        if prop.type == 'time':
            # We let WbTime guess precision, but autodetection
            # looks for None which fromTimestr never sets.
            # TODO: tell about WbTime.FORMATSTR
            pywikibot.output(u'The property wants a WbTime.')
            value = pywikibot.WbTime.fromTimestr(val,
                                                 precision=None)
        elif prop.type == 'wikibase-item':
            pywikibot.output(u'The property wants an item.')
            value = ItemPage(self.repo, title=val)
        else:
            pywikibot.output(u'The property wants whatever?')
            value = val

        try:
            claim.setTarget(value)
            return claim
        except ValueError:
            # TODO: more actionable error message
            pywikibot.error(u'Incorrect value %s' % val)
            return


def main():
    """Process global args and prepare generator args parser."""
    local_args = pywikibot.handle_args()
    gen = pagegenerators.GeneratorFactory()

    options = {}
    for arg in local_args:
        if arg.startswith('-csv:'):
            options['file'] = arg[len('-csv:'):]
        elif not gen.handleArg(arg):
            options[arg[1:].lower()] = True

    generator = gen.getCombinedGenerator()
    # If not generator:
    if not options.get('file'):
        pywikibot.error(u'You need to specify a -csv. See -help for more.')
        return False

    bot = WikibaseCSVBot(generator, **options)
    bot.run()

if __name__ == '__main__':
    main()
