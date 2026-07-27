"""Best-effort redaction for device log messages.

This module intentionally has no Reefy or third-party dependencies so every
device process can use it before writing to journald. It recognizes common
secret syntax, but cannot identify arbitrary unlabelled values. Callers must
still avoid logging structured payloads that may contain secrets.
"""

import re


REDACTED = '[REDACTED]'

_PRIVATE_KEY_RE = re.compile(
    r'-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----.*?'
    r'(?:-----END(?: [A-Z0-9]+)* PRIVATE KEY-----|$)',
    re.IGNORECASE | re.DOTALL,
)
_URL_USERINFO_RE = re.compile(
    r'(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]*:)([^\s/@]+)(@)')
_SENSITIVE_HEADER_RE = re.compile(
    r'(?im)(?P<prefix>\b(?:(?:proxy-)?authorization|(?:set-)?cookie)'
    r'\s*:\s*)[^\r\n]*')
_AUTH_SCHEME_RE = re.compile(
    r'(?i)\b(bearer)(\s+)([a-z0-9._~+/=-]{8,})')
_JWT_RE = re.compile(
    r'(?<![A-Za-z0-9_-])'
    r'[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'
    r'(?![A-Za-z0-9_-])')

_SECRET_KEY = (
    r'(?:aws[_-]?secret[_-]?access[_-]?key|wifi[_-]?psk|'
    r'docker[_-]?auth[_-]?config|psk|auth|passwords?|passwd|'
    r'passphrases?|secrets?|auth[_-]?secrets?|'
    r'client[_-]?secrets?|tokens?|access[_-]?tokens?|refresh[_-]?tokens?|'
    r'api[_-]?keys?|private[_-]?keys?|ssh[_-]?keys?|credentials?|cookies?|'
    r'authorizations?|signatures?|sig|aws[_-]?access[_-]?key[_-]?id|'
    r'google[_-]?access[_-]?id)'
)
_KEY_QUOTE_RE = r'(?:\\["\']|["\'])?'
_SECRET_ASSIGNMENT_RE = re.compile(
    rf'(?i)(?P<prefix>(?<![A-Za-z0-9]){_KEY_QUOTE_RE}{_SECRET_KEY}'
    rf'{_KEY_QUOTE_RE}\s*[:=]\s*)')
_CLI_SECRET_RE = re.compile(
    rf'(?i)(?P<prefix>(?<![A-Za-z0-9])--{_SECRET_KEY}(?:=|\s+))')


def _escaped_quoted_value_end(message, start):
    """Return the end of a quote escaped by an outer representation."""
    quote = message[start + 1]
    index = start + 2
    while index < len(message):
        if message[index] != '\\':
            index += 1
            continue

        slash_end = index
        while slash_end < len(message) and message[slash_end] == '\\':
            slash_end += 1
        if slash_end < len(message) and message[slash_end] == quote:
            if slash_end - index == 1:
                return slash_end + 1
            index = slash_end + 1
            continue
        index = slash_end
    return len(message)


def _structured_value_end(message, start):
    """Return the end of a quoted or balanced structured value."""
    opener = message[start]
    if (opener == '\\' and start + 1 < len(message)
            and message[start + 1] in ('"', "'")):
        return _escaped_quoted_value_end(message, start)
    if opener in ('"', "'"):
        quote = opener
        index = start + 1
        while index < len(message):
            if message[index] == '\\':
                index += 2
                continue
            if message[index] == quote:
                return index + 1
            index += 1
        return len(message)

    closers = {'[': ']', '{': '}', '(': ')'}
    closer = closers[opener]
    stack = [closer]
    quote = None
    escaped_quote = False
    index = start + 1
    while index < len(message):
        char = message[index]
        if quote:
            if escaped_quote and char == '\\':
                slash_end = index
                while (slash_end < len(message)
                       and message[slash_end] == '\\'):
                    slash_end += 1
                if slash_end < len(message) and message[slash_end] == quote:
                    if slash_end - index == 1:
                        quote = None
                        escaped_quote = False
                    index = slash_end + 1
                    continue
                index = slash_end
                continue
            if not escaped_quote and char == '\\':
                index += 2
                continue
            if not escaped_quote and char == quote:
                quote = None
        elif (char == '\\' and index + 1 < len(message)
              and message[index + 1] in ('"', "'")):
            quote = message[index + 1]
            escaped_quote = True
            index += 2
            continue
        elif char in ('"', "'"):
            quote = char
        elif char in closers:
            stack.append(closers[char])
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    return len(message)


def _redact_prefixed_values(message, prefix_pattern):
    """Redact scalar or nested values following matching prefixes."""
    rendered = []
    position = 0
    while True:
        match = prefix_pattern.search(message, position)
        if not match:
            rendered.append(message[position:])
            return ''.join(rendered)

        value_start = match.end()
        rendered.append(message[position:value_start])
        if message.startswith(REDACTED, value_start):
            rendered.append(REDACTED)
            position = value_start + len(REDACTED)
            continue
        if value_start >= len(message):
            position = value_start
            continue

        first = message[value_start]
        if first in ('"', "'", '[', '{', '('):
            value_end = _structured_value_end(message, value_start)
            replacement = (
                f'{first}{REDACTED}{first}'
                if first in ('"', "'") else REDACTED)
        elif (first == '\\' and value_start + 1 < len(message)
              and message[value_start + 1] in ('"', "'")):
            value_end = _structured_value_end(message, value_start)
            delimiter = message[value_start:value_start + 2]
            replacement = f'{delimiter}{REDACTED}{delimiter}'
        else:
            value_end = value_start
            while (value_end < len(message)
                   and message[value_end] not in ' \t\r\n,;&}])'):
                value_end += 1
            if value_end == value_start:
                position = value_start
                continue
            replacement = REDACTED
        rendered.append(replacement)
        position = value_end


def _redact_cli_values(message):
    """Redact command-line values following recognizable secret flags."""
    return _redact_prefixed_values(message, _CLI_SECRET_RE)


def _redact_assignments(message):
    """Redact values following recognizable secret assignments."""
    return _redact_prefixed_values(message, _SECRET_ASSIGNMENT_RE)


def redact_log_message(value):
    """Return a string with recognizable secret material replaced."""
    try:
        message = str(value)
    except Exception:
        return REDACTED
    message = _PRIVATE_KEY_RE.sub(REDACTED, message)
    message = _SENSITIVE_HEADER_RE.sub(
        lambda match: f'{match.group("prefix")}{REDACTED}', message)
    message = _URL_USERINFO_RE.sub(
        lambda match: f'{match.group(1)}{REDACTED}{match.group(3)}', message)
    message = _AUTH_SCHEME_RE.sub(
        lambda match: f'{match.group(1)}{match.group(2)}{REDACTED}', message)
    message = _JWT_RE.sub(REDACTED, message)
    message = _redact_cli_values(message)
    return _redact_assignments(message)
