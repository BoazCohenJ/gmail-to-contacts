import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import yaml

import gmail_to_contacts as g

EXAMPLE = os.path.join(os.path.dirname(__file__), '..', 'config.example.yaml')
ORDERS = os.path.join(os.path.dirname(__file__), '..', 'examples', 'orders-regex.yaml')

SIGNUP = """Welcome to Acme Fitness
Last name: Levi Cohen
First name: Dana
Phone: 050-1234567
Parent name: Rina Cohen
Parent phone: ‎052-7654321
"""


def encode(text, mime='text/plain'):
    data = base64.urlsafe_b64encode(text.encode()).decode()
    return {'mimeType': mime, 'body': {'data': data}}


def test_example_configs_load():
    assert g.load_config(EXAMPLE)['rules']
    assert g.load_config(ORDERS)['rules']


def test_labels_extraction_and_contacts():
    rule = g.load_config(EXAMPLE)['rules'][0]
    assert g.rule_matches(rule, 'Hi', SIGNUP)
    values = g.extract_fields(rule, 'Hi', SIGNUP)
    assert values == {
        'last_name': 'Levi Cohen', 'first_name': 'Dana', 'phone': '050-1234567',
        'parent_name': 'Rina Cohen', 'parent_phone': '052-7654321',
    }
    values['message_id'] = 'abc'
    student = g.build_contact_body(rule['contacts'][0], values)
    assert student['names'] == [{'givenName': 'Dana', 'familyName': 'Levi Cohen (Acme)'}]
    assert student['phoneNumbers'] == [{'value': '050-1234567'}]
    parent = g.build_contact_body(rule['contacts'][1], values)
    assert parent['names'][0]['givenName'] == 'Rina Cohen (parent of Dana Levi Cohen)'


def test_missing_required_field_returns_none():
    rule = g.load_config(EXAMPLE)['rules'][0]
    assert g.extract_fields(rule, 'Hi', 'Welcome to Acme Fitness\nFirst name: Dana') is None


def test_non_matching_email():
    rule = g.load_config(EXAMPLE)['rules'][0]
    assert not g.rule_matches(rule, 'Hi', 'Unrelated newsletter')


def test_regex_extraction_and_dedupe_key():
    rule = g.load_config(ORDERS)['rules'][0]
    body = 'Billing address\nDana Levi\n050-123-4567\ndana@example.com\n'
    subject = '[Shop] New order #1234'
    assert g.rule_matches(rule, subject, body)
    values = g.extract_fields(rule, subject, body)
    assert values['order_number'] == '1234'
    assert values['name'] == 'Dana Levi'
    assert values['phone'] == '050-123-4567'
    assert values['email'] == 'dana@example.com'
    assert g.render(rule['dedupe_key'], values) == 'order-1234'


def test_optional_regex_field_may_be_absent():
    rule = g.load_config(ORDERS)['rules'][0]
    values = g.extract_fields(rule, 'New order #9', 'Billing address\nDana Levi\n050-123-4567\n')
    assert values and 'email' not in values


def test_html_text_extraction():
    html = '<html><style>p{}</style><body><p>First name:</p><div>Dana</div></body></html>'
    text = g.extract_text_from_payload(encode(html, 'text/html'))
    assert 'p{}' not in text and 'First name:' in text and 'Dana' in text


def test_find_duplicate():
    state = {'m1': {'done': True, 'dedupe_key': 'order-1'}}
    assert g.find_duplicate(state, 'm2', 'order-1') == 'm1'
    assert g.find_duplicate(state, 'm1', 'order-1') is None
    assert g.find_duplicate(state, 'm2', None) is None
