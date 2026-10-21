# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which inspect the run logs of the playbook executions
('execution.list' and 'execution.output').

Every execution writes a run log in the user's cache directory (see
'runs'); these tools are the query interface over it. The answer of a
check/run stays deliberately small; the details — which task changed,
what failed, the diff a template task rendered — are fetched on
demand, with caps, instead of shipping the whole log in every answer.

A query serves whatever the log contains: what got written was decided
at run time, by the execution policy ('execute_diff', 'execute_debug')
— that is the boundary. Re-gating at read time would be false comfort;
the file is on disk either way.

The reads are lock-free: the logs live outside the project, and the
execution holding the project lock may still be writing its log while
a query runs.
"""

import collections
import logging
import os
import re

from datetime import datetime, timezone

from .. import runs

from .common import error_result, text_result

logger = logging.getLogger(__name__)

# The caps of a query result; a bad pattern must not re-import the
# whole log through the tool
DEFAULT_MAX_LINES = 200
MAX_LINES = 1000
MAX_LINE_LENGTH = 1000

# The canned queries
KINDS = ('changed', 'failed', 'unreachable', 'recap', 'warnings',
         'tail')

_BLOCK_HEADER = re.compile(r'^(TASK|PLAY) \[')
_CHANGED_LINE = re.compile(r'^changed: \[')
_FAILED_LINE = re.compile(r'^fatal: .*FAILED')
_UNREACHABLE_LINE = re.compile(r'^fatal: .*UNREACHABLE')
_RECAP_HEADER = re.compile(r'^PLAY RECAP \*+')
_MARKER = re.compile(
        r'^debops-mcp: the execution finished with exit code (\d+)$',
        re.M)


def _scan_lines(path):
    """The lines of a log file, one at a time (the file may grow
    while it is read)."""
    with open(path, 'r', encoding='utf-8', errors='replace') as fp:
        for line in fp:
            yield line.rstrip('\n')


def _shorten(line):
    if len(line) > MAX_LINE_LENGTH:
        return line[:MAX_LINE_LENGTH] + '…'
    return line


def _grep(path, pattern, context):
    """Yield the matching lines with the surrounding context, the
    disjoint windows separated by '…', streaming: a huge log does not
    load into memory."""
    before = collections.deque(maxlen=context)
    skip = 0
    fresh = True
    for line in _scan_lines(path):
        if pattern.search(line):
            if not fresh:
                yield '…'
            for previous in before:
                yield _shorten(previous)
            before.clear()
            yield _shorten(line)
            skip = context
            fresh = True
        elif skip > 0:
            yield _shorten(line)
            skip -= 1
        else:
            if context:
                before.append(line)
            fresh = False


def _result_blocks(path, result_re):
    """Yield (task header, matching result lines) for the tasks which
    have such a line, e.g. the tasks which changed something."""
    header = None
    hits = []
    for line in _scan_lines(path):
        if _BLOCK_HEADER.match(line):
            if header is not None and hits:
                yield header, hits
            header = line
            hits = []
        elif result_re.match(line):
            hits.append(_shorten(line))
    if header is not None and hits:
        yield header, hits


def _recap_blocks(lines):
    """Yield the recap blocks: the header and its per-host lines."""
    in_recap = False
    for line in lines:
        if _RECAP_HEADER.match(line):
            in_recap = True
            yield line
        elif in_recap:
            if line.strip():
                yield _shorten(line)
            else:
                in_recap = False


def _log_exit_code(path):
    """The exit code recorded by the detached wrapper's marker."""
    match = _MARKER.search(runs.read_tail(path, 4096))
    return int(match.group(1)) if match is not None else None


def _looks_alien(path):
    """Whether the log does not look like the default ansible output.

    A custom stdout callback prints no play or task headers; a canned
    query on such a log must not come back empty-handed. Checked on
    the head of the log, where the headers would live.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fp:
            head = fp.read(65536)
    except OSError:
        return False
    if not head.strip():
        return False
    return re.search(r'^(TASK|PLAY) \[|^PLAY RECAP ', head,
                     re.M) is None


def _cap(lines, max_lines):
    """Bound the line list; the marker reports what was cut."""
    if len(lines) <= max_lines:
        return lines, 0
    cut = len(lines) - max_lines
    return lines[:max_lines], cut


def tool_execution_list(project, policy, arguments):
    entries = []
    for execution_id, path in runs.list_logs(project):
        entry = {'id': execution_id, 'log': path}
        metadata = runs.read_metadata(path)
        if metadata is not None:
            for key in ('mode', 'playbooks', 'hosts', 'startedAt',
                        'finishedAt', 'exitCode', 'status'):
                if key in metadata:
                    entry[key] = metadata[key]
        else:
            # A log without a sidecar predates the metadata; the file
            # still answers the basics
            try:
                entry['startedAt'] = datetime.fromtimestamp(
                        os.path.getmtime(path), timezone.utc
                        ).isoformat().replace('+00:00', 'Z')
            except OSError:
                pass
            exit_code = _log_exit_code(path)
            if exit_code is not None:
                entry['exitCode'] = exit_code
                entry['status'] = 'done'
        try:
            entry['logBytes'] = os.path.getsize(path)
        except OSError:
            pass
        entries.append(entry)

    if not entries:
        return text_result('No executions recorded for the active '
                           'project view.', structured=[])

    lines = ['# Executions (newest last)', '']
    for entry in entries:
        parts = ['{}:'.format(entry['id'])]
        if entry.get('mode'):
            parts.append(entry['mode'])
        if entry.get('playbooks'):
            parts.append(' '.join(entry['playbooks']))
        if 'exitCode' in entry:
            parts.append('exit {}'.format(entry['exitCode']))
        else:
            parts.append('no outcome recorded (still running, or the '
                         'server died before the run finished)')
        if entry.get('startedAt'):
            parts.append(entry['startedAt'])
        if entry.get('hosts'):
            parts.append('({} hosts)'.format(len(entry['hosts'])))
        lines.append('  '.join(parts))
    return text_result('\n'.join(lines), structured=entries)


def tool_execution_output(project, policy, arguments):
    kind = arguments.get('kind')
    pattern = arguments.get('pattern')
    if kind is not None and pattern is not None:
        return error_result("pass either 'kind' or 'pattern', not both")
    if kind is not None and kind not in KINDS:
        return error_result('unknown kind {!r}; the kinds are: {}'
                            .format(kind, ', '.join(KINDS)))
    if pattern is not None:
        try:
            pattern = re.compile(pattern)
        except re.error as errmsg:
            return error_result('invalid pattern: {}'.format(errmsg))
    try:
        context = int(arguments.get('context', 3))
        if context < 0:
            raise ValueError()
    except (TypeError, ValueError):
        return error_result("'context' must be a non-negative integer")
    try:
        max_lines = int(arguments.get('max_lines', DEFAULT_MAX_LINES))
        if not 1 <= max_lines <= MAX_LINES:
            raise ValueError()
    except (TypeError, ValueError):
        return error_result("'max_lines' must be between 1 and {}"
                            .format(MAX_LINES))

    execution_id = arguments.get('id', 'last')
    if execution_id != 'last':
        try:
            execution_id = int(execution_id)
        except (TypeError, ValueError):
            return error_result("'id' must be an integer, or 'last'")
    execution_id, log = runs.execution_log(project, execution_id)
    if execution_id is None:
        known = [entry_id for entry_id, _path in runs.list_logs(project)]
        hint = ('known executions: {}'
                .format(', '.join(str(entry) for entry in known))
                if known else 'no executions recorded in this view')
        return error_result('no such execution: {} ({})'
                            .format(arguments.get('id', 'last'), hint))

    query = kind if kind is not None else (
            'pattern {!r}'.format(pattern.pattern)
            if pattern is not None else 'tail')

    if pattern is not None:
        # A free regex over the log lines, with context; kind and
        # pattern are mutually exclusive (checked above)
        matched = list(_grep(log, pattern, context))
        total = len(matched)
        lines, cut = _cap(matched, max_lines)
    elif kind in (None, 'tail'):
        # The tail is the default query; it works on any log
        lines = [_shorten(line) for line in
                 runs.read_tail(log, MAX_LINE_LENGTH * max_lines
                                ).splitlines()[-max_lines:]]
        total = len(lines)
        cut = 0
    elif kind == 'recap':
        matched = list(_recap_blocks(_scan_lines(log)))
        total = len(matched)
        lines, cut = _cap(matched, max_lines)
    elif kind == 'warnings':
        # Both the [WARNING] lines and the [DEPRECATION WARNING] ones —
        # the deprecations are usually what the question is about
        matched = [_shorten(line) for line in _scan_lines(log)
                   if '[WARNING]' in line or '[DEPRECATION WARNING]'
                   in line]
        total = len(matched)
        lines, cut = _cap(matched, max_lines)
    else:
        result_re = {'changed': _CHANGED_LINE, 'failed': _FAILED_LINE,
                     'unreachable': _UNREACHABLE_LINE}[kind]
        matched = []
        for header, hits in _result_blocks(log, result_re):
            if matched:
                matched.append('')
            matched.append(_shorten(header))
            matched.extend(hits)
        total = len(matched)
        lines, cut = _cap(matched, max_lines)

    cut_note = cut > 0
    truncated_note = ''
    if cut_note:
        truncated_note = ('\n\n… ({} more lines matched; narrow the '
                          'query)'.format(cut))

    if not lines:
        # A canned query which matches nothing on a log without the
        # ansible shape means a custom stdout callback; the tail is
        # the answer which always works. On a normal log "no matches"
        # is the honest answer: nothing changed, nothing failed.
        if kind not in (None, 'tail') and _looks_alien(log):
            tail = runs.read_tail(log, MAX_LINE_LENGTH * 40
                                  ).splitlines()[-40:]
            return text_result(
                    'execution {}: no {} lines in the log (a custom '
                    'stdout callback changes the format); the tail:\n\n'
                    '{}'.format(execution_id, query, '\n'.join(tail)),
                    structured={'executionId': execution_id,
                                'query': query, 'matched': 0,
                                'returned': len(tail), 'truncated': False,
                                'degraded': True})
        return text_result(
                'execution {}: no matches ({})'.format(execution_id,
                                                       query),
                structured={'executionId': execution_id, 'query': query,
                            'matched': 0, 'returned': 0,
                            'truncated': False})

    text = '# execution {} — {}\n\n{}{}\n'.format(
            execution_id, query, '\n'.join(lines), truncated_note)
    structured = {'executionId': execution_id, 'query': query,
                  'matched': total, 'returned': len(lines),
                  'truncated': cut_note}
    return text_result(text, structured=structured)
