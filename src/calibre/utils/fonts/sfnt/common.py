#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2012, Kovid Goyal <kovid at kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

from collections import OrderedDict, namedtuple
from struct import calcsize, unpack_from

from calibre.utils.fonts.sfnt.errors import UnsupportedFont
from polyglot.builtins import iteritems


class Unpackable:

    def __init__(self, raw, offset):
        self.raw, self.offset = raw, offset
        self.start_pos = offset

    def unpack(self, fmt, single_special=True):
        fmt = fmt.encode('ascii') if not isinstance(fmt, bytes) else fmt
        ans = unpack_from(b'>'+fmt, self.raw, self.offset)
        if single_special and len(ans) == 1:
            ans = ans[0]
        self.offset += calcsize(fmt)
        return ans


class SimpleListTable(list):
    'A table that contains a list of subtables'

    child_class = None

    def __init__(self, raw, offset):
        list.__init__(self)

        data = Unpackable(raw, offset)
        self.read_extra_header(data)

        count = data.unpack('H')
        for i in range(count):
            offset = data.unpack('H')
            self.append(self.child_class(raw, data.start_pos + offset))
        self.read_extra_footer(data)

    def read_extra_header(self, data):
        pass

    def read_extra_footer(self, data):
        pass


class ListTable(OrderedDict):
    'A table that contains an ordered mapping of table tag to subtable'

    child_class = None

    def __init__(self, raw, offset):
        OrderedDict.__init__(self)

        data = Unpackable(raw, offset)
        self.read_extra_header(data)

        count = data.unpack('H')
        for i in range(count):
            tag, coffset = data.unpack('4sH')
            self[tag] = self.child_class(raw, data.start_pos + coffset)

        self.read_extra_footer(data)

    def read_extra_header(self, data):
        pass

    def read_extra_footer(self, data):
        pass

    def dump(self, prefix=''):
        print(prefix, self.__class__.__name__, sep='')
        prefix += '  '
        for tag, child in iteritems(self):
            print(prefix, tag, sep='')
            child.dump(prefix=prefix+'  ')


class IndexTable(list):

    def __init__(self, raw, offset):
        data = Unpackable(raw, offset)
        self.read_extra_header(data)

        count = data.unpack('H')
        for i in range(count):
            self.append(data.unpack('H'))

    def read_extra_header(self, data):
        pass

    def dump(self, prefix=''):
        print(prefix, self.__class__.__name__, sep='')


class LanguageSystemTable(IndexTable):

    def read_extra_header(self, data):
        self.lookup_order, self.required_feature_index = data.unpack('2H')
        if self.lookup_order != 0:
            raise UnsupportedFont('This LanguageSystemTable has an unknown'
                    f' lookup order: 0x{self.lookup_order:x}')


class ScriptTable(ListTable):

    child_class = LanguageSystemTable

    def __init__(self, raw, offset):
        ListTable.__init__(self, raw, offset)

    def read_extra_header(self, data):
        start_pos = data.offset
        default_offset = data.unpack('H')
        self[b'default'] = (LanguageSystemTable(data.raw, start_pos +
            default_offset) if default_offset else None)


class ScriptListTable(ListTable):

    child_class = ScriptTable


class FeatureTable(IndexTable):

    def read_extra_header(self, data):
        self.feature_params = data.unpack('H')
        if False and self.feature_params != 0:
            # Source code pro sets this to non NULL
            raise UnsupportedFont(
                f'This FeatureTable has non NULL FeatureParams: 0x{self.feature_params:x}')


class FeatureListTable(ListTable):

    child_class = FeatureTable


class LookupTable(SimpleListTable):

    def read_extra_header(self, data):
        self.lookup_type, self.lookup_flag = data.unpack('2H')
        self.set_child_class()

    def set_child_class(self):
        raise NotImplementedError()

    def read_extra_footer(self, data):
        if self.lookup_flag & 0x0010:
            self.mark_filtering_set = data.unpack('H')


def ExtensionSubstitution(raw, offset, subtable_map={}):
    data = Unpackable(raw, offset)
    subst_format, extension_lookup_type, offset = data.unpack('2HL')
    if subst_format != 1:
        raise UnsupportedFont(f'ExtensionSubstitution has unknown format: 0x{subst_format:x}')
    return subtable_map[extension_lookup_type](raw, offset+data.start_pos)


CoverageRange = namedtuple('CoverageRange', 'start end start_coverage_index')


class Coverage:

    def __init__(self, raw, offset, parent_table_name):
        data = Unpackable(raw, offset)
        self.format, count = data.unpack('2H')

        if self.format not in {1, 2}:
            raise UnsupportedFont(f'Unknown Coverage format: 0x{self.format:x} in {parent_table_name}')
        if self.format == 1:
            self.glyph_ids = data.unpack(f'{count}H', single_special=False)
            self.glyph_ids_map = {gid:i for i, gid in
                    enumerate(self.glyph_ids)}
        else:
            self.ranges = []
            ranges = data.unpack(f'{3 * count}H', single_special=False)
            for i in range(count):
                start, end, start_coverage_index = ranges[i*3:(i+1)*3]
                self.ranges.append(CoverageRange(start, end, start_coverage_index))

    def coverage_indices(self, glyph_ids):
        '''Return map of glyph_id -> coverage index. Map contains only those
        glyph_ids that are covered by this table and that are present in
        glyph_ids.'''
        ans = OrderedDict()
        for gid in glyph_ids:
            if self.format == 1:
                idx = self.glyph_ids_map.get(gid, None)
                if idx is not None:
                    ans[gid] = idx
            else:
                for start, end, start_coverage_index in self.ranges:
                    if start <= gid <= end:
                        ans[gid] = start_coverage_index + (gid-start)
        return ans


class UnknownLookupSubTable:

    formats = {}

    def __init__(self, raw, offset):
        data = Unpackable(raw, offset)
        self.format = data.unpack('H')
        if self.format not in self.formats:
            raise UnsupportedFont(f'Unknown format for Lookup Subtable {self.__class__.__name__}: 0x{self.format:x}')
        if self.has_initial_coverage:
            coverage_offset = data.unpack('H') + data.start_pos
            self.coverage = Coverage(raw, coverage_offset, self.__class__.__name__)
        self.initialize(data)

    @property
    def has_initial_coverage(self):
        return True

    def all_substitutions(self, glyph_ids):
        ''' Return a set of all glyph ids that could be substituted for any
        subset of the specified glyph ids (which must be a set)'''
        raise NotImplementedError()

    def read_sets(self, data, read_item=None, set_is_index=False):
        count = data.unpack('H')
        sets = data.unpack(f'{count}H', single_special=False)
        coverage_to_items_map = []
        for offset in sets:
            # Read items in the set
            data.offset = start_pos = offset + data.start_pos
            count = data.unpack('H')
            item_offsets = data.unpack(f'{count}H', single_special=False)
            items = []
            for offset in item_offsets:
                data.offset = offset + start_pos
                if set_is_index:
                    items.append(offset)
                else:
                    items.append(read_item(data))
            coverage_to_items_map.append(items)
        return coverage_to_items_map
