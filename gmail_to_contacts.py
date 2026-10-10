#!/usr/bin/env python3
"""
gmail-to-contacts

Watches a Gmail inbox for unread emails that match rules you define in
config.yaml, pulls structured data (names, phone numbers, ...) out of them, and
saves the result as Google Contacts. Processed emails are marked as read.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from html.parser import HTMLParser

import yaml
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CONTACT_SCOPES = ['https://www.googleapis.com/auth/contacts']

DATA_DIR = os.environ.get('GMC_DATA_DIR', 'data')
GMAIL_TOKEN_FILE = os.path.join(DATA_DIR, 'gmail_token.json')
CONTACTS_TOKEN_FILE = os.path.join(DATA_DIR, 'contacts_token.json')
STATE_FILE = os.path.join(DATA_DIR, 'state.json')

# HTML tags that start a new line of visible text
BLOCK_TAGS = {
    'p', 'div', 'tr', 'td', 'th', 'li', 'table', 'address',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
}

VERBOSE = False


def debug(message):
    if VERBOSE:
        print(f'DEBUG: {message}')


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def strip_format_chars(value):
    """Remove invisible Unicode formatting characters (e.g. RTL marks)."""
    return ''.join(c for c in value if unicodedata.category(c) != 'Cf')


def clean_value(value):
    """Strip invisible characters and collapse whitespace in an extracted value."""
    return re.sub(r'\s+', ' ', strip_format_chars(value)).strip()


def normalize_text(value):
    """Normalize dashes and non-breaking spaces so matching is forgiving."""
    return (value.replace('–', '-').replace('—', '-')
            .replace(' ', ' '))


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'style', 'script'}:
            self.ignored_depth += 1
        elif tag in BLOCK_TAGS or tag == 'br':
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'style', 'script'} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.ignored_depth:
            self.parts.append(data)


def extract_text_from_payload(payload):
    """Return the visible text of a Gmail API message payload."""
    def decode_part(part):
        data = part.get('body', {}).get('data')
        mime_type = part.get('mimeType', '')
        if data:
            decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if mime_type == 'text/html':
                parser = _VisibleTextParser()
                parser.feed(decoded)
                return ''.join(parser.parts)
            if mime_type == 'text/plain':
                return decoded
        return ''.join(decode_part(child) for child in part.get('parts', []))

    text = decode_part(payload)
    return re.sub(r'\n\s*\n+', '\n', text).strip()


def get_subject(message):
    headers = message.get('payload', {}).get('headers', [])
    return next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')


class SafeDict(dict):
    """format_map helper: unknown placeholders render as an empty string."""
    def __missing__(self, key):
        return ''


def render(template, values):
    """Fill a '{field}' template. Missing fields become empty strings."""
    if not template:
        return ''
    return str(template).format_map(SafeDict(values)).strip()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path):
    with open(path, 'r', encoding='utf-8') as config_file:
        config = yaml.safe_load(config_file) or {}

    if not config.get('gmail_query'):
        raise ValueError('config: "gmail_query" is required')
    rules = config.get('rules')
    if not rules:
        raise ValueError('config: at least one entry under "rules" is required')

    names = set()
    for index, rule in enumerate(rules):
        name = rule.setdefault('name', f'rule-{index + 1}')
        if name in names:
            raise ValueError(f'config: duplicate rule name {name!r}')
        names.add(name)
        if not rule.get('contacts'):
            raise ValueError(f'config: rule {name!r} needs at least one entry under "contacts"')
        extract = rule.get('extract') or {}
        if extract.get('type') not in ('labels', 'regex'):
            raise ValueError(f'config: rule {name!r} needs extract.type of "labels" or "regex"')
        if not extract.get('fields'):
            raise ValueError(f'config: rule {name!r} needs extract.fields')
    return config


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --------------------------------------------------------------------------
# Rule matching and extraction
# --------------------------------------------------------------------------

def rule_matches(rule, subject, text):
    """Check the rule's match_text / subject_contains conditions."""
    match_text = [normalize_text(t) for t in as_list(rule.get('match_text'))]
    if match_text:
        haystack = normalize_text(text)
        if not any(t in haystack for t in match_text):
            return False
    subject_terms = as_list(rule.get('subject_contains'))
    if subject_terms and not any(t in subject for t in subject_terms):
        return False
    return True


def extract_with_labels(fields, text):
    """
    Extract values that follow fixed labels, e.g. "First name: Dana".

    A value ends at the next known label or at anything that looks like another
    "word: " label. Fields take only the first word unless multiword is true.
    """
    specs = {}
    for key, spec in fields.items():
        if isinstance(spec, str):
            spec = {'label': spec}
        specs[key] = spec

    positions = []
    for key, spec in specs.items():
        start = 0
        while True:
            pos = text.find(spec['label'], start)
            if pos == -1:
                break
            positions.append((pos, key))
            start = pos + 1
    positions.sort()
    debug(f'label positions: {positions}')

    result = {}
    for i, (pos, key) in enumerate(positions):
        if key in result:
            continue
        spec = specs[key]
        value_start = pos + len(spec['label'])
        value_end = len(text)
        if i + 1 < len(positions):
            value_end = min(value_end, positions[i + 1][0])
        lookahead = text[value_start:value_start + 100]
        colon_match = re.search(r'\S+:\s', lookahead)
        if colon_match:
            value_end = min(value_end, value_start + colon_match.start())

        candidate = clean_value(text[value_start:value_end])
        if not spec.get('multiword'):
            words = candidate.split()
            candidate = words[0] if words else ''
        if candidate:
            result[key] = candidate
            debug(f'found {key}: {candidate!r}')
    return result


def extract_with_regex(fields, subject, text):
    """Extract each field with its own regular expression."""
    result = {}
    for key, spec in fields.items():
        if isinstance(spec, str):
            spec = {'regex': spec}
        source = subject if spec.get('source') == 'subject' else text
        match = re.search(spec['regex'], source, re.MULTILINE)
        if not match:
            continue
        group = match.group(spec.get('group', 1 if match.re.groups else 0))
        value = clean_value(group or '')
        if value:
            result[key] = value
            debug(f'found {key}: {value!r}')
    return result


def extract_fields(rule, subject, text):
    """Run the rule's extractor. Returns a dict, or None if required fields are missing."""
    extract = rule['extract']
    fields = extract['fields']
    if extract['type'] == 'labels':
        result = extract_with_labels(fields, normalize_text(text))
    else:
        result = extract_with_regex(fields, subject, text)

    optional = {
        key for key, spec in fields.items()
        if isinstance(spec, dict) and spec.get('optional')
    }
    missing = [key for key in fields if key not in result and key not in optional]
    if missing:
        debug(f'rule {rule["name"]!r}: missing fields {missing}')
        return None
    return result


# --------------------------------------------------------------------------
# State (so a message never creates contacts twice)
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as state_file:
            state = json.load(state_file)
            return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)


def find_duplicate(state, msg_id, dedupe_key):
    """Return the id of an earlier message that already produced these contacts."""
    if not dedupe_key:
        return None
    for existing_id, entry in state.items():
        if (existing_id != msg_id and isinstance(entry, dict)
                and entry.get('done') and entry.get('dedupe_key') == dedupe_key):
            return existing_id
    return None


# --------------------------------------------------------------------------
# Google auth
# --------------------------------------------------------------------------

def refresh_credentials(creds, token_file):
    """Refresh OAuth credentials, retrying temporary network failures."""
    for attempt in range(1, 4):
        try:
            creds.refresh(Request())
            with open(token_file, 'w', encoding='utf-8') as token:
                token.write(creds.to_json())
            return True
        except RefreshError as error:
            print(f'ERROR: Refresh token in {token_file} is expired or revoked: {error}. '
                  f'Run auth.py again to re-authorize.')
            return False
        except TransportError as error:
            if attempt == 3:
                print(f'ERROR: Could not refresh {token_file} after 3 attempts. '
                      f'Check internet connectivity: {error}')
                return False
            delay = 5 * attempt
            print(f'Temporary network error refreshing {token_file}; '
                  f'retrying in {delay}s ({attempt}/3)')
            time.sleep(delay)
    return False


def load_credentials(token_file, scopes, auth_name):
    if not os.path.exists(token_file):
        print(f'ERROR: {token_file} not found. Run: python auth.py {auth_name}')
        return None
    try:
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    except (json.JSONDecodeError, ValueError):
        print(f'ERROR: {token_file} is invalid. Run: python auth.py {auth_name}')
        return None

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            if not refresh_credentials(creds, token_file):
                return None
        else:
            print(f'ERROR: {token_file} cannot be refreshed. Run: python auth.py {auth_name}')
            return None
    return creds


# --------------------------------------------------------------------------
# Gmail and Contacts calls
# --------------------------------------------------------------------------

def list_messages(service, query):
    messages = []
    request = service.users().messages().list(userId='me', q=query)
    try:
        while request is not None:
            response = request.execute()
            messages.extend(response.get('messages', []))
            request = service.users().messages().list_next(request, response)
    except HttpError as error:
        print(f'An error occurred listing messages: {error}')
    return messages


def get_message(service, msg_id):
    try:
        return service.users().messages().get(userId='me', id=msg_id, format='full').execute()
    except HttpError as error:
        print(f'An error occurred getting message {msg_id}: {error}')
        return None


def mark_as_read(service, msg_id):
    try:
        service.users().messages().modify(
            userId='me', id=msg_id, body={'removeLabelIds': ['UNREAD']}
        ).execute()
    except HttpError as error:
        print(f'Failed to mark message {msg_id} as read: {error}')


def build_contact_body(template, values):
    """Turn a contact template from the config into a People API request body."""
    body = {}
    given = render(template.get('given_name'), values)
    family = render(template.get('family_name'), values)
    if given or family:
        body['names'] = [{'givenName': given, 'familyName': family}]
    phone = render(template.get('phone'), values)
    if phone:
        body['phoneNumbers'] = [{'value': phone}]
    email = render(template.get('email'), values)
    if email:
        body['emailAddresses'] = [{'value': email}]
    notes = render(template.get('notes'), values)
    if notes:
        body['biographies'] = [{'value': notes}]
    return body


def create_contacts(people_service, state, msg_id, rule, values, dry_run):
    """
    Create every contact the rule describes for one message.

    Progress is saved after each contact, so a failure halfway through a
    message is resumed on the next run instead of creating duplicates.
    """
    entry = state.get(msg_id) or {}
    created = entry.get('contacts', {})
    entry = {
        'rule': rule['name'],
        'dedupe_key': render(rule.get('dedupe_key'), values) or None,
        'contacts': created,
        'done': False,
    }

    for index, template in enumerate(rule['contacts']):
        if str(index) in created:
            continue
        body = build_contact_body(template, values)
        if not body.get('names') and not body.get('phoneNumbers') and not body.get('emailAddresses'):
            print(f'Skipping empty contact #{index + 1} for rule {rule["name"]!r}')
            continue
        if dry_run:
            print(f'[dry run] Would create contact: {json.dumps(body, ensure_ascii=False)}')
            continue
        try:
            result = people_service.people().createContact(
                body=body,
                personFields='names,phoneNumbers,emailAddresses,biographies',
            ).execute()
        except HttpError as error:
            print(f'Failed to create contact #{index + 1} for message {msg_id}: {error}')
            state[msg_id] = entry
            save_state(state)
            return False
        created[str(index)] = result.get('resourceName', 'unknown')
        state[msg_id] = entry
        save_state(state)
        name = ' '.join(filter(None, [
            body.get('names', [{}])[0].get('givenName'),
            body.get('names', [{}])[0].get('familyName'),
        ]))
        print(f'Created contact: {name or "(no name)"}')

    if not dry_run:
        entry['done'] = True
        state[msg_id] = entry
        save_state(state)
    return True


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def process_message(service, people_service, state, config, msg_id, dry_run):
    """Handle one message. Returns the extracted record, or None if nothing was created."""
    msg = get_message(service, msg_id)
    if not msg:
        return None
    subject = get_subject(msg)
    text = extract_text_from_payload(msg.get('payload', {}))
    debug(f'message {msg_id}: subject={subject!r}, {len(text)} chars of text')
    if not text:
        return None

    for rule in config['rules']:
        if not rule_matches(rule, subject, text):
            continue

        values = extract_fields(rule, subject, text)
        if values is None:
            print(f'Warning: message {msg_id} matched rule {rule["name"]!r} '
                  f'but required fields are missing')
            return None
        values['message_id'] = msg_id
        values['subject'] = subject

        if state.get(msg_id, {}).get('done'):
            print(f'Contacts already exist for message {msg_id}')
            ok = True
        else:
            duplicate = find_duplicate(state, msg_id, render(rule.get('dedupe_key'), values))
            if duplicate:
                print(f'Message {msg_id} duplicates earlier message {duplicate}; '
                      f'not creating contacts again')
                ok = True
            else:
                ok = create_contacts(people_service, state, msg_id, rule, values, dry_run)

        if not ok:
            return None
        if config.get('mark_as_read', True) and not dry_run:
            mark_as_read(service, msg_id)
        print(f'Processed message {msg_id} (rule {rule["name"]!r})')
        return {'rule': rule['name'], **{k: v for k, v in values.items()
                                         if k not in ('message_id', 'subject')}}

    debug(f'message {msg_id} matched no rule')
    return None


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('GMC_CONFIG', 'config.yaml'),
                        help='path to config file (default: config.yaml)')
    parser.add_argument('--dry-run', action='store_true',
                        help="show what would happen; create no contacts and don't mark emails read")
    parser.add_argument('--verbose', '-v', action='store_true', help='print debug output')
    args = parser.parse_args()
    VERBOSE = args.verbose

    stamp = lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{stamp()}] Starting gmail-to-contacts...')

    try:
        config = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f'ERROR: could not load {args.config}: {error}')
        return 1

    gmail_creds = load_credentials(GMAIL_TOKEN_FILE, GMAIL_SCOPES, 'gmail')
    contacts_creds = load_credentials(CONTACTS_TOKEN_FILE, CONTACT_SCOPES, 'contacts')
    if not gmail_creds or not contacts_creds:
        return 1

    service = build('gmail', 'v1', credentials=gmail_creds)
    people_service = build('people', 'v1', credentials=contacts_creds)

    messages = list_messages(service, config['gmail_query'])
    print(f'Found {len(messages)} candidate messages')

    state = load_state()
    records = []
    for message in messages:
        record = process_message(
            service, people_service, state, config, message['id'], args.dry_run)
        if record:
            records.append(record)

    print(json.dumps(records, indent=2, ensure_ascii=False))
    print(f'[{stamp()}] Finished. Processed {len(records)} new emails.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
