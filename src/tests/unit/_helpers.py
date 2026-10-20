# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Helpers shared by the MCP unit tests (not a test module)."""


def check_schema(data, schema, path='$'):
    """A tiny JSON Schema checker for the subset the tools use.

    Covers 'type', 'required', 'properties' and 'items'. Returns the
    list of violations; empty means the data conforms.
    """
    errors = []
    expected = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str,
             'boolean': bool}
    if isinstance(expected, list):
        # A union of types ('null' means Python's None); any
        # alternative may match
        for alternative in expected:
            if alternative == 'null':
                if data is None:
                    return errors
                continue
            if not check_schema(data, {'type': alternative}, path):
                return errors
        errors.append('{}: expected {}, got {}'.format(
                path, '/'.join(expected), type(data).__name__))
        return errors
    if expected in types:
        if not isinstance(data, types[expected]):
            errors.append('{}: expected {}, got {}'.format(
                    path, expected, type(data).__name__))
            return errors
    elif expected in ('integer', 'number'):
        valid = (not isinstance(data, bool)
                 and isinstance(data, int if expected == 'integer'
                                else (int, float)))
        if not valid:
            errors.append('{}: expected {}, got {}'.format(
                    path, expected, type(data).__name__))
            return errors
    if isinstance(data, dict) and expected == 'object':
        for key in schema.get('required', []):
            if key not in data:
                errors.append('{}: missing {!r}'.format(path, key))
        properties = schema.get('properties', {})
        for key, value in data.items():
            if key in properties:
                errors.extend(check_schema(value, properties[key],
                                           '{}.{}'.format(path, key)))
    if isinstance(data, list) and expected == 'array':
        item_schema = schema.get('items', {})
        for index, item in enumerate(data):
            errors.extend(check_schema(item, item_schema,
                                       '{}[{}]'.format(path, index)))
    return errors
