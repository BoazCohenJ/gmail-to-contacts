# gmail-to-contacts

Watch a Gmail inbox for emails that match rules you define, pull names and phone numbers out of them, and save them as Google Contacts automatically.

> **Why this exists:** I originally built this for my own use, to turn signup emails from a fitness-training business into contacts on my phone without typing them in. It worked well enough that I thought it might be useful to other people, so I stripped out everything specific to my setup and made the matching, parsing and contact format configurable. If you have a recurring email that contains someone's name and number, this can probably handle it.

## What it does

1. Searches Gmail with a query you choose (for example `is:unread "Welcome to Acme"`).
2. Checks each result against your rules, in order. The first rule that matches wins.
3. Extracts fields from the email, either from fixed labels (`First name: Dana`) or with regular expressions.
4. Creates one or more Google Contacts from templates such as `{first_name} {last_name} (Acme)`.
5. Marks the email as read, so it won't be processed again.

It also:

- Keeps a state file so a message never creates contacts twice, even after a crash halfway through. Optionally set a `dedupe_key` to catch forwarded copies of the same email.
- Can read the inbox of one Google account and write contacts to another.
- Handles HTML and plain-text emails, and strips invisible characters such as right-to-left marks. Non-English text works fine.
- Has a `--dry-run` mode that shows what would be created without touching anything.

## Setup

### 1. Get Google OAuth credentials

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create (or pick) a project.
2. Under **APIs & Services > Library**, enable the **Gmail API** and the **People API**.
3. Under **APIs & Services > OAuth consent screen**, set up the app. For personal use you can leave it in *Testing* mode and add your own Google account(s) as test users. Note that in Testing mode Google expires refresh tokens after 7 days. Publishing the app (even unverified, for yourself) avoids that.
4. Under **Credentials**, choose **Create credentials > OAuth client ID > Desktop app**.
5. Download the JSON and save it as `credentials.json` in this folder.

### 2. Write your config

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` (see [Configuration](#configuration)). `examples/orders-regex.yaml` shows the regex style.

### 3. Authorize

Run each of these once. Each prints a URL, you sign in and click Allow, then paste back the `http://localhost/?code=...` URL your browser lands on (the page won't load, that's expected).

With Docker:

```bash
docker compose build
docker compose run --rm gmail-to-contacts python auth.py gmail      # inbox to read
docker compose run --rm gmail-to-contacts python auth.py contacts   # account that owns the contacts
```

Or with plain Python 3.10+:

```bash
pip install -r requirements.txt
python auth.py gmail
python auth.py contacts
```

Tokens are saved in `data/`. The two accounts can be the same one.

### 4. Try it

```bash
docker compose run --rm gmail-to-contacts python gmail_to_contacts.py --dry-run -v
```

or `python gmail_to_contacts.py --dry-run -v`. When the output looks right, drop `--dry-run`.

### 5. Schedule it

The program runs once and exits. Run it on a schedule with cron, using the wrapper, which prevents overlapping runs, cleans up one-off containers and keeps a rotating log in `logs/`:

```cron
*/15 * * * * /path/to/gmail-to-contacts/run.sh
```

## Configuration

```yaml
gmail_query: 'is:unread "Welcome to Acme Fitness"'
mark_as_read: true

rules:
  - name: class-signup
    match_text: ["Welcome to Acme Fitness"]   # any of these in the email
    # subject_contains: ["New signup"]        # optional, any of these in the subject

    extract:
      type: labels                            # or: regex
      fields:
        first_name: "First name:"
        last_name: {label: "Last name:", multiword: true}
        phone: "Phone:"
        parent_name: {label: "Parent name:", multiword: true, optional: true}

    # dedupe_key: "order-{order_number}"      # optional

    contacts:
      - given_name: "{first_name}"
        family_name: "{last_name} (Acme)"
        phone: "{phone}"
        notes: "Parent: {parent_name}\nSource email: {message_id}"
```

| Key | Meaning |
|---|---|
| `gmail_query` | Gmail search that selects candidate emails. Keep `is:unread` in it so processed emails drop out of the results. |
| `mark_as_read` | Mark emails as read after their contacts were created. Default `true`. |
| `rules[].match_text` | Email body or subject must contain any of these strings. |
| `rules[].subject_contains` | Subject must contain any of these strings. |
| `rules[].extract.type` | `labels` reads the text after a fixed label. Each value ends at the next label or at anything that looks like another `word:` label, and takes only the first word unless `multiword: true`. `regex` uses a regular expression per field. The first capture group is the value, and `source: subject` reads from the subject instead of the body. |
| `rules[].extract.fields` | Fields to extract. All are required unless marked `optional: true`. If a required field is missing, the email is left unread and a warning is printed. |
| `rules[].dedupe_key` | Template. Emails that render the same key only create contacts once. Useful when the same order email gets forwarded. |
| `rules[].contacts` | One or more contacts to create per email. Supported keys: `given_name`, `family_name`, `phone`, `email`, `notes`. |

Templates use `{field}` placeholders from your extracted fields, plus `{message_id}` and `{subject}`. A placeholder with no value renders as an empty string.

## Files and privacy

Everything private stays on your machine and is git-ignored: `credentials.json`, `config.yaml`, and the `data/` folder (OAuth tokens and the processed-message state). Treat the tokens like passwords. The script asks for the `gmail.modify` scope (needed to mark emails as read) and the `contacts` scope, and talks only to Google's APIs.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT
