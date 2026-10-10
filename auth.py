#!/usr/bin/env python3
"""
Authorize a Google account for gmail-to-contacts.

    python auth.py gmail       # account whose inbox is read
    python auth.py contacts    # account that will own the new contacts

The two can be the same account or different ones. The script prints a URL;
sign in, click Allow, and paste back the (non-loading) localhost URL your
browser ends up on. That works in Docker and on headless machines too.
"""

import argparse
import os
import sys
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

DATA_DIR = os.environ.get('GMC_DATA_DIR', 'data')
CLIENT_SECRETS = os.environ.get('GMC_CREDENTIALS', 'credentials.json')
REDIRECT_URI = 'http://localhost'

ACCOUNTS = {
    'gmail': (['https://www.googleapis.com/auth/gmail.modify'], 'gmail_token.json'),
    'contacts': (['https://www.googleapis.com/auth/contacts'], 'contacts_token.json'),
}


def read_auth_code(prompt):
    """Accept either the bare code or the full http://localhost/?code=... URL."""
    value = input(prompt).strip()
    if value.startswith('http'):
        return parse_qs(urlparse(value).query).get('code', [''])[0]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('account', choices=ACCOUNTS)
    args = parser.parse_args()
    scopes, token_name = ACCOUNTS[args.account]
    token_path = os.path.join(DATA_DIR, token_name)

    if not os.path.exists(CLIENT_SECRETS):
        print(f'ERROR: {CLIENT_SECRETS} not found. Download an OAuth "Desktop app" client '
              f'from Google Cloud Console and save it there (see README).')
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, scopes)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

    purpose = ('the inbox to monitor' if args.account == 'gmail'
               else 'the account that should own the contacts')
    print(f'Open this URL in a browser and sign in to {purpose}:\n')
    print(auth_url)
    print("\nAfter clicking Allow, the browser lands on a localhost page that won't load.")
    print('That is expected. Copy the full URL from the address bar.\n')

    code = read_auth_code('Paste the URL here: ')
    if not code:
        print('ERROR: no authorization code provided.')
        return 1

    flow.fetch_token(code=code)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(token_path, 'w', encoding='utf-8') as token_file:
        token_file.write(flow.credentials.to_json())
    print(f'Saved {token_path}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
