# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The 'keyring_fetch' tool: fetch a GPG key into the project keyring.

The keyring holds trust anchors for third-party software (APT
repositories, signed data), so the agent names a source and an expected
fingerprint while the server fetches and verifies the key itself; the
key material never transits the model. The file is stored as
'0x<FINGERPRINT>.asc', the convention of the debops.keyring role.
"""

import fnmatch
import logging
import os
import re
import subprocess
import tempfile
import urllib.request

from .common import error_result, exclusive_project, text_result
from ..policy import confine_under
from ..server import _supports_form_elicitation

logger = logging.getLogger(__name__)

# Keys are kilobytes; anything larger is not a key
KEY_SIZE_LIMIT = 256 * 1024

FETCH_TIMEOUT = 30  # seconds

# Full v4 fingerprints only: short or long key IDs are spoofable
FINGERPRINT_PATTERN = re.compile(r'^(?:0x)?([0-9a-fA-F]{40})$')


def normalize_fingerprint(value):
    """A fingerprint as 40 uppercase hex characters; ValueError if not.

    Whitespace is stripped first, so the spaced display form is
    accepted as well.
    """
    match = FINGERPRINT_PATTERN.match(
            re.sub(r'\s+', '', str(value or '').strip()))
    if not match:
        raise ValueError('not a full GPG fingerprint: {!r}'.format(value))
    return match.group(1).upper()


def format_fingerprint(fingerprint):
    """A fingerprint as humans know it from gpg: groups of four."""
    return ' '.join(fingerprint[index:index + 4]
                    for index in range(0, len(fingerprint), 4))


def _gpg(project):
    return project.config.raw.get('binaries', {}).get('gpg', 'gpg')


def _fetch_url(url):
    """Fetch key material over HTTPS; the URL's scheme is enforced."""
    if not str(url).lower().startswith('https://'):
        raise ValueError('only https:// sources are allowed: '
                         + str(url))
    request = urllib.request.Request(
            url, headers={'User-Agent': 'debops-mcp keyring_fetch'})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as \
                response:
            data = response.read(KEY_SIZE_LIMIT + 1)
    except Exception as errmsg:
        raise ValueError('cannot fetch the key: {}'.format(errmsg))
    if len(data) > KEY_SIZE_LIMIT:
        raise ValueError('the key is larger than {} bytes'.format(
                KEY_SIZE_LIMIT))
    return data


def _gpg_run(project, homedir, args, input_data=None):
    command = [_gpg(project), '--batch', '--no-tty', '--homedir', homedir,
               '--no-default-keyring'] + args
    if isinstance(input_data, str):
        input_data = input_data.encode()
    try:
        result = subprocess.run(
                command, input=input_data,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=FETCH_TIMEOUT)
    except FileNotFoundError:
        raise ValueError('the gpg command was not found')
    except subprocess.TimeoutExpired:
        raise ValueError('gpg timed out')
    if result.returncode != 0:
        raise ValueError('gpg failed: '
                         + result.stderr.decode(errors='replace')
                         .strip()[-500:])
    return result.stdout


def _import_and_verify(project, homedir, keyserver, expected, content):
    """Get the key into the temporary homedir and describe it.

    Returns (fingerprint, user IDs). ValueError on any failure.
    """
    if keyserver is not None:
        _gpg_run(project, homedir,
                 ['--keyserver', 'hkps://' + keyserver,
                  '--recv-keys', expected])
    else:
        try:
            _gpg_run(project, homedir, ['--import'], input_data=content)
        except ValueError as errmsg:
            raise ValueError('the fetched content holds no GPG key '
                             '({})'.format(errmsg))

    listing = _gpg_run(project, homedir,
                       ['--with-colons', '--list-keys']).decode(
                           errors='replace')
    fingerprints = []
    user_ids = []
    for line in listing.splitlines():
        fields = line.split(':')
        if fields[0] == 'fpr' and fields[9]:
            fingerprints.append(fields[9])
        elif fields[0] == 'uid' and fields[9]:
            user_ids.append(fields[9])
    fingerprints = sorted(set(fingerprints))
    if not fingerprints:
        raise ValueError('the fetched content holds no GPG key')
    if len(fingerprints) > 1:
        raise ValueError('the fetched content holds {} keys; fetch one '
                         'key at a time'.format(len(fingerprints)))
    fingerprint = fingerprints[0]
    return fingerprint, user_ids


def _fetch_key(project, url, keyserver, expected):
    """Fetch and verify the key; returns (fingerprint, uids, armored)."""
    if expected is not None:
        expected = normalize_fingerprint(expected)

    if keyserver is not None and url is not None:
        raise ValueError('give either "url" or "keyserver", not both')
    if keyserver is None and url is None:
        raise ValueError('the "url" or "keyserver" argument is required')
    if url is not None and not str(url).lower().startswith('https://'):
        raise ValueError('only https:// sources are allowed: '
                         + str(url))
    if keyserver is not None and expected is None:
        raise ValueError('a keyserver fetch needs the "fingerprint" '
                         'argument')

    with tempfile.TemporaryDirectory(prefix='debops-mcp-keyring-') as home:
        os.chmod(home, 0o700)
        content = _fetch_url(url) if url is not None else None
        fingerprint, user_ids = _import_and_verify(
                project, home, keyserver, expected, content)
        if expected is not None and fingerprint != expected:
            raise ValueError(
                    'fingerprint mismatch: the key on the server is {}, '
                    'not the requested {}'.format(
                        format_fingerprint(fingerprint),
                        format_fingerprint(expected)))
        armored = _gpg_run(project, home,
                           ['--export', '--armor', fingerprint])
    return fingerprint, user_ids, armored.decode()


def _keyring_dir(project):
    return os.path.join(project.path, 'ansible', 'keyring')


def tool_keyring_fetch(project, policy, arguments, context):
    url = arguments.get('url')
    keyserver = arguments.get('keyserver')
    if url is None and keyserver is None and arguments.get('fingerprint'):
        # The default keyserver
        keyserver = 'keyserver.ubuntu.com'

    # The policy allowlist of fetch origins; empty means any HTTPS host
    host = None
    if url is not None:
        match = re.match(r'https://([^/]+)', str(url).lower())
        host = match.group(1) if match else ''
    elif keyserver is not None:
        host = str(keyserver).lower()
    if (policy is not None and host is not None and policy.keyring_sources
            and not any(fnmatch.fnmatch(host, pattern)
                        for pattern in policy.keyring_sources)):
        return error_result('the source is not allowed by the project '
                            'policy (project.mcp.keyring_sources): '
                            + str(host))

    # Writes are recoverable and attributable only with git; check
    # before any fetching
    if not os.path.lexists(os.path.join(project.path, '.git')):
        return error_result('the project directory is not a git '
                            'repository; initialize it ("debops project '
                            'init") so that changes can be committed')

    try:
        fingerprint, user_ids, armored = _fetch_key(
                project, url, keyserver, arguments.get('fingerprint'))
    except ValueError as errmsg:
        return error_result(str(errmsg))

    # The confirmation carries the server-computed fingerprint, so the
    # user confirms verified data instead of the model's claims
    if policy is not None and 'keyring_fetch' in policy.confirm:
        answers = context.input_responses or {}
        if 'confirmation' not in answers:
            if not _supports_form_elicitation(context.client_capabilities):
                logger.notice('keyring_fetch runs without user '
                              'confirmation: the client cannot elicit')
            else:
                return {'resultType': 'input_required',
                        'inputRequests': {'confirmation': {
                            'method': 'elicitation/create',
                            'params': {
                                'mode': 'form',
                                'message': _confirm_message(
                                    url, keyserver, fingerprint,
                                    user_ids),
                                'requestedSchema': {
                                    'type': 'object',
                                    'properties': {
                                        'confirmed': {
                                            'type': 'boolean',
                                            'title': 'Confirm',
                                            'default': False},
                                        'fingerprint': {
                                            'type': 'string',
                                            'title': 'Fingerprint',
                                            'default': fingerprint}},
                                    'required': ['confirmed',
                                                 'fingerprint']}}}}}
        else:
            answer = answers.get('confirmation') or {}
            content = answer.get('content') or {}
            if not (answer.get('action') == 'accept'
                    and content.get('confirmed') is True):
                return error_result('the fetch was declined by the user')
            echoed = content.get('fingerprint')
            try:
                if normalize_fingerprint(echoed) != fingerprint:
                    raise ValueError('mismatch')
            except (ValueError, TypeError):
                return error_result('the confirmed fingerprint does not '
                                    'match the fetched key; refusing')

    filename = '0x{}.asc'.format(fingerprint)
    path = confine_under(_keyring_dir(project), filename)
    with exclusive_project(project, policy,
                           note='keyring_fetch ' + filename):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fp:
            fp.write(armored)
        # The commit is part of the tool's contract, as for view_write
        project.commit(message='DebOps MCP: keyring_fetch ' + filename)

    lines = ['# Key fetched into the keyring', '',
             'fingerprint: {}'.format(format_fingerprint(fingerprint)),
             'source: {}'.format(url or 'hkps://' + keyserver),
             'file: ansible/keyring/{}'.format(filename)]
    if user_ids:
        lines.extend(['', 'user IDs:'])
        lines.extend('- {}'.format(uid) for uid in user_ids)
    return text_result('\n'.join(lines), structured={
            'fingerprint': fingerprint,
            'fingerprintDisplay': format_fingerprint(fingerprint),
            'userIds': user_ids,
            'file': 'ansible/keyring/{}'.format(filename),
            'source': url or 'hkps://' + keyserver})


def _confirm_message(url, keyserver, fingerprint, user_ids):
    """The confirmation text: source, computed fingerprint, identities."""
    lines = ['Add a GPG key to the project keyring?', '',
             'Source: {}'.format(url or 'hkps://' + keyserver),
             'Fingerprint: {}'.format(format_fingerprint(fingerprint))]
    if user_ids:
        lines.append('Key: ' + '; '.join(user_ids[:3]))
    return '\n'.join(lines)
